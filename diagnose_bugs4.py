"""Check for non-DrawingObject shapes with non-identity matrices (the double-transform bug).
Also check Celestia tail symbols specifically."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from fla_inspect import open_fla, get_symbols, t, read_matrix, _IDENTITY_MAT

def count_bad_shapes(syms):
    """Count DOMShape elements that have non-identity matrices but are NOT Drawing Objects."""
    bad = []
    for sym_name, sym in syms.items():
        for shape in sym.iter(t('DOMShape')):
            if shape.get('isDrawingObject') == 'true':
                continue  # These are correct to have matrices applied
            mc = shape.find(t('matrix'))
            if mc is None:
                continue
            m = read_matrix(mc)
            if m != _IDENTITY_MAT:
                bad.append((sym_name, m))
    return bad

print('='*70)
print('  Non-DrawingObject shapes with non-identity matrices (double-transform bug)')
print('='*70)

# IUC
iuc = open_fla('fla/new/stk_C20_Incidental_Unicorns.fla')
iuc_syms = get_symbols(iuc)
iuc_bad = count_bad_shapes(iuc_syms)
print(f'\nIUC Unicorns FLA: {len(iuc_bad)} affected shapes')
# Print counts per symbol
from collections import Counter
iuc_counts = Counter(s for s,m in iuc_bad)
for name, count in iuc_counts.most_common(20):
    print(f'  {count:4d}  {name}')

# Celestia
cel = open_fla('fla/new/stk_c08_princess_celestia.fla')
cel_syms = get_symbols(cel)
cel_bad = count_bad_shapes(cel_syms)
print(f'\nCelestia FLA: {len(cel_bad)} affected shapes')
cel_counts = Counter(s for s,m in cel_bad)
for name, count in cel_counts.most_common(20):
    print(f'  {count:4d}  {name}')

# Check specifically in PC_tailcycle_front
print('\n--- PC_tailcycle_front non-DrawingObject shapes with matrices ---')
tcf = cel_syms.get('PC_tailcycle_front')
if tcf:
    for shape in tcf.iter(t('DOMShape')):
        if shape.get('isDrawingObject') == 'true':
            continue
        mc = shape.find(t('matrix'))
        if mc is None: continue
        m = read_matrix(mc)
        if m != _IDENTITY_MAT:
            a,b,c,d,tx,ty = m
            n_edges = sum(1 for _ in shape.iter(t('Edge')))
            print(f'  DOMShape: {n_edges} edges  matrix(a={a:.4f},b={b:.4f},c={c:.4f},d={d:.4f},tx={tx:.2f},ty={ty:.2f})')

# And PC_tailCycleMask_Green1_front
print('\n--- PC_tailCycleMask_Green1_front non-DrawingObject shapes with matrices ---')
tmg = cel_syms.get('PC_tailCycleMask_Green1_front')
if tmg:
    for shape in tmg.iter(t('DOMShape')):
        if shape.get('isDrawingObject') == 'true':
            continue
        mc = shape.find(t('matrix'))
        if mc is None: continue
        m = read_matrix(mc)
        if m != _IDENTITY_MAT:
            a,b,c,d,tx,ty = m
            n_edges = sum(1 for _ in shape.iter(t('Edge')))
            print(f'  DOMShape: {n_edges} edges  matrix(a={a:.4f},b={b:.4f},c={c:.4f},d={d:.4f},tx={tx:.2f},ty={ty:.2f})')

# Rarity for comparison (should have mostly isDrawingObject=True ones)
rar = open_fla('fla/new/stk_c05_rarity.fla')
rar_syms = get_symbols(rar)
rar_bad = count_bad_shapes(rar_syms)
print(f'\nRarity FLA: {len(rar_bad)} affected non-DrawingObject shapes with matrices')
rar_counts = Counter(s for s,m in rar_bad)
for name, count in rar_counts.most_common(10):
    print(f'  {count:4d}  {name}')

# Count actual Drawing Objects (isDrawingObject=true) for reference
print('\n--- Drawing Object counts (isDrawingObject=true) ---')
for label, syms_dict in [('IUC', iuc_syms), ('Celestia', cel_syms), ('Rarity', rar_syms)]:
    do_count = sum(1 for sym in syms_dict.values()
                   for shape in sym.iter(t('DOMShape'))
                   if shape.get('isDrawingObject') == 'true')
    non_do_mat = sum(1 for sym in syms_dict.values()
                     for shape in sym.iter(t('DOMShape'))
                     if shape.get('isDrawingObject') != 'true'
                     and shape.find(t('matrix')) is not None
                     and read_matrix(shape.find(t('matrix'))) != _IDENTITY_MAT)
    print(f'  {label}: {do_count} DrawingObjects, {non_do_mat} non-DrawingObject shapes with non-identity matrices')
