
import cadquery as cq

TEMPLATE = {
    "id": "tumbler",
    "title": "Tumbler / mug with handle",
    "title_zh": "带把手杯",
    "article_en": "tumbler",
    "article_zh": "杯子",
    "category": "kitchenware",
    "locarno": "07-01",
    "features": {
        "handle": ["handle_style", "handle_size", "handle_thickness"],
        "body_profile": ["height", "top_diameter", "bottom_diameter"],
        "grooves": ["groove_count", "groove_depth"],
    },
    "locked_by_default": ["wall"],
}

PARAMS = {
    "height": {"default": 150.0, "min": 80.0, "max": 260.0, "unit": "mm"},
    "top_diameter": {"default": 90.0, "min": 55.0, "max": 130.0, "unit": "mm"},
    "bottom_diameter": {"default": 70.0, "min": 45.0, "max": 120.0, "unit": "mm"},
    "wall": {"default": 2.5, "min": 1.6, "max": 5.0, "unit": "mm"},
    "base_thickness": {"default": 4.0, "min": 2.0, "max": 10.0, "unit": "mm"},
    "handle_style": {"default": "loop", "choices": ["loop", "bar", "none"]},
    "handle_size": {"default": 55.0, "min": 34.0, "max": 90.0, "unit": "mm"},
    "handle_thickness": {"default": 10.0, "min": 6.0, "max": 16.0, "unit": "mm"},
    "groove_count": {"default": 0, "min": 0, "max": 6, "type": "int"},
    "groove_depth": {"default": 1.0, "min": 0.4, "max": 2.5, "unit": "mm"},
}

UNCLAIMED = []


def _radius_at(p, z):
    rb, rt = p["bottom_diameter"] / 2, p["top_diameter"] / 2
    return rb + (rt - rb) * z / p["height"]


def build(p):
    H, wall, base = p["height"], p["wall"], p["base_thickness"]
    rb, rt = p["bottom_diameter"] / 2, p["top_diameter"] / 2
    outer = [(0, 0), (rb, 0), (rt, H), (rt - wall, H), (rb - wall, base), (0, base)]
    cup = cq.Workplane("XZ").polyline(outer).close().revolve(360, (0, 0, 0), (0, 1, 0))

    n = int(p["groove_count"])
    depth = min(p["groove_depth"], wall - 0.6)
    if n > 0 and depth > 0.2:
        for i in range(n):
            z = H * (0.3 + (0.45 * i / (n - 1) if n > 1 else 0.2))
            r = _radius_at(p, z)
            ring = (
                cq.Workplane("XZ")
                .polyline([(r - depth, z - 1.2), (r + 2, z - 1.2), (r + 2, z + 1.2), (r - depth, z + 1.2)])
                .close()
                .revolve(360, (0, 0, 0), (0, 1, 0))
            )
            cup = cup.cut(ring)

    parts = {"cup": cup}
    style = p["handle_style"]
    if style != "none":
        size, thick = p["handle_size"], p["handle_thickness"]
        z_mid = H * 0.52
        r_mid = _radius_at(p, z_mid)
        cavity = (
            cq.Workplane("XZ")
            .polyline([(0, base), (rb - wall, base), (rt - wall, H + 1), (0, H + 1)])
            .close()
            .revolve(360, (0, 0, 0), (0, 1, 0))
        )
        if style == "loop":
            torus = cq.Solid.makeTorus(size / 2, thick / 2)
            handle = (
                cq.Workplane("XY")
                .add(torus)
                .rotate((0, 0, 0), (1, 0, 0), 90)
                .translate((r_mid + size / 2 - thick * 0.6, 0, z_mid))
            )
        else:  # bar
            reach = size * 0.55
            arm_top = cq.Workplane("XY").box(reach + thick, thick, thick).translate((r_mid + reach / 2, 0, z_mid + size / 2))
            arm_bot = cq.Workplane("XY").box(reach + thick, thick, thick).translate((r_mid + reach / 2, 0, z_mid - size / 2))
            grip = (
                cq.Workplane("XY")
                .circle(thick / 2)
                .extrude(size + thick)
                .translate((r_mid + reach, 0, z_mid - size / 2 - thick / 2))
            )
            handle = arm_top.union(arm_bot).union(grip)
        handle = handle.cut(cavity)
        parts["handle"] = handle
    return parts
