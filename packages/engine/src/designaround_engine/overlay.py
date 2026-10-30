"""Two designs drawn at the same scale in every standard view (FR-CASE-02, FR-EVAL-06).

Unlike the per-version silhouettes (each scaled to fill its own frame, which is right
for shape ranking), these overlays keep millimetres comparable, so a design that
grew, shrank or lost a feature shows it. Red = A only, blue = B only, purple = both.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .camera import STANDARD_VIEWS, project
from .geometry import combined_mesh, load_version_parts

A_ONLY = (226, 84, 84)
B_ONLY = (70, 110, 230)
BOTH = (150, 110, 190)


def _mask(P: np.ndarray, F: np.ndarray, scale: float, ox: float, oy: float, size: tuple[int, int]) -> np.ndarray:
    image = Image.new("L", size, 0)
    draw = ImageDraw.Draw(image)
    sx = ox + P[:, 0] * scale
    sy = oy - P[:, 1] * scale
    for tri in F:
        draw.polygon([(sx[i], sy[i]) for i in tri], fill=255)
    return np.asarray(image) > 127


def overlay_sheet(dir_a: str | os.PathLike, dir_b: str | os.PathLike, out: str | os.PathLike, *, labels: tuple[str, str] = ("A", "B"), cell: int = 360, margin: int = 18) -> dict:
    Va, Fa, _ = combined_mesh(load_version_parts(dir_a))
    Vb, Fb, _ = combined_mesh(load_version_parts(dir_b))
    order = ["perspective", "front", "rear", "left", "right", "top", "bottom"]
    cols, rows = 4, 2
    head = 26
    sheet = Image.new("RGB", (cols * cell, rows * (cell + head) + 34), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    per_view = {}
    for i, key in enumerate(order):
        v = STANDARD_VIEWS[key]
        Pa, Pb = project(Va, v), project(Vb, v)
        both = np.vstack([Pa[:, :2], Pb[:, :2]])
        x0, y0 = both.min(axis=0)
        x1, y1 = both.max(axis=0)
        w, h = max(x1 - x0, 1e-9), max(y1 - y0, 1e-9)
        scale = min((cell - 2 * margin) / w, (cell - 2 * margin) / h)
        ox = (cell - w * scale) / 2 - x0 * scale
        oy = (cell - h * scale) / 2 + y1 * scale
        ma = _mask(Pa, Fa, scale, ox, oy, (cell, cell))
        mb = _mask(Pb, Fb, scale, ox, oy, (cell, cell))
        rgb = np.full((cell, cell, 3), 255, dtype=np.uint8)
        rgb[ma & ~mb] = A_ONLY
        rgb[mb & ~ma] = B_ONLY
        rgb[ma & mb] = BOTH
        union = int((ma | mb).sum())
        iou = float((ma & mb).sum() / union) if union else 1.0
        area_a, area_b = int(ma.sum()), int(mb.sum())
        per_view[key] = {"iou": round(iou, 4), "area_change": round((area_b - area_a) / area_a, 4) if area_a else None}
        x, y = (i % cols) * cell, (i // cols) * (cell + head)
        sheet.paste(Image.fromarray(rgb), (x, y + head))
        draw.text((x + 6, y + 6), f"{key}  overlap {iou:.2f}", fill=(0, 0, 0), font=font)
    legend_y = rows * (cell + head) + 10
    for j, (color, text) in enumerate(((A_ONLY, f"{labels[0]} only"), (B_ONLY, f"{labels[1]} only"), (BOTH, "both"))):
        x = 10 + j * 200
        draw.rectangle([x, legend_y, x + 16, legend_y + 14], fill=color)
        draw.text((x + 22, legend_y + 2), text, fill=(0, 0, 0), font=font)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    return {"path": str(out), "per_view": per_view, "min_overlap": min(r["iou"] for r in per_view.values())}
