"""Batch-compose all parseable FLAs in fla/new/ to output/."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fla_inspect import open_fla, parse_dom, get_symbols, find_roots, is_zip, compose_to_svg

fla_dir = Path('fla/new')
out_dir  = Path('output')
out_dir.mkdir(exist_ok=True)

for fla_path in sorted(fla_dir.glob('*.fla')):
    if not is_zip(fla_path):
        print(f'[SKIP binary] {fla_path.name}')
        continue
    try:
        zip_data = open_fla(fla_path)
        symbols  = get_symbols(zip_data)
        roots    = find_roots(symbols)
        # Find the main character root (starts with ~)
        char_roots = [r for r in roots if r.startswith('~') and 'Character' in r]
        if not char_roots:
            char_roots = [r for r in roots if r.startswith('~')]
        if not char_roots:
            print(f'[NO ROOT] {fla_path.name}')
            continue
        root = char_roots[0]
        safe = fla_path.stem.replace(' ', '_')
        out  = out_dir / f'{safe}_composed.svg'
        compose_to_svg(root, symbols, frame=0, out_path=str(out))
    except Exception as e:
        print(f'[ERROR] {fla_path.name}: {e}')
