"""Read-only share links (FR-CASE-09), client side.

``designaround share create`` picks the files for the recipient (factory: the
prototype pack; practitioner: drawings and filing package; other: reports and
views), writes watermarked copies into ``<case>/shares/<share_id>/`` (the originals
are untouched), uploads them and publishes the link. The case evidence log records
what was shared with whom, with hashes; the link token itself stays in the local
share record only.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path

from designaround_engine.workspace import open_case

from . import config
from .client import Server
from .output import EXIT_INVALID, CliExit

ALLOWED = {".png", ".jpg", ".jpeg", ".webp", ".pdf", ".step", ".stp", ".stl", ".3mf", ".obj", ".glb", ".zip", ".txt", ".md", ".csv", ".json", ".docx", ".xlsx"}
MAX_BYTES = 100 * 1024**2
MAX_FILE_BYTES = 45 * 1024**2  # per file (the server's edge accepts request bodies up to 50 MB)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _files_under(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    return sorted(p for p in folder.rglob("*") if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in ALLOWED and p.stat().st_size > 0)


def default_version(case) -> str:
    ids = [v for v in case.version_ids() if (case.version_meta(v).get("kind") != "draft")]
    if not ids:
        raise CliExit(EXIT_INVALID, "no_versions", "This case has no design version to share yet.")
    return ids[-1]


def pick_files(case, vid: str, purpose: str) -> list[Path]:
    """Default files for a recipient; may build the (free, local) prototype pack."""

    root = case.root
    if purpose == "factory":
        proto = root / "proto" / vid
        if not _files_under(proto):
            from designaround_engine import api

            api.proto_pack(case.id, vid)
        files = _files_under(proto)
        files += [p for p in _files_under(root / "compare") if vid in p.stem and p.suffix == ".png"][-2:]
        return files
    if purpose == "agent":
        files = []
        for office_dir in sorted((root / "filings").glob("*")):
            files += [p for p in _files_under(office_dir / vid) if p.suffix.lower() != ".json"]  # machine manifests stay local
        files += _files_under(root / "reports")[-2:]
        if not files:
            raise CliExit(EXIT_INVALID, "nothing_to_share", f"No drawings or filing package for {vid} yet.", next_actions=[f"designaround viewpack {case.id} {vid} --office CN", f"designaround filing-pack {case.id} {vid} --office CN"])
        return files
    files = _files_under(root / "reports")[-2:]
    views = case.version_path(vid) / "views"
    files += sorted(p for p in views.glob("*.render.png"))[:6] if views.exists() else []
    if not files:
        raise CliExit(EXIT_INVALID, "nothing_to_share", f"Nothing to share for {vid} yet; pass --file or make a report first.", next_actions=[f"designaround report {case.id} {vid}"])
    return files


def _display_name(rel: Path, vid: str) -> str:
    """File name the recipient sees: the office or version where it helps."""

    parts = rel.parts
    if parts[0] == "filings" and len(parts) > 3:
        return f"{parts[1]}-{parts[2]}-{rel.name}"
    if parts[0] == "versions":
        return f"{vid}-{rel.name}"
    if parts[0] == "proto" and len(parts) > 2:
        return f"{parts[1]}-{rel.name}"
    return rel.name


def _record_path(case, share_id: str) -> Path:
    return case.root / "shares" / share_id / "share.json"


def find_case(share_id: str):
    """The local case a share was created from (the server does not know case ids)."""

    for path in (config.home() / "cases").glob(f"*/shares/{share_id}/share.json"):
        return open_case(path.parent.parent.parent.name)
    return None


def create(case_id: str, vid: str | None, *, to: str, purpose: str, days: int, note: str | None, extra: list[Path], watermark: bool) -> dict:
    from designaround_engine import watermark as wm

    case = open_case(case_id)
    vid = vid or default_version(case)
    case.version_path(vid)  # raises when the version does not exist
    files = [] if extra and purpose == "other" else pick_files(case, vid, purpose)
    for path in extra:
        if not path.exists() or not path.is_file():
            raise CliExit(EXIT_INVALID, "file_not_found", f"{path} does not exist.")
        if not _inside(case.root, path):
            raise CliExit(EXIT_INVALID, "outside_case", f"{path} is not inside case {case.id}; only files of this case can be shared.")
        if path.suffix.lower() not in ALLOWED:
            raise CliExit(EXIT_INVALID, "file_type", f"{path.name}: this file type cannot be shared.", data={"allowed": sorted(ALLOWED)})
        if path.stat().st_size == 0:
            raise CliExit(EXIT_INVALID, "empty_file", f"{path.name} is empty.")
        files.append(path)
    seen: set[Path] = set()
    files = [p for p in files if not (p.resolve() in seen or seen.add(p.resolve()))]
    big = [p for p in files if p.stat().st_size > MAX_FILE_BYTES]
    if big:
        raise CliExit(EXIT_INVALID, "file_too_large", f"{', '.join(p.name for p in big)}: a shared file can be at most {MAX_FILE_BYTES // 1024**2} MB. Share a smaller export, or send it another way.")
    total = sum(p.stat().st_size for p in files)
    if total > MAX_BYTES:
        raise CliExit(EXIT_INVALID, "too_large", f"{total / 1024**2:.0f} MB selected; a share holds at most {MAX_BYTES // 1024**2} MB. Pick fewer files with --file.")
    server = Server()
    title = f"{case.meta.get('name') or case.id} · {vid}"
    draft = server.post("/v1/shares", {"title": title, "recipient": to, "purpose": purpose, "note": note, "days": days, "watermarked": watermark}, timeout=30)["data"]
    share_id = draft["share_id"]
    out_dir = case.root / "shares" / share_id
    try:
        date = time.strftime("%Y-%m-%d")
        text = f"仅供 {to} 查看 · {share_id} · {date} · 请勿转发"
        ascii_text = f"DesignAround share {share_id} · {date} · do not forward"
        prepared = []
        for src in files:
            rel = src.resolve().relative_to(case.root.resolve())
            dst = out_dir / "files" / "__".join(rel.parts)
            if watermark:
                marked = wm.mark_copy(src, dst, text, ascii_text)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
                marked = False
            prepared.append({"source": rel.as_posix(), "path": dst, "name": _display_name(rel, vid), "size": dst.stat().st_size, "sha256": _sha256(dst), "source_sha256": _sha256(src), "watermarked": marked})
        declared = server.post(f"/v1/shares/{share_id}/files", {"files": [{"name": p["name"], "size": p["size"], "sha256": p["sha256"]} for p in prepared]}, timeout=30)["data"]["files"]
        for item, row in zip(prepared, declared, strict=True):
            server.put_file(row["put"], str(item["path"]))
            item["file_id"], item["shared_name"] = row["file_id"], row["name"]
        published = server.post(f"/v1/shares/{share_id}/publish", {}, timeout=30)["data"]
    except BaseException:
        try:
            server.delete(f"/v1/shares/{share_id}", timeout=15)
        except CliExit:
            pass
        raise
    record = {"share_id": share_id, "url": draft["url"], "case": case.id, "version": vid, "recipient": to, "purpose": purpose, "days": days, "expires_at": published["expires_at"], "watermarked": watermark, "created_at": time.time(), "files": [{k: v for k, v in p.items() if k != "path"} | {"path": str(p["path"])} for p in prepared]}
    path = _record_path(case, share_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)  # holds the link
    except OSError:
        pass
    case.event("share.created", {"share_id": share_id, "version": vid, "recipient": to, "purpose": purpose, "expires_at": published["expires_at"], "watermarked": watermark, "files": [{"name": p["shared_name"], "source": p["source"], "sha256": p["sha256"], "source_sha256": p["source_sha256"]} for p in prepared]})
    return {**record, "status": published["status"], "bytes": sum(p["size"] for p in prepared)}


def revoke(share_id: str) -> dict:
    data = Server().delete(f"/v1/shares/{share_id}", timeout=20)["data"]
    case = find_case(share_id)
    if case is not None:
        case.event("share.revoked", {"share_id": share_id, "status": data.get("status"), "views": data.get("views"), "downloads": data.get("downloads")})
    return data


def local_record(share_id: str) -> dict | None:
    case = find_case(share_id)
    if case is None:
        return None
    return json.loads(_record_path(case, share_id).read_text(encoding="utf-8"))
