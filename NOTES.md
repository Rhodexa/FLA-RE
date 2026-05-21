# XFL / FLA Reverse Engineering Notes
## for Twist — puppet animation tool

---

## What This Folder Is

Research into the Adobe Animate CC `.fla` (XFL) format for importing MLP:FiM puppet rigs into Twist.
The tool `fla_inspect.py` can parse, inspect, and compose ZIP-based FLA files into SVG.

---

## File Format

### Container
Modern FLAs (CS5+, saved by Animate CC) are ZIP archives.
Old FLAs (pre-CS5) are OLE2 binary — cannot be parsed without Flash/Animate.

Detect: if first two bytes are `PK` it's a ZIP. Otherwise skip it.

The ZIP contains:
- `DOMDocument.xml` — the scene (stage size, fps, root timeline)
- `LIBRARY/*.xml` — one XML file per symbol, named by the symbol's path in the library

XML namespace: `http://ns.adobe.com/xfl/2008/`

---

## Symbol Structure

Each `LIBRARY/*.xml` file is a `<DOMSymbol>` with:
- `name` attribute = the symbol's library path (e.g. `~BB_Head`, `BB_Skull`)
- A `<timeline><DOMTimeline><layers>` hierarchy

Symbols starting with `~` are composites (contain sub-instances of other symbols).
Symbols without `~` are usually leaf geometry (shapes only).

### Layer types
- `normal` — rendered
- `guide` — reference/alignment only, NOT rendered
- `folder` — organizational, skip
- `mask` — NOT rendered directly; its geometry defines a clipping region for layers beneath it

