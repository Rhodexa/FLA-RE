"""Dump raw XML for specific symbols to check Group/matrix structure."""
import sys, xml.etree.ElementTree as ET
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from fla_inspect import open_fla, get_symbols, t

def dump_sym_xml(name, symbols, max_chars=3000):
    sym = symbols.get(name)
    if sym is None:
        print(f'!! {name} not found')
        return
    xml_str = ET.tostring(sym, encoding='unicode')
    print(xml_str[:max_chars])
    if len(xml_str) > max_chars:
        print(f'  ... ({len(xml_str)} chars total, truncated)')

iuc = open_fla('fla/new/stk_C20_Incidental_Unicorns.fla')
iuc_syms = get_symbols(iuc)

print('='*70)
print('  IUC01_CutieMark XML')
print('='*70)
dump_sym_xml('IUC01_CutieMark', iuc_syms, max_chars=4000)

print('\n' + '='*70)
print('  IUC02_CutieMark XML')
print('='*70)
dump_sym_xml('IUC02_CutieMark', iuc_syms, max_chars=4000)

# Also look at IUC01_HindLeg_3-4 layer 1 (the one with cutie mark) in detail
print('\n' + '='*70)
print('  Where the cutie mark sits in IUC01_Character (layer detail)')
print('='*70)
iuc01 = iuc_syms.get('~IUC01_Character')
if iuc01 is not None:
    tl = iuc01.find(t('timeline'))
    dom_tl = tl.find(t('DOMTimeline'))
    layers_e = dom_tl.find(t('layers'))
    for i, layer in enumerate(list(layers_e)):
        lname = layer.get('name', f'layer{i}')
        ltype = layer.get('layerType', 'normal')
        par = layer.get('parentLayerIndex')
        par_str = f' [parentLayerIndex={par}]' if par else ''
        # Quick check: does this layer contain HindLeg_3-4?
        for fe in layer.iter(t('DOMFrame')):
            fi = int(fe.get('index', 0))
            if fi != 0: continue
            elems = fe.find(t('elements'))
            if elems is None: continue
            for elem in list(elems):
                child = elem.get('libraryItemName', '')
                if child == 'IUC01_HindLeg_3-4':
                    xml_str = ET.tostring(elem, encoding='unicode')
                    print(f'\nLayer {i} ("{lname}" {ltype}{par_str}) contains HindLeg_3-4:')
                    print(xml_str[:800])
                    break

# Now check the IUC01_HindLeg_3-4_nocutie symbol to understand the "nocutie" pattern
print('\n' + '='*70)
print('  nocutie variant symbol names')
print('='*70)
nocutie = [n for n in iuc_syms if 'nocutie' in n.lower()]
for n in sorted(nocutie):
    print(f'  {n}')

# Check if there's a mask around cutie mark in the character-level assembly
print('\n' + '='*70)
print('  IUC01_Character full layer list with parentLayerIndex')
print('='*70)
if iuc01 is not None:
    tl = iuc01.find(t('timeline'))
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
            par_str = f' -> parent layer {pi} "{pl.get("name","")}" ({pl.get("layerType","normal")})'
        print(f'  Layer {i}: "{lname}" ({ltype}) parentLayerIndex={par}{par_str}')
