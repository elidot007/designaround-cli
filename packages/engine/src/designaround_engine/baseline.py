"""Submission baselines (FR-DRAW-11) and modification-scope checks (groundwork for FR-FILE-07).

Freezing copies what is submitted, the filing package, its view pack and the 3D
version they were drawn from, into ``<case>/baselines/<id>/``, records every file's
SHA-256 in BASELINE.json, makes the copy read-only and writes an evidence event.
Later, ``verify`` proves nothing changed, and ``compare`` measures how far a new
version departs from the baseline in every view: replacement drawings that change
the design shown can exceed what the application disclosed (CN Patent Law Art. 33;
no new matter in US practice). The comparison is a measurement for the
practitioner, not a legal conclusion.
"""

from __future__ import annotations

import json
import shutil
import stat
import time
from pathlib import Path

from . import cad, originality, overlay
from .errors import InvalidInput, NotFound
from .evidence import manifest_for_dir, sha256_file
from .workspace import Case, open_case

CHANGED_BELOW = 0.97  # per-view overlap under this counts as a visible change


def _dir(case: Case) -> Path:
    path = case.root / "baselines"
    path.mkdir(exist_ok=True)
    return path


def _read_only(root: Path) -> None:
    # Files become read-only; folders stay writable so the person can still delete the
    # whole case. Integrity rests on the SHA-256 record and the evidence event.
    for path in root.rglob("*"):
        if path.is_file():
            path.chmod(path.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def freeze(case_id: str, vid: str, office: str, *, note: str | None = None, application_number: str | None = None) -> dict:
    case = open_case(case_id)
    office = office.upper()
    filing = case.filing_dir(office, vid)
    package, viewpack = filing / "package", filing / "viewpack"
    if not package.exists() or not viewpack.exists():
        raise InvalidInput(f"No {office} filing package for {vid} yet.", details={"next": f"designaround filing-pack {case_id} {vid} --office {office}"})
    bid = f"{office}-{vid}-{time.strftime('%Y%m%dT%H%M%S')}"
    dest = _dir(case) / bid
    dest.mkdir()
    shutil.copytree(package, dest / "package")
    shutil.copytree(viewpack, dest / "viewpack")
    shutil.copytree(case.version_path(vid), dest / "model", ignore=shutil.ignore_patterns("scratch", "*.tmp"))
    files = manifest_for_dir(dest)
    record = {
        "id": bid,
        "office": office,
        "version": vid,
        "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "application_number": application_number,
        "note": note,
        "files": files,
        "params": cad.read_manifest(case.version_path(vid)).get("params", {}),
    }
    (dest / "BASELINE.json").write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    digest = sha256_file(dest / "BASELINE.json")
    _read_only(dest)
    event = case.event("baseline.frozen", {"baseline": bid, "office": office, "version": vid, "files": len(files), "baseline_sha256": digest, "application_number": application_number})
    return {"id": bid, "dir": str(dest), "office": office, "version": vid, "files": len(files), "sha256": digest, "event_hash": event["hash"]}


def _load(case: Case, bid: str) -> tuple[Path, dict]:
    dest = _dir(case) / bid
    path = dest / "BASELINE.json"
    if not path.exists():
        raise NotFound(f"Baseline {bid} not found in case {case.id}")
    return dest, json.loads(path.read_text(encoding="utf-8"))


def list_baselines(case_id: str) -> list[dict]:
    case = open_case(case_id)
    rows = []
    for path in sorted(_dir(case).glob("*/BASELINE.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        rows.append({k: data.get(k) for k in ("id", "office", "version", "frozen_at", "application_number", "note")} | {"files": len(data.get("files") or []), "dir": str(path.parent)})
    return rows


def verify(case_id: str, bid: str) -> dict:
    case = open_case(case_id)
    dest, record = _load(case, bid)
    expected = {f["path"]: f["sha256"] for f in record["files"]}
    actual = {f["path"]: f["sha256"] for f in manifest_for_dir(dest) if f["path"] != "BASELINE.json"}
    changed = sorted(p for p in expected if p in actual and actual[p] != expected[p])
    missing = sorted(p for p in expected if p not in actual)
    added = sorted(p for p in actual if p not in expected)
    events = [e for e in case.log.read() if e.get("type") == "baseline.frozen" and (e.get("payload") or {}).get("baseline") == bid]
    recorded = (events[-1].get("payload") or {}).get("baseline_sha256") if events else None
    record_ok = recorded == sha256_file(dest / "BASELINE.json")
    ok = not (changed or missing or added) and record_ok
    return {"id": bid, "ok": ok, "files": len(expected), "changed": changed, "missing": missing, "added": added, "record_matches_evidence": record_ok}


def compare(case_id: str, bid: str, vid: str) -> dict:
    """How far version ``vid`` departs from the frozen baseline, view by view."""

    case = open_case(case_id)
    dest, record = _load(case, bid)
    out = case.root / "baselines-compare" / f"{bid}__{vid}.png"
    sheet = overlay.overlay_sheet(dest / "model", case.version_path(vid), out, labels=(f"baseline {record['version']}", vid))
    changed_views = sorted(k for k, r in sheet["per_view"].items() if r["iou"] < CHANGED_BELOW)
    params = originality.changed_params(record.get("params") or {}, cad.read_manifest(case.version_path(vid)).get("params", {}))
    office = record["office"]
    rule = {
        "CN": "专利法第 33 条：修改不得超出原图片或照片表示的范围。",
        "US": "No new matter may be added to the disclosure after filing (35 U.S.C. 132).",
        "EU": "Amendments must keep the identity of the design (EUDR Art. 12 / Art. 64(2) CDR practice).",
    }.get(office, "")
    findings = []
    for key in changed_views:
        findings.append({"view": key, "overlap": sheet["per_view"][key]["iou"], "area_change": sheet["per_view"][key]["area_change"]})
    result = {
        "baseline": bid,
        "office": office,
        "baseline_version": record["version"],
        "version": vid,
        "changed_views": changed_views,
        "per_view": sheet["per_view"],
        "changed_params": params,
        "sheet": sheet["path"],
        "same_design_shown": not changed_views,
        "rule": rule,
        "findings": findings,
        "wording": "Measurement for the practitioner, not a legal conclusion: a practitioner decides whether a replacement drawing stays within the filed disclosure.",
    }
    case.event("baseline.compared", {"baseline": bid, "version": vid, "changed_views": changed_views, "min_overlap": sheet["min_overlap"]})
    return result



