"""Loading parts, tessellation and mesh export (STL / 3MF / GLB)."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .errors import NotFound


@dataclass
class Part:
    name: str
    shape: object  # cadquery.Shape
    claimed: bool = True
    faceted: bool = False  # made of mesh facets (imported STL/OBJ/GLB/3MF), see meshimport


def _import_step(path: Path):
    import cadquery as cq

    workplane = cq.importers.importStep(str(path))
    vals = [v for v in workplane.vals() if isinstance(v, cq.Shape)]
    if not vals:
        raise NotFound(f"No geometry in {path.name}")
    return vals[0] if len(vals) == 1 else cq.Compound.makeCompound(vals)


def load_version_parts(version_dir: str | os.PathLike, *, part_rows: list[dict] | None = None) -> list[Part]:
    version_dir = Path(version_dir)
    if part_rows is None:
        manifest_path = version_dir / "manifest.json"
        if not manifest_path.exists():
            raise NotFound(f"No manifest.json in {version_dir}")
        part_rows = json.loads(manifest_path.read_text(encoding="utf-8"))["parts"]
    parts = []
    for row in part_rows:
        path = version_dir / row["file"]
        if path.suffix.lower() == ".brep":  # faceted parts: BREP reloads ~20x faster than STEP
            import cadquery as cq

            shape = cq.Shape.importBrep(str(path))
        else:
            shape = _import_step(path)
        parts.append(Part(name=row["name"], shape=shape, claimed=bool(row.get("claimed", True)), faceted=bool(row.get("faceted"))))
    return parts


def import_cad_file(path: str | os.PathLike) -> list[Part]:
    """Import a user CAD file (path B). STEP/IGES become B-rep parts; meshes stay meshes."""

    import cadquery as cq

    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in {".step", ".stp"}:
        return [Part(name=path.stem or "body", shape=_import_step(path))]
    if suffix in {".brep"}:
        return [Part(name=path.stem or "body", shape=cq.importers.importBrep(str(path)).val())]
    if suffix in {".iges", ".igs"}:
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.IGESControl import IGESControl_Reader

        reader = IGESControl_Reader()
        if reader.ReadFile(str(path)) != IFSelect_RetDone:
            raise NotFound(f"Cannot read the IGES file {path.name}.")
        reader.TransferRoots()
        shape = reader.OneShape()
        if shape.IsNull():
            raise NotFound(f"No geometry in {path.name}")
        return [Part(name=re.sub(r"[^A-Za-z0-9_-]", "_", path.stem)[:40] or "body", shape=cq.Shape.cast(shape))]
    raise NotFound(f"Unsupported CAD format {suffix}; meshes (STL/OBJ/GLB/3MF) are imported with api.import_mesh().")


def tessellate(shape, *, tolerance: float | None = None, angular: float = 0.35) -> tuple[np.ndarray, np.ndarray]:
    """Triangles of a B-rep, read face by face with indexed access.

    cadquery's Shape.tessellate iterates OCP collections in Python, which costs
    milliseconds per face: fine for a CAD model with a few hundred faces, minutes
    for an imported mesh with thousands of facets. Same triangulation, same
    orientation rules.
    """

    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    bb = shape.BoundingBox()
    size = max(bb.xlen, bb.ylen, bb.zlen, 1e-6)
    tol = tolerance if tolerance is not None else max(size * 0.002, 0.05)
    if not BRepTools.Triangulation_s(shape.wrapped, tol):
        BRepMesh_IncrementalMesh(shape.wrapped, tol, True, angular)
    verts: list[np.ndarray] = []
    tris: list[np.ndarray] = []
    offset = 0
    explorer = TopExp_Explorer(shape.wrapped, TopAbs_FACE)
    while explorer.More():
        face = TopoDS.Face_s(explorer.Current())
        explorer.Next()
        loc = TopLoc_Location()
        poly = BRep_Tool.Triangulation_s(face, loc)
        if poly is None:
            continue
        trsf = loc.Transformation()
        identity = loc.IsIdentity()
        nodes = []
        for i in range(1, poly.NbNodes() + 1):
            p = poly.Node(i) if identity else poly.Node(i).Transformed(trsf)
            nodes.append((p.X(), p.Y(), p.Z()))
        reverse = face.Orientation() == TopAbs_REVERSED
        faces = []
        for i in range(1, poly.NbTriangles() + 1):
            t = poly.Triangle(i)
            a, b, c = t.Value(1), t.Value(2), t.Value(3)
            faces.append((a, c, b) if reverse else (a, b, c))
        if not nodes or not faces:
            continue
        verts.append(np.asarray(nodes, dtype=np.float64))
        tris.append(np.asarray(faces, dtype=np.int64) - 1 + offset)
        offset += len(nodes)
    if not tris:
        return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64)
    return np.vstack(verts), np.vstack(tris)


def combined_mesh(parts: list[Part], *, tolerance: float | None = None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Merge all parts. Returns (V, F, part_index_per_face)."""

    all_v, all_f, owners = [], [], []
    offset = 0
    for index, part in enumerate(parts):
        V, F = tessellate(part.shape, tolerance=tolerance)
        if len(F) == 0:
            continue
        all_v.append(V)
        all_f.append(F + offset)
        owners.append(np.full(len(F), index, dtype=np.int64))
        offset += len(V)
    if not all_f:
        return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64), np.zeros(0, dtype=np.int64)
    return np.vstack(all_v), np.vstack(all_f), np.concatenate(owners)


