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

def safe_name(s: str) -> str:
    return s.replace('/', '_').replace('~', '').replace('(', '').replace(')', '')

def read_matrix(mc) -> tuple:
    """Read (a,b,c,d,tx,ty) from a Flash <matrix><Matrix .../></matrix> wrapper.
    Returns identity (1,0,0,1,0,0) if the wrapper or Matrix child is absent."""
    m = mc.find(t('Matrix')) if mc is not None else None
    if m is None:
        return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    return (float(m.get('a', 1)), float(m.get('b', 0)),
            float(m.get('c', 0)), float(m.get('d', 1)),
            float(m.get('tx', 0)), float(m.get('ty', 0)))

def parse_edge_str(s):
    """Flash edge string → list of ('M'|'L'|'Q', ...) tuples."""
    tokens = _TOK.findall(s)
    cmds = []
    i    = 0

    def num():
        nonlocal i
        tok = tokens[i]; i += 1
        return (decode_hex(tok) if tok.startswith('#') else float(tok)) / 20.0

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
    eps = 0.015

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

def _split_segs(cmds):
    """Split a flat command list into segments; each starts with exactly one M."""
    segs = []
    cur = []
    for c in cmds:
        if c[0] == 'M':
            if cur:
                segs.append(cur)
            cur = [c]
        else:
            cur.append(c)
    if cur:
        segs.append(cur)
    return [s for s in segs if len(s) > 1]  # drop bare M with no geometry


def _seg_startpoint(seg):
    return seg[0][1], seg[0][2]


def _seg_endpoint(seg):
    c = seg[-1]
    if c[0] == 'L': return c[1], c[2]
    if c[0] == 'Q': return c[3], c[4]
    return c[1], c[2]


def _reverse_seg(seg):
    """Reverse a path segment — turns a fillStyle1 (fill-right) edge into fill-left."""
    if not seg or len(seg) < 2:
        return seg
    cx, cy = seg[0][1], seg[0][2]
    annotated = []
    for c in seg[1:]:
        if c[0] == 'L':
            annotated.append((cx, cy, c))
            cx, cy = c[1], c[2]
        elif c[0] == 'Q':
            annotated.append((cx, cy, c))
            cx, cy = c[3], c[4]
    result = [('M', cx, cy)]
    for sx, sy, c in reversed(annotated):
        if c[0] == 'L':
            result.append(('L', sx, sy))
        elif c[0] == 'Q':
            result.append(('Q', c[1], c[2], sx, sy))  # control point stays
    return result


def _segs_to_svg_d(segs, eps=0.05):
    """Chain segments into closed contours and return an SVG d string.

    Segments are linked by matching endpoints within eps twips.
    Each closed contour gets a trailing Z.  Open chains are emitted as-is.

    Two-phase approach:
      Phase 1 — greedy chaining via startpoint index (fast, handles most shapes).
      Phase 2 — merge any leftover open chains that connect end-to-end, using a
                 slightly relaxed epsilon.  This fixes cases where a degenerate
                 micro-segment (Flash gap-patch) gets visited first and steals a
                 connection point that the main outline needs to close itself.
    """
    if not segs:
        return ''

    def rk(x, y):
        return (round(x / eps), round(y / eps))

    def close_ok(ex, ey, sx, sy, e):
        return abs(ex - sx) <= e and abs(ey - sy) <= e

    # Build start-point index: rounded key -> list of seg indices
    start_idx: dict = {}
    for i, seg in enumerate(segs):
        k = rk(*_seg_startpoint(seg))
        start_idx.setdefault(k, []).append(i)

    visited: set = set()
    chains = []   # list of [seg_index, ...]

    # ── Phase 1: greedy chaining ─────────────────────────────────────────────
    for seed in range(len(segs)):
        if seed in visited:
            continue
        chain = [seed]
        visited.add(seed)

        while True:
            ex, ey = _seg_endpoint(segs[chain[-1]])
            sx0, sy0 = _seg_startpoint(segs[chain[0]])
            if len(chain) > 1 and close_ok(ex, ey, sx0, sy0, eps):
                break  # closed loop
            cands = [i for i in start_idx.get(rk(ex, ey), []) if i not in visited]
            if not cands:
                break
            nxt = cands[0]
            visited.add(nxt)
            chain.append(nxt)

        chains.append(chain)

    # ── Phase 2: merge open chains ───────────────────────────────────────────
    # A degenerate micro-segment can get visited early and steal a connection
    # point, leaving the real outline as several open fragments.  Stitch those
    # fragments together using a 2× epsilon so tiny gaps (exactly == eps) close.
    merge_eps = eps * 2.0

    def chain_end(ch):
        return _seg_endpoint(segs[ch[-1]])

    def chain_start(ch):
        return _seg_startpoint(segs[ch[0]])

    # Repeat until no more merges are possible.
    for _ in range(len(chains)):
        # Find the first open chain whose endpoint matches another open chain's start.
        merged = False
        open_idx = [i for i, ch in enumerate(chains) if ch]   # non-empty
        for i in open_idx:
            chi = chains[i]
            ei = chain_end(chi)
            ex_closed = close_ok(*ei, *chain_start(chi), merge_eps)
            if ex_closed:
                continue  # already effectively closed, leave it
            for j in open_idx:
                if j == i:
                    continue
                chj = chains[j]
                if close_ok(*ei, *chain_start(chj), merge_eps):
                    chains[i] = chi + chj
                    chains[j] = []          # consumed
                    merged = True
                    break
            if merged:
                break
        if not merged:
            break

    # ── Emit ─────────────────────────────────────────────────────────────────
    parts_all = []
    for chain in chains:
        if not chain:
            continue
        ex, ey = chain_end(chain)
        sx0, sy0 = chain_start(chain)
        closed = close_ok(ex, ey, sx0, sy0, merge_eps)

        parts = []
        for k, si in enumerate(chain):
            seg = segs[si]
            if k == 0:
                parts.append(f'M{seg[0][1]:.2f} {seg[0][2]:.2f}')
            for c in seg[1:]:
                if c[0] == 'L':
                    parts.append(f'L{c[1]:.2f} {c[2]:.2f}')
                elif c[0] == 'Q':
                    parts.append(f'Q{c[1]:.2f} {c[2]:.2f} {c[3]:.2f} {c[4]:.2f}')
        if closed:
            parts.append('Z')
        parts_all.append(' '.join(parts))

    return ' '.join(parts_all)


