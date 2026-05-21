#!/usr/bin/env python3
"""
fla_inspect.py — XFL/FLA structure explorer for Twist

Usage:
  python tools/fla_inspect.py <file.fla>              # list all symbols
  python tools/fla_inspect.py <file.fla> --roots      # find top-level symbols (not used by anything else)
  python tools/fla_inspect.py <file.fla> --tree NAME  # print symbol dependency tree
  python tools/fla_inspect.py <file.fla> --shape NAME # dump raw edge data for a symbol
  python tools/fla_inspect.py <dir/>                  # scan a directory for parseable FLAs
"""

import sys, zipfile, xml.etree.ElementTree as ET, os, re, io, struct
from pathlib import Path

XFL_NS  = 'http://ns.adobe.com/xfl/2008/'
def t(name): return f'{{{XFL_NS}}}{name}'


# ── File loading ─────────────────────────────────────────────────────────────

def _fix_zip_eocd(data: bytes) -> bytes:
    """Fix Adobe Animate FLAs where the EOCD central-directory-size field is
    larger than the actual space between the CD offset and the EOCD record.
    Python's zipfile uses that field to detect concatenated ZIPs and will seek
    to the wrong position when the value is wrong (concat goes negative).
    We rewrite the CD size field in-memory to match the real gap."""
    # EOCD is the last 22 bytes (assuming no ZIP comment, which FLAs never have)
    if len(data) < 22 or data[-22:-18] != b'PK\x05\x06':
        return data
    eocd_pos   = len(data) - 22
    cd_offset  = struct.unpack_from('<I', data, eocd_pos + 16)[0]
    real_cd_size = eocd_pos - cd_offset
    stored_cd_size = struct.unpack_from('<I', data, eocd_pos + 12)[0]
    if stored_cd_size == real_cd_size:
        return data
    patched = bytearray(data)
    struct.pack_into('<I', patched, eocd_pos + 12, real_cd_size)
    return bytes(patched)

def open_fla(path):
    data = Path(path).read_bytes()
    data = _fix_zip_eocd(data)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {name: z.read(name) for name in z.namelist()}

def magic(path):
    with open(path, 'rb') as f:
        return f.read(4)

def is_zip(path):
    return magic(path)[:2] == b'PK'


# ── XML helpers ───────────────────────────────────────────────────────────────

def parse_xml(data):
    return ET.fromstring(data)

def parse_dom(zip_data):
    raw = zip_data.get('DOMDocument.xml')
    if not raw:
        raise ValueError('No DOMDocument.xml')
    return parse_xml(raw)

def get_symbols(zip_data):
    """Return {symbol_name: ET.Element} for every LIBRARY/*.xml."""
    out = {}
    for key, data in zip_data.items():
        if key.startswith('LIBRARY/') and key.endswith('.xml'):
            try:
                root = parse_xml(data)
                name = root.get('name', key)
                out[name] = root
            except ET.ParseError:
                pass
    return out

def child_refs(sym_elem):
    """Names of all DOMSymbolInstance children (may repeat)."""
    return [e.get('libraryItemName')
            for e in sym_elem.iter(t('DOMSymbolInstance'))
            if e.get('libraryItemName')]

def find_roots(symbols):
    """Symbols not referenced by any other symbol — likely top-level comps."""
    referenced = set()
    for elem in symbols.values():
        referenced.update(child_refs(elem))
    return sorted(k for k in symbols if k not in referenced)


# ── Printers ──────────────────────────────────────────────────────────────────

def print_header(dom, n_symbols):
    w   = dom.get('width',      '?')
    h   = dom.get('height',     '?')
    fps = dom.get('frameRate',  '?')
    app = dom.get('creatorInfo','?')
    ver = dom.get('versionInfo','?')
    print(f'Stage : {w} x {h}    fps = {fps}')
    print(f'Tool  : {app}')
    print(f'Save  : {ver}')
    print(f'Symbols in library: {n_symbols}')

def print_tree(name, symbols, depth=0, visited=None):
    if visited is None:
        visited = set()
    pad  = '  ' * depth
    sym  = symbols.get(name)
    refs = child_refs(sym) if sym is not None else []
    unique = list(dict.fromkeys(refs))
    flag = ' [MISSING]' if sym is None else ''
    loop_flag = ' [loop]' if name in visited else ''
    sym_type = sym.get('symbolType', '?') if sym is not None else '?'
    n_layers = len(list(sym.iter(t('DOMLayer')))) if sym is not None else 0
    print(f'{pad}{name}  ({sym_type}, {n_layers} layers, {len(refs)} inst refs){flag}{loop_flag}')
    if name in visited:
        return
    visited.add(name)
    for child in unique:
        print_tree(child, symbols, depth + 1, visited)