### Layer ordering
XML lists layers **front-to-back** (first = topmost in Flash UI).
Reverse the list to render back-to-front (correct painter's algorithm order).

### Masked layers
Layers masked by a mask layer have a `parentLayerIndex` attribute pointing to the mask layer's XML index (0-based).

---

## Coordinate System

- **Y-axis points DOWN** (same as screen space, same as SVG default)
- Origin `(0, 0)` = symbol's registration point
- Coordinates are in pixels at the document's native resolution (typically 1920×1080 for MLP files)
- Coordinates are **ABSOLUTE** in XFL (not delta) — Animate CC converts from SWF's delta/twips when saving

---

## Edge / Path Encoding

Flash edge strings use a compact command format:
- `!x y` → moveTo (x, y)
- `|x y` → lineTo (x, y)
- `[cx cy ax ay` → quadratic bezier, control (cx,cy) anchor (ax,ay)

Coordinates are either:
- Plain decimal integers/floats: `364.5`, `-281`
- Hex with optional fractional: `#FFA`, `#1AD.3D`, `#FFFF89.DC`

### decode_hex — CRITICAL FIX
Flash uses **24-bit two's complement** for negative values.
The threshold is `0x800000` (not the number of hex digits).

```python
def decode_hex(tok):
    s = tok[1:]
    ih, fh = (s.split('.') + ['0'])[:2]
    v = int(ih, 16)
    if v >= 0x800000:        # 24-bit two's complement
        v -= 0x1000000
    return v + int(fh, 16) / 256.0
```

**Wrong approaches:**
- Using digit count to determine bit width (breaks `#8EF` = 2287, wrongly gives -1809)
- Using 16-bit threshold

---

## Affine Transform (Matrix)

XFL `<Matrix a b c d tx ty>` maps local → parent:
```
x' = a*x + c*y + tx
y' = b*x + d*y + ty
```

This is identical to SVG `matrix(a,b,c,d,tx,ty)`. Use directly.

Flash matrix convention — column-major for a rotation+scale:
```
[ a  c  tx ]
[ b  d  ty ]
[ 0  0   1 ]
```

---

## Fill Model

`fillStyle0` = fill on the **left** side of the edge direction  
`fillStyle1` = fill on the **right** side

Both can be set on the same edge (shared boundary between two fill regions).
FillStyle indices are 1-based; the `index` attribute on `<FillStyle>` matches `fillStyle0`/`fillStyle1`.

To render fills correctly: collect ALL edges for a given fill index into one `d` string, then output a single `<path fill="..." fill-rule="evenodd">`. The evenodd rule handles inner/outer boundaries correctly.

---

## Puppet Rig Architecture (MLP Style)

All geometry is stored in **character-space coordinates** — each leaf symbol's geometry is already positioned at the correct location relative to the character's origin. The matrices on instances are small fine-tuning corrections (tx/ty typically ±50 units or less).

This means:
- `BB_Skull` geometry spans ~(-957, -962) to (1045, 1022) in its LOCAL space
- `BB_Eyewhite_3-4` geometry spans ~(-489, -498) to (429, 537) in its LOCAL space, already centered near the eye position on the skull
- The instance matrix `tx=-1.7, ty=-4.6` is just a tiny correction

**Consequence:** you do NOT need large tx/ty to assemble the character. The geometry self-assembles when matrices are applied — the small matrices are correct.

### Typical head composition (3/4 view, `~BB_Head`):
- Eye white center at ~50% from skull top (correct for MLP's large eyes)
- Ear top at ~3% from skull top (crown of head)
- Mouth at ~61% from skull top (lower face)

---

## SVG Masking

Flash mask layers → SVG `<mask>` elements.

The mask layer geometry should be rendered as **white fills** (SVG masks use luminance: white = visible, black = clipped).

```xml
<defs>
  <mask id="mask_X" maskUnits="userSpaceOnUse" x="-9999" y="-9999" width="99999" height="99999">
    <!-- mask layer geometry, all fills = white -->
    <path d="..." fill="white" fill-rule="evenodd"/>
  </mask>
</defs>
<g mask="url(#mask_X)">
  <!-- layers with parentLayerIndex pointing to X, rendered normally -->
</g>
```

`maskUnits="userSpaceOnUse"` is required — default is `objectBoundingBox` which breaks coordinate interpretation. The large x/y/width/height ensure the mask region isn't accidentally clipped.

---

## fla_inspect.py — Usage

---

## FLA Collection Notes

### `fla/Slice_of_Life/` and `fla/Twilight_Time/`
Mixed bag — most are **OLE2 binary** (pre-CS5), not parseable.

### `fla/Slice_of_Life/new/` ← **USE THIS**
Newer ZIP-based XFL files. These appear to be DHX's **stock character** library —
reusable puppet rigs per character (Bon Bon, Octavia, Lyra, etc.) that were dropped into
scene shots as-needed. The naming convention (e.g. `509_C19_Sweetie_Drop_Bon_Bon.fla`)
is episode + cut number + character.

These are the files `fla_inspect.py` was developed against. Python's `zipfile` can open them.
fflate (Node.js) also opens them fine.

Verified working: `509_C19_Sweetie_Drop_Bon_Bon.fla` (contains full Bon Bon / BB_* rig).

### Trixie FLA — not yet located
Should be findable in the collection. Unicorn rig will have an extra horn layer in the head
symbol — good test case for multi-mask and horn geometry.

---

## Usage

```
python fla_inspect.py <file.fla>                    # list symbols
python fla_inspect.py <file.fla> --roots            # find root (top-level) symbols
python fla_inspect.py <file.fla> --tree NAME        # dependency tree
python fla_inspect.py <file.fla> --inspect NAME     # dump layer/instance data at frame 0
python fla_inspect.py <file.fla> --inspect NAME --frame N   # at frame N
python fla_inspect.py <file.fla> --shape NAME       # raw edge data
python fla_inspect.py <file.fla> --svg NAME [out]   # single symbol → SVG (stroke outline)
python fla_inspect.py <file.fla> --compose NAME [out]       # full hierarchy → SVG (fills + masks)
python fla_inspect.py <file.fla> --compose NAME [out] --frame N
python fla_inspect.py <dir/>                        # scan directory, count parseable FLAs
```

---

## Known Gaps / Next Steps

- **BB_Iris missing at frame 0** — iris geometry lives at frame 1+, need to read `firstFrame` from the instance correctly in leaf symbols
- **`~BB_Head` has 30 layers**, many for lip sync / nostril sync / blink variants — need to handle `loop="single frame"` at `firstFrame=N` correctly for animated symbols
- **gradient fills** rendered as placeholder colors (#88aaff / #ffaa88) — not decoded yet
- **stroke rendering** not implemented in `--compose` mode (only fills)
- **multiple animation frames** — compose currently takes a `--frame N` parameter but the character has 11+ frames of head variation. Need to render all frames for animation.
- **`~BB_Character` hierarchy**: head, body, legs, tail all as sub-symbols with their own animation tracks
- **Trixie FLA** — newly found, should have different puppet structure (unicorn vs earth pony)
