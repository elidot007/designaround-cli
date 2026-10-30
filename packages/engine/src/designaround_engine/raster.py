"""CPU rasteriser: shaded renders and silhouette masks without a GPU.

A painter's-algorithm renderer on top of Pillow. Good enough for previews,
factory communication images, silhouettes for shape comparison and overlays.
Photoreal renders (Blender) are optional and not required by any workflow.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from .camera import View, project

PART_COLORS = [(176, 186, 198), (122, 160, 214), (222, 170, 112), (146, 196, 146), (196, 150, 196)]


def _fit_transform(P: np.ndarray, width: int, height: int, margin: int, scale: float | None):
    x0, y0 = P[:, 0].min(), P[:, 1].min()
    x1, y1 = P[:, 0].max(), P[:, 1].max()
    w = max(x1 - x0, 1e-9)
    h = max(y1 - y0, 1e-9)
    if scale is None:
        scale = min((width - 2 * margin) / w, (height - 2 * margin) / h)
    ox = (width - w * scale) / 2 - x0 * scale
    oy = (height - h * scale) / 2 + y1 * scale
    return scale, ox, oy


def render(
    V: np.ndarray,
    F: np.ndarray,
    v: View,
    *,
    owners: np.ndarray | None = None,
    claimed: list[bool] | None = None,
    width: int = 1024,
    height: int = 768,
    margin: int = 40,
    mode: str = "shaded",
    background: int | tuple = 255,
    scale: float | None = None,
) -> Image.Image:
    """Render a triangle mesh. mode = shaded | silhouette."""

    if len(F) == 0:
        return Image.new("L" if mode == "silhouette" else "RGB", (width, height), 0 if mode == "silhouette" else (255, 255, 255))
    P = project(V, v)
    scale, ox, oy = _fit_transform(P, width, height, margin, scale)
    sx = ox + P[:, 0] * scale
    sy = oy - P[:, 1] * scale
    tri_x = sx[F]
    tri_y = sy[F]

    if mode == "silhouette":
        image = Image.new("L", (width, height), 0)
        draw = ImageDraw.Draw(image)
        for xs, ys in zip(tri_x, tri_y, strict=True):
            draw.polygon([(xs[0], ys[0]), (xs[1], ys[1]), (xs[2], ys[2])], fill=255)
        return image

    a = V[F[:, 1]] - V[F[:, 0]]
    b = V[F[:, 2]] - V[F[:, 0]]
    normals = np.cross(a, b)
    lengths = np.linalg.norm(normals, axis=1)
    lengths[lengths == 0] = 1.0
    normals /= lengths[:, None]
    _x, up, n = v.basis()
    facing = normals @ n
    keep = facing > 1e-6
    key_light = n + 0.6 * up + 0.3 * _x
    key_light /= np.linalg.norm(key_light)
    diffuse = np.clip(normals @ key_light, 0, 1)
    shade = 0.35 + 0.65 * diffuse

    depth = P[:, 2][F].mean(axis=1)
    order = np.argsort(depth)  # far first
    bg = background if isinstance(background, tuple) else (background,) * 3
    image = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(image)
    for idx in order:
        if not keep[idx]:
            continue
        base = PART_COLORS[0]
        if owners is not None:
            part = int(owners[idx])
            base = PART_COLORS[part % len(PART_COLORS)]
            if claimed is not None and part < len(claimed) and not claimed[part]:
                base = (225, 225, 225)
        s = shade[idx]
        color = tuple(int(min(255, c * s)) for c in base)
        xs, ys = tri_x[idx], tri_y[idx]
        draw.polygon([(xs[0], ys[0]), (xs[1], ys[1]), (xs[2], ys[2])], fill=color)
    return image


def silhouette_mask(V: np.ndarray, F: np.ndarray, v: View, *, size: int = 256) -> np.ndarray:
    image = render(V, F, v, width=size, height=size, margin=4, mode="silhouette")
    return np.asarray(image) > 127