def print_shapes(name, symbols, max_edges=10):
    sym = symbols.get(name)
    if sym is None:
        print(f'Symbol not found: {name}')
        return
    print(f'=== {name} ===')
    edge_count = 0
    for layer in sym.iter(t('DOMLayer')):
        layer_name = layer.get('name', '?')
        for frame in layer.iter(t('DOMFrame')):
            idx = frame.get('index', '?')
            for shape in frame.iter(t('DOMShape')):
                fills   = [f.get('index') for f in shape.iter(t('FillStyle'))]
                strokes = [s.get('index') for s in shape.iter(t('StrokeStyle'))]
                for edge in shape.iter(t('Edge')):
                    if edge_count >= max_edges:
                        print(f'  ... (stopped at {max_edges} edges)')
                        return
                    es = edge.get('edges', '')
                    f0 = edge.get('fillStyle0', '-')
                    f1 = edge.get('fillStyle1', '-')
                    sk = edge.get('strokeStyle', '-')
                    print(f'  layer={layer_name!r} frame={idx}  fill0={f0} fill1={f1} stroke={sk}')
                    print(f'    {es[:300]}')
                    edge_count += 1

# ── Edge / SVG ───────────────────────────────────────────────────────────────

_TOK = re.compile(
    r'[!\|\[]'                               # command chars
    r'|S\d+'                                 # Sn style selectors — skip
    r'|#[0-9A-Fa-f]+(?:\.[0-9A-Fa-f]+)?'   # #hex.hex coordinates
    r'|[-+]?\d+(?:\.\d+)?'                  # plain decimal coordinates
)

def decode_hex(tok):
    """#hexInt.hexFrac → float, signed.

    Flash XFL always uses 6-digit hex (0xFFxxxx) for negative values.
    Short hex (1-5 digits) is always positive.  Using digit-count as the
    bit-width incorrectly sign-extends values like #8EF (2287, not -1809).
    Fixed 24-bit threshold (0x800000) matches Flash's actual convention.
    """
    s = tok[1:]
    ih, fh = (s.split('.') + ['0'])[:2]
    v = int(ih, 16)
    if v >= 0x800000:       # 24-bit two's complement — negative
        v -= 0x1000000
    return v + int(fh, 16) / 256.0

def parse_edge_str(s):
    """Flash edge string → list of ('M'|'L'|'Q', ...) tuples."""
    tokens = _TOK.findall(s)
    cmds = []
    i    = 0

    def num():
        nonlocal i
        tok = tokens[i]; i += 1
        return decode_hex(tok) if tok.startswith('#') else float(tok)

    while i < len(tokens):
        tok = tokens[i]; i += 1
        if   tok == '!': cmds.append(('M', num(), num()))
        elif tok == '|': cmds.append(('L', num(), num()))
        elif tok == '[': cmds.append(('Q', num(), num(), num(), num()))
        # S tokens matched but skipped

    return cmds

def cmds_to_svg_d(cmds):
    """Convert path commands to SVG d string.
    Suppresses M when it matches the previous endpoint (Flash restates the
    current point after every curve — treating those as real moves creates
    disconnected filled arcs instead of continuous filled regions).
    """
    parts = []
    cx = cy = None
    eps = 0.3   # tolerance for "same point" in Flash's coordinate units

    for c in cmds:
        if c[0] == 'M':
            if cx is not None and abs(c[1]-cx) < eps and abs(c[2]-cy) < eps:
                pass    # redundant restatement — skip it
            else:
                parts.append(f'M{c[1]:.2f} {c[2]:.2f}')
            cx, cy = c[1], c[2]
        elif c[0] == 'L':
            parts.append(f'L{c[1]:.2f} {c[2]:.2f}')
            cx, cy = c[1], c[2]
        elif c[0] == 'Q':
            parts.append(f'Q{c[1]:.2f} {c[2]:.2f} {c[3]:.2f} {c[4]:.2f}')
            cx, cy = c[3], c[4]   # anchor is the new current point
    return ' '.join(parts)

