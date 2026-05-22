# FLA Inspector — Refactor Plan
Ordered from **safest / highest impact** → **most fragile / lower priority**

---

## 1. `safe_name(s)` helper
**Risk: none**

Three identical inline sanitization strings. Extract to one function, do a find-replace. Zero logic change, zero output change.

```python
def safe_name(s: str) -> str:
    return s.replace('/', '_').replace('~', '').replace('(', '').replace(')', '')
```

---

## 2. `read_matrix(elem)` helper
**Risk: negligible**

The six-field matrix extraction (`a, b, c, d, tx, ty`) is copy-pasted ~6 times with identical logic. Extract to a helper returning a named tuple or plain tuple. No computation changes, just consolidation. Easiest win in the whole file.

```python
def read_matrix(elem) -> tuple:
    m = elem.find(t('Matrix')) if elem is not None else None
    if m is None:
        return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    return (float(m.get('a',1)), float(m.get('b',0)),
            float(m.get('c',0)), float(m.get('d',1)),
            float(m.get('tx',0)), float(m.get('ty',0)))
```

---

## 3. CLI → `argparse`
**Risk: low**

Replace the manual `args.index('--flag') + 1` indexing in `main()` with `argparse`. Fixes the missing-argument crash paths as a side effect. No rendering logic touched at all — pure CLI surface change. Move `print_header`, `print_shapes`, `print_tree` alongside `main()` or into a `cli.py`.

---

## 4. Resolve the unit system and remove `_PREPOS_Y_THRESHOLD` / `_sym_local_y_min` / `_prepos_cache`
**Risk: low-medium** ⚠️

This is the most impactful correctness fix and directly addresses the nested symbol translation bug. But before touching any code, settle the unit question first:

**The unit question:** the ×20 factor exists because Flash geometry is in twips (1/20th px) but `tx`/`ty` in some contexts are stored in pixels. There are two equivalent fixes but they have different correctness properties under matrix composition:

- **Current approach** — multiply `tx`/`ty` by 20 to lift translations into twip space. Works for flat transforms but breaks under `_compose_mat` because the parent's linear components (`a/b/c/d`) are twip-scale while the child's translation was pixel-scale before the patch, causing incorrect accumulation at each nesting level.
- **Better approach** — divide all geometry coordinates coming out of `parse_edge_str` by 20, normalising everything to pixels from the start. The matrix is then used as-is with no scaling anywhere. Composition stays clean at every level because there's no unit mismatch to propagate.

**Recommended: try the divide-geometry branch first.** Strip all `tx_scale` logic, divide coords in `decode_hex` or at the `parse_edge_str` output, and test flat vs. nested symbols. If flat symbols look identical and nested ones improve, that's the confirmation — and the fix is actually simpler than the current approach.

Either way, `_prepos_cache`, `_sym_local_y_min`, `_PREPOS_Y_THRESHOLD`, and the `tx_scale` parameter disappear entirely. Test on symbols with 1, 2, and 3 levels of nesting before/after.

---

## 5. `iter_fill_cmds(shape)` generator
**Risk: low-medium** ⚠️

The "iterate edges, skip stroke-only, parse edge string, yield commands" pattern appears in `_collect_shape_pts`, `_shape_is_stray`, `symbol_to_svg`, and `_bbox_sym`. Extract to a single generator. The only risk is accidentally changing which edges are included/excluded (the `fillStyle0`/`fillStyle1` guard). Verify the filter condition is identical across all four sites before collapsing — they're close but worth checking line by line.

---

## 6. `_active_frame` — fix missing `duration` on last frame
**Risk: low-medium** ⚠️

Flash omits `duration` on the final keyframe of a layer. Current code defaults to `duration=1`, silently returning `None` for any frame index beyond that keyframe's start. Fix: if no frame matches, return the last frame whose `index <= frame_num`. Low risk for frame-0 exports (your current use), higher risk once you start exporting animations.

---

## 7. Merge `_render_sym` + `_render_sym_white`
**Risk: medium** ⚠️⚠️

The two functions share identical traversal structure but differ in color handling and mask-layer skipping. Merge with a `white: bool` parameter propagated through. The mask-layer guard (`'mask'` skipped in white mode) is the tricky part — get that condition wrong and masks either vanish or render as solid blocks. Validate with a symbol that actually uses Flash mask layers.

---

## 8. `walk_elements` visitor abstraction
**Risk: medium** ⚠️⚠️

Generalise the shape/group/symbol-instance dispatch that appears in `_render_sym`, `_render_sym_white`, `_collect_group_pts`, `_iter_group_shapes_white`, and `_bbox_sym` into a single `walk_elements(layer, frame, visitor)` function. The main risk is the `visited` set semantics — some call sites copy it (`visited = visited | {name}`) and some mutate it. Normalise to copy-on-descent before abstracting, or the walk will incorrectly prune branches in some traversal paths. This is worth doing but needs careful testing of the visited-set behaviour across all consumers.

---

## 9. Fix `_segs_to_svg_d` ambiguous candidate selection
**Risk: medium-high** ⚠️⚠️⚠️

The chain-stitcher picks `cands[0]` when multiple segments share a start point. For shapes with shared vertices (adjacent filled regions) this is order-dependent and can produce wrong contour closures. Fix requires a deterministic tie-breaking rule — e.g. sort candidates by angle relative to the incoming direction. This touches the core path output and needs broad visual regression testing across complex symbols.

---

## 10. Fix `_reverse_seg` quadratic control point
**Risk: high** ⚠️⚠️⚠️

The reversed quadratic bezier keeps the original control point, which is geometrically wrong. The correct reversed curve requires reflecting the control point. This will visibly change output for any edge where `fillStyle1` is active and the curve is non-trivial. Fix it only after you have a visual regression baseline — wrong-but-consistent is safer than wrong-in-a-new-way while other things are still being fixed.

---

## 11. Module split
**Risk: high (organisational)** ⚠️⚠️⚠️

Split into `fla_loader.py`, `xfl_model.py`, `svg_writer.py`, `cli.py` only **after** all the above are done and tested. Doing this earlier makes every other fix harder to diff and review. The split itself is mechanical once the internal boundaries are clean, but it's easy to accidentally break an import or circular-ref if the helpers aren't fully consolidated first (hence why items 1–2 come first).

---

## Summary table

| # | Item | Output risk | Impact |
|---|------|-------------|--------|
| 1 | `safe_name` helper | None | Low |
| 2 | `read_matrix` helper | None | Medium |
| 3 | CLI → argparse | None | Low |
| 4 | Resolve unit system (divide geometry vs multiply tx) + remove heuristic | Low-medium | **High** |
| 5 | `iter_fill_cmds` generator | Low-medium | Medium |
| 6 | `_active_frame` last-frame fix | Low-medium | Medium |
| 7 | Merge render/render_white | Medium | Medium |
| 8 | `walk_elements` abstraction | Medium | Medium |
| 9 | Segment chain tie-breaking | Medium-high | Medium |
| 10 | `_reverse_seg` bezier fix | High | Medium |
| 11 | Module split | High (org.) | High |
