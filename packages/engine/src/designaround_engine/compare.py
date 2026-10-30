"""CPU-only shape comparison between our views and prior-art images.

No vector index and no GPU. Prior-art candidates come from text/structured
search (product name, Locarno class, status). We then:

1. turn every image into a filled silhouette mask (line drawing, photo or render),
2. normalise masks (crop, keep aspect, pad, resize),
3. score IoU, boundary chamfer distance, aspect ratio and a radial signature,
4. take the best (our view x their figure) pair as the candidate's score.

Only the top candidates are sent to the vision-language observer.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageOps
from scipy import ndimage

NORM = 128


def load_gray(source) -> np.ndarray:
    if isinstance(source, np.ndarray):
        arr = source
        if arr.dtype == bool:
            return np.where(arr, 0, 255).astype(np.uint8)
        return arr.astype(np.uint8)
    if isinstance(source, (bytes, bytearray)):
        image = Image.open(io.BytesIO(source))
    elif isinstance(source, Image.Image):
        image = source
    else:
        image = Image.open(os.fspath(source))
    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA"):
        background = Image.new("RGBA", image.size, (255, 255, 255, 255))
        image = Image.alpha_composite(background, image.convert("RGBA"))
    return np.asarray(image.convert("L"))


def _largest_components(mask: np.ndarray, keep_ratio: float = 0.08) -> np.ndarray:
    labels, count = ndimage.label(mask)
    if count == 0:
        return mask
    sizes = ndimage.sum(mask, labels, index=range(1, count + 1))
    biggest = sizes.max()
    keep = [i + 1 for i, s in enumerate(sizes) if s >= biggest * keep_ratio]
    return np.isin(labels, keep)


def mask_from_lineart(gray: np.ndarray, *, threshold: int = 160) -> np.ndarray:
    """Patent drawing (dark lines on white) -> filled silhouette."""

    lines = gray < threshold
    if lines.sum() == 0:
        return lines
    size = max(gray.shape)
    radius = max(1, int(round(size / 300)))
    closed = ndimage.binary_dilation(lines, iterations=radius)
    filled = ndimage.binary_fill_holes(closed)
    filled = _largest_components(filled)
    return ndimage.binary_erosion(filled, iterations=max(0, radius - 1)) if radius > 1 else filled


def mask_from_photo(gray: np.ndarray) -> np.ndarray:
    """Product photo on a plain background -> silhouette (background = border median)."""

    border = np.concatenate([gray[0, :], gray[-1, :], gray[:, 0], gray[:, -1]])
    bg = np.median(border)
    spread = max(12.0, float(np.percentile(np.abs(border - bg), 90)) * 2.5)
    fg = np.abs(gray.astype(float) - bg) > spread
    fg = ndimage.binary_opening(fg, iterations=1)
    fg = ndimage.binary_fill_holes(ndimage.binary_closing(fg, iterations=3))
    return _largest_components(fg, keep_ratio=0.2)


def mask_from_silhouette(gray: np.ndarray) -> np.ndarray:
    white = gray > 127
    # Silhouettes from the rasteriser are white on black; flip if needed.
    if white.mean() > 0.5:
        white = ~white
    return white


def auto_mask(gray: np.ndarray) -> np.ndarray:
    """Guess the image kind: mostly-white with thin dark strokes => line drawing."""

    dark = (gray < 160).mean()
    border = np.concatenate([gray[0, :], gray[-1, :], gray[:, 0], gray[:, -1]])
    if dark < 0.12 and np.median(border) > 200:
        return mask_from_lineart(gray)
    if np.median(border) < 30 and (gray > 127).mean() < 0.7:
        return mask_from_silhouette(gray)
    return mask_from_photo(gray)


@dataclass
class NormMask:
    mask: np.ndarray  # NORM x NORM bool
    aspect: float  # width / height of the original bbox


def normalise(mask: np.ndarray, size: int = NORM) -> NormMask | None:
    ys, xs = np.nonzero(mask)
    if len(xs) < 10:
        return None
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    crop = mask[y0:y1, x0:x1]
    h, w = crop.shape
    side = max(h, w)
    canvas = np.zeros((side, side), dtype=bool)
    oy, ox = (side - h) // 2, (side - w) // 2
    canvas[oy : oy + h, ox : ox + w] = crop
    image = Image.fromarray((canvas * 255).astype(np.uint8)).resize((size, size), Image.Resampling.BILINEAR)
    return NormMask(mask=np.asarray(image) > 127, aspect=w / max(h, 1))


def _boundary(mask: np.ndarray) -> np.ndarray:
    return mask & ~ndimage.binary_erosion(mask)


def _radial_signature(mask: np.ndarray, bins: int = 64) -> np.ndarray:
    ys, xs = np.nonzero(_boundary(mask))
    if len(xs) == 0:
        return np.zeros(bins)
    cy, cx = np.nonzero(mask)
    cyc, cxc = cy.mean(), cx.mean()
    angles = np.arctan2(ys - cyc, xs - cxc)
    radii = np.hypot(ys - cyc, xs - cxc)
    idx = ((angles + np.pi) / (2 * np.pi) * bins).astype(int) % bins
    sig = np.zeros(bins)
    np.maximum.at(sig, idx, radii)
    peak = sig.max()
    return sig / peak if peak > 0 else sig


def similarity(a: NormMask, b: NormMask) -> dict:
    inter = np.logical_and(a.mask, b.mask).sum()
    union = np.logical_or(a.mask, b.mask).sum()
    iou = float(inter / union) if union else 0.0
    ba, bb = _boundary(a.mask), _boundary(b.mask)
    if ba.any() and bb.any():
        da = ndimage.distance_transform_edt(~ba)
        db = ndimage.distance_transform_edt(~bb)
        chamfer = float((da[bb].mean() + db[ba].mean()) / 2) / NORM
    else:
        chamfer = 1.0
    aspect_sim = float(min(a.aspect, b.aspect) / max(a.aspect, b.aspect)) if min(a.aspect, b.aspect) > 0 else 0.0
    sa, sb = _radial_signature(a.mask), _radial_signature(b.mask)
    radial = float(1 - np.abs(sa - sb).mean())
    chamfer_sim = float(max(0.0, 1 - chamfer * 8))
    score = 0.40 * iou + 0.25 * chamfer_sim + 0.20 * aspect_sim + 0.15 * radial
    return {
        "score": round(score, 4),
        "iou": round(iou, 4),
        "chamfer": round(chamfer, 4),
        "aspect": round(aspect_sim, 4),
        "radial": round(radial, 4),
    }


def best_match(ours: dict[str, NormMask], theirs: list[NormMask]) -> dict:
    """Best pair between our views and the candidate's figures."""

    best = {"score": 0.0, "view": None, "figure": None}
    for view_key, our_mask in ours.items():
        if our_mask is None:
            continue
        for index, their_mask in enumerate(theirs):
            if their_mask is None:
                continue
            sim = similarity(our_mask, their_mask)
            if sim["score"] > best["score"]:
                best = {**sim, "view": view_key, "figure": index}
    return best


def rank_candidates(ours: dict[str, NormMask], candidates: list[dict], *, top: int | None = None) -> list[dict]:
    """candidates: [{"id": ..., "masks": [NormMask, ...], ...}] -> sorted with "shape" scores."""

    ranked = []
    for candidate in candidates:
        match = best_match(ours, candidate.get("masks") or [])
        row = {k: v for k, v in candidate.items() if k != "masks"}
        row["shape"] = match
        ranked.append(row)
    ranked.sort(key=lambda r: r["shape"]["score"], reverse=True)
    return ranked[:top] if top else ranked


def overlay(our_gray: np.ndarray, their_gray: np.ndarray, *, size: int = 512) -> Image.Image:
    """Red = ours, blue = prior art, purple = overlap. Inputs are any images."""

    a = normalise(auto_mask(our_gray), size)
    b = normalise(auto_mask(their_gray), size)
    rgb = np.full((size, size, 3), 255, dtype=np.uint8)
    if a is not None:
        rgb[a.mask] = (235, 90, 90)
    if b is not None:
        rgb[b.mask & ~(a.mask if a is not None else False)] = (90, 120, 235)
        if a is not None:
            rgb[a.mask & b.mask] = (150, 90, 190)
    return Image.fromarray(rgb)
