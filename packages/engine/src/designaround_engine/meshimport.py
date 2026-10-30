"""Mesh import and repair (FR-3D-10): STL / OBJ / GLB / glTF / PLY / OFF / 3MF -> a version.

A mesh becomes a faceted B-rep, one solid per connected piece, so every later step
(views, patent line drawings, DFM, view packs, prototype files) works as for CAD.
Steps, all recorded in the version manifest:

1. load: 3MF is read directly (units, object names); glTF content is Y-up by
   convention and turned Z-up (``up`` overrides); everything is in millimetres;
2. repair: merge coincident vertices, drop degenerate and duplicate triangles,
   remove tiny loose pieces, fill holes up to ``HOLE_MAX_EDGES`` edges, make the
   winding consistent and outward;
3. reduce: above ``MAX_TRIANGLES`` the mesh is simplified with quadric error
   decimation; the original file is kept unchanged as ``source.*``;
4. sew the triangles into shells, merge coplanar facets, make solids, and mark
   edges where the surface bends less than ``SMOOTH_ANGLE`` as smooth.

Patent drawings of a faceted model use polygonal hidden-line removal (exact for
planar facets). Smooth facet seams are not drawn; for each view, the seams on
the silhouette are marked sharp first (``ViewContours``), so curved surfaces get
their true outline. Drawings from scans are drafts: a practitioner checks them
before filing.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import numpy as np

from .errors import InvalidInput

MESH_SUFFIXES = {".stl", ".obj", ".glb", ".gltf", ".ply", ".off", ".3mf"}
MAX_TRIANGLES = 15_000
SMOOTH_ANGLE = 25.0  # degrees between facet normals below which an edge is smooth
LOOSE_SHARE = 0.002  # pieces with less than this share of the surface area are dropped
HOLE_MAX_EDGES = 200  # boundary loops up to this many edges are filled
UNIT_TO_MM = {"micron": 0.001, "millimeter": 1.0, "centimeter": 10.0, "inch": 25.4, "foot": 304.8, "meter": 1000.0}
UNIT_ALIASES = {"mm": "millimeter", "cm": "centimeter", "m": "meter", "in": "inch", "ft": "foot", "um": "micron"}


# ------------------------------------------------------------------- loading


def _load_3mf(path: Path) -> tuple[np.ndarray, np.ndarray, list[str], float]:
    """Vertices, faces, object names and the millimetre factor of a 3MF package."""

    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".model")]
        if not names:
            raise InvalidInput(f"{path.name}: no 3D model inside the 3MF package.")
        verts, faces, objects = [], [], []
        unit, offset = 1.0, 0
        for name in names:
            xml = z.read(name).decode("utf-8", errors="replace")
            m = re.search(r'<model[^>]*\sunit="([a-z]+)"', xml)
            if m:
                unit = UNIT_TO_MM.get(m.group(1), 1.0)
            for obj in re.finditer(r"<object\b([^>]*)>(.*?)</object>", xml, re.S):
                attrs, body = obj.group(1), obj.group(2)
                vs = re.findall(r'<vertex\s+x="([-+\deE.]+)"\s+y="([-+\deE.]+)"\s+z="([-+\deE.]+)"', body)
                ts = re.findall(r'<triangle\s+v1="(\d+)"\s+v2="(\d+)"\s+v3="(\d+)"', body)
                if not vs or not ts:
                    continue
                verts.append(np.array(vs, dtype=float))
                faces.append(np.array(ts, dtype=np.int64) + offset)
                offset += len(vs)
                nm = re.search(r'name="([^"]*)"', attrs)
                objects.append(nm.group(1) if nm else f"object{len(objects) + 1}")
    if not verts:
        raise InvalidInput(f"{path.name}: the 3MF package has no triangle mesh.")
    return np.vstack(verts), np.vstack(faces), objects, unit


def _gltf_generator(path: Path) -> str:
    import json
    import struct

    try:
        raw = path.read_bytes()
        if path.suffix.lower() == ".glb" and raw[:4] == b"glTF":
            length = struct.unpack("<I", raw[12:16])[0]
            return str(json.loads(raw[20 : 20 + length]).get("asset", {}).get("generator") or "")
        return str(json.loads(raw).get("asset", {}).get("generator") or "")
    except Exception:  # noqa: BLE001 - a missing generator only changes the up-axis guess
        return ""


def load(path: str | Path, *, units: str | None = None, up: str | None = None) -> tuple[np.ndarray, np.ndarray, dict]:
    """Triangles in millimetres (Z up) and notes about the source."""

    import trimesh

    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in MESH_SUFFIXES:
        raise InvalidInput(f"Unsupported mesh format {suffix}; use STL, OBJ, GLB/glTF, PLY, OFF or 3MF.")
    units = UNIT_ALIASES.get(units or "", units)
    if units and units not in UNIT_TO_MM:
        raise InvalidInput(f"units must be one of {', '.join(sorted(UNIT_TO_MM))} (or mm, cm, m, in).")
    if up and up not in ("y", "z"):
        raise InvalidInput("up must be y or z.")
    info: dict = {"format": suffix.lstrip("."), "file": path.name}
    objects: list[str] = []
    if suffix == ".3mf":
        V, F, objects, factor = _load_3mf(path)
        info["unit_source"] = "3MF unit attribute"
    else:
        try:
            mesh = trimesh.load(str(path), force="mesh", process=False)
        except Exception as exc:  # noqa: BLE001 - unreadable or empty file
            raise InvalidInput(f"{path.name}: cannot read the mesh ({type(exc).__name__}: {str(exc)[:160]}).") from exc
        if mesh is None or getattr(mesh, "is_empty", True) or len(getattr(mesh, "faces", [])) == 0:
            raise InvalidInput(f"{path.name}: the file has no triangles.")
        V, F = np.asarray(mesh.vertices, dtype=float), np.asarray(mesh.faces, dtype=np.int64)
        factor = 1.0
        info["unit_source"] = "assumed millimetres"
        if suffix in (".glb", ".gltf"):
            if "trimesh" in _gltf_generator(path).lower():
                info["unit_source"] = "as written (trimesh-made glTF, e.g. a DesignAround export)"
            else:
                factor, info["unit_source"] = 1000.0, "glTF metres"
    if units:
        factor, info["unit_source"] = UNIT_TO_MM[units], f"given: {units}"
    V = V * factor
    info["unit_factor"] = factor
    if suffix in (".glb", ".gltf"):
        generator = _gltf_generator(path)
        # glTF is Y-up; files written with trimesh (DesignAround's own GLB among them) keep Z up.
        y_up = up == "y" or (up is None and "trimesh" not in generator.lower())
        if y_up:
            V = np.stack([V[:, 0], -V[:, 2], V[:, 1]], axis=1)  # proper rotation: winding is kept
        info["up"] = "y (glTF convention, turned to Z-up)" if y_up else "z (kept)"
        info["generator"] = generator[:80] or None
    elif up == "y":
        V = np.stack([V[:, 0], -V[:, 2], V[:, 1]], axis=1)
        info["up"] = "y (turned to Z-up)"
    if not np.isfinite(V).all():
        raise InvalidInput(f"{path.name}: the mesh has invalid coordinates (NaN or infinite).")
    size = float(np.ptp(V, axis=0).max())
    if size <= 0:
        raise InvalidInput(f"{path.name}: the mesh has no extent.")
    info["size_mm"] = round(size, 3)
    if size < 5 or size > 3000:
        info["unit_warning"] = f"The model is {size:.4g} mm across. If that is wrong, import again with --units (mm, cm, m, inch)."
    info["objects"] = objects
    return V, F, info


# -------------------------------------------------------------------- repair


def _merge(V: np.ndarray, F: np.ndarray, tol: float) -> tuple[np.ndarray, np.ndarray, int]:
    keys = np.round(V / tol).astype(np.int64)
    _, first, inverse = np.unique(keys, axis=0, return_index=True, return_inverse=True)
    return V[first], inverse.reshape(-1)[F], len(V) - len(first)


def _areas(V: np.ndarray, F: np.ndarray) -> np.ndarray:
    return 0.5 * np.linalg.norm(np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]]), axis=1)


def _clean_faces(V: np.ndarray, F: np.ndarray, area_eps: float) -> tuple[np.ndarray, int, int]:
    distinct = (F[:, 0] != F[:, 1]) & (F[:, 1] != F[:, 2]) & (F[:, 0] != F[:, 2])
    keep = distinct & (_areas(V, F) > area_eps)
    degenerate = int((~keep).sum())
    F = F[keep]
    _, idx = np.unique(np.sort(F, axis=1), axis=0, return_index=True)
    duplicates = len(F) - len(idx)
    return F[np.sort(idx)], degenerate, duplicates


def _compact(V: np.ndarray, F: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    used = np.unique(F)
    remap = -np.ones(len(V), dtype=np.int64)
    remap[used] = np.arange(len(used))
    return V[used], remap[F]


def components(F: np.ndarray, n_vertices: int) -> np.ndarray:
    """Connected-component label per face (faces sharing an edge are connected)."""

    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    e = np.sort(np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]]), axis=1)
    face_of = np.tile(np.arange(len(F)), 3)
    _, edge_id = np.unique(e, axis=0, return_inverse=True)
    edge_id = edge_id.reshape(-1)
    incidence = coo_matrix((np.ones(len(face_of), dtype=np.int8), (face_of, edge_id)), shape=(len(F), int(edge_id.max()) + 1)).tocsr()
    _n, labels = connected_components(incidence @ incidence.T, directed=False)
    return labels


def _edge_counts(F: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    e = np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]])
    _, inverse, counts = np.unique(np.sort(e, axis=1), axis=0, return_inverse=True, return_counts=True)
    return e, inverse.reshape(-1), counts


def _boundary_loops(F: np.ndarray) -> list[list[int]]:
    """Simple closed vertex loops along edges used by exactly one face, in the faces' direction.

    Two holes that touch at one vertex form a figure-8 border; when the walk comes
    back to a vertex already on the current path, that sub-loop is cut off as its
    own hole. Chains that do not close (non-manifold borders) are left open.
    """

    e, inverse, counts = _edge_counts(F)
    single = e[counts[inverse] == 1]
    nxt: dict[int, list[int]] = {}
    for a, b in single:
        nxt.setdefault(int(a), []).append(int(b))
    loops: list[list[int]] = []
    for start in list(nxt):
        while nxt.get(start):
            path, pos, v = [start], {start: 0}, start
            while nxt.get(v):
                w = nxt[v].pop()
                if w in pos:  # closed a loop: cut it off and continue from w
                    i = pos[w]
                    if len(path) - i >= 3:
                        loops.append(path[i:])
                    for u in path[i + 1 :]:
                        del pos[u]
                    path = path[: i + 1]
                    v = w
                    continue
                pos[w] = len(path)
                path.append(w)
                v = w
    return loops


def _fill_holes(V: np.ndarray, F: np.ndarray) -> tuple[np.ndarray, np.ndarray, int, int]:
    """Fan-fill each small hole around its centroid (the patch winds against the border)."""

    filled, left = 0, 0
    new_v, new_f, count = [V], [F], len(V)
    for loop in _boundary_loops(F):
        if len(loop) > HOLE_MAX_EDGES:
            left += 1
            continue
        new_v.append(V[loop].mean(axis=0)[None, :])
        centre = count
        count += 1
        new_f.append(np.array([(loop[(i + 1) % len(loop)], loop[i], centre) for i in range(len(loop))], dtype=np.int64))
        filled += 1
    return np.vstack(new_v), np.vstack(new_f), filled, left


def _orient(V: np.ndarray, F: np.ndarray) -> tuple[np.ndarray, int]:
    """Consistent winding (flood fill over manifold edges), then outward per piece."""

    from collections import deque

    e, inverse, counts = _edge_counts(F)
    face_of = np.tile(np.arange(len(F)), 3)
    manifold = counts[inverse] == 2
    order = np.argsort(inverse[manifold], kind="stable")
    ids, faces_m, edges_m = inverse[manifold][order], face_of[manifold][order], e[manifold][order]
    neighbours: list[list[tuple[int, bool]]] = [[] for _ in range(len(F))]
    for k in range(0, len(ids) - 1, 2):
        fa, fb = int(faces_m[k]), int(faces_m[k + 1])
        same = bool((edges_m[k] == edges_m[k + 1]).all())  # consistent neighbours run a shared edge in opposite directions
        neighbours[fa].append((fb, same))
        neighbours[fb].append((fa, same))
    flip = np.zeros(len(F), dtype=bool)
    seen = np.zeros(len(F), dtype=bool)
    for seed in range(len(F)):
        if seen[seed]:
            continue
        seen[seed] = True
        queue = deque([seed])
        while queue:
            f = queue.popleft()
            for g, same in neighbours[f]:
                if not seen[g]:
                    seen[g] = True
                    flip[g] = flip[f] ^ same
                    queue.append(g)
    F = F.copy()
    F[flip] = F[flip][:, [0, 2, 1]]
    flipped = int(flip.sum())
    labels = components(F, len(V))
    for label in np.unique(labels):
        sel = labels == label
        tri = V[F[sel]]
        if np.einsum("ij,ij->i", tri[:, 0], np.cross(tri[:, 1], tri[:, 2])).sum() < 0:
            F[sel] = F[sel][:, [0, 2, 1]]
            flipped += int(sel.sum())
    return F, flipped


def _decimate(V: np.ndarray, F: np.ndarray, target: int) -> tuple[np.ndarray, np.ndarray]:
    import fast_simplification

    reduction = 1.0 - target / len(F)
    points, faces = fast_simplification.simplify(V.astype(np.float64), F.astype(np.int64), target_reduction=reduction)
    return np.asarray(points, dtype=float), np.asarray(faces, dtype=np.int64)


def repair(V: np.ndarray, F: np.ndarray, *, max_triangles: int = MAX_TRIANGLES) -> tuple[np.ndarray, np.ndarray, dict]:
    report: dict = {"triangles_in": int(len(F)), "vertices_in": int(len(V))}
    size = float(np.ptp(V, axis=0).max())
    V, F, merged = _merge(V, F, max(size * 1e-6, 1e-6))
    F, degenerate, duplicates = _clean_faces(V, F, (size * 1e-7) ** 2)
    report.update({"merged_vertices": merged, "degenerate_removed": degenerate, "duplicates_removed": duplicates})
    if len(F) == 0:
        raise InvalidInput("The mesh has no usable triangles after cleaning.")
    labels = components(F, len(V))
    piece_area = np.bincount(labels, weights=_areas(V, F))
    small = np.where(piece_area < LOOSE_SHARE * piece_area.sum())[0]
    if len(small):
        F = F[~np.isin(labels, small)]
    report["loose_pieces_removed"] = int(len(small))
    V, F = _compact(V, F)
    # Orient first: hole borders can only be traced when neighbouring faces agree on direction.
    F, flipped = _orient(V, F)
    V, F, filled, left = _fill_holes(V, F)
    F, _ = _orient(V, F)  # the patches join the surface; keep it outward
    report.update({"holes_filled": filled, "holes_left": left, "faces_flipped": flipped})
    if len(F) > max_triangles:
        V, F = _decimate(V, F, max_triangles)
        V, F, _ = _merge(V, F, max(size * 1e-6, 1e-6))
        F, _, _ = _clean_faces(V, F, (size * 1e-7) ** 2)
        V, F = _compact(V, F)
        F, _ = _orient(V, F)
        V, F, more, _ = _fill_holes(V, F)
        F, _ = _orient(V, F)
        report["decimated_to"] = int(len(F))
        report["holes_filled"] += more
    _e, inverse, counts = _edge_counts(F)
    report["noise_creases"] = int(len(noise_creases(V, F)[1]))
    report.update({
        "triangles_out": int(len(F)),
        "open_edges": int((counts == 1).sum()),
        "non_manifold_edges": int((counts > 2).sum()),
        "watertight": bool((counts == 2).all()),
        "pieces": int(len(np.unique(components(F, len(V))))),
    })
    return V, F, report


# ------------------------------------------------------------- mesh -> B-rep


def to_solids(V: np.ndarray, F: np.ndarray, names: list[str] | None = None) -> list[tuple[str, object]]:
    """One faceted solid (a shell when it cannot be closed) per connected piece, largest first."""

    import cadquery as cq
    from OCP.BRepBuilderAPI import (
        BRepBuilderAPI_MakeFace,
        BRepBuilderAPI_MakePolygon,
        BRepBuilderAPI_MakeSolid,
        BRepBuilderAPI_Sewing,
    )
    from OCP.BRepLib import BRepLib
    from OCP.gp import gp_Pnt
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
    from OCP.TopAbs import TopAbs_SHELL
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    labels = components(F, len(V))
    area = _areas(V, F)
    order = sorted(np.unique(labels), key=lambda lab: -area[labels == lab].sum())
    size = float(np.ptp(V, axis=0).max())
    noise = noise_creases(V, F)
    out, taken = [], set()
    for rank, label in enumerate(order):
        faces = F[labels == label]
        sewing = BRepBuilderAPI_Sewing(max(size * 1e-5, 1e-4))
        pts: dict[int, object] = {}
        for a, b, c in faces:
            for k in (int(a), int(b), int(c)):
                if k not in pts:
                    pts[k] = gp_Pnt(*map(float, V[k]))
            poly = BRepBuilderAPI_MakePolygon(pts[int(a)], pts[int(b)], pts[int(c)], True)
            if not poly.IsDone():
                continue
            face = BRepBuilderAPI_MakeFace(poly.Wire(), True)
            if face.IsDone():
                sewing.Add(face.Face())
        sewing.Perform()
        unify = ShapeUpgrade_UnifySameDomain(sewing.SewedShape(), True, True, False)
        unify.SetAngularTolerance(np.radians(0.5))
        unify.Build()
        shape = unify.Shape()
        exp = TopExp_Explorer(shape, TopAbs_SHELL)
        if exp.More():
            maker = BRepBuilderAPI_MakeSolid(TopoDS.Shell_s(exp.Current()))
            if maker.IsDone():
                shape = _checked_solid(maker.Solid())
        BRepLib.EncodeRegularity_s(shape, np.radians(SMOOTH_ANGLE))
        if noise is not None and len(noise[0]):
            _smooth_noise(shape, noise, size)
        base = (names[rank] if names and rank < len(names) else None) or ("body" if rank == 0 else f"piece{rank + 1}")
        name = re.sub(r"[^A-Za-z0-9_-]", "_", base)[:40] or f"piece{rank + 1}"
        while name in taken:
            name = f"{name}_{rank + 1}"
        taken.add(name)
        out.append((name, cq.Shape.cast(shape)))
    return out


def _checked_solid(solid):
    """The sewn triangles form a valid solid, but merging coplanar triangles into polygons can leave
    faces the shell check cannot orient (BRepCheck_UnorientableShape). ShapeFix_Solid re-orients
    them without moving geometry; the fixed solid is used only if it checks valid and keeps the volume."""
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.ShapeFix import ShapeFix_Solid
    from OCP.TopAbs import TopAbs_SOLID

    if BRepCheck_Analyzer(solid).IsValid():
        return solid

    def volume(shape) -> float:
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(shape, props)
        return abs(props.Mass())

    fix = ShapeFix_Solid(solid)
    fix.Perform()
    fixed = fix.Solid()
    before = volume(solid)
    if fixed.ShapeType() == TopAbs_SOLID and BRepCheck_Analyzer(fixed).IsValid() and abs(volume(fixed) - before) <= 1e-3 * max(before, 1e-9):
        return fixed
    return solid


NOISE_MAX_EDGES = 12  # a crease network with at most this many mesh edges ...
NOISE_MAX_SHARE = 0.15  # ... and shorter than this share of the model size is scan noise


def noise_creases(V: np.ndarray, F: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Midpoints and lengths of mesh edges that are sharp only because of noise.

    Real creases (box edges, rims, engraved features) join into long networks of
    sharp edges; scanner noise leaves short, isolated ones (measured: 79% of a
    noisy scan's sharp edges sit in networks of three edges or fewer, while CAD
    exports have none below 36).
    """

    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    e, inverse, counts = _edge_counts(F)
    face_of = np.tile(np.arange(len(F)), 3)
    normals = np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]])
    normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-30)
    order = np.argsort(inverse, kind="stable")
    ids, faces = inverse[order], face_of[order]
    pair = counts[ids] == 2
    ids, faces = ids[pair], faces[pair]
    if not len(ids):
        return np.zeros((0, 3)), np.zeros(0)
    eid, fa, fb = ids[0::2], faces[0::2], faces[1::2]
    angle = np.degrees(np.arccos(np.clip((normals[fa] * normals[fb]).sum(axis=1), -1.0, 1.0)))
    unique_edges = np.zeros((counts.size, 2), dtype=np.int64)
    unique_edges[inverse] = np.sort(e, axis=1)
    sharp = unique_edges[eid[angle > SMOOTH_ANGLE]]
    if not len(sharp):
        return np.zeros((0, 3)), np.zeros(0)
    length = np.linalg.norm(V[sharp[:, 0]] - V[sharp[:, 1]], axis=1)
    graph = coo_matrix((np.ones(len(sharp)), (sharp[:, 0], sharp[:, 1])), shape=(len(V), len(V)))
    _n, vertex_label = connected_components(graph, directed=False)
    net = vertex_label[sharp[:, 0]]
    net_edges = np.bincount(net)
    net_length = np.bincount(net, weights=length)
    size = float(np.ptp(V, axis=0).max())
    is_noise = (net_edges[net] <= NOISE_MAX_EDGES) & (net_length[net] < NOISE_MAX_SHARE * size)
    picked = sharp[is_noise]
    return (V[picked[:, 0]] + V[picked[:, 1]]) / 2.0, length[is_noise]


