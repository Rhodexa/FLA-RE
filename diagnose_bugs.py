"""Diagnostic script for three visual bugs:
  1. IUC01 (Amethyst Star) cutie mark straying up-right
  2. IUC02 (Lyra) cutie mark clipping through body
  3. PC_Character (Celestia) tail fill masks broken
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from fla_inspect import open_fla, get_symbols, t, read_matrix, _IDENTITY_MAT, parse_xml
import xml.etree.ElementTree as ET

def print_sep(label):
    print(f'\n{"="*70}')
    print(f'  {label}')
    print(f'{"="*70}')

def describe_mat(m):
    a,b,c,d,tx,ty = m
    if m == _IDENTITY_MAT:
        return 'identity'
    if b==0 and c==0 and a==1 and d==1:
        return f'translate({tx:.2f}, {ty:.2f})'
    return f'matrix({a:.4f},{b:.4f},{c:.4f},{d:.4f},{tx:.2f},{ty:.2f})'

def walk_sym_tree(name, symbols, depth=0, visited=None, frame=0):
    """Print the layer/symbol tree for a symbol, highlighting masks."""
    if visited is None: visited = set()
    if name in visited: return
    visited = visited | {name}
    sym = symbols.get(name)
    if sym is None: return

    indent = '  ' * depth
    tl = sym.find(t('timeline'))
    if tl is None: return
    dom_tl = tl.find(t('DOMTimeline'))
    if dom_tl is None: return
    layers_e = dom_tl.find(t('layers'))
    if layers_e is None: return
    all_layers = list(layers_e)

    for i, layer in enumerate(all_layers):
        lname = layer.get('name', f'layer{i}')
        ltype = layer.get('layerType', 'normal')
        par = layer.get('parentLayerIndex')
        flag = ''
        if ltype == 'mask': flag = ' [MASK]'
        elif par is not None:
            pi = int(par)
            if all_layers[pi].get('layerType') == 'mask':
                flag = f' [masked by layer {pi}: {all_layers[pi].get("name","")}]'
        print(f'{indent}  Layer {i}: "{lname}" ({ltype}){flag}')

def find_cutie_mark_instances(name, symbols, path='', visited=None, frame=0):
    """Search the symbol tree for any instance whose libraryItemName contains
    'cutie', 'cm', 'CutieMark', 'mark', and print its matrix."""
    if visited is None: visited = set()
    if name in visited: return
    visited = visited | {name}
    sym = symbols.get(name)
    if sym is None: return

    tl = sym.find(t('timeline'))
    if tl is None: return
    dom_tl = tl.find(t('DOMTimeline'))
    if dom_tl is None: return
    layers_e = dom_tl.find(t('layers'))
    if layers_e is None: return

    for layer in layers_e:
        lname = layer.get('name', '')
        ltype = layer.get('layerType', 'normal')
        for frame_elem in layer.iter(t('DOMFrame')):
            fi = int(frame_elem.get('index', 0))
            if fi != frame: continue
            elements = frame_elem.find(t('elements'))
            if elements is None: continue
            for elem in elements:
                if elem.tag == t('DOMSymbolInstance'):
                    child = elem.get('libraryItemName', '')
                    keywords = ('cutie', 'Cutie', 'CM', '_cm', 'CutieMark', 'Mark', 'mark')
                    is_match = any(kw in child for kw in keywords)
                    if is_match or any(kw.lower() in child.lower() for kw in ('cutie', 'mark')):
                        mc = elem.find(t('matrix'))
                        m = read_matrix(mc)
                        print(f'  FOUND: {path}  > layer "{lname}" ({ltype})')
                        print(f'         instance={child}')
                        print(f'         matrix={describe_mat(m)}')
                        print(f'         firstFrame={elem.get("firstFrame", 0)}')
                    find_cutie_mark_instances(child, symbols, f'{path}>{child}', visited, 0)

def show_mask_layers(name, symbols, depth=0, visited=None):
    """Show all mask layers and their parentLayerIndex in a symbol and its children."""
    if visited is None: visited = set()
    if name in visited: return
    visited = visited | {name}
    sym = symbols.get(name)
    if sym is None: return
    indent = '  ' * depth

    tl = sym.find(t('timeline'))
    if tl is None: return
    dom_tl = tl.find(t('DOMTimeline'))
    if dom_tl is None: return
    layers_e = dom_tl.find(t('layers'))
    if layers_e is None: return

    all_layers = list(layers_e)
    has_mask = any(l.get('layerType') == 'mask' for l in all_layers)
    has_masked = any(l.get('parentLayerIndex') is not None for l in all_layers)

    if has_mask or has_masked:
        print(f'{indent}Symbol: {name}')
        for i, layer in enumerate(all_layers):
            lname = layer.get('name', f'layer{i}')
            ltype = layer.get('layerType', 'normal')
            par = layer.get('parentLayerIndex')
            if ltype == 'mask' or par is not None:
                pi_str = f' parentLayerIndex={par}' if par else ''
                pi_name = ''
                if par is not None:
                    pi = int(par)
                    pl = all_layers[pi]
                    pl_type = pl.get('layerType', 'normal')
                    pl_name = pl.get('name', f'layer{pi}')
                    pi_name = f' -> "{pl_name}" ({pl_type})'
                print(f'{indent}  Layer {i}: "{lname}" ({ltype}){pi_str}{pi_name}')

    # Walk into instances
    for layer in all_layers:
        ltype = layer.get('layerType', 'normal')
        if ltype in ('guide', 'folder'): continue
        for frame_elem in layer.iter(t('DOMFrame')):
            for elem in (frame_elem.find(t('elements')) or []):
                if elem.tag == t('DOMSymbolInstance'):
                    child = elem.get('libraryItemName', '')
                    if child and child not in visited:
                        show_mask_layers(child, symbols, depth+1, visited)


# ── IUC (Incidental Unicorns) ────────────────────────────────────────────────

print_sep('IUC01/IUC02 — Cutie Mark Investigation')
iuc_path = Path('fla/new/stk_C20_Incidental_Unicorns.fla')
from fla_inspect import open_fla, get_symbols
zip_data = open_fla(iuc_path)
iuc_syms = get_symbols(zip_data)

# List all symbols that have 'cutie' or 'cm' or 'mark' in their name (case insensitive)
cm_syms = [n for n in iuc_syms if any(kw in n.lower() for kw in ('cutie', 'cutiemark', '_cm', 'mark'))]
print(f'\nCutie-mark-related symbols ({len(cm_syms)}):')
for n in sorted(cm_syms):
    print(f'  {n}')

print('\n--- Cutie mark instances in IUC01_Character ---')
find_cutie_mark_instances('~IUC01_Character', iuc_syms)

print('\n--- Cutie mark instances in IUC02_Character ---')
find_cutie_mark_instances('~IUC02_Character', iuc_syms)

print('\n--- Layer structure for IUC01_Character ---')
walk_sym_tree('~IUC01_Character', iuc_syms)

print('\n--- Layer structure for IUC02_Character ---')
walk_sym_tree('~IUC02_Character', iuc_syms)

# ── Celestia tail mask ───────────────────────────────────────────────────────

print_sep('PC_Character — Tail Mask Investigation')
cel_path = Path('fla/new/stk_c08_princess_celestia.fla')
cel_data = open_fla(cel_path)
cel_syms = get_symbols(cel_data)

tail_syms = [n for n in cel_syms if 'tail' in n.lower() or 'Tail' in n]
print(f'\nTail-related symbols ({len(tail_syms)}):')
for n in sorted(tail_syms):
    print(f'  {n}')

print('\n--- Mask layers in PC_Character (and children with masks) ---')
show_mask_layers('~PC_Character', cel_syms)
