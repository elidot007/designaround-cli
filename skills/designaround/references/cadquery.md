# CAD program contract

`cad run` executes programs in a separate process after a static check. A program that breaks these rules is rejected with exit code 5 (`program_rejected`).

## Rules

- Imports: only `cadquery` (as `cq`) and `math`.
- No `open`, `eval`, `exec`, `getattr`, `__import__`, dunder attributes, file import/export methods.
- `PARAMS` is a **literal** dict at module level. Every entry has a `default`; numbers should have `min` and `max`; choices use `choices`; integers use `"type": "int"`. Units are millimetres and degrees.
- Optional `TEMPLATE` literal dict with `features` (feature group → parameter names) and `locked_by_default`. The originality gate and the designer use these groups.
- Optional `UNCLAIMED = ["handle"]`: part names drawn in broken lines.
- `build(p)` returns a dict `{part_name: Workplane | Shape}`. Use meaningful part names (`body`, `lid`, `handle`). A single shape is accepted and named `body`.
- Axes: X runs left to right as seen in the front view, Y runs front to back, Z is up. The front view is seen from −Y. Stand the product on Z = 0.
- Builds time out (exit 5, `build_failed`); keep programs simple and avoid long loops of boolean operations.

## Example

```python
import math

import cadquery as cq

TEMPLATE = {
    "id": "tumbler_custom",
    "features": {"body_outline": ["height", "top_d", "bottom_d"], "handle": ["handle_style"]},
    "locked_by_default": ["wall"],
}

PARAMS = {
    "height": {"default": 180.0, "min": 80.0, "max": 300.0, "unit": "mm"},
    "top_d": {"default": 90.0, "min": 50.0, "max": 140.0, "unit": "mm"},
    "bottom_d": {"default": 70.0, "min": 40.0, "max": 120.0, "unit": "mm"},
    "wall": {"default": 2.0, "min": 1.2, "max": 4.0, "unit": "mm"},
    "handle_style": {"default": "loop", "choices": ["loop", "none"]},
}


def _frustum(z0, h, d0, d1):
    return cq.Workplane("XY").workplane(offset=z0).circle(d0 / 2).workplane(offset=h).circle(d1 / 2).loft()


def build(p):
    w = p["wall"]
    outer = _frustum(0, p["height"], p["bottom_d"], p["top_d"])
    inner = _frustum(w, p["height"], p["bottom_d"] - 2 * w, p["top_d"] - 2 * w)
    body = outer.cut(inner)
    parts = {"body": body}
    if p["handle_style"] == "loop":
        r = (p["top_d"] + p["bottom_d"]) / 4
        handle = cq.Workplane("XZ").center(r + 18, p["height"] / 2).rect(36, p["height"] * 0.55).extrude(6, both=True)
        hole = cq.Workplane("XZ").center(r + 18, p["height"] / 2).rect(20, p["height"] * 0.4).extrude(10, both=True)
        parts["handle"] = handle.cut(hole)
    return parts
```

## Loop

```bash
designaround --json cad run <case> model.py --try            # builds, renders; open data.contact_sheet
designaround --json cad run <case> model.py --kind original  # saves v1
designaround --json cad run <case> around1.py --parent v1 --kind candidate --rationale "loft profile instead of straight taper"
designaround --json cad tweak <case> v1 --set handle_style=none --rationale "remove the handle the prior art claims"
```

Parametric tweaks (`cad tweak`) are cheaper than new programs and keep the change record precise. Write a new program when the change is structural (a new silhouette, a different handle construction).

Look at the contact sheet after every build. Check proportions against the photos and the known dimension.