def _gradient_to_svg(grad_elem, grad_tag, grad_id):
    """Build SVG <linearGradient> or <radialGradient> lines from XFL element.

    Flash gradient space: linear goes from x=-16384 to x=+16384 at y=0;
    radial has center (0,0) radius=16384.  The XFL Matrix maps gradient space
    → shape's local coordinate space, so we use gradientTransform.
    """
    mc = grad_elem.find(t('matrix'))
    m  = mc.find(t('Matrix')) if mc is not None else None
    xf_attr = ''
    if m is not None:
        a = float(m.get('a', 1)); b = float(m.get('b', 0))
        c = float(m.get('c', 0)); d = float(m.get('d', 1))
        tx = float(m.get('tx', 0)); ty = float(m.get('ty', 0))
        xf_attr = f' gradientTransform="matrix({a},{b},{c},{d},{tx},{ty})"'

    stops = []
    for entry in grad_elem.findall(t('GradientEntry')):
        pct   = float(entry.get('ratio', 0)) * 100
        color = entry.get('color', '#000000')
        alpha = float(entry.get('alpha', 1))
        opac  = f' stop-opacity="{alpha:.4f}"' if alpha < 0.9999 else ''
        stops.append(f'<stop offset="{pct:.2f}%" stop-color="{color}"{opac}/>')

    if grad_tag == 'LinearGradient':
        head = (f'<linearGradient id="{grad_id}" gradientUnits="userSpaceOnUse"'
                f' x1="-16384" y1="0" x2="16384" y2="0"{xf_attr}>')
        tail = '</linearGradient>'
    else:
        head = (f'<radialGradient id="{grad_id}" gradientUnits="userSpaceOnUse"'
                f' cx="0" cy="0" r="16384" fx="0" fy="0"{xf_attr}>')
        tail = '</radialGradient>'

    return [head] + [f'  {s}' for s in stops] + [tail]


def _get_fill_color(shape, idx, _defs=None, _grad_cache=None):
    """Return (fill_value, alpha) for fill index idx.

    If _defs and _grad_cache are provided, gradient fills emit proper SVG
    gradient defs and return 'url(#id)' as the fill value.
    """
    for fs in shape.iter(t('FillStyle')):
        if fs.get('index') != str(idx):
            continue
        sc = fs.find(t('SolidColor'))
        if sc is not None:
            a = float(sc.get('alpha', 1.0))
            c = sc.get('color', '#888888')
            return c, a
        for grad_tag in ('LinearGradient', 'RadialGradient'):
            grad = fs.find(t(grad_tag))
            if grad is None:
                continue
            if _defs is not None and _grad_cache is not None:
                key = id(grad)
                if key not in _grad_cache:
                    grad_id = f'grad{len(_grad_cache)}'
                    _grad_cache[key] = grad_id
                    _defs.extend(_gradient_to_svg(grad, grad_tag, grad_id))
                return f'url(#{_grad_cache[key]})', 1.0
            # Fallback placeholder when gradient decoding is not available
            return ('#88aaff' if grad_tag == 'LinearGradient' else '#ffaa88'), 1.0
    return '#888888', 1.0

def _active_frame(layer, frame_num):
    """Return the DOMFrame active at frame_num in this layer, or None."""
    frames = layer.find(t('frames'))
    if frames is None:
        return None
    for f in frames.findall(t('DOMFrame')):
        idx = int(f.get('index', 0))
        dur = int(f.get('duration', 1))
        if idx <= frame_num < idx + dur:
            return f
    return None

def _shape_svg(shape, _defs=None, _grad_cache=None):
    """Render a DOMShape as filled SVG paths (one <path> per fill region).
    All edges for a fill are merged into one command list so that M-dedup
    in cmds_to_svg_d can stitch adjacent curves into continuous paths.

    _defs / _grad_cache: shared lists/dicts for gradient def accumulation.
    """
    fill_cmds = {}   # fill_idx → flat list of path commands
    fill_meta = {}   # fill_idx → (color_or_url, alpha)

    for fs in shape.iter(t('FillStyle')):
        idx = int(fs.get('index', 0))
        fill_meta[idx] = _get_fill_color(shape, idx, _defs, _grad_cache)

    for edge in shape.iter(t('Edge')):
        es = edge.get('edges', '').strip()
        if not es:
            continue
        try:
            cmds = parse_edge_str(es)
        except Exception:
            continue
        f0s = edge.get('fillStyle0')
        f1s = edge.get('fillStyle1')
        if f0s: fill_cmds.setdefault(int(f0s), []).extend(cmds)
        if f1s: fill_cmds.setdefault(int(f1s), []).extend(cmds)

    lines = []
    for fi, cmds in fill_cmds.items():
        color, alpha = fill_meta.get(fi, ('#888888', 1.0))
        d = cmds_to_svg_d(cmds)
        opac = f' fill-opacity="{alpha:.2f}"' if alpha < 0.9999 else ''
        lines.append(
            f'<path d="{d}" fill="{color}" fill-rule="evenodd"{opac} stroke="none"/>'
        )
    return lines

