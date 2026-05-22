# Bug: DOMShape matrix ignored — "Drawing Object" shapes render at wrong position

## Symptom
Rarity's tail (and many other symbols) have shape pieces displaced to wrong
positions — some far off-canvas, some rotated/scaled incorrectly.

## Root cause

Flash has two kinds of vector shapes on a layer:

1. **Normal shapes** — geometry whose coordinates are placed directly in the
   parent's coordinate space. No matrix on the shape element.

2. **Drawing Objects** (`isDrawingObject="true"` on `<DOMShape>`) — Flash
   treats these more like objects. They get their own `<matrix>` child that
   offsets (and potentially rotates/scales) the raw edge geometry into its
   intended position.

The parser's `_shape_svg`, `_shape_svg_white`, and `_collect_shape_pts`
**never read the `<matrix>` child of a DOMShape**. They render/collect the
raw edge coordinates as-is, in whatever coordinate space the parent provides,
completely ignoring the shape's own positioning transform.

### Concrete example — RY_TailBit_7

```xml
<DOMShape isDrawingObject="true">
  <matrix>
    <Matrix tx="-485.4" ty="-504.3"/>   <!-- ← parser ignores this -->
  </matrix>
  <edges>
    <!-- raw coords: !9738 10272 = (486.9px, 513.6px) after ÷20 -->
    <Edge fillStyle1="1" edges="!9738 10272[9692 10092 9646 10022..."/>
  </edges>
</DOMShape>
```

**Without matrix:**  shape appears at (486.9, 513.6) — nearly 500px off.
**With matrix:**     486.9 + (−485.4) = **1.5px** — correct, near origin.

## Scale of impact (stk_c05_rarity.fla)

| Metric | Count |
|--------|-------|
| DOMShapes with non-identity matrix | **811** |
| — translate-only | 85 |
| — with rotation/scale too | **726** |
| Affected symbols | **53** |

Top affected symbols:
```
210  RY_Blink_front
107  RY_Blink_3-4rear
 88  RY_Blink_7-8_far
 75  RY_Blink_3-4
 63  RY_up_Mouth_front(hole)
 61  RY_HornMagicSparkles_2
 60  RY_CutieMark
  3  RY_TailBit_3
  2  RY_TailBit_7
```

The tail bug is actually the smallest manifestation. Eyes, mouth, horn magic,
and cutie mark are all broken much more severely.

## Why this is common

Flash's "Drawing Object" mode (introduced in Flash 8) is the default when
you draw on a layer that already has content. In animation rigs, animators
often draw directly on layers, producing Drawing Objects. The matrix on the
shape acts like a "pivot" — it lets Flash move the shape without rewriting
every vertex coordinate.

## Fix

In `_shape_svg` and `_shape_svg_white`, after generating the path lines,
check if the shape has a matrix. If non-identity, wrap the paths in a
`<g transform="matrix(a,b,c,d,tx,ty)">` group.

In `_collect_shape_pts`, apply the shape matrix to each collected point
via `_apply_mat`.

See the fix in `fla_inspect.py`.
