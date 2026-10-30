"""Standard design-patent views.

Product frame: X = width (left → right), Y = depth (front → back), Z = height.
Each view is (n, x_axis): ``n`` points from the object toward the viewer,
``x_axis`` is the screen-right direction. Screen-up is ``n × x_axis``. The same
definitions drive the hidden-line projector (lineart) and the CPU rasteriser
(raster), so line drawings and renders always align.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class View:
    key: str
    n: tuple[float, float, float]
    x_axis: tuple[float, float, float]
    us_label: str
    cn_label: str
    orthographic: bool = True

    def basis(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        n = np.asarray(self.n, dtype=float)
        n = n / np.linalg.norm(n)
        x = np.asarray(self.x_axis, dtype=float)
        x = x - n * np.dot(x, n)
        x = x / np.linalg.norm(x)
        up = np.cross(n, x)
        return x, up, n


_ISO = (-1.0, -1.0, 1.0)
_ISO_X = (1.0, -1.0, 0.0)

STANDARD_VIEWS: dict[str, View] = {
    "perspective": View("perspective", _ISO, _ISO_X, "perspective view", "立体图", orthographic=False),
    "front": View("front", (0, -1, 0), (1, 0, 0), "front view", "主视图"),
    "rear": View("rear", (0, 1, 0), (-1, 0, 0), "rear view", "后视图"),
    "left": View("left", (-1, 0, 0), (0, -1, 0), "left side view", "左视图"),
    "right": View("right", (1, 0, 0), (0, 1, 0), "right side view", "右视图"),
    "top": View("top", (0, 0, 1), (1, 0, 0), "top plan view", "俯视图"),
    "bottom": View("bottom", (0, 0, -1), (1, 0, 0), "bottom plan view", "仰视图"),
}

# Order used for figure numbering. US practice puts the perspective view first.
US_ORDER = ["perspective", "front", "rear", "left", "right", "top", "bottom"]
CN_ORDER = ["front", "rear", "left", "right", "top", "bottom", "perspective"]


def view(key: str) -> View:
    try:
        return STANDARD_VIEWS[key]
    except KeyError as exc:
        raise KeyError(f"Unknown view '{key}'. Known: {', '.join(STANDARD_VIEWS)}") from exc


def from_angles(azimuth_deg: float, elevation_deg: float, key: str = "custom") -> View:
    """Viewer direction from azimuth (0 = front, 90 = right) and elevation (90 = top)."""

    az = np.radians(azimuth_deg)
    el = np.radians(elevation_deg)
    n = (np.sin(az) * np.cos(el), -np.cos(az) * np.cos(el), np.sin(el))
    x_axis = (np.cos(az), np.sin(az), 0.0)
    if abs(np.cos(el)) < 1e-6:  # straight up or down
        x_axis = (1.0, 0.0, 0.0)
    return View(key, tuple(map(float, n)), tuple(map(float, x_axis)), f"{key} view", key, orthographic=False)


def project(points: np.ndarray, v: View) -> np.ndarray:
    """Orthographic projection to (screen_x, screen_y, depth_toward_viewer)."""

    x, up, n = v.basis()
    return np.stack([points @ x, points @ up, points @ n], axis=1)