def _shape_svg_white(shape):
    """Like _shape_svg but renders ALL fills as white (for SVG <mask> use)."""
    all_cmds = []
    for edge in shape.iter(t('Edge')):
        es = edge.get('edges', '').strip()
        if not es:
            continue
        try:
            cmds = parse_edge_str(es)
        except Exception:
            continue
        if edge.get('fillStyle0') or edge.get('fillStyle1'):
            all_cmds.extend(cmds)
    if not all_cmds:
        return []
    d = cmds_to_svg_d(all_cmds)
    return [f'<path d="{d}" fill="white" fill-rule="evenodd" stroke="none"/>']


def _render_sym_white(name, symbols, inst_frame=0, visited=None, tx_scale=20.0):
    """Render a symbol entirely as white fills — used to build SVG mask shapes."""
    if visited is None:
        visited = set()
    sym = symbols.get(name)
    if sym is None:
        return []
    visited = visited | {name}
    tl = sym.find(t('timeline'))
    if tl is None: return []
    dom_tl = tl.find(t('DOMTimeline'))
    if dom_tl is None: return []
    layers_e = dom_tl.find(t('layers'))
    if layers_e is None: return []

    lines = []
    for layer in reversed(list(layers_e)):
        if layer.get('layerType') in ('guide', 'folder', 'mask'): continue
        if layer.get('visible') == 'false': continue
        frame = _active_frame(layer, inst_frame)
        if frame is None: continue
        elements = frame.find(t('elements'))
        if elements is None: continue
        for elem in list(elements):
            if elem.tag == t('DOMShape'):
                lines.extend(_shape_svg_white(elem))
            elif elem.tag == t('DOMSymbolInstance'):
                child = elem.get('libraryItemName')
                if not child or child in visited: continue
                first = int(elem.get('firstFrame', 0))
                _mc = elem.find(t('matrix'))
                m = _mc.find(t('Matrix')) if _mc is not None else None
                if m is not None:
                    a=float(m.get('a',1)); b=float(m.get('b',0))
                    c=float(m.get('c',0)); d=float(m.get('d',1))
                    tx=float(m.get('tx',0)) * tx_scale; ty=float(m.get('ty',0)) * tx_scale
                    xf = f'matrix({a},{b},{c},{d},{tx},{ty})'
                else:
                    xf = None
                child_lines = _render_sym_white(child, symbols, first, visited, tx_scale)
                if child_lines:
                    lines.append(f'<g transform="{xf}">' if xf else '<g>')
                    lines.extend(child_lines)
                    lines.append('</g>')
    return lines