def to_trimesh(V: np.ndarray, F: np.ndarray):
    import trimesh

    return trimesh.Trimesh(vertices=V, faces=F, process=True)


def _part_mesh(V, F, owners, index: int):
    """Vertices and faces of one part, re-indexed (empty arrays if the part has no faces)."""

    mask = owners == index
    sub_faces = F[mask]
    used = np.unique(sub_faces)
    remap = -np.ones(len(V), dtype=np.int64)
    remap[used] = np.arange(len(used))
    return V[used], remap[sub_faces]


def write_3mf(path: str | os.PathLike, objects: list[tuple[str, np.ndarray, np.ndarray]]) -> None:
    """3MF core package (millimetres), one object and build item per part.

    Written directly (a zip with three XML parts) so it needs no optional exporter
    dependencies; slicers read it with units, unlike STL.
    """

    import zipfile
    from xml.sax.saxutils import quoteattr

    body = ['<?xml version="1.0" encoding="UTF-8"?>', '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">', "<resources>"]
    items = []
    oid = 0
    for name, verts, faces in objects:
        if len(faces) == 0:
            continue
        oid += 1
        body.append(f'<object id="{oid}" type="model" name={quoteattr(str(name))}><mesh><vertices>')
        body.extend(f'<vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>' for x, y, z in np.asarray(verts, dtype=float))
        body.append("</vertices><triangles>")
        body.extend(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in np.asarray(faces, dtype=np.int64))
        body.append("</triangles></mesh></object>")
        items.append(f'<item objectid="{oid}"/>')
    if not items:
        raise ValueError("no geometry to write to 3MF")
    body += ["</resources>", "<build>", *items, "</build>", "</model>"]
    content_types = '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>'
    rels = '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>'
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("3D/3dmodel.model", "\n".join(body))


def export_meshes(parts: list[Part], outdir: str | os.PathLike) -> dict[str, str]:
    """Write model.stl (print), model.3mf (print, with units) and model.glb (viewer)."""

    import trimesh

    outdir = Path(outdir)
    V, F, owners = combined_mesh(parts)
    mesh = trimesh.Trimesh(vertices=V, faces=F, process=True)
    files = {}
    mesh.export(outdir / "model.stl")
    files["model.stl"] = "model.stl"
    write_3mf(outdir / "model.3mf", [(part.name, *_part_mesh(V, F, owners, index)) for index, part in enumerate(parts)])
    files["model.3mf"] = "model.3mf"
    scene = trimesh.Scene()
    palette = [(186, 194, 204, 255), (120, 160, 220, 255), (225, 170, 110, 255), (150, 200, 150, 255)]
    for index, part in enumerate(parts):
        verts, faces = _part_mesh(V, F, owners, index)
        if not len(faces):
            continue
        sub = trimesh.Trimesh(vertices=verts, faces=faces, process=False)
        color = palette[index % len(palette)] if part.claimed else (210, 210, 210, 120)
        sub.visual.face_colors = np.tile(np.array(color, dtype=np.uint8), (len(sub.faces), 1))
        scene.add_geometry(sub, node_name=part.name, geom_name=part.name)
    scene.export(outdir / "model.glb")
    files["model.glb"] = "model.glb"
    return files


def load_mesh_file(path: str | os.PathLike):
    import trimesh

    mesh = trimesh.load(str(path), force="mesh")
    if mesh.is_empty:
        raise NotFound(f"Mesh {path} is empty")
    return mesh
