"""Patent-style line drawings via OpenCascade hidden-line removal (HLR).

Every standard view is projected from the same B-rep, so all views are
consistent by construction (FR-DRAW-01). Parts flagged ``claimed=False`` are
drawn in broken lines (FR-DRAW-03). Hidden lines are excluded from patent views
but available for engineering drawings.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageDraw

from .camera import View


@dataclass
class Polyline:
    points: np.ndarray  # (k, 2) in model millimetres, screen coordinates
    part: str
    claimed: bool
    kind: str  # visible | smooth | outline | hidden


@dataclass
class LineDrawing:
    view: View
    lines: list[Polyline] = field(default_factory=list)
    hlr: str = "exact"

    def bounds(self, *, include_hidden: bool = False) -> tuple[float, float, float, float]:
        pts = [ln.points for ln in self.lines if include_hidden or ln.kind != "hidden"]
        if not pts:
            return (0.0, 0.0, 1.0, 1.0)
        allp = np.vstack(pts)
        return float(allp[:, 0].min()), float(allp[:, 1].min()), float(allp[:, 0].max()), float(allp[:, 1].max())

    def extent(self) -> tuple[float, float]:
        x0, y0, x1, y1 = self.bounds()
        return x1 - x0, y1 - y0


def _edges_to_polylines(compound, deflection: float, part: str, claimed: bool, kind: str) -> list[Polyline]:
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepLib import BRepLib
    from OCP.GCPnts import GCPnts_QuasiUniformDeflection
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    if compound is None or compound.IsNull():
        return []
    BRepLib.BuildCurves3d_s(compound, 1e-6)
    out: list[Polyline] = []
    explorer = TopExp_Explorer(compound, TopAbs_EDGE)
    while explorer.More():
        edge = TopoDS.Edge_s(explorer.Current())
        explorer.Next()
        try:
            curve = BRepAdaptor_Curve(edge)
            sampler = GCPnts_QuasiUniformDeflection(curve, deflection)
            if not sampler.IsDone() or sampler.NbPoints() < 2:
                continue
            pts = np.array([(sampler.Value(i).X(), sampler.Value(i).Y()) for i in range(1, sampler.NbPoints() + 1)])
        except Exception:  # noqa: BLE001 - degenerate edges are skipped
            continue
        if np.ptp(pts[:, 0]) + np.ptp(pts[:, 1]) < 1e-9:
            continue
        out.append(Polyline(points=pts, part=part, claimed=claimed, kind=kind))
    return out


def _collect(shapes, parts, deflection: float, *, include_hidden: bool, include_smooth: bool, v: View) -> LineDrawing:
    drawing = LineDrawing(view=v)
    for part in parts:
        wrapped = part.shape.wrapped
        drawing.lines += _edges_to_polylines(shapes.VCompound(wrapped), deflection, part.name, part.claimed, "visible")
        drawing.lines += _edges_to_polylines(shapes.OutLineVCompound(wrapped), deflection, part.name, part.claimed, "outline")
        if include_smooth:
            drawing.lines += _edges_to_polylines(shapes.Rg1LineVCompound(wrapped), deflection, part.name, part.claimed, "smooth")
        if include_hidden:
            drawing.lines += _edges_to_polylines(shapes.HCompound(wrapped), deflection, part.name, part.claimed, "hidden")
            drawing.lines += _edges_to_polylines(shapes.OutLineHCompound(wrapped), deflection, part.name, part.claimed, "hidden")
    return drawing


def _mesh_extent(parts, v: View) -> tuple[float, float, float, float] | None:
    from .camera import project
    from .geometry import combined_mesh

    V, F, _ = combined_mesh(parts)
    if len(V) == 0:
        return None
    P = project(V, v)
    return float(P[:, 0].min()), float(P[:, 1].min()), float(P[:, 0].max()), float(P[:, 1].max())


def _setup(parts, v: View):
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    sizes = []
    for part in parts:
        bb = part.shape.BoundingBox()
        sizes.append(max(bb.xlen, bb.ylen, bb.zlen))
    size = max(sizes)
    deflection = max(size * 0.0008, 0.01)
    x, _up, n = v.basis()
    axes = gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(*map(float, n)), gp_Dir(*map(float, x)))
    return size, deflection, axes


def _polygonal(parts, v: View, *, include_hidden: bool, include_smooth: bool) -> LineDrawing:
    """Polygonal HLR on the tessellation: fast and never stalls, slightly faceted on curves."""

    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.HLRAlgo import HLRAlgo_Projector
    from OCP.HLRBRep import HLRBRep_PolyAlgo, HLRBRep_PolyHLRToShape

    size, deflection, axes = _setup(parts, v)
    poly = HLRBRep_PolyAlgo()
    for part in parts:
        BRepMesh_IncrementalMesh(part.shape.wrapped, max(size * 0.0015, 0.05), False, 0.2)
        poly.Load(part.shape.wrapped)
    poly.Projector(HLRAlgo_Projector(axes))
    poly.Update()
    shapes = HLRBRep_PolyHLRToShape()
    shapes.Update(poly)
    drawing = _collect(shapes, parts, deflection, include_hidden=include_hidden, include_smooth=include_smooth, v=v)
    drawing.lines = [ln for ln in drawing.lines if ln.kind != "smooth"]  # polygon seams are not design lines
    drawing.hlr = "polygonal"
    return drawing


def _all_faceted(parts) -> bool:
    return bool(parts) and all(getattr(p, "faceted", False) for p in parts)


def faceted_lines(parts, v: View, *, include_hidden: bool = False) -> LineDrawing:
    """Line drawing of an imported mesh (faceted B-rep).

    Polygonal HLR is exact on planar facets and fast. Facet seams that are smooth
    are not design lines; the seams on this view's silhouette are made sharp for
    the duration (``ViewContours``), so curved surfaces get their outline.
    """

    from .meshimport import ViewContours

    with ViewContours(parts, v):
        drawing = _polygonal(parts, v, include_hidden=include_hidden, include_smooth=False)
    drawing.hlr = "faceted"
    return drawing


def project_lines(parts, v: View, *, include_hidden: bool = False, include_smooth: bool = True) -> LineDrawing:
    """Exact HLR; falls back to polygonal HLR for a view whose exact outline is incomplete.

    OpenCascade's exact algorithm occasionally drops the silhouette of a torus
    or a doubly-curved surface seen edge-on. The projected tessellation gives
    the true extent; when the exact drawing is short of it, the polygonal
    algorithm (built on the same tessellation) is used for that view.

    This runs in the calling process and cannot be interrupted; for models with
    freeform faces use ``project_views``, which applies a time budget.
    """

    from OCP.HLRAlgo import HLRAlgo_Projector
    from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape

    if not parts:
        return LineDrawing(view=v)
    if _all_faceted(parts):
        return faceted_lines(parts, v, include_hidden=include_hidden)
    size, deflection, axes = _setup(parts, v)

    algo = HLRBRep_Algo()
    for part in parts:
        algo.Add(part.shape.wrapped)
    algo.Projector(HLRAlgo_Projector(axes))
    algo.Update()
    algo.Hide()
    drawing = _collect(HLRBRep_HLRToShape(algo), parts, deflection, include_hidden=include_hidden, include_smooth=include_smooth, v=v)

    truth = _mesh_extent(parts, v)
    if truth is not None:
        x0, y0, x1, y1 = drawing.bounds()
        tol = max(size * 0.004, 0.2)
        short = (x0 - truth[0] > tol) or (y0 - truth[1] > tol) or (truth[2] - x1 > tol) or (truth[3] - y1 > tol)
        if short:
            fallback = _polygonal(parts, v, include_hidden=include_hidden, include_smooth=include_smooth)
            if fallback.lines:
                return fallback
    drawing.hlr = "exact"
    return drawing


# ------------------------------------------------------------ time-limited exact HLR

_SIMPLE_SURFACES = ("GeomAbs_Plane", "GeomAbs_Cylinder", "GeomAbs_Cone")


def _only_simple_faces(parts, limit: int = 400) -> bool:
    """True when every face is a plane, cylinder or cone (exact HLR is fast and reliable on these)."""

    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    count = 0
    for part in parts:
        explorer = TopExp_Explorer(part.shape.wrapped, TopAbs_FACE)
        while explorer.More():
            kind = str(BRepAdaptor_Surface(TopoDS.Face_s(explorer.Current())).GetType())
            explorer.Next()
            count += 1
            if count > limit or not kind.endswith(_SIMPLE_SURFACES):
                return False
    return True


def drawing_to_json(drawing: LineDrawing) -> dict:
    return {
        "hlr": drawing.hlr,
        "lines": [{"part": ln.part, "claimed": ln.claimed, "kind": ln.kind, "points": np.round(ln.points, 4).tolist()} for ln in drawing.lines],
    }


def drawing_from_json(data: dict, v: View) -> LineDrawing:
    lines = [Polyline(points=np.asarray(row["points"], dtype=float).reshape(-1, 2), part=row["part"], claimed=bool(row["claimed"]), kind=row["kind"]) for row in data["lines"]]
    return LineDrawing(view=v, lines=lines, hlr=data.get("hlr", "exact"))


def project_views(parts, keys: list[str], *, include_hidden: bool = False, include_smooth: bool = True, budget_s: float | None = None) -> dict[str, LineDrawing]:
    """Line drawings for several standard views, never stalling on a hard model.

    Models made only of planes, cylinders and cones use exact HLR in this process.
    Anything with freeform faces runs exact HLR in a child process with a time
    budget (DA_HLR_SECONDS, default 40 s for all views); views that are not done
    in time use polygonal HLR. A crash in the kernel also falls back.
    """

    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    from .camera import STANDARD_VIEWS

    if not parts:
        return {k: LineDrawing(view=STANDARD_VIEWS[k]) for k in keys}
    if _all_faceted(parts):  # imported meshes: polygonal HLR in this process (fast, cannot stall)
        return {k: faceted_lines(parts, STANDARD_VIEWS[k], include_hidden=include_hidden) for k in keys}
    if os.environ.get("DA_HLR_ISOLATE") != "1" and _only_simple_faces(parts):
        return {k: project_lines(parts, STANDARD_VIEWS[k], include_hidden=include_hidden, include_smooth=include_smooth) for k in keys}

    budget = budget_s if budget_s is not None else float(os.environ.get("DA_HLR_SECONDS", "40"))
    out: dict[str, LineDrawing] = {}
    with tempfile.TemporaryDirectory(prefix="da-hlr-") as tmp:
        rows = []
        for i, part in enumerate(parts):
            path = str(Path(tmp) / f"part{i}.brep")
            part.shape.exportBrep(path)
            rows.append({"name": part.name, "file": path, "claimed": part.claimed})
        request = {"parts": rows, "keys": list(keys), "include_hidden": include_hidden, "include_smooth": include_smooth, "out": tmp}
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
        proc = subprocess.Popen(
            [sys.executable, "-m", "designaround_engine.hlr_worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            proc.communicate(json.dumps(request).encode("utf-8"), timeout=budget)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        for key in keys:
            done = Path(tmp) / f"{key}.json"
            if done.exists():
                out[key] = drawing_from_json(json.loads(done.read_text(encoding="utf-8")), STANDARD_VIEWS[key])
            else:
                out[key] = _polygonal(parts, STANDARD_VIEWS[key], include_hidden=include_hidden, include_smooth=include_smooth)
    return out


# --------------------------------------------------------------------------- output


def _fit(drawing: LineDrawing, width: float, height: float, margin: float, scale: float | None):
    x0, y0, x1, y1 = drawing.bounds()
    w = max(x1 - x0, 1e-6)
    h = max(y1 - y0, 1e-6)
    if scale is None:
        scale = min((width - 2 * margin) / w, (height - 2 * margin) / h)
    ox = (width - w * scale) / 2 - x0 * scale
    oy = (height - h * scale) / 2 + y1 * scale  # flip y for image coordinates
    return scale, ox, oy


def to_svg(
    drawing: LineDrawing,
    *,
    width: float = 800,
    height: float = 600,
    margin: float = 30,
    stroke: float = 1.4,
    scale: float | None = None,
    include_hidden: bool = False,
) -> str:
    scale, ox, oy = _fit(drawing, width, height, margin, scale)
    solid, broken, hidden, shading = [], [], [], []
    for line in drawing.lines:
        if line.kind == "hidden" and not include_hidden:
            continue
        pts = " ".join(f"{ox + px * scale:.2f},{oy - py * scale:.2f}" for px, py in line.points)
        element = f'<polyline points="{pts}"/>'
        if line.kind == "hidden":
            hidden.append(element)
        elif line.kind == "shading":
            shading.append(element)
        elif line.claimed:
            solid.append(element)
        else:
            broken.append(element)
    dash = f"{stroke * 4:.1f},{stroke * 3:.1f}"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
        f'viewBox="0 0 {width:.0f} {height:.0f}">\n'
        f'<rect width="100%" height="100%" fill="white"/>\n'
        f'<g fill="none" stroke="black" stroke-width="{stroke}" stroke-linejoin="round" stroke-linecap="round">\n'
        + "\n".join(solid)
        + f'\n</g>\n<g fill="none" stroke="black" stroke-width="{stroke * 0.45:.2f}" stroke-linecap="round" class="shading">\n'
        + "\n".join(shading)
        + f'\n</g>\n<g fill="none" stroke="black" stroke-width="{stroke * 0.8:.2f}" stroke-dasharray="{dash}">\n'
        + "\n".join(broken)
        + f'\n</g>\n<g fill="none" stroke="#888" stroke-width="{stroke * 0.6:.2f}" stroke-dasharray="{dash}">\n'
        + "\n".join(hidden)
        + "\n</g>\n</svg>\n"
    )


def _dashed(draw: ImageDraw.ImageDraw, pts: list[tuple[float, float]], width: int, on: float, off: float) -> None:
    carry = 0.0
    drawing_on = True
    for (xa, ya), (xb, yb) in zip(pts, pts[1:], strict=False):
        seg = float(np.hypot(xb - xa, yb - ya))
        pos = 0.0
        while pos < seg:
            step = (on if drawing_on else off) - carry
            end = min(pos + step, seg)
            if drawing_on:
                t0, t1 = pos / seg, end / seg
                draw.line(
                    [(xa + (xb - xa) * t0, ya + (yb - ya) * t0), (xa + (xb - xa) * t1, ya + (yb - ya) * t1)],
                    fill=0,
                    width=width,
                )
            if end - pos < step:
                carry += end - pos
                break
            carry = 0.0
            pos = end
            drawing_on = not drawing_on


def to_png(
    drawing: LineDrawing,
    *,
    width: int = 1600,
    height: int = 1200,
    margin: int = 60,
    stroke: int = 3,
    scale: float | None = None,
) -> Image.Image:
    scale, ox, oy = _fit(drawing, width, height, margin, scale)
    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    for line in drawing.lines:
        if line.kind == "hidden":
            continue
        pts = [(ox + px * scale, oy - py * scale) for px, py in line.points]
        if line.kind == "shading":
            draw.line(pts, fill=0, width=max(1, stroke // 2), joint="curve")
        elif line.claimed:
            draw.line(pts, fill=0, width=stroke, joint="curve")
        else:
            _dashed(draw, pts, max(1, stroke - 1), on=stroke * 5, off=stroke * 4)
    return image
