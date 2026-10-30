"""Photo path A: quality checks for product photos, photo silhouettes, and the
reconstruction quality gate (FR-3D-07, FR-3D-13).
"""

from __future__ import annotations

import os

import numpy as np
from PIL import Image, ImageOps
from scipy import ndimage

from .compare import mask_from_photo, normalise, similarity
from .views import view_masks

REQUIRED_VIEWS = ["front", "rear", "left", "right", "top", "bottom", "perspective"]
PHOTO_RULES = {"min_side_px": 800, "min_sharpness": 60.0, "max_border_std": 22.0, "min_fill": 0.08, "max_fill": 0.92}


def _gray(path) -> np.ndarray:
    image = ImageOps.exif_transpose(Image.open(os.fspath(path)))
    return np.asarray(image.convert("L")).astype(np.float64)


def check_photo(path) -> dict:
    gray = _gray(path)
    h, w = gray.shape
    lap = ndimage.laplace(gray)
    sharpness = float(lap.var())
    border = np.concatenate([gray[:8].ravel(), gray[-8:].ravel(), gray[:, :8].ravel(), gray[:, -8:].ravel()])
    border_std = float(border.std())
    mask = mask_from_photo(gray.astype(np.uint8))
    fill = float(mask.mean())
    touches = bool(mask[0].any() or mask[-1].any() or mask[:, 0].any() or mask[:, -1].any())
    issues = []
    if min(h, w) < PHOTO_RULES["min_side_px"]:
        issues.append(f"Resolution too low ({w}x{h}); shoot at least {PHOTO_RULES['min_side_px']} px on the short side.")
    if sharpness < PHOTO_RULES["min_sharpness"]:
        issues.append("Image looks blurry; use a tripod or more light.")
    if border_std > PHOTO_RULES["max_border_std"]:
        issues.append("Background is not plain; shoot on a uniform white or grey background.")
    if fill < PHOTO_RULES["min_fill"]:
        issues.append("Product is too small in the frame; move closer.")
    if fill > PHOTO_RULES["max_fill"] or touches:
        issues.append("Product is cropped or touches the frame edge; leave a margin.")
    return {
        "path": os.fspath(path),
        "pass": not issues,
        "issues": issues,
        "size_px": [w, h],
        "sharpness": round(sharpness, 1),
        "border_std": round(border_std, 2),
        "fill": round(fill, 3),
    }


def check_photo_set(photos: dict[str, str]) -> dict:
    results = {view: check_photo(path) for view, path in photos.items()}
    missing = [v for v in REQUIRED_VIEWS if v not in photos]
    omittable = {"bottom", "rear"}
    hard_missing = [v for v in missing if v not in omittable]
    return {
        "pass": all(r["pass"] for r in results.values()) and not hard_missing,
        "photos": results,
        "missing_views": missing,
        "note": "Bottom/rear views may be omitted only with a statement (CN: 简要说明; US: practitioner decision)." if missing else None,
    }


def reconstruction_gate(version_dir, photos: dict[str, str], *, min_score: float = 0.72) -> dict:
    """Compare our model's silhouette with labelled photos (orthographic approximation)."""

    keys = [k for k in photos if k in REQUIRED_VIEWS]
    ours = view_masks(version_dir, keys=keys)
    rows = {}
    for key in keys:
        their = normalise(mask_from_photo(_gray(photos[key]).astype(np.uint8)))
        if their is None or ours.get(key) is None:
            rows[key] = {"score": 0.0, "note": "no silhouette"}
            continue
        rows[key] = similarity(ours[key], their)
    scores = [r["score"] for r in rows.values()]
    worst = min(scores) if scores else 0.0
    return {
        "pass": bool(scores) and worst >= min_score,
        "min_score": min_score,
        "worst": round(worst, 4),
        "per_view": rows,
        "advice": None if (scores and worst >= min_score) else "Model does not match the photos well enough; fix proportions in the weakest view before design-around.",
    }