def _render_sym(name, symbols, inst_frame=0, visited=None, _defs=None, _grad_cache=None, _tx_scale=1.0):
    """Recursively render a symbol and its children as SVG lines.

    Returns (body_lines, defs_lines).  defs_lines accumulates SVG <mask> and
    gradient definitions that must be emitted inside <defs> before the body.
    Pass _defs=[] on the first call; inner calls share the same list.
    _grad_cache is a {id(grad_elem): svg_id} dict shared across all shapes.
    """
    if _defs is None:
        _defs = []
    if _grad_cache is None:
        _grad_cache = {}
    if visited is None:
        visited = set()
    sym = symbols.get(name)
    if sym is None:
        return [], _defs

    visited = visited | {name}    # copy — each branch tracks its own path

    tl = sym.find(t('timeline'))
    if tl is None: return [], _defs
    dom_tl = tl.find(t('DOMTimeline'))
    if dom_tl is None: return [], _defs
    layers_e = dom_tl.find(t('layers'))
    if layers_e is None: return [], _defs

    all_layers = list(layers_e)

    # Which layers are masked (parentLayerIndex → mask layer index).
    # mask_groups: mask_layer_idx → [masked layer indices, front-to-back order]
    mask_groups = {}
    consumed    = set()
    for i, layer in enumerate(all_layers):
        ps = layer.get('parentLayerIndex')
        if ps is not None:
            pi = int(ps)
            mask_groups.setdefault(pi, []).append(i)
            consumed.add(i)

    def _inst_lines(elem, white=False):
        child = elem.get('libraryItemName')
        if not child or child in visited:
            return []
        first = int(elem.get('firstFrame', 0))
        _mc = elem.find(t('matrix'))
        m   = _mc.find(t('Matrix')) if _mc is not None else None
        if m is not None:
            a=float(m.get('a',1)); b=float(m.get('b',0))
            c=float(m.get('c',0)); d=float(m.get('d',1))
            tx=float(m.get('tx',0)) * _tx_scale
            ty=float(m.get('ty',0)) * _tx_scale
            xf = f'matrix({a},{b},{c},{d},{tx},{ty})'
        else:
            xf = None
        if white:
            cl = _render_sym_white(child, symbols, first, visited, _tx_scale)
        else:
            cl, _ = _render_sym(child, symbols, first, visited, _defs, _grad_cache, _tx_scale)
        if not cl:
            return []
        return ([f'<g transform="{xf}">'] if xf else ['<g>']) + cl + ['</g>']

    def _layer_lines(layer, white=False):
        frame = _active_frame(layer, inst_frame)
        if frame is None: return []
        elems = frame.find(t('elements'))
        if elems is None: return []
        out = []
        for elem in list(elems):
            if elem.tag == t('DOMShape'):
                out.extend(_shape_svg_white(elem) if white else _shape_svg(elem, _defs, _grad_cache))
            elif elem.tag == t('DOMSymbolInstance'):
                out.extend(_inst_lines(elem, white=white))
        return out

    body = []
    # Flash XML: first layer = topmost in UI. Reverse to draw back-to-front.
    for i, layer in reversed(list(enumerate(all_layers))):
        ltype = layer.get('layerType', 'normal')
        if ltype in ('guide', 'folder'): continue
        if layer.get('visible') == 'false': continue
        if i in consumed: continue   # handled inside its mask group

        if ltype == 'mask':
            # Build SVG <mask> from the mask layer geometry (all white).
            safe_name = name.replace('/', '_').replace('~', 'T').replace(' ', '_')
            mask_id = f'mask_{safe_name}_{i}'

            mask_shape = _layer_lines(layer, white=True)

            # Render masked layers back-to-front within the group.
            group_content = []
            for mi in reversed(mask_groups.get(i, [])):
                ml = all_layers[mi]
                if ml.get('visible') == 'false': continue
                group_content.extend(_layer_lines(ml))

            if mask_shape and group_content:
                _defs += [
                    f'<mask id="{mask_id}" maskUnits="userSpaceOnUse"'
                    f' x="-9999" y="-9999" width="99999" height="99999">',
                    *mask_shape,
                    '</mask>'
                ]
                body.append(f'<g mask="url(#{mask_id})">')
                body.extend(group_content)
                body.append('</g>')
            else:
                # Degenerate: no mask shape — render content unmasked.
                body.extend(group_content)
        else:
            body.extend(_layer_lines(layer))

    return body, _defs

# ── Bounding box helpers ──────────────────────────────────────────────────────

_IDENTITY_MAT = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)   # (a, b, c, d, tx, ty)

def _compose_mat(parent, child):
    """Compose two affine transforms: result = parent ∘ child."""
    pa, pb, pc, pd, ptx, pty = parent
    ca, cb, cc, cd, ctx, cty = child
    return (
        pa*ca + pc*cb,          pb*ca + pd*cb,
        pa*cc + pc*cd,          pb*cc + pd*cd,
        pa*ctx + pc*cty + ptx,  pb*ctx + pd*cty + pty,
    )

def _apply_mat(mat, x, y):
    """Apply affine transform to a point."""
    a, b, c, d, tx, ty = mat
    return a*x + c*y + tx, b*x + d*y + ty

