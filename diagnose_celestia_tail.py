"""Inspect PC_tailcycle_front layer 8 content and the mask-in-mask structure."""
import sys, xml.etree.ElementTree as ET
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from fla_inspect import open_fla, get_symbols, t, read_matrix, _IDENTITY_MAT, _active_frame

cel = open_fla('fla/new/stk_c08_princess_celestia.fla')
cel_syms = get_symbols(cel)

tcf = cel_syms['PC_tailcycle_front']
tl = tcf.find(t('timeline'))
dom_tl = tl.find(t('DOMTimeline'))
layers_e = dom_tl.find(t('layers'))
all_layers = list(layers_e)

print('=== PC_tailcycle_front ===')
for i, layer in enumerate(all_layers):
    lname = layer.get('name', f'layer{i}')
    ltype = layer.get('layerType', 'normal')
    par = layer.get('parentLayerIndex')
    frame = _active_frame(layer, 0)
    if frame is None:
        print(f'Layer {i}: "{lname}" ({ltype}) parentLayerIndex={par} — NO frame at 0')
        continue
    elems = frame.find(t('elements'))
    if elems is None:
        print(f'Layer {i}: "{lname}" ({ltype}) parentLayerIndex={par} — empty frame')
        continue
    elem_list = list(elems)
    elem_descs = []
    for elem in elem_list:
        tag = elem.tag.split('}')[-1]
        if tag == 'DOMShape':
            n_e = sum(1 for _ in elem.iter(t('Edge')))
            isdraw = elem.get('isDrawingObject', 'false')
            elem_descs.append(f'DOMShape({n_e} edges, isDraw={isdraw})')
        elif tag == 'DOMSymbolInstance':
            child = elem.get('libraryItemName', '?')
            ff = elem.get('firstFrame', '0')
            mc = elem.find(t('matrix'))
            m = read_matrix(mc)
            a,b,c,d,tx,ty = m
            elem_descs.append(f'Instance("{child}" ff={ff} tx={tx:.1f},ty={ty:.1f})')
        elif tag == 'DOMGroup':
            elem_descs.append(f'DOMGroup')
        else:
            elem_descs.append(tag)
    print(f'Layer {i}: "{lname}" ({ltype}) parentLayerIndex={par}')
    for d in elem_descs:
        print(f'  {d}')

print()
print('=== PC_tailcycle_1 for comparison ===')
tc1 = cel_syms['PC_tailcycle_1']
tl1 = tc1.find(t('timeline'))
dom_tl1 = tl1.find(t('DOMTimeline'))
layers_e1 = dom_tl1.find(t('layers'))
all_layers1 = list(layers_e1)

for i, layer in enumerate(all_layers1):
    lname = layer.get('name', f'layer{i}')
    ltype = layer.get('layerType', 'normal')
    par = layer.get('parentLayerIndex')
    frame = _active_frame(layer, 0)
    if frame is None:
        print(f'Layer {i}: "{lname}" ({ltype}) parentLayerIndex={par} — NO frame')
        continue
    elems = frame.find(t('elements'))
    if elems is None:
        print(f'Layer {i}: "{lname}" ({ltype}) parentLayerIndex={par} — empty')
        continue
    elem_list = list(elems)
    for elem in elem_list:
        tag = elem.tag.split('}')[-1]
        if tag == 'DOMSymbolInstance':
            child = elem.get('libraryItemName', '?')
            mc = elem.find(t('matrix'))
            m = read_matrix(mc)
            a,b,c,d,tx,ty = m
            print(f'Layer {i}: "{lname}" ({ltype}) par={par} → Instance("{child}" tx={tx:.1f},ty={ty:.1f})')
        elif tag == 'DOMShape':
            n_e = sum(1 for _ in elem.iter(t('Edge')))
            print(f'Layer {i}: "{lname}" ({ltype}) par={par} → DOMShape({n_e} edges)')
