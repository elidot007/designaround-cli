"""Surface shading lines for US design drawings (37 CFR 1.152, MPEP 1503.02 II).

Curved surfaces get thin lines that follow the surface's direction of least
curvature (along the axis of a cylinder, along the run of a fillet), spaced at
equal steps of surface-normal rotation. Projected, equal angular steps bunch
together towards the silhouette, which is the conventional look. Lines are
drawn only on the side turned away from a light at the upper left, so the
lit side stays clean, and only where the surface is visible (z-buffer test).

Generated shading is a draft: a practitioner reviews it before filing (adding
or changing shading after filing can be new matter).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .camera import View, project
from .lineart import Polyline

STEP_DEG = 9.0  # normal rotation between neighbouring shading lines
SMALL_FACE_STEP_DEG = 22.0  # small fillets get only a couple of lines
MAX_LINES_PER_FACE = 28
SAMPLES_ALONG = 90
SHADE_THRESHOLD = 0.42  # 0 = fully lit, 1 = facing away from the light


@dataclass
class ShadingCurve:
    points: np.ndarray  # (k, 3)
    normals: np.ndarray  # (k, 3) outward unit normals
    part: str
    claimed: bool


def _face_curves(face, part: str, claimed: bool, *, small: float) -> list[ShadingCurve]:
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepLProp import BRepLProp_SLProps
    from OCP.BRepTools import BRepTools
    from OCP.BRepTopAdaptor import BRepTopAdaptor_FClass2d
    from OCP.GeomAbs import GeomAbs_Plane
    from OCP.gp import gp_Dir, gp_Pnt2d
    from OCP.TopAbs import TopAbs_IN, TopAbs_ON, TopAbs_REVERSED

    surf = BRepAdaptor_Surface(face)
    if surf.GetType() == GeomAbs_Plane:
        return []
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    if not (np.isfinite([u0, u1, v0, v1]).all()) or u1 - u0 < 1e-9 or v1 - v0 < 1e-9:
        return []
    reversed_ = face.Orientation() == TopAbs_REVERSED
    props = BRepLProp_SLProps(surf, 2, 1e-6)
    classifier = BRepTopAdaptor_FClass2d(face, 1e-6)

    def sample(u: float, v: float):
        props.SetParameters(u, v)
        if not props.IsNormalDefined():
            return None
        p = props.Value()
        n = props.Normal()
        vec = np.array([n.X(), n.Y(), n.Z()])
        return np.array([p.X(), p.Y(), p.Z()]), (-vec if reversed_ else vec)

    # Direction of least curvature at the centre decides which family of iso-lines to draw.
    uc, vc = (u0 + u1) / 2, (v0 + v1) / 2
    props.SetParameters(uc, vc)
    along_u = True  # True: lines run in u (fixed v)
    if props.IsCurvatureDefined():
        dmin = gp_Dir()
        dmax = gp_Dir()
        props.CurvatureDirections(dmax, dmin)
        kmin, kmax = abs(props.MinCurvature()), abs(props.MaxCurvature())
        du, dv = props.D1U(), props.D1V()
        dirv = np.array([dmin.X(), dmin.Y(), dmin.Z()])
        if kmin > kmax:  # MinCurvature is signed; pick the flatter direction by magnitude
            dirv = np.array([dmax.X(), dmax.Y(), dmax.Z()])
        du_v = np.array([du.X(), du.Y(), du.Z()])
        dv_v = np.array([dv.X(), dv.Y(), dv.Z()])
        nu, nv = np.linalg.norm(du_v), np.linalg.norm(dv_v)
        if nu > 1e-12 and nv > 1e-12:
            along_u = abs(dirv @ du_v) / nu >= abs(dirv @ dv_v) / nv
    # "across" parameter: lines are placed at steps of normal rotation along it.
    a0, a1 = (v0, v1) if along_u else (u0, u1)
    s0, s1 = (u0, u1) if along_u else (v0, v1)

    def uv(a: float, s: float) -> tuple[float, float]:
        return (s, a) if along_u else (a, s)

    across = np.linspace(a0, a1, 64)
    sm = (s0 + s1) / 2
    normals = []
    for a in across:
        got = sample(*uv(a, sm))
        normals.append(got[1] if got else None)
    if any(n is None for n in normals):
        return []
    turn = [0.0]
    for na, nb in zip(normals, normals[1:], strict=False):
        turn.append(turn[-1] + float(np.degrees(np.arccos(np.clip(na @ nb, -1.0, 1.0)))))
    total = turn[-1]
    if total < 4.0:  # nearly flat (gentle B-spline): no shading
        return []
    radius_ok = True
    if props.IsCurvatureDefined():
        props.SetParameters(*uv((a0 + a1) / 2, sm))
        k = max(abs(props.MaxCurvature()), abs(props.MinCurvature()), 1e-12)
        radius_ok = (1.0 / k) >= small
    step = STEP_DEG if radius_ok else SMALL_FACE_STEP_DEG
    count = min(MAX_LINES_PER_FACE, max(1, int(total // step)))
    targets = [(i + 0.5) * total / count for i in range(count)]
    line_params = np.interp(targets, turn, across)

    curves = []
    for a in line_params:
        pts, nrm = [], []
        run_pts, run_nrm = [], []
        for s in np.linspace(s0, s1, SAMPLES_ALONG):
            u, v = uv(float(a), float(s))
            state = classifier.Perform(gp_Pnt2d(u, v))
            got = sample(u, v) if state in (TopAbs_IN, TopAbs_ON) else None
            if got is None:
                if len(run_pts) >= 2:
                    pts.append(np.array(run_pts))
                    nrm.append(np.array(run_nrm))
                run_pts, run_nrm = [], []
                continue
            run_pts.append(got[0])
            run_nrm.append(got[1])
        if len(run_pts) >= 2:
            pts.append(np.array(run_pts))
            nrm.append(np.array(run_nrm))
        curves += [ShadingCurve(points=p, normals=n, part=part, claimed=claimed) for p, n in zip(pts, nrm, strict=True)]
    return curves


def shading_curves(parts) -> list[ShadingCurve]:
    """View-independent shading curves of all curved faces (computed once per version)."""

    sizes = []
    for part in parts:
        bb = part.shape.BoundingBox()
        sizes.append(max(bb.xlen, bb.ylen, bb.zlen))
    small = max(sizes or [1.0]) * 0.02
    curves: list[ShadingCurve] = []
    for part in parts:
        if not part.claimed:  # unclaimed environment is drawn in broken lines only
            continue
        for face in part.shape.Faces():
            try:
                curves += _face_curves(face.wrapped, part.name, part.claimed, small=small)
            except Exception:  # noqa: BLE001 - one odd face must not stop the drawing
                continue
    return curves


class DepthBuffer:
    """Front-most depth per pixel of the mesh in one view (orthographic)."""

    def __init__(self, V: np.ndarray, F: np.ndarray, v: View, *, pixels: int = 900) -> None:
        P = project(V, v)
        self.x0, self.y0 = P[:, 0].min(), P[:, 1].min()
        span = max(P[:, 0].max() - self.x0, P[:, 1].max() - self.y0, 1e-6)
        self.res = span / (pixels - 1)
        w = int(np.ceil((P[:, 0].max() - self.x0) / self.res)) + 2
        h = int(np.ceil((P[:, 1].max() - self.y0) / self.res)) + 2
        z = np.full((h, w), -np.inf)
        sx = (P[:, 0] - self.x0) / self.res
        sy = (P[:, 1] - self.y0) / self.res
        sz = P[:, 2]
        for tri in F:
            xs, ys, zs = sx[tri], sy[tri], sz[tri]
            xmin, xmax = int(np.floor(xs.min())), int(np.ceil(xs.max()))
            ymin, ymax = int(np.floor(ys.min())), int(np.ceil(ys.max()))
            if xmax < 0 or ymax < 0 or xmin >= w or ymin >= h:
                continue
            gx, gy = np.meshgrid(np.arange(max(xmin, 0), min(xmax, w - 1) + 1), np.arange(max(ymin, 0), min(ymax, h - 1) + 1))
            (xa, xb, xc), (ya, yb, yc) = xs, ys
            det = (yb - yc) * (xa - xc) + (xc - xb) * (ya - yc)
            if abs(det) < 1e-12:
                continue
            l1 = ((yb - yc) * (gx - xc) + (xc - xb) * (gy - yc)) / det
            l2 = ((yc - ya) * (gx - xc) + (xa - xc) * (gy - yc)) / det
            l3 = 1 - l1 - l2
            inside = (l1 >= -0.02) & (l2 >= -0.02) & (l3 >= -0.02)
            if not inside.any():
                continue
            depth = l1 * zs[0] + l2 * zs[1] + l3 * zs[2]
            cur = z[gy[inside], gx[inside]]
            z[gy[inside], gx[inside]] = np.maximum(cur, depth[inside])
        self.z = z

    def visible(self, P: np.ndarray, tol: float) -> np.ndarray:
        ix = np.clip(np.round((P[:, 0] - self.x0) / self.res).astype(int), 0, self.z.shape[1] - 1)
        iy = np.clip(np.round((P[:, 1] - self.y0) / self.res).astype(int), 0, self.z.shape[0] - 1)
        best = self.z[iy, ix]
        # neighbours: a sample right on a silhouette may fall on the far side of a pixel edge
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            jx = np.clip(ix + dx, 0, self.z.shape[1] - 1)
            jy = np.clip(iy + dy, 0, self.z.shape[0] - 1)
            best = np.minimum(best, np.where(np.isfinite(self.z[jy, jx]), self.z[jy, jx], best))
        return P[:, 2] >= best - tol


def shading_polylines(curves: list[ShadingCurve], v: View, depth: DepthBuffer, *, extent_mm: float) -> list[Polyline]:
    x, up, n = v.basis()
    light = -x + up + 0.8 * n  # light from the upper left, in front of the drawing
    light /= np.linalg.norm(light)
    tol = depth.res * 2.5 + extent_mm * 0.002
    out: list[Polyline] = []
    for curve in curves:
        facing = curve.normals @ n
        shade = 1.0 - np.clip(curve.normals @ light, 0.0, 1.0)
        P = project(curve.points, v)
        keep = (facing > 0.08) & (shade > SHADE_THRESHOLD) & depth.visible(P, tol)
        # split into runs of kept samples
        start = None
        for i, flag in enumerate(np.append(keep, False)):
            if flag and start is None:
                start = i
            elif not flag and start is not None:
                if i - start >= 2:
                    seg = P[start:i, :2]
                    if float(np.linalg.norm(seg[-1] - seg[0])) > extent_mm * 0.01:
                        out.append(Polyline(points=seg.copy(), part=curve.part, claimed=curve.claimed, kind="shading"))
                start = None
    return out


def add_shading(drawings: dict, parts, V: np.ndarray, F: np.ndarray, views: dict[str, View]) -> dict:
    """Append shading polylines to each LineDrawing (in place). Returns per-view line counts."""

    curves = shading_curves(parts)
    counts = {}
    for key, drawing in drawings.items():
        if not curves or len(F) == 0:
            counts[key] = 0
            continue
        extent = max(drawing.extent())
        depth = DepthBuffer(V, F, views[key])
        lines = shading_polylines(curves, views[key], depth, extent_mm=extent)
        drawing.lines += lines
        counts[key] = len(lines)
    return counts
