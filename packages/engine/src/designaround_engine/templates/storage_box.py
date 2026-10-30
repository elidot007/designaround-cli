import math

import cadquery as cq

TEMPLATE = {
    "id": "storage_box",
    "title": "Storage box with lid",
    "title_zh": "带盖收纳箱",
    "article_en": "storage box",
    "article_zh": "收纳箱",
    "category": "home_storage",
    "locarno": "06-04",
    "features": {
        "handle": ["handle_style", "handle_length", "handle_height"],
        "body_outline": ["corner_radius", "taper_deg", "length", "width", "height"],
        "lid": ["lid_height", "lid_overhang", "lid_dome"],
        "side_ribs": ["rib_count", "rib_depth"],
    },
    "locked_by_default": ["wall"],
}

PARAMS = {
    "length": {"default": 320.0, "min": 160.0, "max": 600.0, "unit": "mm", "desc": "Outer length at the base (X)"},
    "width": {"default": 220.0, "min": 110.0, "max": 450.0, "unit": "mm", "desc": "Outer width at the base (Y)"},
    "height": {"default": 180.0, "min": 70.0, "max": 400.0, "unit": "mm", "desc": "Body height without lid (Z)"},
    "wall": {"default": 2.4, "min": 1.4, "max": 4.0, "unit": "mm", "desc": "Wall thickness"},
    "corner_radius": {"default": 18.0, "min": 2.0, "max": 50.0, "unit": "mm", "desc": "Plan-view corner radius"},
    "taper_deg": {"default": 3.0, "min": 0.5, "max": 7.0, "unit": "deg", "desc": "Side wall flare (also mould draft)"},
    "lid_height": {"default": 22.0, "min": 12.0, "max": 50.0, "unit": "mm"},
    "lid_overhang": {"default": 4.0, "min": 0.0, "max": 12.0, "unit": "mm"},
    "lid_dome": {"default": 0.0, "min": 0.0, "max": 25.0, "unit": "mm", "desc": "Rounding of the lid top edge"},
    "handle_style": {"default": "arched", "choices": ["arched", "bar", "recessed", "none"]},
    "handle_length": {"default": 120.0, "min": 60.0, "max": 260.0, "unit": "mm"},
    "handle_height": {"default": 28.0, "min": 14.0, "max": 50.0, "unit": "mm"},
    "rib_count": {"default": 0, "min": 0, "max": 6, "type": "int", "desc": "Vertical ribs on front and back"},
    "rib_depth": {"default": 2.0, "min": 1.0, "max": 5.0, "unit": "mm"},
}

UNCLAIMED = []


def _rounded(wp, length, width, radius):
    radius = max(0.0, min(radius, length / 2 - 1.0, width / 2 - 1.0))
    sketch = wp.sketch().rect(length, width)
    if radius > 0.3:
        sketch = sketch.vertices().fillet(radius)
    return sketch.finalize()


def _body(p):
    L, W, H, wall, r, t = p["length"], p["width"], p["height"], p["wall"], p["corner_radius"], p["taper_deg"]
    grow = math.tan(math.radians(t)) * H
    body = _rounded(cq.Workplane("XY"), L, W, r).extrude(H, taper=-t)
    body = body.faces(">Z").shell(-wall)
    top_L, top_W, top_r = L + 2 * grow, W + 2 * grow, r + grow
    rim_h = 8.0
    rim_outer = _rounded(cq.Workplane("XY").workplane(offset=H - rim_h), top_L + 6, top_W + 6, top_r + 3).extrude(rim_h)
    rim_inner = _rounded(
        cq.Workplane("XY").workplane(offset=H - rim_h - 1), top_L - wall, top_W - wall, max(top_r - wall / 2, 0.5)
    ).extrude(rim_h + 2)
    body = body.union(rim_outer.cut(rim_inner))

    n = int(p["rib_count"])
    spread = min(0.35 * L, L / 2 - r - 8)
    if n > 0 and spread > 0:
        depth = p["rib_depth"]
        rib_h = 0.55 * H
        z_mid = 0.45 * H
        y_face = W / 2 + math.tan(math.radians(t)) * z_mid
        xs = [0.0] if n == 1 else [-spread + i * 2 * spread / (n - 1) for i in range(n)]
        for x in xs:
            for side in (-1, 1):
                y = side * (y_face + depth / 2 - 0.6)
                rib = cq.Workplane("XY").box(8, depth, rib_h).translate((x, y, z_mid))
                rib = rib.rotate((x, y, z_mid), (x + 1, y, z_mid), -side * t)
                body = body.union(rib)
    return body, top_L, top_W, top_r


def _lid(p, top_L, top_W, top_r):
    H, wall = p["height"], p["wall"]
    over = p["lid_overhang"]
    lid_L, lid_W, lid_r = top_L + 6 + 2 * over, top_W + 6 + 2 * over, top_r + 3 + over
    lid_h = p["lid_height"]
    z0 = H + 0.5
    lid = _rounded(cq.Workplane("XY").workplane(offset=z0), lid_L, lid_W, lid_r).extrude(lid_h)
    dome = min(p["lid_dome"], lid_h - wall - 2, lid_r - 1.0)
    if dome > 0.8:
        try:
            lid = lid.faces(">Z").edges().fillet(dome)
        except Exception:
            pass
    if p["handle_style"] == "recessed":
        depth = min(lid_h - wall - 2, 16.0)
        pocket = (
            cq.Workplane("XY")
            .workplane(offset=z0 + lid_h - depth)
            .slot2D(min(p["handle_length"], lid_L * 0.7), 34)
            .extrude(depth + 1)
        )
        lid = lid.cut(pocket)
    try:
        lid = lid.faces("<Z").shell(-wall)
    except Exception:
        pass
    return lid, z0 + lid_h, lid_L, lid_W


def _handle(p, lid_top, lid_L):
    style = p["handle_style"]
    if style in ("recessed", "none"):
        return None
    length = min(p["handle_length"], lid_L * 0.8)
    height = p["handle_height"]
    if style == "arched":
        thickness, leg, clearance = 16.0, 18.0, height - 8.0
    else:  # bar
        thickness, leg, clearance = 11.0, 10.0, height - 6.0
    handle = cq.Workplane("XY").box(length, thickness, height).translate((0, 0, lid_top + height / 2 - 1.0))
    window = cq.Workplane("XY").box(length - 2 * leg, thickness + 4, clearance).translate(
        (0, 0, lid_top - 1.0 + clearance / 2 - 0.01)
    )
    handle = handle.cut(window)
    try:
        handle = handle.edges("|Y").fillet(min(3.0, leg / 3))
    except Exception:
        pass
    return handle


def build(p):
    body, top_L, top_W, top_r = _body(p)
    lid, lid_top, lid_L, _lid_W = _lid(p, top_L, top_W, top_r)
    parts = {"body": body, "lid": lid}
    handle = _handle(p, lid_top, lid_L)
    if handle is not None:
        parts["handle"] = handle
    return parts
