"""High-level engine operations. The CLI, the JSON-RPC bridge used by the
embedded Pi Agent team, and the desktop app all call these functions.

Every function returns JSON-serialisable data and records an evidence event.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from designaround_core import quote as quote_mod

from . import cad, compare, dfm, filing, originality, photos, risk, viewpack, views
from .camera import STANDARD_VIEWS
from .errors import InvalidInput, NotFound
from .evidence import manifest_for_dir, sha256_file
from .templates import get_template_meta, get_template_source, list_templates, program_meta
from .workspace import create_case, list_cases, open_case


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------- cases


def case_create(name: str, *, article: str | None = None, article_zh: str | None = None, category: str | None = None, jurisdictions: list[str] | None = None, platforms: list[str] | None = None) -> dict:
    case = create_case(name, article=article, article_zh=article_zh, category=category, jurisdictions=jurisdictions, platforms=platforms)
    return case.summary()


def case_list() -> list[dict]:
    return list_cases()


def case_show(case_id: str) -> dict:
    case = open_case(case_id)
    data = case.summary()
    ok, bad = case.log.verify()
    data["evidence"] = {"events": len(case.log.read()), "chain_ok": ok, "first_bad_index": bad}
    return data


def case_update(case_id: str, **fields) -> dict:
    allowed = {"applicant", "inventors", "constraints", "article", "article_zh", "use_zh", "design_points_zh", "jurisdictions", "category", "server_case_id", "status", "known_dimension", "description", "locarno", "brief"}
    unknown = set(fields) - allowed
    if unknown:
        raise InvalidInput(f"Unknown case fields: {', '.join(sorted(unknown))}")
    case = open_case(case_id)
    meta = case.update(**fields)
    case.event("case.updated", {"fields": sorted(fields)})
    return meta


def decision(case_id: str, decision_text: str, detail: dict | None = None, person: str | None = None) -> dict:
    """Record a human decision (selection, constraint, tweak). Feeds the human-contribution record."""

    case = open_case(case_id)
    payload = {"actor": "user", "decision": decision_text, "detail": detail or {}, "person": person, "ts_iso": _now_iso()}
    record = case.event("decision", payload)
    return {"recorded": True, "hash": record["hash"]}


def decisions(case_id: str) -> list[dict]:
    case = open_case(case_id)
    return [e["payload"] for e in case.log.read() if e["type"] == "decision"]


# ----------------------------------------------------------------- versions


def templates() -> list[dict]:
    return list_templates()


def _version_entry(case, vid: str) -> dict:
    meta = case.version_meta(vid)
    manifest = cad.read_manifest(case.version_path(vid))
    return {
        **meta,
        "params": manifest.get("params"),
        "param_schema": manifest.get("param_schema"),
        "parts": [{k: p[k] for k in ("name", "claimed", "valid", "volume_mm3")} for p in manifest["parts"]],
        "bbox": manifest["bbox"],
        "path": str(case.version_path(vid)),
    }


def _finish_version(case, vid: str, *, kind: str, parent: str | None, created_by: str, label: str | None, ops: list | None, rationale: str | None, extra: dict | None = None, render: bool = True) -> dict:
    meta = {
        "id": vid,
        "kind": kind,
        "parent": parent,
        "label": label or vid,
        "created_by": created_by,
        "created": time.time(),
        "ops": ops or [],
        "rationale": rationale,
        **(extra or {}),
    }
    case.write_version_meta(vid, meta)
    path = case.version_path(vid)
    if render:
        views.render_views(path)
    manifest = cad.read_manifest(path)
    case.event(
        "version.created",
        {
            "version": vid,
            "kind": kind,
            "parent": parent,
            "created_by": created_by,
            "ops": ops or [],
            "files": {k: v["sha256"] for k, v in manifest["files"].items()},
        },
    )
    return _version_entry(case, vid)


def version_from_template(case_id: str, template_id: str, params: dict | None = None, *, created_by: str = "user", label: str | None = None) -> dict:
    case = open_case(case_id)
    vid = case.new_version_id()
    source = get_template_source(template_id)
    cad.build_program(source, params, case.versions_dir() / vid, provenance={"template": template_id})
    kind = "original" if not case.original_version() else "candidate"
    meta = get_template_meta(template_id)
    if not case.meta.get("article_zh") and meta.get("article_zh"):
        case.update(article_zh=meta["article_zh"])
    return _finish_version(case, vid, kind=kind, parent=None, created_by=created_by, label=label, ops=[{"op": "from_template", "template": template_id, "params": params or {}}], rationale=None, extra={"template": template_id})


def version_from_program(case_id: str, source: str, params: dict | None = None, *, parent: str | None = None, kind: str | None = None, created_by: str = "agent", label: str | None = None, rationale: str | None = None, ops: list | None = None, concept: str | None = None, review: dict | None = None) -> dict:
    case = open_case(case_id)
    if kind == "original" and case.original_version():
        # A new original (e.g. built from a chosen concept) replaces the previous baseline.
        entry = version_from_program(case_id, source, params, parent=parent, kind="candidate", created_by=created_by, label=label, rationale=rationale, ops=ops, concept=concept, review=review)
        meta = case.version_meta(entry["id"])
        meta["kind"] = "original"
        case.write_version_meta(entry["id"], meta)
        set_original(case_id, entry["id"], reason=f"built from concept {concept}" if concept else rationale)
        return _version_entry(case, entry["id"])
    vid = case.new_version_id()
    try:
        cad.build_program(source, params, case.versions_dir() / vid, provenance={"parent": parent, "created_by": created_by})
    except Exception:
        shutil.rmtree(case.versions_dir() / vid, ignore_errors=True)
        raise
    if kind is None:
        kind = "original" if not case.original_version() else "candidate"
    if kind not in ("original", "candidate", "draft"):
        raise InvalidInput(f"Unknown version kind {kind!r} (original, candidate or draft).")
    meta = program_meta(source)
    extra = {"template": meta.get("id")}
    if concept:
        extra["concept"] = concept
    if review:
        extra["review"] = review
    return _finish_version(case, vid, kind=kind, parent=parent, created_by=created_by, label=label, ops=ops or [{"op": "program", "params": params or {}}], rationale=rationale, extra=extra)


def try_program(source: str, params: dict | None = None, *, scratch: str | None = None, renders: bool = False, lineart: bool = True) -> dict:
    """Build without saving a version: used by the CAD Agent while iterating.

    renders=True also writes shaded views and a render sheet, which the independent
    3D reviewer compares with the chosen concept image. lineart=False skips the line
    drawings (needs renders=True); contact_sheet is then the render sheet.
    """

    from .workspace import home

    if not lineart and not renders:
        raise InvalidInput("try_program needs line art or renders")
    root = Path(scratch) if scratch else home() / "scratch" / f"t{int(time.time() * 1000)}"
    manifest = cad.build_program(source, params, root)
    # Iteration speed matters more than exact curves here; saved versions get the full budget.
    views.render_views(root, renders=renders, size=800, hlr_budget_s=float(os.environ.get("DA_TRY_HLR_SECONDS", "15")), lineart=lineart)
    out = {"path": str(root), "bbox": manifest["bbox"], "parts": manifest["parts"], "params": manifest["params"], "warnings": manifest["warnings"]}
    if renders:
        out["render_sheet"] = contact_sheet(root, out=root / "contact-render.png", kind="render")
    out["contact_sheet"] = contact_sheet(root, out=root / "contact.png") if lineart else out["render_sheet"]
    return out


def model_image(path: str, *, max_side: int = 1024, quality: int = 85) -> dict:
    """A JPEG copy of an image, at most max_side px, for sending to a vision model.

    Concept images are ~1 MB PNGs and are resent with every turn of an agent session;
    the copy is ~10x smaller with no visible loss at the size models look at.
    Cached by content hash under <home>/scratch/model-images/.
    """

    from .workspace import home

    src = Path(path)
    if not src.is_file():
        raise NotFound(f"Image not found: {path}")
    digest = sha256_file(src)[:24]
    out = home() / "scratch" / "model-images" / f"{digest}-{max_side}.jpg"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        image = Image.open(src)
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGBA")
            flat = Image.new("RGB", image.size, "white")
            flat.paste(image, mask=image.getchannel("A"))
            image = flat
        else:
            image = image.convert("RGB")
        image.thumbnail((max_side, max_side))
        tmp = out.with_suffix(".tmp")
        image.save(tmp, "JPEG", quality=quality, optimize=True)
        tmp.replace(out)
    return {"path": str(out), "bytes": out.stat().st_size}


def accept_version(case_id: str, vid: str, *, person: str | None = None, note: str | None = None) -> dict:
    """The person accepts a version (e.g. a 3D draft the reviewer did not accept) as the original.

    Recorded as a human decision: the reviewer's verdict stays in the version's metadata.
    """

    case = open_case(case_id)
    if vid not in case.version_ids():
        raise NotFound(f"Version {vid} not found")
    previous = case.original_version()
    meta = case.version_meta(vid)
    previous_kind = meta.get("kind")
    meta["kind"] = "original"
    meta["accepted_by"] = {"person": person, "note": note, "previous_kind": previous_kind, "ts_iso": _now_iso()}
    case.write_version_meta(vid, meta)
    set_original(case_id, vid, reason=f"accepted by {person or 'the user'}")
    decision(case_id, f"accepted {vid} as the original design", {"version": vid, "previous_kind": previous_kind, "previous_original": previous, "review": meta.get("review"), "note": note}, person=person)
    return {**_version_entry(case, vid), "previous": previous}


def set_original(case_id: str, vid: str, *, reason: str | None = None) -> dict:
    """Make a version the case's original design (the baseline for screening and design-around)."""

    case = open_case(case_id)
    if vid not in case.version_ids():
        raise NotFound(f"Version {vid} not found")
    previous = case.original_version()
    case.update(original=vid)
    case.event("original.set", {"version": vid, "previous": previous, "reason": reason})
    return {"original": vid, "previous": previous}