def _collect_shape_pts(shape):
    """Fill-edge geometry points in a DOMShape (local space, untransformed).
    Stroke-only edges (no fillStyle0/1) are excluded — they can live at
    arbitrary positions and would bloat the bounding box."""
    pts = []
    for edge in shape.iter(t('Edge')):
        if not edge.get('fillStyle0') and not edge.get('fillStyle1'):
            continue
        es = edge.get('edges', '').strip()
        if not es:
            continue
        try:
            cmds = parse_edge_str(es)
        except Exception:
            continue
        for c in cmds:
            if c[0] == 'M':  pts.append((c[1], c[2]))
            elif c[0] == 'L': pts.append((c[1], c[2]))
            elif c[0] == 'Q': pts += [(c[1], c[2]), (c[3], c[4])]
    return pts

def _bbox_sym(name, symbols, inst_frame=0, visited=None, mat=None, tx_scale=20.0):
    """World-space AABB for a symbol hierarchy.  Returns (xmin,ymin,xmax,ymax) or None."""
    if visited is None: visited = set()
    if mat is None:     mat     = _IDENTITY_MAT

    sym = symbols.get(name)
    if sym is None:
        return None
    visited = visited | {name}

    tl = sym.find(t('timeline'))
    if tl is None: return None
    dom_tl = tl.find(t('DOMTimeline'))
    if dom_tl is None: return None
    layers_e = dom_tl.find(t('layers'))
    if layers_e is None: return None

    all_pts = []

    for layer in layers_e:
        if layer.get('layerType') in ('guide', 'folder'): continue
        if layer.get('visible') == 'false':               continue
        frame = _active_frame(layer, inst_frame)
        if frame is None: continue
        elements = frame.find(t('elements'))
        if elements is None: continue

        for elem in list(elements):
            if elem.tag == t('DOMShape'):
                for lx, ly in _collect_shape_pts(elem):
                    all_pts.append(_apply_mat(mat, lx, ly))

            elif elem.tag == t('DOMSymbolInstance'):
                child = elem.get('libraryItemName')
                if not child or child in visited: continue
                first = int(elem.get('firstFrame', 0))

                _mc = elem.find(t('matrix'))
                m   = _mc.find(t('Matrix')) if _mc is not None else None
                if m is not None:
                    child_mat = (
                        float(m.get('a',  1)), float(m.get('b',  0)),
                        float(m.get('c',  0)), float(m.get('d',  1)),
                        float(m.get('tx', 0)) * tx_scale, float(m.get('ty', 0)) * tx_scale,
                    )
                else:
                    child_mat = _IDENTITY_MAT

                result = _bbox_sym(child, symbols, first, visited, _compose_mat(mat, child_mat), tx_scale)
                if result:
                    xmin, ymin, xmax, ymax = result
                    all_pts += [(xmin, ymin), (xmax, ymax)]   # already world-space

    if not all_pts:
        return None
    xs = [p[0] for p in all_pts]
    ys = [p[1] for p in all_pts]
    return min(xs), min(ys), max(xs), max(ys)


def compose_to_svg(root_name, symbols, frame=0, out_path=None, tx_scale=20.0):
    """Compose a symbol hierarchy into a single SVG with proper matrix transforms."""
    print(f'Composing {root_name} at frame {frame} (tx_scale={tx_scale})...')

    # First pass: tight world-space bounding box
    bbox = _bbox_sym(root_name, symbols, frame, tx_scale=tx_scale)
    if bbox:
        pad = 80
        xmin, ymin, xmax, ymax = bbox
        vx, vy = xmin - pad, ymin - pad
        vw, vh = (xmax - xmin) + pad*2, (ymax - ymin) + pad*2
        print(f'  bbox: ({xmin:.0f},{ymin:.0f}) to ({xmax:.0f},{ymax:.0f})')
    else:
        vx, vy, vw, vh = -2000, -2000, 4000, 4000

    # Second pass: generate SVG
    body, defs = _render_sym(root_name, symbols, frame, _tx_scale=tx_scale)
    if not body:
        print('Nothing rendered.'); return

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg"'
        f' viewBox="{vx:.1f} {vy:.1f} {vw:.1f} {vh:.1f}">',
        f'  <rect x="{vx:.1f}" y="{vy:.1f}" width="{vw:.1f}" height="{vh:.1f}" fill="#1a1a1a"/>',
    ]
    if defs:
        lines.append('  <defs>')
        lines += ['    ' + d for d in defs]
        lines.append('  </defs>')
    lines += ['  ' + l for l in body] + ['</svg>']

    safe = root_name.replace('/', '_').replace('~', '').replace('(', '').replace(')', '')
    dest = Path(out_path) if out_path else Path(safe + '_composed.svg')
    dest.write_text('\n'.join(lines), encoding='utf-8')
    print(f'Written: {dest}  ({len(body)} SVG lines, viewBox {vx:.0f} {vy:.0f} {vw:.0f}x{vh:.0f})')

