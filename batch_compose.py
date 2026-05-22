"""Batch-compose all parseable FLAs in fla/new/ to output/."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fla_inspect import open_fla, get_symbols, is_zip, compose_to_svg

fla_dir = Path('fla/new')
out_dir  = Path('output')
out_dir.mkdir(exist_ok=True)

def pick_character_symbols(symbols):
    """Return the best candidate(s) to compose for this FLA.

    Priority order:
      1. Any symbol whose name starts with '~' and contains 'Character'
      2. Any symbol whose name starts with '~' (master comps without 'Character')
      3. Give up and return []
    Excludes STOCK/* junk and Design/* folder cruft.
    """
    def is_junk(name):
        return any(name.startswith(p) for p in ('STOCK', 'Design/', '--', 'z_'))

    tier1 = [n for n in symbols if n.startswith('~') and 'Character' in n and not is_junk(n)]
    if tier1:
        return sorted(tier1)

    tier2 = [n for n in symbols if n.startswith('~') and not is_junk(n)]
    if tier2:
        return sorted(tier2)

    return []

ok = 0; skipped = 0; errors = 0

for fla_path in sorted(fla_dir.glob('*.fla')):
    if not is_zip(fla_path):
        print(f'[SKIP binary ] {fla_path.name}')
        skipped += 1
        continue

    try:
        zip_data = open_fla(fla_path)
        symbols  = get_symbols(zip_data)
        targets  = pick_character_symbols(symbols)

        if not targets:
            print(f'[NO CHAR ROOT] {fla_path.name}')
            skipped += 1
            continue

        stem = fla_path.stem.replace(' ', '_')
        for root in targets:
            safe_root = root.lstrip('~').replace('/', '_').replace(' ', '_')
            out = out_dir / f'{stem}__{safe_root}.svg'
            compose_to_svg(root, symbols, frame=0, out_path=str(out), masking=True)
        ok += 1

    except Exception as e:
        import traceback
        print(f'[ERROR] {fla_path.name}: {e}')
        traceback.print_exc()
        errors += 1

print(f'\nDone — {ok} rendered, {skipped} skipped, {errors} errors')
