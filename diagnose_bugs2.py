"""Deep diagnostic for three bugs."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from fla_inspect import open_fla, get_symbols, t, read_matrix, _IDENTITY_MAT, parse_edge_str

def describe_mat(m):
    a,b,c,d,tx,ty = m
    if m == _IDENTITY_MAT: return 'identity'
    if b==0 and c==0 and a==1 and d==1: return f'translate({tx:.2f}, {ty:.2f})'
    return f'matrix({a:.4f},{b:.4f},{c:.4f},{d:.4f},{tx:.2f},{ty:.2f})'

def sym_layer_detail(name, symbols, frame=0):
    """Print detailed layer content for one symbol."""
    sym = symbols.get(name)
    if sym is None:
        print(f'  !! symbol not found: {name}')
        return
    tl = sym.find(t('timeline'))
    if tl is None: print('  no timeline'); return
    dom_tl = tl.find(t('DOMTimeline'))
    if dom_tl is None: print('  no DOMTimeline'); return
    layers_e = dom_tl.find(t('layers'))
    if layers_e is None: print('  no layers'); return
    all_layers = list(layers_e)
    print(f'  Symbol "{name}" — {len(all_layers)} layers, showing frame {frame}')
    for i, layer in enumerate(all_layers):
        lname = layer.get('name', f'layer{i}')
        ltype = layer.get('layerType', 'normal')
        par = layer.get('parentLayerIndex')
        par_str = ''
        if par is not None:
            pi = int(par)
            pl = all_layers[pi]
            par_str = f' [child-of layer {pi}: "{pl.get("name","")}" ({pl.get("layerType","normal")})]'
        print(f'  Layer {i}: "{lname}" ({ltype}){par_str}')
        # Find active frame
        active = None
        for fe in layer.iter(t('DOMFrame')):
            fi = int(fe.get('index', 0))
            dur = int(fe.get('duration', 1))
            if fi <= frame < fi + dur:
                active = fe
                break
        if active is None:
            print(f'    (no frame at {frame})')
            continue
        elems = active.find(t('elements'))
        if elems is None:
            print(f'    (empty frame)')
            continue
        for elem in list(elems):
            tag = elem.tag.split('}')[-1]
            if tag == 'DOMShape':
                mc = elem.find(t('matrix'))
                m = read_matrix(mc)
                isdrawing = elem.get('isDrawingObject', 'false')
                n_edges = sum(1 for _ in elem.iter(t('Edge')))
                print(f'    DOMShape: isDrawingObject={isdrawing}, {n_edges} edges, matrix={describe_mat(m)}')
            elif tag == 'DOMSymbolInstance':
                child = elem.get('libraryItemName', '?')
                mc = elem.find(t('matrix'))
                m = read_matrix(mc)
                ff = elem.get('firstFrame', '0')
                print(f'    Instance: "{child}" firstFrame={ff} matrix={describe_mat(m)}')
            elif tag == 'DOMGroup':
                mc = elem.find(t('matrix'))
                m = read_matrix(mc)
                members = elem.find(t('members'))
                n = len(list(members)) if members is not None else 0
                print(f'    Group: {n} members, matrix={describe_mat(m)}')
            else:
                print(f'    {tag}')

def check_drawing_objects(name, symbols, path='', visited=None):
    """Find any DOMShape with isDrawingObject=true in a symbol and its children."""
    if visited is None: visited = set()
    if name in visited: return
    visited = visited | {name}
    sym = symbols.get(name)
    if sym is None: return

    for shape in sym.iter(t('DOMShape')):
        if shape.get('isDrawingObject') == 'true':
            mc = shape.find(t('matrix'))
            m = read_matrix(mc)
            n_edges = sum(1 for _ in shape.iter(t('Edge')))
            print(f'  DrawingObject in {path}: {n_edges} edges, matrix={describe_mat(m)}')

    # Walk children
    tl = sym.find(t('timeline'))
    if tl is None: return
    for elem in tl.iter(t('DOMSymbolInstance')):
        child = elem.get('libraryItemName', '')
        if child and child not in visited:
            check_drawing_objects(child, symbols, f'{path}>{child}', visited)


print('='*70)
print('  IUC — HindLeg & CutieMark symbol detail')
print('='*70)
iuc = open_fla('fla/new/stk_C20_Incidental_Unicorns.fla')
iuc_syms = get_symbols(iuc)

# IUC01 HindLeg structure at frame 0
print('\n--- IUC01_HindLeg_3-4 at frame 0 ---')
sym_layer_detail('IUC01_HindLeg_3-4', iuc_syms, frame=0)

print('\n--- IUC01_HindLeg_3-4 at frame 1 ---')
sym_layer_detail('IUC01_HindLeg_3-4', iuc_syms, frame=1)

print('\n--- IUC01_CutieMark ---')
sym_layer_detail('IUC01_CutieMark', iuc_syms, frame=0)
sym_layer_detail('IUC01_CutieMark', iuc_syms, frame=1)

print('\n--- Drawing Objects in IUC01_CutieMark (and its children) ---')
check_drawing_objects('IUC01_CutieMark', iuc_syms, 'IUC01_CutieMark')

print('\n--- Drawing Objects in IUC01_HindLeg_3-4 (and its children) ---')
check_drawing_objects('IUC01_HindLeg_3-4', iuc_syms, 'IUC01_HindLeg_3-4')

# Where is IUC01_HindLeg_3-4 placed in IUC01_Character?
print('\n--- Where HindLeg_3-4 is placed in IUC01_Character ---')
iuc01 = iuc_syms.get('~IUC01_Character')
if iuc01:
    for elem in iuc01.iter(t('DOMSymbolInstance')):
        child = elem.get('libraryItemName', '')
        if 'HindLeg_3-4' in child and 'rear' not in child:
            mc = elem.find(t('matrix'))
            m = read_matrix(mc)
            ff = elem.get('firstFrame', '0')
            print(f'  Instance "{child}" firstFrame={ff} matrix={describe_mat(m)}')

print('\n--- IUC02_HindLeg_3-4 at frame 0 ---')
sym_layer_detail('IUC02_HindLeg_3-4', iuc_syms, frame=0)

print('\n--- IUC02_HindLeg_3-4 at frame 1 ---')
sym_layer_detail('IUC02_HindLeg_3-4', iuc_syms, frame=1)

print('\n--- Drawing Objects in IUC02_HindLeg_3-4 ---')
check_drawing_objects('IUC02_HindLeg_3-4', iuc_syms, 'IUC02_HindLeg_3-4')

# Is there a mask on the HindLeg that should be clipping the cutie mark?
print('\n--- IUC02 HindLeg mask structure ---')
hindb = iuc_syms.get('IUC02_HindLeg_3-4')
if hindb:
    tl = hindb.find(t('timeline'))
    dom_tl = tl.find(t('DOMTimeline'))
    layers_e = dom_tl.find(t('layers'))
    for i, layer in enumerate(list(layers_e)):
        lname = layer.get('name', f'layer{i}')
        ltype = layer.get('layerType', 'normal')
        par = layer.get('parentLayerIndex')
        print(f'  Layer {i}: "{lname}" ({ltype}) parentLayerIndex={par}')

print('\n\n' + '='*70)
print('  PC_tailcycle_front — Layer 8 FILL mask-of-mask issue')
print('='*70)

cel = open_fla('fla/new/stk_c08_princess_celestia.fla')
cel_syms = get_symbols(cel)

print('\n--- PC_tailcycle_front layers ---')
tf = cel_syms.get('PC_tailcycle_front')
if tf:
    tl = tf.find(t('timeline'))
    dom_tl = tl.find(t('DOMTimeline'))
    layers_e = dom_tl.find(t('layers'))
    all_layers = list(layers_e)
    for i, layer in enumerate(all_layers):
        lname = layer.get('name', f'layer{i}')
        ltype = layer.get('layerType', 'normal')
        par = layer.get('parentLayerIndex')
        par_str = ''
        if par is not None:
            pi = int(par)
            pl = all_layers[pi]
            par_str = f' -> parent "{pl.get("name","")}" ({pl.get("layerType","normal")})'
        print(f'  Layer {i}: "{lname}" ({ltype}) parentLayerIndex={par}{par_str}')

print('\n--- PC_tailcycle_1 layers ---')
t1 = cel_syms.get('PC_tailcycle_1')
if t1:
    tl = t1.find(t('timeline'))
    dom_tl = tl.find(t('DOMTimeline'))
    layers_e = dom_tl.find(t('layers'))
    all_layers = list(layers_e)
    for i, layer in enumerate(all_layers):
        lname = layer.get('name', f'layer{i}')
        ltype = layer.get('layerType', 'normal')
        par = layer.get('parentLayerIndex')
        par_str = ''
        if par is not None:
            pi = int(par)
            pl = all_layers[pi]
            par_str = f' -> parent "{pl.get("name","")}" ({pl.get("layerType","normal")})'
        print(f'  Layer {i}: "{lname}" ({ltype}) parentLayerIndex={par}{par_str}')