def tweak(case_id: str, vid: str, overrides: dict, *, created_by: str = "user", label: str | None = None, rationale: str | None = None) -> dict:
    case = open_case(case_id)
    base = case.version_path(vid)
    if not (base / "program.py").exists():
        raise InvalidInput(f"Version {vid} has no parametric program (imported mesh/STEP); tweaks need a CAD program.")
    new = case.new_version_id()
    cad.rebuild_with(base, overrides, case.versions_dir() / new, provenance={"parent": vid})
    ops = [{"op": "set_param", "name": k, "value": v} for k, v in overrides.items()]
    result = _finish_version(case, new, kind="candidate", parent=vid, created_by=created_by, label=label, ops=ops, rationale=rationale)
    if created_by == "user":
        decision(case_id, "parameter tweak", {"from": vid, "to": new, "set": overrides})
    return result


def import_cad(case_id: str, path: str, *, label: str | None = None) -> dict:
    """Path B: import STEP/IGES/BREP as the original (no program; tweaks need the CAD Agent to rebuild)."""

    import cadquery as cq

    from .geometry import export_meshes, import_cad_file

    case = open_case(case_id)
    vid = case.new_version_id()
    root = case.versions_dir() / vid
    (root / "parts").mkdir(parents=True)
    parts = import_cad_file(path)
    rows = []
    for part in parts:
        rel = f"parts/{part.name}.step"
        cq.exporters.export(cq.Workplane("XY").add(part.shape), str(root / rel))
        bb = part.shape.BoundingBox()
        rows.append({"name": part.name, "file": rel, "claimed": True, "valid": bool(part.shape.isValid()), "volume_mm3": round(float(part.shape.Volume()), 3), "bbox": {"xmin": bb.xmin, "ymin": bb.ymin, "zmin": bb.zmin, "xmax": bb.xmax, "ymax": bb.ymax, "zmax": bb.zmax}})
    shutil.copyfile(path, root / ("source" + Path(path).suffix.lower()))
    mesh_files = export_meshes(parts, root)
    bb = parts[0].shape.BoundingBox() if len(parts) == 1 else cq.Compound.makeCompound([p.shape for p in parts]).BoundingBox()
    manifest = {
        "schema_version": 1,
        "representation": "imported_brep",
        "params": {},
        "param_schema": {},
        "warnings": [],
        "parts": rows,
        "bbox": {"xmin": bb.xmin, "ymin": bb.ymin, "zmin": bb.zmin, "xmax": bb.xmax, "ymax": bb.ymax, "zmax": bb.zmax, "xlen": bb.xlen, "ylen": bb.ylen, "zlen": bb.zlen},
        "volume_mm3": round(sum(r["volume_mm3"] for r in rows), 3),
        "files": {name: {"path": rel, "sha256": sha256_file(root / rel)} for name, rel in {**mesh_files, **{r["file"]: r["file"] for r in rows}}.items()},
        "provenance": {"imported_from": Path(path).name, "source_sha256": sha256_file(path)},
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    kind = "original" if not case.original_version() else "candidate"
    return _finish_version(case, vid, kind=kind, parent=None, created_by="user", label=label, ops=[{"op": "import_cad", "file": Path(path).name}], rationale=None)


def import_mesh(case_id: str, path: str, *, label: str | None = None, units: str | None = None, up: str | None = None) -> dict:
    """Path B for meshes (FR-3D-10): STL / OBJ / GLB / glTF / PLY / OFF / 3MF, repaired, as a faceted version."""

    from . import meshimport
    from .geometry import Part, export_meshes

    case = open_case(case_id)
    src = Path(path)
    if not src.is_file():
        raise NotFound(f"File not found: {path}")
    V, F, info = meshimport.load(src, units=units, up=up)
    V, F, report = meshimport.repair(V, F)
    solids = meshimport.to_solids(V, F)
    vid = case.new_version_id()
    root = case.versions_dir() / vid
    (root / "parts").mkdir(parents=True)
    rows, parts = [], []
    for name, shape in solids:
        rel = f"parts/{name}.brep"
        shape.exportBrep(str(root / rel))
        bb = shape.BoundingBox()
        rows.append({"name": name, "file": rel, "claimed": True, "faceted": True, "solid": shape.ShapeType() == "Solid", "valid": bool(shape.isValid()), "faces": len(shape.Faces()), "volume_mm3": round(abs(float(shape.Volume())), 3), "bbox": {"xmin": bb.xmin, "ymin": bb.ymin, "zmin": bb.zmin, "xmax": bb.xmax, "ymax": bb.ymax, "zmax": bb.zmax}})
        parts.append(Part(name=name, shape=shape, faceted=True))
    shutil.copyfile(src, root / ("source" + src.suffix.lower()))
    mesh_files = export_meshes(parts, root)
    lo, hi = V.min(axis=0), V.max(axis=0)
    warnings = [info["unit_warning"]] if info.get("unit_warning") else []
    if not report["watertight"]:
        warnings.append(f"The mesh is still open after repair ({report['open_edges']} open edges, {report['holes_left']} large holes left): wall-thickness and volume checks are approximate; fix the model or re-export it closed for 3D printing.")
    if report.get("decimated_to"):
        warnings.append(f"Simplified from {report['triangles_in']} to {report['decimated_to']} triangles for drawings and checks; the original file is kept as source{src.suffix.lower()}.")
    manifest = {
        "schema_version": 1,
        "representation": "mesh",
        "params": {},
        "param_schema": {},
        "warnings": warnings,
        "parts": rows,
        "bbox": {"xmin": float(lo[0]), "ymin": float(lo[1]), "zmin": float(lo[2]), "xmax": float(hi[0]), "ymax": float(hi[1]), "zmax": float(hi[2]), "xlen": float(hi[0] - lo[0]), "ylen": float(hi[1] - lo[1]), "zlen": float(hi[2] - lo[2])},
        "volume_mm3": round(sum(r["volume_mm3"] for r in rows), 3),
        "files": {name: {"path": rel, "sha256": sha256_file(root / rel)} for name, rel in {**mesh_files, **{r["file"]: r["file"] for r in rows}}.items()},
        "provenance": {"imported_from": src.name, "source_sha256": sha256_file(src), "mesh": info, "repair": report},
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    kind = "original" if not case.original_version() else "candidate"
    result = _finish_version(case, vid, kind=kind, parent=None, created_by="user", label=label, ops=[{"op": "import_mesh", "file": src.name, "units": info.get("unit_source"), "up": info.get("up"), "repair": report}], rationale=None)
    return {**result, "repair": report, "source": info, "warnings": warnings}


def import_model_file(case_id: str, path: str, *, label: str | None = None, units: str | None = None, up: str | None = None) -> dict:
    """CAD (STEP/IGES/BREP) or mesh, chosen by the file extension."""

    from .meshimport import MESH_SUFFIXES

    if Path(path).suffix.lower() in MESH_SUFFIXES:
        return import_mesh(case_id, path, label=label, units=units, up=up)
    return import_cad(case_id, path, label=label)


def set_claimed(case_id: str, vid: str, part: str, claimed: bool) -> dict:
    case = open_case(case_id)
    path = case.version_path(vid)
    manifest = cad.read_manifest(path)
    names = [p["name"] for p in manifest["parts"]]
    if part not in names:
        raise NotFound(f"Part '{part}' not in {vid}", details={"parts": names})
    for row in manifest["parts"]:
        if row["name"] == part:
            row["claimed"] = bool(claimed)
    (path / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    views.render_views(path)
    decision(case_id, "set claimed scope", {"version": vid, "part": part, "claimed": claimed})
    return _version_entry(case, vid)


def versions(case_id: str) -> list[dict]:
    case = open_case(case_id)
    return [_version_entry(case, vid) for vid in case.version_ids()]


def version_show(case_id: str, vid: str) -> dict:
    case = open_case(case_id)
    data = _version_entry(case, vid)
    eval_path = case.evaluation_path(vid)
    if eval_path.exists():
        data["evaluation"] = json.loads(eval_path.read_text(encoding="utf-8"))
    return data


def contact_sheet(version_dir: str | os.PathLike, *, out: str | os.PathLike | None = None, kind: str = "lineart", cell: int = 380) -> str:
    """7-view grid image used as tool output for vision models and as a preview."""

    version_dir = Path(version_dir)
    order = ["perspective", "front", "rear", "left", "right", "top", "bottom"]
    cols, rows = 4, 2
    sheet = Image.new("RGB", (cols * cell, rows * (cell + 22)), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for i, key in enumerate(order):
        path = version_dir / "views" / f"{key}.{kind}.png"
        if not path.exists():
            continue
        image = Image.open(path).convert("RGB")
        image.thumbnail((cell - 8, cell - 8))
        x, y = (i % cols) * cell, (i // cols) * (cell + 22)
        sheet.paste(image, (x + (cell - image.width) // 2, y + 22 + (cell - image.height) // 2))
        draw.text((x + 6, y + 4), key, fill=(0, 0, 0), font=font)
    out = Path(out) if out else version_dir / "views" / f"contact.{kind}.png"
    sheet.save(out)
    return str(out)


def compare_versions(case_id: str, a: str, b: str) -> dict:
    case = open_case(case_id)
    pa, pb = case.version_path(a), case.version_path(b)
    ma, mb = cad.read_manifest(pa), cad.read_manifest(pb)
    sims = {}
    masks_a, masks_b = views.view_masks(pa), views.view_masks(pb)
    for key in STANDARD_VIEWS:
        if masks_a.get(key) is not None and masks_b.get(key) is not None:
            sims[key] = compare.similarity(masks_a[key], masks_b[key])["score"]
    ea, eb = case.evaluation_path(a), case.evaluation_path(b)
    return {
        "a": a,
        "b": b,
        "changed_params": originality.changed_params(ma.get("params", {}), mb.get("params", {})),
        "view_similarity": sims,
        "risk_a": json.loads(ea.read_text())["summary"] if ea.exists() else None,
        "risk_b": json.loads(eb.read_text())["summary"] if eb.exists() else None,
    }


# ---------------------------------------------------------------- prior art


def prior_art_add(case_id: str, *, number: str, office: str | None = None, title: str | None = None, figures: list[str] | None = None, status: str | None = None, date: str | None = None, owner: str | None = None, source: str = "user", url: str | None = None, scope_notes: str | None = None, design_points: list[str] | None = None) -> dict:
    case = open_case(case_id)
    figs = []
    for path in figures or []:
        p = Path(path)
        if not p.exists():
            raise NotFound(f"Figure not found: {path}")
        figs.append((p.name, p.read_bytes()))
    meta = case.add_prior_art(
        {"id": re.sub(r"[^A-Za-z0-9]", "", number) or None, "number": number, "office": (office or "").upper() or None, "title": title, "status": status, "date": date, "owner": owner, "source": source, "url": url, "scope_notes": scope_notes, "design_points": design_points or []},
        figs,
    )
    case.event("prior_art.added", {"id": meta["id"], "number": number, "source": source, "figures": [sha256_file(f) for f in figures or []]})
    return meta


def prior_art_list(case_id: str) -> list[dict]:
    return open_case(case_id).prior_art()


def prior_art_set_figures(case_id: str, prior_id: str, figures: list[str], *, source: str, note: str | None = None) -> dict:
    """Replace the figures of a prior-art record (e.g. thumbnails -> full drawing sheets)."""

    case = open_case(case_id)
    path = case.prior_art_path(prior_id)
    blobs = []
    for fig in figures:
        p = Path(fig)
        if not p.exists():
            raise NotFound(f"Figure not found: {fig}")
        blobs.append((p.name, p.read_bytes()))
    if not blobs:
        raise InvalidInput("No figures given.")
    figs_dir = path / "figs"
    old = sorted(f.name for f in figs_dir.glob("*") if f.is_file()) if figs_dir.exists() else []
    if figs_dir.exists():
        shutil.rmtree(figs_dir)
    figs_dir.mkdir(parents=True)
    for name, data in blobs:
        (figs_dir / re.sub(r"[^A-Za-z0-9_.-]", "_", name)).write_bytes(data)
    meta_path = path / "meta.json"
    stored = json.loads(meta_path.read_text(encoding="utf-8"))
    stored["figure_source"] = source
    if note:
        stored["figure_note"] = note
    meta_path.write_text(json.dumps(stored, indent=2, ensure_ascii=False), encoding="utf-8")
    case.event("prior_art.figures", {"id": prior_id, "source": source, "replaced": len(old), "figures": [sha256_file(f) for f in figures]})
    return next(p for p in case.prior_art() if p["id"] == prior_id)


def _prior_masks(case, pa: dict) -> list:
    out = []
    for rel in pa.get("figures", []):
        try:
            gray = compare.load_gray(case.root / rel)
            out.append(compare.normalise(compare.auto_mask(gray)))
        except Exception:  # noqa: BLE001 - unreadable figure
            out.append(None)
    return out


def shape_scores(case_id: str, vid: str, prior_ids: list[str] | None = None) -> list[dict]:
    """CPU coarse ranking of prior art against one version."""

    case = open_case(case_id)
    ours = views.view_masks(case.version_path(vid))
    candidates = []
    for pa in case.prior_art():
        if prior_ids and pa["id"] not in prior_ids:
            continue
        candidates.append({"id": pa["id"], "number": pa.get("number"), "title": pa.get("title"), "masks": _prior_masks(case, pa)})
    return compare.rank_candidates(ours, candidates)


COMPARISON_VIEWS = ["perspective", "front", "right", "top"]


def comparison_image(case_id: str, vid: str, prior_id: str, *, max_figures: int = 6, cell: int = 320) -> dict:
    """Multi-view comparison sheet for the observer.

    Row 1: candidate perspective, front, right and top line drawings.
    Row 2: the prior art's figures (full drawing sheets when available).
    The overall impression comes from all views; a single best-matching view
    would overweight the outline that every product of the type shares.
    """

    case = open_case(case_id)
    pa = next((p for p in case.prior_art() if p["id"] == prior_id), None)
    if pa is None:
        raise NotFound(f"Prior art {prior_id} not found")
    if not pa["figures"]:
        raise NotFound(f"Prior art {prior_id} has no figures", details={"hint": "add figures with prior-art add --figure"})
    views_dir = case.version_path(vid) / "views"
    ours = [(key, views_dir / f"{key}.lineart.png") for key in COMPARISON_VIEWS if (views_dir / f"{key}.lineart.png").exists()]
    theirs = [(f"FIG. {i + 1}", case.root / rel) for i, rel in enumerate(pa["figures"][:max_figures])]
    cols = max(len(ours), len(theirs))
    header = 30
    canvas = Image.new("RGB", (cols * cell, 2 * (cell + header)), "white")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    for row, (title, items) in enumerate([(f"CANDIDATE {vid}", ours), (f"PRIOR ART {pa.get('number') or prior_id}", theirs)]):
        y0 = row * (cell + header)
        draw.rectangle([0, y0, cols * cell - 1, y0 + header - 2], fill=(235, 235, 235))
        draw.text((8, y0 + 8), title, fill=(0, 0, 0), font=font)
        for col, (label, path) in enumerate(items):
            try:
                img = Image.open(path).convert("L")
            except Exception:  # noqa: BLE001 - unreadable figure
                continue
            img.thumbnail((cell - 16, cell - 30), Image.Resampling.LANCZOS)
            if row == 1:  # prior-art scans: keep dense surface shading from reading as black
                img = img.point(lambda v: int(255 - 255 * ((255 - v) / 255) ** 1.8))
            img = img.convert("RGB")
            x = col * cell
            canvas.paste(img, (x + (cell - img.width) // 2, y0 + header + 18 + (cell - 30 - img.height) // 2))
            draw.text((x + 8, y0 + header + 4), label, fill=(60, 60, 60), font=font)
            if col:
                draw.line([(x, y0 + header), (x, y0 + header + cell)], fill=(220, 220, 220))
    out_dir = case.root / "evaluations" / "pairs"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{vid}__{prior_id}__multi.png"
    canvas.save(out)
    return {"path": str(out), "view": "multi", "candidate_views": [k for k, _ in ours], "prior_figures": len(theirs)}


def pair_image(case_id: str, vid: str, prior_id: str, *, view: str | None = None, figure: int | None = None) -> dict:
    """Side-by-side image (ours | prior art | overlay) of the best-matching view."""

    case = open_case(case_id)
    pa = next((p for p in case.prior_art() if p["id"] == prior_id), None)
    if pa is None:
        raise NotFound(f"Prior art {prior_id} not found")
    if view is None or figure is None:
        best = next((r for r in shape_scores(case_id, vid, [prior_id])), None)
        view = view or (best["shape"]["view"] if best else "perspective") or "perspective"
        figure = figure if figure is not None else (best["shape"]["figure"] if best else 0) or 0
    if not pa["figures"]:
        raise NotFound(f"Prior art {prior_id} has no figures")
    ours = Image.open(case.version_path(vid) / "views" / f"{view}.lineart.png").convert("L")
    theirs = Image.open(case.root / pa["figures"][min(figure, len(pa["figures"]) - 1)]).convert("L")
    over = compare.overlay(np.asarray(ours), np.asarray(theirs), size=420)
    cell = 420
    canvas = Image.new("RGB", (cell * 3, cell + 26), "white")
    draw = ImageDraw.Draw(canvas)
    for i, (img, label) in enumerate([(ours, f"candidate {vid} ({view})"), (theirs, f"prior art {prior_id}"), (over, "overlay: red=candidate blue=prior")]):
        img = img.convert("RGB")
        img.thumbnail((cell - 10, cell - 10))
        canvas.paste(img, (i * cell + (cell - img.width) // 2, 26 + (cell - img.height) // 2))
        draw.text((i * cell + 6, 6), label, fill=(0, 0, 0))
    out_dir = case.root / "evaluations" / "pairs"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{vid}__{prior_id}__{view}.png"
    canvas.save(out)
    return {"path": str(out), "view": view, "figure": figure}


# ------------------------------------------------------------------ evaluate


def review_set(case_id: str, vid: str, top_k: int | None = None) -> list[str]:
    """Prior rights that need an observer score for this version (top K by silhouette)."""

    k = risk.REVIEW_TOP_K if top_k is None else int(top_k)
    return [r["id"] for r in shape_scores(case_id, vid)[:k]]


def _judgments_dir(case) -> Path:
    path = case.root / "evaluations" / "observer"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_judgment(case_id: str, vid: str, prior_id: str, result: dict, source: str) -> dict:
    """Persist one observer judgment (hosted, embedded agent or imported)."""

    case = open_case(case_id)
    record = {**result, "prior_art_id": prior_id, "version": vid, "source": source, "judged_at": result.get("judged_at") or _now_iso()}
    (_judgments_dir(case) / f"{vid}__{prior_id}.json").write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    case.event("observer.judgment", {"version": vid, "prior_art": prior_id, "score": result.get("score"), "model": result.get("model"), "source": source, "rubric_version": result.get("rubric_version")})
    return record


def stored_judgments(case_id: str, vid: str) -> dict[str, dict]:
    """Stored judgments made with the current observer rubric (older rubrics are not mixed in)."""

    from designaround_core import observer

    case = open_case(case_id)
    out = {}
    for path in sorted(_judgments_dir(case).glob(f"{vid}__*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("rubric_version") not in (None, observer.RUBRIC_VERSION):
            continue
        out[data["prior_art_id"]] = data
    return out


def evaluate(case_id: str, vid: str, *, judge: dict[str, dict] | None = None, review_top_k: int | None = None, judge_source: str = "agent") -> dict:
    """Fuse CPU shape scores with observer judgments.

    judge: {prior_id: {"score": 0-10, "similar": [...], "different": [...], "rationale": str, "model": str}}
    New judgments are stored; stored judgments of earlier calls are reused.
    """

    case = open_case(case_id)
    for pid, result in (judge or {}).items():
        if isinstance(result, dict) and result.get("score") is not None and not (isinstance(result.get("source"), str) and result.get("judged_at")):
            save_judgment(case_id, vid, pid, result, judge_source)
    judge = {**stored_judgments(case_id, vid), **(judge or {})}
    rows = []
    for row in shape_scores(case_id, vid):
        j = judge.get(row["id"]) or {}
        fused = risk.fuse(row["shape"]["score"], j.get("score"))
        rows.append({"prior_art_id": row["id"], "number": row.get("number"), "shape": row["shape"], "observer": j or None, **fused})
    summary = risk.summarize(rows, top_k=risk.REVIEW_TOP_K if review_top_k is None else int(review_top_k))
    result = {"version": vid, "evaluated_at": _now_iso(), "per_prior_art": rows, "summary": summary, "wording": "Risk assessment only. Not a legal opinion; a practitioner must confirm."}
    path = case.evaluation_path(vid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    meta = case.version_meta(vid)
    # While an observer score is pending the number is an outline-only placeholder;
    # interfaces must not present it as the risk.
    meta.update({"risk": summary["max_risk"], "risk_level": summary["level"], "qualified": summary["qualified"], "risk_provisional": bool(summary.get("pending_observer"))})
    case.write_version_meta(vid, meta)
    for pa in case.prior_art():
        if any(r["prior_art_id"] == pa["id"] for r in rows):
            pa_meta_path = case.prior_art_path(pa["id"]) / "meta.json"
            stored = json.loads(pa_meta_path.read_text(encoding="utf-8"))
            stored["evaluated"] = True
            pa_meta_path.write_text(json.dumps(stored, indent=2, ensure_ascii=False), encoding="utf-8")
    case.event("evaluation", {"version": vid, "summary": summary, "fusion": risk.FUSION["version"], "file_sha256": sha256_file(path)})
    return result


def originality_check(case_id: str, vid: str, *, base: str | None = None, target_features: list[str] | None = None) -> dict:
    case = open_case(case_id)
    base = base or case.original_version()
    if base is None or base == vid:
        raise InvalidInput("Need an original version to compare against.")
    locked = (case.meta.get("constraints") or {}).get("locked_params") or []
    result = originality.check(case.version_path(base), case.version_path(vid), target_features=target_features, locked_params=locked)
    meta = case.version_meta(vid)
    meta["originality"] = {"pass": result["pass"], "reasons": result["reasons"]}
    case.write_version_meta(vid, meta)
    case.event("originality", {"version": vid, "base": base, "pass": result["pass"], "reasons": result["reasons"], "policy": result["policy"]["policy_version"]})
    return result


def dfm_check(case_id: str, vid: str, process: str = "injection") -> dict:
    case = open_case(case_id)
    result = dfm.check_version(case.version_path(vid), process)
    interfaces = (case.meta.get("constraints") or {}).get("interfaces") or []
    base = case.original_version()
    if interfaces and base and base != vid:
        result["interfaces"] = dfm.check_interfaces(case.version_path(base), case.version_path(vid), interfaces)
        if not all(i["pass"] for i in result["interfaces"]):
            result["pass"] = False
    meta = case.version_meta(vid)
    meta["dfm"] = {"process": process, "pass": result["pass"]}
    case.write_version_meta(vid, meta)
    case.event("dfm", {"version": vid, "process": process, "pass": result["pass"], "rule_pack": result["rule_pack_version"]})
    return result


# ------------------------------------------------------------------- photos


def photo_check(paths: dict[str, str]) -> dict:
    return photos.check_photo_set(paths)


def reconstruction_gate(case_id: str, vid: str, photo_paths: dict[str, str], min_score: float = 0.72) -> dict:
    case = open_case(case_id)
    result = photos.reconstruction_gate(case.version_path(vid), photo_paths, min_score=min_score)
    case.event("reconstruction_gate", {"version": vid, "pass": result["pass"], "worst": result["worst"], "photos": {k: sha256_file(v) for k, v in photo_paths.items()}})
    return result


# ------------------------------------------------------------- deliverables


def make_viewpack(case_id: str, vid: str, office: str, *, omit: list[str] | None = None, watermark: bool = False, shading: bool | None = None) -> dict:
    case = open_case(case_id)
    office = office.upper()
    out = case.filing_dir(office, vid) / "viewpack"
    if out.exists():
        shutil.rmtree(out)
    meta = case.meta
    article = meta.get("article") if office in ("US", "EU") else (meta.get("article_zh") or meta.get("article"))
    if office == "EU":
        result = viewpack.build_eu_viewpack(case.version_path(vid), out, article=article, omit=omit, watermark=watermark, shading=bool(shading))
    else:
        result = viewpack.build_line_viewpack(case.version_path(vid), office, out, article=article, omit=omit, watermark=watermark, shading=shading)
    case.event("viewpack", {"version": vid, "office": office.upper(), "status": result["rules"]["status"], "files": {f["path"]: f["sha256"] for f in result["files"]}})
    result["dir"] = str(out)
    return result


def make_photo_viewpack(case_id: str, photo_paths: dict[str, str], office: str, *, watermark: bool = False) -> dict:
    case = open_case(case_id)
    out = case.root / "filings" / office.upper() / "photos" / "viewpack"
    if out.exists():
        shutil.rmtree(out)
    article = case.meta.get("article") if office.upper() == "US" else (case.meta.get("article_zh") or case.meta.get("article"))
    result = viewpack.build_photo_viewpack(photo_paths, office, out, article=article, watermark=watermark)
    case.event("viewpack.photo", {"office": office.upper(), "files": {f["path"]: f["sha256"] for f in result["files"]}})
    result["dir"] = str(out)
    return result


def similar_candidates(case_id: str, main: str) -> list[dict]:
    case = open_case(case_id)
    main_masks = views.view_masks(case.version_path(main))
    rows = []
    for meta in case.versions():
        vid = meta["id"]
        if vid == main or meta.get("kind") == "original":
            continue
        other = views.view_masks(case.version_path(vid))
        sims = [compare.similarity(main_masks[k], other[k])["score"] for k in STANDARD_VIEWS if main_masks.get(k) is not None and other.get(k) is not None]
        rows.append({"version": vid, "qualified": bool(meta.get("qualified")), "similarity_to_main": round(float(np.mean(sims)) if sims else 0.0, 4)})
    return filing.suggest_similar_designs(main, rows)


def filing_pack(case_id: str, vid: str, office: str, *, similar: list[str] | str | None = None, planned_filing_date: str | None = None, watermark: bool = False) -> dict:
    case = open_case(case_id)
    office = office.upper()
    vp_dir = case.filing_dir(office, vid) / "viewpack"
    vp_json = vp_dir / "viewpack.json"
    vp = json.loads(vp_json.read_text(encoding="utf-8")) if vp_json.exists() else make_viewpack(case_id, vid, office, watermark=watermark)
    if similar == "auto":
        similar_ids = [c["version"] for c in similar_candidates(case_id, vid)] if office == "CN" else []
    else:
        similar_ids = list(similar or [])
    manifest = cad.read_manifest(case.version_path(vid))
    has_unclaimed = any(not p.get("claimed", True) for p in manifest["parts"])
    out = case.filing_dir(office, vid) / "package"
    if out.exists():
        shutil.rmtree(out)
    result = filing.build_filing_package(
        office=office,
        outdir=out,
        case_meta=case.meta,
        viewpack=vp,
        prior_art=case.prior_art(),
        decisions=decisions(case_id),
        similar_versions=similar_ids,
        has_unclaimed=has_unclaimed,
        disclosures=case.disclosures(),
        planned_filing_date=planned_filing_date,
    )
    if similar_ids and office == "CN":
        for index, sid in enumerate(similar_ids, start=2):
            make_dir = out / f"设计{index}_{sid}"
            sub = viewpack.build_line_viewpack(case.version_path(sid), "CN", make_dir, article=case.meta.get("article_zh") or case.meta.get("article"), watermark=watermark)
            result.setdefault("similar_viewpacks", []).append({"version": sid, "dir": str(make_dir), "status": sub["rules"]["status"]})
    case.event("filing_pack", {"version": vid, "office": office, "status": result["checklist"]["status"], "similar": similar_ids, "files": {f["path"]: f["sha256"] for f in result["manifest"]}})
    result["dir"] = str(out)
    result["viewpack_dir"] = str(vp_dir)
    return result


def ids_list(case_id: str) -> list[dict]:
    return filing.ids_rows(open_case(case_id).prior_art())


def disclose(case_id: str, kind: str, when: str, *, by_applicant: bool = True, exception: str | None = None, note: str | None = None) -> dict:
    from datetime import date as _date

    _date.fromisoformat(when)
    case = open_case(case_id)
    rows = case.disclosures()
    rows.append({"kind": kind, "date": when, "by_applicant": by_applicant, "exception": exception, "note": note})
    case.set_disclosures(rows)
    case.event("disclosure", rows[-1])
    return timeline(case_id)


def timeline(case_id: str, planned_filing_date: str | None = None) -> dict:
    from datetime import date as _date

    from designaround_core import rules

    case = open_case(case_id)
    rows = case.disclosures()
    filing_date = _date.fromisoformat(planned_filing_date) if planned_filing_date else None
    out = {}
    for office in case.meta.get("jurisdictions", []):
        out[office] = [f.to_dict() for f in rules.disclosure_findings(office, rows, filing_date)]
    return {"disclosures": rows, "findings": out}


def quote(offices: list[str], **kw) -> dict:
    return quote_mod.filing_quote(offices, **kw)


# ------------------------------------------------- filings, notices, deadlines (FR-FILE-12)


def _prosecution_path(case) -> Path:
    return case.root / "prosecution.json"


def prosecution(case_id: str) -> dict:
    case = open_case(case_id)
    path = _prosecution_path(case)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return {"filings": data.get("filings", []), "notices": data.get("notices", [])}


def _save_prosecution(case, data: dict) -> None:
    _prosecution_path(case).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _iso_day(value: str, what: str, *, not_future: bool = False) -> str:
    from datetime import date as _date

    try:
        day = _date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise InvalidInput(f"{what} must be YYYY-MM-DD, got {value!r}.") from exc
    if not_future and day > _date.today():
        raise InvalidInput(f"{what} {day} is in the future; record it once it has happened.")
    return day.isoformat()


def filing_record(case_id: str, office: str, filed: str, *, number: str | None = None, version: str | None = None, designated: list[str] | None = None) -> dict:
    from designaround_core import deadlines as dl

    case = open_case(case_id)
    office = office.upper()
    if office not in dl.OFFICES:
        raise InvalidInput(f"Office must be one of {', '.join(dl.OFFICES)}.")
    if version:
        case.version_path(version)
    row = {"office": office, "filed": _iso_day(filed, "Filing date", not_future=True), "number": (number or "").strip() or None, "version": version, "route": "hague" if office == "HAGUE" else "national"}
    if office == "HAGUE":
        row["designated"] = sorted({d.upper() for d in (designated or case.meta.get("jurisdictions") or [])} - {"HAGUE"})
    data = prosecution(case_id)
    data["filings"] = [f for f in data["filings"] if not (f["office"] == office and f.get("number") == row["number"] and f["filed"] == row["filed"])] + [row]
    _save_prosecution(case, data)
    case.event("filing.recorded", row)
    return {"filing": row, **deadlines(case_id)}


def notice_record(case_id: str, office: str, kind: str, issued: str, *, due: str | None = None, mailed: bool = False, number: str | None = None, note: str | None = None) -> dict:
    from designaround_core import deadlines as dl

    case = open_case(case_id)
    office = office.upper()
    if office not in dl.OFFICES:
        raise InvalidInput(f"Office must be one of {', '.join(dl.OFFICES)}.")
    if kind not in dl.NOTICE_KINDS:
        raise InvalidInput(f"Notice kind must be one of {', '.join(dl.NOTICE_KINDS)}.")
    row = {"office": office, "kind": kind, "date": _iso_day(issued, "Notice date", not_future=True), "due": _iso_day(due, "Due date") if due else None, "mailed": bool(mailed), "number": (number or "").strip() or None, "note": (note or "").strip()[:300] or None}
    if row["due"] and row["due"] < row["date"]:
        raise InvalidInput("The due date cannot be before the notice date.")
    data = prosecution(case_id)
    data["notices"].append(row)
    _save_prosecution(case, data)
    case.event("notice.recorded", row)
    return {"notice": row, **deadlines(case_id)}


def _matters(case) -> list[dict]:
    if not (case.root / "defense").exists():
        return []
    from . import defense

    return defense.matters(case.id)


def deadlines(case_id: str, *, lang: str = "zh", ics: str | None = None) -> dict:
    """Every deadline of the case (with legal basis) and an .ics calendar with reminders."""

    from datetime import date as _date

    from designaround_core import deadlines as dl

    case = open_case(case_id)
    data = prosecution(case_id)
    rows = dl.compute(offices=case.meta.get("jurisdictions") or [], filings=data["filings"], notices=data["notices"], disclosures=case.disclosures(), matters=_matters(case))
    today = _date.today()
    out_path = Path(ics) if ics else case.root / "calendar" / "deadlines.ics"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    name = f"DesignAround · {case.meta.get('name') or case.id}"
    out_path.write_text(dl.to_ics(rows, calendar_name=name, lang=lang), encoding="utf-8", newline="")
    items = [r.to_dict(today) for r in rows]
    upcoming = [r for r in items if r["days_left"] >= 0]
    return {"case": case.id, "filings": data["filings"], "notices": data["notices"], "deadlines": items, "next": next((r for r in upcoming if r["hard"]), None), "ics": str(out_path), "reminders_days": list(dl.REMINDER_DAYS)}


def defense_intake(case_id: str, **kw) -> dict:
    from . import defense

    return defense.intake(case_id, **kw)


def defense_link(case_id: str, prior_id: str, matter_id: str | None = None) -> dict:
    from . import defense

    return defense.link_prior_art(case_id, matter_id, prior_id)


def defense_matters(case_id: str) -> list[dict]:
    from . import defense

    return defense.matters(case_id)


def defense_pack(case_id: str, version: str, **kw) -> dict:
    from . import defense

    return defense.pack(case_id, version, **kw)


def upcoming_deadlines(within_days: int = 30) -> list[dict]:
    """Hard deadlines in the next ``within_days`` days across all cases (for reminders)."""

    from datetime import date as _date

    from designaround_core import deadlines as dl

    today = _date.today()
    rows = []
    for summary in case_list():
        case = open_case(summary["id"])
        if not _prosecution_path(case).exists() and not case.disclosures() and not (case.root / "defense").exists():
            continue
        data = prosecution(case.id)
        for d in dl.compute(offices=case.meta.get("jurisdictions") or [], filings=data["filings"], notices=data["notices"], disclosures=case.disclosures(), matters=_matters(case)):
            left = (d.due - today).days
            if d.hard and 0 <= left <= within_days:
                rows.append({"case": case.id, "name": case.meta.get("name"), **d.to_dict(today)})
    return sorted(rows, key=lambda r: r["due"])


def proto_pack(case_id: str, vid: str, process: str = "injection") -> dict:
    """Factory communication pack (FR-PROTO-01/02): model files + Chinese PDF summary."""

    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    from .viewpack import A4, CM, _ensure_cjk

    case = open_case(case_id)
    root = case.version_path(vid)
    manifest = cad.read_manifest(root)
    meta = case.version_meta(vid)
    dfm_result = dfm.check_version(root, process)
    out = case.root / "proto" / vid
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for name in ("model.stl", "model.3mf", "model.step", "model.glb"):
        if (root / name).exists() and (root / name).stat().st_size > 0:  # versions built before 0.1.3 have an empty 3MF
            shutil.copyfile(root / name, out / name)
    font = _ensure_cjk()
    pdf = out / "工厂沟通包.pdf"
    c = canvas.Canvas(str(pdf), pagesize=A4)
    w, h = A4
    c.setFont(font, 16)
    c.drawString(2 * CM, h - 2.2 * CM, f"工厂沟通包 · {case.meta.get('article_zh') or case.meta['name']} · 版本 {vid}")
    c.setFont(font, 10)
    bb = manifest["bbox"]
    lines = [
        f"外形尺寸（长×宽×高）：{bb['xlen']:.1f} × {bb['ylen']:.1f} × {bb['zlen']:.1f} mm",
        f"部件：{'、'.join(p['name'] for p in manifest['parts'])}",
        f"工艺：{process}；可制造性检查：{'通过' if dfm_result['pass'] else '未通过'}",
    ]
    parent = meta.get("parent") or case.original_version()
    if parent and parent != vid:
        changes = originality.changed_params(cad.read_manifest(case.version_path(parent)).get("params", {}), manifest.get("params", {}))
        lines.append(f"相对 {parent} 的改动：" + ("；".join(f"{k}: {v['from']} → {v['to']}" for k, v in changes.items()) or "程序结构改动"))
    for check in dfm_result["checks"]:
        if check["severity"] != "ok":
            lines.append(f"注意 {check['rule_id']}：{check['message']}")
    y = h - 3.2 * CM
    for line in lines:
        for chunk in [line[i : i + 52] for i in range(0, len(line), 52)]:
            c.drawString(2 * CM, y, chunk)
            y -= 0.6 * CM
    for i, key in enumerate(["perspective", "front", "top", "left"]):
        path = root / "views" / f"{key}.render.png"
        if not path.exists():
            continue
        x = 2 * CM + (i % 2) * 8.6 * CM
        yy = 3 * CM + (1 - i // 2) * 7 * CM
        c.drawImage(ImageReader(str(path)), x, yy, 8.2 * CM, 6.15 * CM, preserveAspectRatio=True)
    c.setFont(font, 8)
    c.drawString(2 * CM, 1.5 * CM, "打样级文件，量产开模前须由结构工程师复核。STEP 为参数化 B-rep，STL/3MF 用于 3D 打印。")
    c.showPage()
    c.save()
    files = manifest_for_dir(out)
    case.event("proto_pack", {"version": vid, "files": {f["path"]: f["sha256"] for f in files}})
    return {"dir": str(out), "files": files, "dfm": {"pass": dfm_result["pass"]}}


def evidence_export(case_id: str, out: str | None = None) -> dict:
    case = open_case(case_id)
    ok, bad = case.log.verify()
    files = manifest_for_dir(case.root, exclude=("events.jsonl",))
    summary = {
        "case": case.id,
        "exported_at": _now_iso(),
        "chain_ok": ok,
        "first_bad_index": bad,
        "events": len(case.log.read()),
        "head_hash": (case.log.read() or [{"hash": None}])[-1]["hash"],
        "files": files,
    }
    target = Path(out) if out else case.root.parent / f"{case.id}-evidence.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(summary, indent=2, ensure_ascii=False))
        zf.write(case.root / "events.jsonl", "events.jsonl")
        for row in files:
            zf.write(case.root / row["path"], f"files/{row['path']}")
    return {"zip": str(target), "chain_ok": ok, "events": summary["events"], "head_hash": summary["head_hash"], "files": len(files)}


def evidence_head(case_id: str) -> dict:
    case = open_case(case_id)
    events = case.log.read()
    ok, bad = case.log.verify()
    return {"case": case.id, "seq": len(events), "head_hash": events[-1]["hash"] if events else None, "chain_ok": ok}


# ------------------------------------------------------------ agent helpers


def observer_rubric() -> dict:
    from designaround_core import observer

    return observer.rubric()


def observer_prompt(case_id: str, vid: str, prior_id: str, view: str) -> dict:
    from designaround_core import observer

    case = open_case(case_id)
    pa = json.loads((case.prior_art_path(prior_id) / "meta.json").read_text(encoding="utf-8"))
    others = [p for p in case.prior_art() if p["id"] != prior_id]
    article = case.meta.get("article") or case.meta.get("name")
    return {
        "system": observer.SYSTEM_PROMPT,
        "user": observer.user_prompt(number=pa.get("number") or prior_id, office=pa.get("office"), title=pa.get("title"), scope=pa.get("scope_notes"), version=vid, view=view, article=article, field_size=len(others)),
        "rubric_version": observer.RUBRIC_VERSION,
    }


def parse_observer_reply(text: str) -> dict:
    from designaround_core import observer

    return observer.parse_reply(text)


def combine_observer_samples(samples: list[dict]) -> dict:
    from designaround_core import observer

    return observer.combine(samples)


def contact_sheet_for(case_id: str, vid: str, kind: str = "lineart") -> dict:
    case = open_case(case_id)
    return {"path": contact_sheet(case.version_path(vid), kind=kind)}


def params_schema(case_id: str, vid: str) -> dict:
    case = open_case(case_id)
    manifest = cad.read_manifest(case.version_path(vid))
    meta = program_meta((case.version_path(vid) / "program.py").read_text(encoding="utf-8")) if (case.version_path(vid) / "program.py").exists() else {}
    return {"params": manifest.get("params"), "schema": manifest.get("param_schema"), "features": meta.get("features", {}), "locked_by_default": meta.get("locked_by_default", [])}


def agent_event(case_id: str, kind: str, payload: dict) -> dict:
    """Record an agent-side event (run start/end, budget, candidate summary) in the evidence chain."""

    case = open_case(case_id)
    record = case.event(f"agent.{kind}", {"actor": "agent", **payload})
    return {"hash": record["hash"]}


def case_photos(case_id: str) -> dict:
    """Labelled reference photos stored on the case (view -> absolute path)."""

    case = open_case(case_id)
    photos_meta = (case.meta.get("photos") or {})
    return {view: str(case.root / rel) for view, rel in photos_meta.items() if (case.root / rel).exists()}


def case_add_photos(case_id: str, photos: dict[str, str]) -> dict:
    case = open_case(case_id)
    target = case.root / "photos"
    target.mkdir(exist_ok=True)
    stored = dict(case.meta.get("photos") or {})
    for view, path in photos.items():
        if view not in STANDARD_VIEWS:
            raise InvalidInput(f"Unknown view '{view}'", details={"views": list(STANDARD_VIEWS)})
        src = Path(path)
        if not src.exists():
            raise NotFound(f"Photo not found: {path}")
        dst = target / f"{view}{src.suffix.lower() or '.jpg'}"
        shutil.copyfile(src, dst)
        stored[view] = str(dst.relative_to(case.root))
    case.update(photos=stored)
    case.event("photos.added", {"views": sorted(photos), "sha256": {v: sha256_file(case.root / stored[v]) for v in photos}})
    return {"photos": stored, "check": photos_mod_check({v: str(case.root / stored[v]) for v in stored})}


def photos_mod_check(paths: dict[str, str]) -> dict:
    return photos.check_photo_set(paths)


# ----------------------------------------------------------- decisions and reports


def decision_card(case_id: str, lang: str = "zh") -> dict:
    """FR-UX-02/03: verdict, reasons, next steps with cost and time, keep/redesign/abandon."""

    from . import advisor

    card = advisor.decision_card(case_id, lang=lang)
    card["markdown"] = advisor.card_markdown(card)
    return card


def risk_report(case_id: str, vid: str | None = None, lang: str = "zh", out: str | None = None) -> dict:
    """FR-RISK-07: risk assessment report PDF (zh or en) with blind spots and method."""

    from . import report

    return report.risk_report(case_id, vid, lang=lang, out=out)


def compare_sheet(case_id: str, a: str, b: str) -> dict:
    """Same-scale overlay of two versions in every view, with parameter and risk changes."""

    from . import overlay

    case = open_case(case_id)
    sheet = overlay.overlay_sheet(case.version_path(a), case.version_path(b), case.root / "compare" / f"{a}__{b}.png", labels=(a, b))
    data = compare_versions(case_id, a, b)
    ba, bb = cad.read_manifest(case.version_path(a))["bbox"], cad.read_manifest(case.version_path(b))["bbox"]
    size = {k: [round(ba[k], 1), round(bb[k], 1)] for k in ("xlen", "ylen", "zlen")}
    return {**data, "sheet": sheet["path"], "overlap": sheet["per_view"], "size_mm": size}


def baseline_freeze(case_id: str, vid: str, office: str, note: str | None = None, application_number: str | None = None) -> dict:
    from . import baseline

    return baseline.freeze(case_id, vid, office, note=note, application_number=application_number)


def baseline_list(case_id: str) -> list[dict]:
    from . import baseline

    return baseline.list_baselines(case_id)


def baseline_verify(case_id: str, bid: str) -> dict:
    from . import baseline

    return baseline.verify(case_id, bid)


def baseline_compare(case_id: str, bid: str, vid: str) -> dict:
    from . import baseline

    return baseline.compare(case_id, bid, vid)


DELIVERABLE_KINDS = [
    ("reports", "report", "*.pdf"),
    ("filings", "filing", "*"),
    ("proto", "proto", "*"),
    ("exports", "evidence", "*"),
    ("baselines", "baseline", "*"),
    ("compare", "compare", "*.png"),
    ("defense", "defense", "*"),
]


def deliverables(case_id: str) -> list[dict]:
    """T2-26 deliverables centre: every generated file group of a case, newest first."""

    case = open_case(case_id)
    rows: list[dict] = []
    for folder, kind, _pattern in DELIVERABLE_KINDS:
        root = case.root / folder
        if not root.exists():
            continue
        if kind == "report":
            for f in root.glob("*.pdf"):
                rows.append({"kind": kind, "title": f.name, "path": str(f), "files": 1, "bytes": f.stat().st_size, "modified": f.stat().st_mtime})
        elif kind == "filing":
            for pkg in root.glob("*/*/*"):
                if pkg.is_dir():
                    files = [p for p in pkg.rglob("*") if p.is_file()]
                    office, vid = pkg.parent.parent.name, pkg.parent.name
                    main = next((p for p in files if p.suffix == ".pdf"), files[0] if files else None)
                    rows.append({"kind": f"filing_{pkg.name}", "title": f"{office} {vid} {pkg.name}", "path": str(pkg), "open": str(main) if main else None, "files": len(files), "bytes": sum(p.stat().st_size for p in files), "modified": max((p.stat().st_mtime for p in files), default=pkg.stat().st_mtime)})
        else:
            for item in root.iterdir():
                files = [item] if item.is_file() else [p for p in item.rglob("*") if p.is_file()]
                if not files:
                    continue
                main = item if item.is_file() else next((p for p in files if p.suffix in (".pdf", ".zip", ".png")), files[0])
                rows.append({"kind": kind, "title": item.name, "path": str(item), "open": str(main), "files": len(files), "bytes": sum(p.stat().st_size for p in files), "modified": max(p.stat().st_mtime for p in files)})
    evidence_zip = case.root.parent / f"{case.id}-evidence.zip"
    if evidence_zip.exists():
        rows.append({"kind": "evidence", "title": evidence_zip.name, "path": str(evidence_zip), "open": str(evidence_zip), "files": 1, "bytes": evidence_zip.stat().st_size, "modified": evidence_zip.stat().st_mtime})
    return sorted(rows, key=lambda r: -r["modified"])


def sample_case(lang: str = "zh") -> dict:
    """FR-UX-05: a free sample case with illustrative prior art (no credits, no network)."""

    from . import sample

    return sample.create_sample(lang=lang)
