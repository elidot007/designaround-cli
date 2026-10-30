"""Screening: server-side registry search, then local CPU ranking.

1. POST /v1/screen (ClearComply, design-phrased text queries per office;
   image query for deep screens). Metered on the server.
2. Save the raw result on the case (screens/<timestamp>.json, evidence event).
3. Add each candidate as prior art with its inline thumbnails.
4. Rank prior art against the chosen version (CPU silhouettes).
5. Replace the thumbnails of the top US designs with full-size drawing sheets
   from the USPTO public search API, split into figures, and rank again.
"""

from __future__ import annotations

import base64
import io
import json
import re
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from PIL import Image

from designaround_engine import api
from designaround_engine.workspace import open_case

from . import __version__
from .client import Server
from .figures import (
    MAX_FIGURE_BYTES,
    UsptoFigures,
    decode_data_uri,
    split_figures,
    us_design_number,
    valid_image,
)
from .output import EXIT_INVALID, STATE, CliExit

MAX_UPLOAD_EDGE = 1600


def encode_image(path: Path) -> tuple[str, str]:
    """Downscale to <= 1600 px and re-encode, so uploads stay small."""

    if not path.exists():
        raise CliExit(EXIT_INVALID, "not_found", f"Image not found: {path}")
    try:
        image = Image.open(path)
        image.load()
    except Exception as exc:  # noqa: BLE001
        raise CliExit(EXIT_INVALID, "bad_image", f"Cannot read image {path}: {exc}") from exc
    image.thumbnail((MAX_UPLOAD_EDGE, MAX_UPLOAD_EDGE))
    buf = io.BytesIO()
    if image.mode in ("RGBA", "LA", "P"):
        image.convert("RGBA").save(buf, format="PNG", optimize=True)
        mime = "image/png"
    else:
        image.convert("RGB").save(buf, format="JPEG", quality=88)
        mime = "image/jpeg"
    return base64.b64encode(buf.getvalue()).decode("ascii"), mime


def default_image(case_id: str, version: str | None) -> Path | None:
    case = open_case(case_id)
    vid = version or case.original_version()
    if vid:
        views_dir = case.version_path(vid) / "views"
        for name in ("perspective.render.png", "perspective.lineart.png"):
            if (views_dir / name).exists():
                return views_dir / name
    photos = api.case_photos(case_id)
    for view in ("perspective", "front"):
        if view in photos:
            return Path(photos[view])
    return None


def _fetch_figure(url: str, dest: Path) -> Path | None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    try:
        with httpx.stream("GET", url, headers={"User-Agent": f"designaround-cli/{__version__}"}, timeout=30, follow_redirects=True) as response:
            if response.status_code != 200 or not response.headers.get("content-type", "").startswith("image/"):
                return None
            data = bytearray()
            for chunk in response.iter_bytes():
                data += chunk
                if len(data) > MAX_FIGURE_BYTES:
                    return None
    except httpx.HTTPError:
        return None
    if not valid_image(bytes(data)):
        return None
    dest.write_bytes(bytes(data))
    return dest


def _figure_files(row: dict, tmp: Path, index: int, limit: int) -> list[str]:
    files = []
    for j, url in enumerate((row.get("image_urls") or [])[:limit]):
        inline = decode_data_uri(url)
        if inline:
            data, ext = inline
            path = tmp / f"{index}_{j}{ext}"
            path.write_bytes(data)
            files.append(str(path))
            continue
        suffix = Path(urlparse(url).path).suffix.lower()
        got = _fetch_figure(url, tmp / f"{index}_{j}{suffix if suffix in ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.tif', '.tiff') else '.png'}")
        if got:
            files.append(str(got))
    return files


def _norm(number: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (number or "").upper())


def _progress(text: str) -> None:
    if not STATE.get("quiet"):
        sys.stderr.write(text + "\n")
        sys.stderr.flush()


def _rank(case_id: str, vid: str | None) -> list[dict]:
    if not vid:
        return []
    return [{"id": r["id"], "number": r.get("number"), "title": r.get("title"), "shape_score": r["shape"]["score"], "view": r["shape"].get("view")} for r in api.shape_scores(case_id, vid)]


def upgrade_us_figures(case_id: str, prior_ids: list[str], *, max_sheets: int = 4, sheets_fn=None) -> list[dict]:
    """Full-size drawing sheets for US designs; returns per-record outcome."""

    case = open_case(case_id)
    by_id = {p["id"]: p for p in case.prior_art()}
    outcomes = []
    fetcher = None
    try:
        for pid in prior_ids:
            pa = by_id.get(pid)
            if not pa or not us_design_number(pa.get("number")) or pa.get("figure_source") == "uspto_ppubs":
                continue
            _progress(f"  full drawings for {pa['number']}…")
            try:
                if sheets_fn is None:
                    fetcher = fetcher or UsptoFigures()
                    sheets = fetcher.sheets(pa["number"], max_sheets=max_sheets)
                else:
                    sheets = sheets_fn(pa["number"], max_sheets)
            except Exception as exc:  # noqa: BLE001 - registry hiccups must not fail the screen
                outcomes.append({"id": pid, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]})
                continue
            with tempfile.TemporaryDirectory(prefix="da-sheets-") as tmp:
                files = []
                for s_index, sheet in enumerate(sheets):
                    for f_index, crop in enumerate(split_figures(sheet, max_figures=2)):
                        path = Path(tmp) / f"sheet{s_index + 1}_fig{f_index + 1}.png"
                        crop.save(path)
                        files.append(str(path))
                if files:
                    api.prior_art_set_figures(case_id, pid, files, source="uspto_ppubs", note=f"{len(sheets)} drawing sheets from ppubs.uspto.gov")
                outcomes.append({"id": pid, "ok": bool(files), "sheets": len(sheets), "figures": len(files)})
    finally:
        if fetcher is not None:
            fetcher.close()
    return outcomes