def _smooth_noise(shape, noise: tuple[np.ndarray, np.ndarray], size: float) -> int:
    """Mark B-rep edges that match noise creases as smooth (G1)."""

    from OCP.BRep import BRep_Builder, BRep_Tool
    from OCP.GeomAbs import GeomAbs_C0, GeomAbs_G1
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape
    from scipy.spatial import cKDTree

    mids, lengths = noise
    tree = cKDTree(mids)
    tol = max(size * 2e-4, 1e-4)
    amap = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, amap)
    builder = BRep_Builder()
    marked = 0
    for i in range(1, amap.Extent() + 1):
        ancestors = amap.FindFromIndex(i)
        if ancestors.Size() != 2:
            continue
        edge = TopoDS.Edge_s(amap.FindKey(i))
        f1, f2 = TopoDS.Face_s(ancestors.First()), TopoDS.Face_s(ancestors.Last())
        if BRep_Tool.Continuity_s(edge, f1, f2) != GeomAbs_C0:
            continue
        a = BRep_Tool.Pnt_s(TopExp.FirstVertex_s(edge))
        b = BRep_Tool.Pnt_s(TopExp.LastVertex_s(edge))
        pa, pb = np.array([a.X(), a.Y(), a.Z()]), np.array([b.X(), b.Y(), b.Z()])
        dist, j = tree.query((pa + pb) / 2.0)
        if dist <= tol and abs(lengths[j] - np.linalg.norm(pb - pa)) <= tol * 2:
            builder.Continuity(edge, f1, f2, GeomAbs_G1)
            marked += 1
    return marked