def export_all_symbols(symbols, outdir):
    """Export every non-trivial symbol as an individual SVG."""
    d = Path(outdir)
    d.mkdir(parents=True, exist_ok=True)
    count = 0
    for name, sym in symbols.items():
        # Skip pure-reference / empty symbols
        has_edges = any(True for _ in sym.iter(t('Edge')))
        if not has_edges:
            continue
        safe = name.replace('/', '_').replace('~', '').replace('(', '').replace(')', '')
        out  = d / (safe + '.svg')
        symbol_to_svg(name, symbols, str(out))
        count += 1
    print(f'\nExported {count} symbols to {d}/')


def symbol_to_svg(name, symbols, out_path=None):
    sym = symbols.get(name)
    if sym is None:
        print(f'Symbol not found: {name}'); return

    paths   = []   # (svg_d, stroke_color, opacity)
    all_pts = []

    for layer in sym.iter(t('DOMLayer')):
        for frame in layer.iter(t('DOMFrame')):
            if int(frame.get('index', 0)) != 0:
                continue
            for shape in frame.iter(t('DOMShape')):
                for edge in shape.iter(t('Edge')):
                    es = edge.get('edges', '').strip()
                    if not es:
                        continue
                    try:
                        cmds = parse_edge_str(es)
                    except Exception as e:
                        print(f'  parse error: {e}'); continue

                    for c in cmds:
                        if c[0] == 'M': all_pts.append((c[1], c[2]))
                        elif c[0] == 'L': all_pts.append((c[1], c[2]))
                        elif c[0] == 'Q': all_pts += [(c[1],c[2]),(c[3],c[4])]

                    # Pick a stroke color from whichever fill side isn't None
                    f1 = edge.get('fillStyle1')
                    f0 = edge.get('fillStyle0')
                    fi = int(f1) if f1 else (int(f0) if f0 else 0)
                    color, alpha = _get_fill_color(shape, fi) if fi else ('#ffffff', 0.4)

                    d = cmds_to_svg_d(cmds)
                    paths.append((d, color, alpha))

    if not all_pts:
        print('No geometry found'); return

    xs = [p[0] for p in all_pts]; ys = [p[1] for p in all_pts]
    pad  = 40
    vx   = min(xs) - pad;  vy = min(ys) - pad
    vw   = (max(xs) - min(xs)) + pad*2
    vh   = (max(ys) - min(ys)) + pad*2

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vx:.1f} {vy:.1f} {vw:.1f} {vh:.1f}">',
        f'  <rect x="{vx:.1f}" y="{vy:.1f}" width="{vw:.1f}" height="{vh:.1f}" fill="#1a1a1a"/>',
    ]
    for d, color, alpha in paths:
        lines.append(f'  <path d="{d}" fill="none" stroke="{color}" stroke-width="4" opacity="{alpha:.2f}"/>')
    lines.append('</svg>')

    safe  = name.replace('/', '_').replace('~', '').replace('(', '').replace(')', '')
    dest  = Path(out_path) if out_path else Path(safe + '.svg')
    dest.write_text('\n'.join(lines), encoding='utf-8')
    print(f'Written: {dest}   ({len(paths)} paths, {vw:.0f}x{vh:.0f} units)')


def scan_dir(dirpath):
    p = Path(dirpath)
    flas = sorted(p.rglob('*.fla'))
    print(f'Found {len(flas)} .fla files under {p}\n')
    zip_count = 0
    for fla in flas:
        zipped = is_zip(fla)
        label  = 'ZIP/XFL' if zipped else 'binary '
        print(f'  [{label}]  {fla.name}')
        if zipped:
            zip_count += 1
    print(f'\n{zip_count} / {len(flas)} are parseable (ZIP-based)')


# ── Entry point ───────────────────────────────────────────────────────────────

