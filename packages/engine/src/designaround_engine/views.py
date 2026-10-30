"""Produce the standard view set for a version: line art, silhouettes, renders.

Outputs go to ``<version>/views/``. They feed retrieval queries (line art is
the same visual domain as patent drawings), shape comparison, overlays, the
viewpack and factory images.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

from .camera import STANDARD_VIEWS, View
from .compare import NormMask, normalise
from .evidence import sha256_file
from .geometry import combined_mesh, load_version_parts
from .lineart import project_views, to_png, to_svg
from .raster import render, silhouette_mask


def render_views(
    version_dir: str | os.PathLike,
    *,
    keys: list[str] | None = None,
    renders: bool = True,
    size: int = 1200,
    hlr_budget_s: float | None = None,
    lineart: bool = True,
) -> dict:
    """lineart=False skips hidden-line removal (the slow step) and writes only
    silhouettes and shaded renders; used while the CAD Agent iterates."""

    version_dir = Path(version_dir)
    out = version_dir / "views"
    out.mkdir(exist_ok=True)
    parts = load_version_parts(version_dir)
    V, F, owners = combined_mesh(parts)
    claimed = [p.claimed for p in parts]
    keys = keys or list(STANDARD_VIEWS)
    index: dict[str, dict] = {}
    drawings = project_views(parts, keys, budget_s=hlr_budget_s) if lineart else {}
    for key in keys:
        v = STANDARD_VIEWS[key]
        sil = render(V, F, v, width=512, height=512, margin=8, mode="silhouette")
        sil.save(out / f"{key}.silhouette.png")
        entry: dict = {"silhouette_png": f"views/{key}.silhouette.png"}
        if lineart:
            drawing = drawings[key]
            (out / f"{key}.svg").write_text(to_svg(drawing), encoding="utf-8")
            to_png(drawing, width=size, height=int(size * 0.75)).save(out / f"{key}.lineart.png")
            entry.update({
                "lineart_svg": f"views/{key}.svg",
                "lineart_png": f"views/{key}.lineart.png",
                "extent_mm": [round(x, 3) for x in drawing.extent()],
                "line_count": len(drawing.lines),
                "hlr": drawing.hlr,
            })
        if renders:
            render(V, F, v, owners=owners, claimed=claimed, width=size, height=int(size * 0.75)).save(out / f"{key}.render.png")
            entry["render_png"] = f"views/{key}.render.png"
        for name in ("lineart_png", "silhouette_png", "lineart_svg", "render_png"):
            if name in entry:
                entry[name + "_sha256"] = sha256_file(version_dir / entry[name])
        index[key] = entry
    (out / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    return index


def view_masks(version_dir: str | os.PathLike, *, keys: list[str] | None = None, size: int = 256) -> dict[str, NormMask]:
    """Normalised silhouettes of our design, computed directly from the mesh."""

    parts = load_version_parts(version_dir)
    V, F, _owners = combined_mesh(parts)
    keys = keys or [k for k in STANDARD_VIEWS]
    return {key: normalise(silhouette_mask(V, F, STANDARD_VIEWS[key], size=size)) for key in keys}


def masks_for_view(V: np.ndarray, F: np.ndarray, v: View, size: int = 256) -> NormMask | None:
    return normalise(silhouette_mask(V, F, v, size=size))