def contour_seams(shape) -> tuple[list, np.ndarray, np.ndarray]:
    """Smooth seams between two planar facets and both outward facet normals."""

    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_C0, GeomAbs_Plane
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    amap = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape.wrapped, TopAbs_EDGE, TopAbs_FACE, amap)
    normal_of: dict[int, np.ndarray | None] = {}

    def normal(face) -> np.ndarray | None:
        key = face.__hash__()
        if key not in normal_of:
            surface = BRepAdaptor_Surface(face, False)
            if surface.GetType() != GeomAbs_Plane:
                normal_of[key] = None
            else:
                d = surface.Plane().Axis().Direction()
                n = np.array([d.X(), d.Y(), d.Z()])
                normal_of[key] = -n if face.Orientation() == TopAbs_REVERSED else n
        return normal_of[key]

    rows, pairs = [], []
    index: dict[int, int] = {}
    face_normals: list[np.ndarray] = []

    def face_id(face) -> int | None:
        n = normal(face)
        if n is None:
            return None
        key = face.__hash__()
        if key not in index:
            index[key] = len(face_normals)
            face_normals.append(n)
        return index[key]

    for i in range(1, amap.Extent() + 1):
        ancestors = amap.FindFromIndex(i)  # list() on this is slow (milliseconds per edge); use First/Last
        if ancestors.Size() != 2:
            continue
        edge = TopoDS.Edge_s(amap.FindKey(i))
        f1, f2 = TopoDS.Face_s(ancestors.First()), TopoDS.Face_s(ancestors.Last())
        if BRep_Tool.Continuity_s(edge, f1, f2) == GeomAbs_C0:
            continue
        a, b = face_id(f1), face_id(f2)
        if a is None or b is None:
            continue
        rows.append((edge, f1, f2))
        pairs.append((a, b))
    if not rows:
        return rows, np.zeros((0, 3)), np.zeros((0, 3))
    normals = _smoothed(np.array(face_normals), np.array(pairs))
    pairs_arr = np.array(pairs)
    return rows, normals[pairs_arr[:, 0]], normals[pairs_arr[:, 1]]