def main(argv):
    if not argv:
        print(__doc__)
        return

    target = Path(argv[0])

    # Directory scan mode
    if target.is_dir():
        scan_dir(target)
        return

    if not target.exists():
        print(f'Not found: {target}'); return

    if not is_zip(target):
        mb = magic(target).hex(' ')
        print(f'Not a ZIP-based FLA (magic: {mb})')
        print('This is a pre-CS5 binary FLA — cannot parse without Flash/Animate.')
        return

    print(f'Opening {target.name}...')
    zip_data = open_fla(target)
    dom      = parse_dom(zip_data)
    symbols  = get_symbols(zip_data)
    print_header(dom, len(symbols))
    print()

    args = argv[1:]

    if not args or '--list' in args:
        for name in sorted(symbols):
            refs = child_refs(symbols[name])
            print(f'  {name}  [{len(refs)} refs]')

    elif '--roots' in args:
        roots = find_roots(symbols)
        print(f'Root symbols ({len(roots)} total — not referenced by any other symbol):')
        for r in roots:
            sym  = symbols[r]
            refs = child_refs(sym)
            print(f'  {r}  ({sym.get("symbolType","?")} — {len(refs)} child refs)')

    elif '--tree' in args:
        idx  = args.index('--tree')
        name = args[idx + 1] if idx + 1 < len(args) else find_roots(symbols)[0]
        print_tree(name, symbols)

    elif '--shape' in args:
        idx  = args.index('--shape')
        name = args[idx + 1] if idx + 1 < len(args) else ''
        print_shapes(name, symbols)

    elif '--svg' in args:
        idx      = args.index('--svg')
        name     = args[idx + 1] if idx + 1 < len(args) else ''
        out_path = args[idx + 2] if idx + 2 < len(args) else None
        symbol_to_svg(name, symbols, out_path)

    elif '--compose' in args:
        idx      = args.index('--compose')
        name     = args[idx + 1] if idx + 1 < len(args) else find_roots(symbols)[0]
        # optional --frame N
        frame    = 0
        if '--frame' in args:
            fidx  = args.index('--frame')
            frame = int(args[fidx + 1]) if fidx + 1 < len(args) else 0
        out_path = args[idx + 2] if idx + 2 < len(args) and not args[idx+2].startswith('--') else None
        compose_to_svg(name, symbols, frame=frame, out_path=out_path)

    elif '--inspect' in args:
        idx   = args.index('--inspect')
        name  = args[idx + 1] if idx + 1 < len(args) else find_roots(symbols)[0]
        frame = 0
        if '--frame' in args:
            fidx  = args.index('--frame')
            frame = int(args[fidx + 1]) if fidx + 1 < len(args) else 0
        print(f'(frame {frame})')
        sym  = symbols.get(name)
        if sym is None:
            print(f'Symbol not found: {name}'); return
        tl      = sym.find(t('timeline'))
        dom_tl  = tl.find(t('DOMTimeline')) if tl is not None else None
        layers_e = dom_tl.find(t('layers')) if dom_tl is not None else None
        if layers_e is None:
            print('No layers found'); return
        print(f'Frame-{frame} DOMSymbolInstances in {name!r}:\n')
        for layer in layers_e:
            lname = layer.get('name', '?')
            ltype = layer.get('layerType', 'normal')
            vis   = layer.get('visible', 'true')
            frame_elem = _active_frame(layer, frame)
            if frame_elem is None: continue
            elems = frame_elem.find(t('elements'))
            if elems is None: continue
            for elem in elems:
                if elem.tag != t('DOMSymbolInstance'): continue
                child = elem.get('libraryItemName', '?')
                first = elem.get('firstFrame', '0')
                loop  = elem.get('loop', '?')
                _mc   = elem.find(t('matrix'))
                m     = _mc.find(t('Matrix')) if _mc is not None else None
                if m is not None:
                    a  = float(m.get('a',  1)); b  = float(m.get('b',  0))
                    c  = float(m.get('c',  0)); d  = float(m.get('d',  1))
                    tx = float(m.get('tx', 0)); ty = float(m.get('ty', 0))
                    mat_str = f'a={a:.3f} b={b:.3f} c={c:.3f} d={d:.3f} tx={tx:.1f} ty={ty:.1f}'
                else:
                    mat_str = '(no matrix — identity)'
                print(f'  layer={lname!r} [{ltype}, vis={vis}]')
                print(f'    -> {child!r}  firstFrame={first} loop={loop}')
                print(f'       {mat_str}')

    elif '--export-all' in args:
        idx    = args.index('--export-all')
        outdir = args[idx + 1] if idx + 1 < len(args) else 'svg_export'
        export_all_symbols(symbols, outdir)

    else:
        # bare symbol name → tree
        print_tree(args[0], symbols)


if __name__ == '__main__':
    main(sys.argv[1:])