def _gradient_to_svg(grad_elem, grad_tag, grad_id):
    """Build SVG <linearGradient> or <radialGradient> lines from XFL element.

    Flash gradient space: linear goes from x=-16384 to x=+16384 at y=0;
    radial has center (0,0) radius=16384.  The XFL Matrix maps gradient space
    → shape's local coordinate space, so we use gradientTransform.
    """
    a, b, c, d, tx, ty = read_matrix(grad_elem.find(t('matrix')))
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
                f' x1="-819.2" y1="0" x2="819.2" y2="0"{xf_attr}>')
        tail = '</linearGradient>'
    else:
        head = (f'<radialGradient id="{grad_id}" gradientUnits="userSpaceOnUse"'
                f' cx="0" cy="0" r="819.2" fx="0" fy="0"{xf_attr}>')
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
            c = sc.get('color', '#000000')
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
    return '#000000', 1.0


def _get_stroke_style(shape, idx):
    """Return (color, alpha, weight_px, linecap) for stroke style index idx."""
    for ss in shape.iter(t('StrokeStyle')):
        if ss.get('index') != str(idx):
            continue
        sol = ss.find(t('SolidStroke'))
        if sol is None:
            continue
        weight = float(sol.get('weight', 2.0))
        caps   = sol.get('caps', 'round')
        if caps == 'none': caps = 'butt'
        fc = sol.find(t('fill'))
        if fc is not None:
            sc = fc.find(t('SolidColor'))
            if sc is not None:
                return sc.get('color', '#000000'), float(sc.get('alpha', 1.0)), weight, caps
    return '#000000', 1.0, 2.0, 'round'


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