def _smoothed(normals: np.ndarray, pairs: np.ndarray, rounds: int = 3) -> np.ndarray:
    """Average each facet normal with its neighbours across smooth seams.

    Noise makes neighbouring facets on a curved surface tilt back and forth, so
    the facing test flickers near the silhouette and leaves stubs beside the
    outline. A few averaging rounds make the sign change once, where the
    surface really turns away. Sharp creases are not crossed.
    """

    from scipy.sparse import coo_matrix

    n = len(normals)
    rows = np.concatenate([pairs[:, 0], pairs[:, 1]])
    cols = np.concatenate([pairs[:, 1], pairs[:, 0]])
    adjacency = coo_matrix((np.ones(len(rows)), (rows, cols)), shape=(n, n)).tocsr()
    out = normals.copy()
    for _ in range(rounds):
        out = out + adjacency @ out
        out /= np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-30)
    return out


_SEAMS: dict[int, tuple] = {}


def _seams_cached(shape) -> tuple:
    key = shape.wrapped.__hash__()
    if key not in _SEAMS:
        if len(_SEAMS) > 32:
            _SEAMS.clear()
        _SEAMS[key] = contour_seams(shape)
    return _SEAMS[key]


class ViewContours:
    """While active, the facet seams on this view's silhouette are sharp edges."""

    def __init__(self, parts, view) -> None:
        self.marks = []
        _x, _up, n = view.basis()
        for part in parts:
            if not getattr(part, "faceted", False):
                continue
            rows, n1, n2 = _seams_cached(part.shape)
            if not rows:
                continue
            turning = (n1 @ n) * (n2 @ n) < 0
            self.marks += [rows[i] for i in np.nonzero(turning)[0]]

    def __enter__(self):
        from OCP.BRep import BRep_Builder
        from OCP.GeomAbs import GeomAbs_C0

        builder = BRep_Builder()
        for edge, f1, f2 in self.marks:
            builder.Continuity(edge, f1, f2, GeomAbs_C0)
        return self

    def __exit__(self, *exc):
        from OCP.BRep import BRep_Builder
        from OCP.GeomAbs import GeomAbs_G1

        builder = BRep_Builder()
        for edge, f1, f2 in self.marks:
            builder.Continuity(edge, f1, f2, GeomAbs_G1)
        return False


def curved_share(parts) -> float:
    """Share of the surface on small facets (curved regions of a faceted model)."""

    total = curved = 0.0
    for part in parts:
        faces = part.shape.Faces()
        areas = np.array([f.Area() for f in faces])
        if not len(areas):
            continue
        total += areas.sum()
        curved += areas[areas < 0.01 * areas.sum()].sum()
    return curved / total if total else 0.0