def run_screen(case_id: str, *, image: Path | None, query: str | None, deep: bool, jurisdictions: list[str], fetch: int, figures_per: int, version: str | None, hires: int = 5) -> dict:
    case = open_case(case_id)
    meta = case.meta
    query = query or meta.get("article") or meta.get("name")
    image = image or (default_image(case_id, version) if deep else None)
    body: dict = {"query": query, "article_zh": meta.get("article_zh"), "jurisdictions": jurisdictions, "deep": deep, "request_id": f"{case_id}-{int(time.time())}"}
    if image is not None:
        body["image_base64"], body["image_mime_type"] = encode_image(image)
    _progress(f"screening {', '.join(jurisdictions)} for '{query}'" + (" (deep)" if deep else "") + "…")
    response = Server().post("/v1/screen", body, timeout=420)
    data = response.get("data") or {}
    candidates = data.get("candidates") or []

    screens = case.root / "screens"
    screens.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    raw_path = screens / f"{stamp}.json"
    if raw_path.exists():  # two screens within one second (batch re-runs)
        stamp = time.strftime("%Y%m%dT%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
        raw_path = screens / f"{stamp}.json"
    # The parameters are kept so the screen can be re-run as it was (FR-CASE-07).
    image_ref = None
    if image is not None:
        try:
            image_ref = Path(image).resolve().relative_to(case.root.resolve()).as_posix()
        except ValueError:
            image_ref = str(image)
    params = {"query": query, "deep": deep, "jurisdictions": jurisdictions, "image": image_ref, "version": version, "fetch": fetch, "figures_per": figures_per, "hires": hires}
    slim = {**data, "id": stamp, "params": params, "candidates": [{k: v for k, v in c.items() if k != "image_urls"} | {"figures_inline": len(c.get("image_urls") or [])} for c in candidates]}
    raw_path.write_text(json.dumps(slim, indent=2, ensure_ascii=False), encoding="utf-8")
    case.event("screen", {"query": query, "deep": deep, "jurisdictions": jurisdictions, "image": image.name if image else None, "candidates": len(candidates), "charged": (response.get("cost") or {}).get("charged", 0), "file": str(raw_path.relative_to(case.root))})

    existing = {_norm(p.get("number")) for p in case.prior_art()}
    added = []
    with tempfile.TemporaryDirectory(prefix="da-figs-") as tmp:
        for index, row in enumerate(candidates[:fetch]):
            number = row.get("number")
            if not number or _norm(number) in existing:
                continue
            figs = _figure_files(row, Path(tmp), index, figures_per * 2)
            pa = api.prior_art_add(case_id, number=number, office=row.get("office"), title=row.get("title"), figures=figs, status=row.get("status"), date=row.get("date"), owner=row.get("owner"), source=f"screen:{row.get('source') or 'clearcomply'}", url=row.get("detail_url"))
            existing.add(_norm(number))
            added.append({"id": pa["id"], "number": number, "office": row.get("office"), "figures": len(figs)})

    vid = version or case.original_version()
    ranked = _rank(case_id, vid)
    upgraded = []
    if hires > 0:
        # US designs without any figure first (they cannot be ranked at all), then the top-ranked ones.
        by_id = {p["id"]: p for p in case.prior_art()}
        missing = [pid for pid, p in by_id.items() if us_design_number(p.get("number")) and not p.get("figures")]
        top = [r["id"] for r in ranked if us_design_number(r.get("number")) and r["id"] not in missing]
        targets = (missing + top)[: max(hires, len(missing))]
        if targets:
            upgraded = upgrade_us_figures(case_id, targets, max_sheets=figures_per)
            if any(u.get("ok") for u in upgraded):
                ranked = _rank(case_id, vid)
    still_missing = {p["number"] for p in case.prior_art() if not p.get("figures")}
    no_figures = [a["number"] for a in added if a["number"] in still_missing]
    return {
        "case": case_id,
        "version": vid,
        "query": query,
        "jurisdictions": jurisdictions,
        "queries": data.get("queries", []),
        "candidates": len(candidates),
        "added": added,
        "ranked": ranked,
        "full_drawings": upgraded,
        "without_figures": no_figures,
        "source_failures": data.get("source_failures", []),
        "blind_spots": data.get("blind_spots", []) + (["Some candidates have no figures (e.g. CN designs from Google Patents); they cannot be ranked by shape. Add figures with `prior-art add --figure`."] if no_figures else []),
        "raw": str(raw_path),
        "charged": (response.get("cost") or {}).get("charged", 0),
    }