def _shape_svg(shape, _defs=None, _grad_cache=None, _in_group=False):
    """Render a DOMShape as filled + stroked SVG paths.

    Each Edge element is split into individual segments.  fillStyle0 edges are
    added forward; fillStyle1 edges are reversed (fill-on-right → fill-on-left).
    strokeStyle edges are collected as-is (direction doesn't affect stroke appearance).
    All segment groups are stitched into contours before rendering.

    _in_group=True suppresses the shape-level matrix transform.  Flash writes a
    redundant <matrix> on shapes inside DOMGroups that duplicates the group's own
    matrix; the group wrapper already applies it, so we must not apply it again.
    Layer-level shapes (_in_group=False, the default) always need their matrix
    applied — this covers both Drawing Objects and any positioned regular shapes.
    """
    fill_segs   = {}   # fill_idx   → list of segments
    fill_meta   = {}   # fill_idx   → (color_or_url, alpha)
    stroke_segs = {}   # stroke_idx → list of segments
    stroke_meta = {}   # stroke_idx → (color, alpha, weight_twips, linecap)

    for fs in shape.iter(t('FillStyle')):
        idx = int(fs.get('index', 0))
        fill_meta[idx] = _get_fill_color(shape, idx, _defs, _grad_cache)

    for ss in shape.iter(t('StrokeStyle')):
        idx = int(ss.get('index', 0))
        stroke_meta[idx] = _get_stroke_style(shape, idx)

    for edge in shape.iter(t('Edge')):
        es = edge.get('edges', '').strip()
        if not es:
            continue
        try:
            cmds = parse_edge_str(es)
        except Exception:
            continue
        segs = _split_segs(cmds)
        if not segs:
            continue
        f0s = edge.get('fillStyle0')
        f1s = edge.get('fillStyle1')
        sss = edge.get('strokeStyle')
        if f0s:
            fill_segs.setdefault(int(f0s), []).extend(segs)
        if f1s:
            fill_segs.setdefault(int(f1s), []).extend([_reverse_seg(s) for s in segs])
        if sss:
            stroke_segs.setdefault(int(sss), []).extend(segs)

    lines = []
    for fi, segs in fill_segs.items():
        color, alpha = fill_meta.get(fi, ('#000000', 1.0))
        d = _segs_to_svg_d(segs)
        if not d:
            continue
        opac = f' fill-opacity="{alpha:.2f}"' if alpha < 0.9999 else ''
        lines.append(
            f'<path d="{d}" fill="{color}" fill-rule="evenodd"{opac} stroke="none"/>'
        )
    for si, segs in stroke_segs.items():
        color, alpha, weight, caps = stroke_meta.get(si, ('#000000', 1.0, 40.0, 'round'))
        d = _segs_to_svg_d(segs)
        if not d:
            continue
        opac = f' stroke-opacity="{alpha:.2f}"' if alpha < 0.9999 else ''
        lines.append(
            f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{weight:.1f}"'
            f' stroke-linecap="{caps}" stroke-linejoin="round"{opac}/>'
        )
    if not _in_group:
        mc = shape.find(t('matrix'))
        if mc is not None:
            a, b, c, d, tx, ty = read_matrix(mc)
            if (a, b, c, d, tx, ty) != (1.0, 0.0, 0.0, 1.0, 0.0, 0.0):
                lines = [f'<g transform="matrix({a},{b},{c},{d},{tx},{ty})">'] + lines + ['</g>']
    return lines

def _shape_svg_white(shape, _in_group=False):
    """Like _shape_svg but renders ALL fills as white (for SVG <mask> use)."""
    all_segs = []
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
        if not f0s and not f1s:
            continue
        segs = _split_segs(cmds)
        if f0s:
            all_segs.extend(segs)
        if f1s:
            all_segs.extend([_reverse_seg(s) for s in segs])
    if not all_segs:
        return []
    d = _segs_to_svg_d(all_segs)
    if not d:
        return []
    result = [f'<path d="{d}" fill="white" fill-rule="evenodd" stroke="none"/>']
    if not _in_group:
        mc = shape.find(t('matrix'))
        if mc is not None:
            a, b, c, d, tx, ty = read_matrix(mc)
            if (a, b, c, d, tx, ty) != (1.0, 0.0, 0.0, 1.0, 0.0, 0.0):
                result = [f'<g transform="matrix({a},{b},{c},{d},{tx},{ty})">'] + result + ['</g>']
    return result


def _iter_group_shapes_white(group_elem):
    """Recursively yield white-fill SVG lines for all DOMShapes inside a DOMGroup."""
    members = group_elem.find(t('members'))
    if members is None:
        return
    a, b, c, d, tx, ty = read_matrix(group_elem.find(t('matrix')))
    group_mat = (a, b, c, d, tx, ty)
    xf = None if group_mat == _IDENTITY_MAT else f'matrix({a},{b},{c},{d},{tx},{ty})'
    inner = []
    for child in list(members):
        if child.tag == t('DOMShape'):
            # Suppress shape's own matrix only when Flash has mirrored the parent
            # group's matrix onto it (redundant metadata). If different, it's real.
            suppress = (read_matrix(child.find(t('matrix'))) == group_mat)
            inner.extend(_shape_svg_white(child, _in_group=suppress))
        elif child.tag == t('DOMGroup'):
            inner.extend(_iter_group_shapes_white(child))
    if inner:
        if xf:
            yield f'<g transform="{xf}">'
            yield from inner
            yield '</g>'
        else:
            yield from inner


def _render_sym_white(name, symbols, inst_frame=0, visited=None):
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
        frame = _active_frame(layer, inst_frame)
        if frame is None: continue
        elements = frame.find(t('elements'))
        if elements is None: continue
        for elem in list(elements):
            if elem.tag == t('DOMShape'):
                lines.extend(_shape_svg_white(elem))
            elif elem.tag == t('DOMGroup'):
                lines.extend(_iter_group_shapes_white(elem))
            elif elem.tag == t('DOMSymbolInstance'):
                child = elem.get('libraryItemName')
                if not child or child in visited: continue
                first = int(elem.get('firstFrame', 0))
                a, b, c, d, tx, ty = read_matrix(elem.find(t('matrix')))
                xf = f'matrix({a},{b},{c},{d},{tx},{ty})'
                child_lines = _render_sym_white(child, symbols, first, visited)
                if child_lines:
                    lines.append(f'<g transform="{xf}">' if xf else '<g>')
                    lines.extend(child_lines)
                    lines.append('</g>')
    return lines


def _render_sym(name, symbols, inst_frame=0, visited=None, _defs=None, _grad_cache=None, masking=True):
    """Recursively render a symbol and its children as SVG lines.

    Returns (body_lines, defs_lines).  defs_lines accumulates SVG <mask> and
    gradient definitions that must be emitted inside <defs> before the body.
    Pass _defs=[] on the first call; inner calls share the same list.
    _grad_cache is a {id(grad_elem): svg_id} dict shared across all shapes.
    masking=False renders all layers as plain geometry (ignores SVG masking).
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
    if masking:
        for i, layer in enumerate(all_layers):
            ps = layer.get('parentLayerIndex')
            if ps is not None:
                pi = int(ps)
                if all_layers[pi].get('layerType') == 'mask':
                    mask_groups.setdefault(pi, []).append(i)
                    consumed.add(i)

    def _inst_lines(elem, white=False):
        child = elem.get('libraryItemName')
        if not child or child in visited:
            return []
        first = int(elem.get('firstFrame', 0))
        a, b, c, d, tx, ty = read_matrix(elem.find(t('matrix')))
        xf = f'matrix({a},{b},{c},{d},{tx},{ty})'
        if white:
            cl = _render_sym_white(child, symbols, first, visited)
        else:
            cl, _ = _render_sym(child, symbols, first, visited, _defs, _grad_cache, masking)
        if not cl:
            return []
        return [f'<g transform="{xf}">'] + cl + ['</g>']

    def _group_lines(group_elem, white=False):
        members = group_elem.find(t('members'))
        if members is None:
            return []
        a, b, c, d, tx, ty = read_matrix(group_elem.find(t('matrix')))
        group_mat = (a, b, c, d, tx, ty)
        xf = None if group_mat == _IDENTITY_MAT else f'matrix({a},{b},{c},{d},{tx},{ty})'
        inner = []
        for child in list(members):
            if child.tag == t('DOMShape'):
                suppress = (read_matrix(child.find(t('matrix'))) == group_mat)
                inner.extend(_shape_svg_white(child, _in_group=suppress) if white else _shape_svg(child, _defs, _grad_cache, _in_group=suppress))
            elif child.tag == t('DOMGroup'):
                inner.extend(_group_lines(child, white))
            elif child.tag == t('DOMSymbolInstance'):
                inner.extend(_inst_lines(child, white))
        if not inner:
            return []
        if xf:
            return [f'<g transform="{xf}">'] + inner + ['</g>']
        return inner

    def _layer_lines(layer, white=False):
        frame = _active_frame(layer, inst_frame)
        if frame is None: return []
        elems = frame.find(t('elements'))
        if elems is None: return []
        out = []
        for elem in list(elems):
            if elem.tag == t('DOMShape'):
                out.extend(_shape_svg_white(elem) if white else _shape_svg(elem, _defs, _grad_cache))
            elif elem.tag == t('DOMGroup'):
                out.extend(_group_lines(elem, white=white))
            elif elem.tag == t('DOMSymbolInstance'):
                out.extend(_inst_lines(elem, white=white))
        return out

    body = []
    # Flash XML: first layer = topmost in UI. Reverse to draw back-to-front.
    for i, layer in reversed(list(enumerate(all_layers))):
        ltype = layer.get('layerType', 'normal')
        if ltype in ('guide', 'folder'): continue
        if i in consumed: continue   # handled inside its mask group

        if masking and ltype == 'mask':
            # Build SVG <mask> from the mask layer geometry (all white).
            # mask-type:alpha means only the opacity of the mask shapes matters —
            # the white fill colour is ignored and does not bleed into the content.
            mask_id = f'mask_{safe_name(name)}_{i}'

            mask_shape = _layer_lines(layer, white=True)

            # Render masked layers back-to-front within the group.
            group_content = []
            for mi in reversed(mask_groups.get(i, [])):
                ml = all_layers[mi]
                group_content.extend(_layer_lines(ml))

            if mask_shape and group_content:
                _defs += [
                    f'<mask id="{mask_id}" maskUnits="userSpaceOnUse"'
                    f' x="-500" y="-500" width="5000" height="5000">',
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

def _collect_shape_pts(shape, _in_group=False):
    """Fill-edge geometry points for a DOMShape, in the parent's local space.
    Applies the shape's own matrix for layer-level shapes (_in_group=False).
    Shapes inside DOMGroups (_in_group=True) skip this — the group's matrix is
    applied by the caller (_collect_group_pts) instead.
    Stroke-only edges (no fillStyle0/1) are excluded."""
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
    if not _in_group:
        mc = shape.find(t('matrix'))
        if mc is not None:
            sm = read_matrix(mc)
            if sm != _IDENTITY_MAT:
                pts = [_apply_mat(sm, x, y) for x, y in pts]
    return pts

def _collect_group_pts(group_elem, mat):
    """Recursively collect world-space points from all DOMShapes inside a DOMGroup."""
    pts = []
    group_local_mat = read_matrix(group_elem.find(t('matrix')))
    effective = _compose_mat(mat, group_local_mat)
    members = group_elem.find(t('members'))
    if members is None:
        return pts
    for child in list(members):
        if child.tag == t('DOMShape'):
            suppress = (read_matrix(child.find(t('matrix'))) == group_local_mat)
            for lx, ly in _collect_shape_pts(child, _in_group=suppress):
                pts.append(_apply_mat(effective, lx, ly))
        elif child.tag == t('DOMGroup'):
            pts.extend(_collect_group_pts(child, effective))
    return pts


def _bbox_sym(name, symbols, inst_frame=0, visited=None, mat=None):
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
        frame = _active_frame(layer, inst_frame)
        if frame is None: continue
        elements = frame.find(t('elements'))
        if elements is None: continue

        for elem in list(elements):
            if elem.tag == t('DOMShape'):
                for lx, ly in _collect_shape_pts(elem):
                    all_pts.append(_apply_mat(mat, lx, ly))

            elif elem.tag == t('DOMGroup'):
                all_pts.extend(_collect_group_pts(elem, mat))

            elif elem.tag == t('DOMSymbolInstance'):
                child = elem.get('libraryItemName')
                if not child or child in visited: continue
                first = int(elem.get('firstFrame', 0))

                child_mat = read_matrix(elem.find(t('matrix')))
                result = _bbox_sym(child, symbols, first, visited, _compose_mat(mat, child_mat))
                if result:
                    xmin, ymin, xmax, ymax = result
                    all_pts += [(xmin, ymin), (xmax, ymax)]   # already world-space

    if not all_pts:
        return None
    xs = [p[0] for p in all_pts]
    ys = [p[1] for p in all_pts]
    return min(xs), min(ys), max(xs), max(ys)


def compose_to_svg(root_name, symbols, frame=0, out_path=None, masking=True):
    """Compose a symbol hierarchy into a single SVG with proper matrix transforms."""
    mask_note = '' if masking else ' [masking OFF]'
    print(f'Composing {root_name} at frame {frame}{mask_note}...')

    # First pass: tight world-space bounding box
    bbox = _bbox_sym(root_name, symbols, frame)
    if bbox:
        pad = 80
        xmin, ymin, xmax, ymax = bbox
        vx, vy = xmin - pad, ymin - pad
        vw, vh = (xmax - xmin) + pad*2, (ymax - ymin) + pad*2
        print(f'  bbox: ({xmin:.0f},{ymin:.0f}) to ({xmax:.0f},{ymax:.0f})')
    else:
        vx, vy, vw, vh = -2000, -2000, 4000, 4000

    # Second pass: generate SVG
    body, defs = _render_sym(root_name, symbols, frame, masking=masking)
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

    dest = Path(out_path) if out_path else Path(safe_name(root_name) + '_composed.svg')
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
        out  = d / (safe_name(name) + '.svg')
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

    dest  = Path(out_path) if out_path else Path(safe_name(name) + '.svg')
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
