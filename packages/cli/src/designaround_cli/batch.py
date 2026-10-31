"""Batch screening from a CSV (FR-RISK-06, FR-CASE-08, FR-CLI-07).

Columns (header row, UTF-8; only ``name`` and ``article`` are required):

    name, article, article_zh, jurisdictions ("US;CN"), image (path), cad (STEP/IGES path), case

Each row becomes a case (or reuses ``case``), is screened on the server, and, when a
3D version exists (``cad`` column or an existing case), ranked locally. Progress is
kept in ``<csv>.state.json`` next to the CSV, so a second run continues where the
first stopped and retries only failed rows. The whole run has one credit cap,
checked against the estimate before anything is charged.
"""

from __future__ import annotations

import csv
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from designaround_core import catalog
from designaround_engine import api
from designaround_engine.errors import EngineError

from .output import EXIT_BUDGET, EXIT_INVALID, CliExit

MAX_ROWS = 1000
OFFICES = {"US", "CN", "EU"}


def top_hits(data: dict) -> list[dict]:
    """Up to three closest designs. Shape score when the row was ranked; otherwise the screen hit, with no fake 0."""

    ranked = data.get("ranked") or []
    rows = []
    for row in ranked[:3]:
        number = row.get("number")
        if not number:
            continue
        shape = row.get("shape")
        score = None
        if isinstance(shape, dict) and isinstance(shape.get("score"), (int, float)):
            score = round(float(shape["score"]), 3)
        elif isinstance(row.get("shape_score"), (int, float)):
            score = round(float(row["shape_score"]), 3)
        rows.append({"number": number, "shape": score})
    if rows:
        return rows
    hits = []
    for row in (data.get("hits") or [])[:3]:
        if row.get("number"):
            hits.append({"number": row["number"], "shape": None})
    return hits


def format_top(items: list[dict]) -> str:
    parts = []
    for item in items:
        if item.get("shape") is None:
            parts.append(str(item.get("number") or ""))
        else:
            parts.append(f"{item.get('number')} ({item['shape']})")
    return "; ".join(p for p in parts if p)


def _key(row: dict) -> str:
    raw = json.dumps({k: (row.get(k) or "").strip() for k in ("name", "article", "image", "cad", "case")}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def read_rows(path: Path) -> list[dict]:
    if not path.exists():
        raise CliExit(EXIT_INVALID, "not_found", f"CSV not found: {path}")
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(fh)]
    rows = [r for r in rows if any(r.values())]
    if not rows:
        raise CliExit(EXIT_INVALID, "empty_csv", "The CSV has no rows.", next_actions=["Header: name,article,article_zh,jurisdictions,image,cad"])
    if len(rows) > MAX_ROWS:
        raise CliExit(EXIT_INVALID, "too_many_rows", f"{len(rows)} rows; one batch handles at most {MAX_ROWS}.")
    bad = [i + 2 for i, r in enumerate(rows) if not (r.get("name") and (r.get("article") or r.get("case")))]
    if bad:
        raise CliExit(EXIT_INVALID, "missing_columns", f"Rows {bad[:10]} need name and article (or an existing case id).")
    for r in rows:
        r["_key"] = _key(r)
        r["_offices"] = [o.strip().upper() for o in (r.get("jurisdictions") or "US").replace(",", ";").split(";") if o.strip().upper() in OFFICES] or ["US"]
        for col in ("image", "cad"):
            if r.get(col) and not Path(r[col]).expanduser().is_absolute():
                r[col] = str((path.parent / r[col]).resolve())
    return rows


def estimate(rows: list[dict], deep: bool, done: set[str]) -> int:
    per = catalog.ACTION_CREDITS["screen.deep.batch" if deep and len(rows) >= 100 else ("screen.deep" if deep else "screen.quick")]
    return per * sum(1 for r in rows if r["_key"] not in done)


def run_batch(csv_path: Path, *, deep: bool, max_credits: int, concurrency: int = 2, fetch: int = 8, figures_per: int = 3, out: Path | None = None) -> dict:
    from .screening import STATE as SCREEN_STATE
    from .screening import run_screen

    rows = read_rows(csv_path)
    state_path = csv_path.with_suffix(csv_path.suffix + ".state.json")
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"rows": {}}
    done = {k for k, v in state["rows"].items() if v.get("status") == "done"}
    est = estimate(rows, deep, done)
    if est > max_credits:
        raise CliExit(EXIT_BUDGET, "budget_exceeded", f"Estimated {est} credits for {len(rows) - len(done)} rows exceeds --max-credits {max_credits}.", data={"estimate": est, "rows": len(rows), "done": len(done)}, next_actions=[f"Re-run with --max-credits {est}", "Use quick screening (drop --deep) for a first pass"])
    lock = threading.Lock()
    SCREEN_STATE["quiet"] = True

    def save() -> None:
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(state_path)

    def one(row: dict) -> dict:
        record = {"name": row["name"], "status": "running", "started": time.time(), "error": None}
        try:
            case_id = row.get("case") or (state["rows"].get(row["_key"]) or {}).get("case")
            if not case_id:
                case_id = api.case_create(row["name"], article=row.get("article") or None, article_zh=row.get("article_zh") or None, jurisdictions=row["_offices"])["id"]
                api.case_update(case_id, status="batch")
            record["case"] = case_id
            with lock:
                state["rows"][row["_key"]] = {**(state["rows"].get(row["_key"]) or {}), **record}
                save()
            if row.get("cad") and not api.case_show(case_id).get("original"):
                api.import_model_file(case_id, row["cad"])
            image = Path(row["image"]).expanduser() if row.get("image") else None
            if image is not None and not image.exists():
                raise CliExit(EXIT_INVALID, "not_found", f"Image not found: {image}")
            data = run_screen(case_id, image=image, query=None, deep=deep, jurisdictions=row["_offices"], fetch=fetch, figures_per=figures_per, version=None, hires=2 if deep else 0)
            record.update({
                "status": "done",
                "candidates": data.get("candidates", 0),
                "prior_art_added": len(data.get("added") or []),
                "top": top_hits(data),
                "version": data.get("version"),
                "charged": data.get("charged", 0),
                "source_failures": len(data.get("source_failures") or []),
            })
        except CliExit as exc:
            record.update({"status": "failed", "error": f"{exc.kind}: {exc.message}"[:300]})
            if exc.code == EXIT_BUDGET:
                record["budget"] = True
        except EngineError as exc:
            record.update({"status": "failed", "error": f"{exc.code}: {exc}"[:300]})
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the batch
            record.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:300]})
        record["finished"] = time.time()
        with lock:
            state["rows"][row["_key"]] = {**(state["rows"].get(row["_key"]) or {}), **record}
            save()
        return record

    todo = [r for r in rows if r["_key"] not in done]
    spent = 0
    stopped = False
    with ThreadPoolExecutor(max_workers=max(1, min(concurrency, 6))) as pool:
        futures = {pool.submit(one, r): r for r in todo}
        for future in as_completed(futures):
            rec = future.result()
            spent += int(rec.get("charged") or 0)
            if rec.get("budget"):
                stopped = True
    results = []
    for r in rows:
        rec = state["rows"].get(r["_key"]) or {"status": "pending"}
        results.append({"name": r["name"], "case": rec.get("case"), "status": rec.get("status"), "candidates": rec.get("candidates"), "top": format_top(rec.get("top") or []), "error": rec.get("error")})
    target = out or csv_path.with_name(csv_path.stem + "-results.csv")
    with target.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["name", "case", "status", "candidates", "top", "error"])
        writer.writeheader()
        writer.writerows(results)
    counts = {s: sum(1 for x in results if x["status"] == s) for s in ("done", "failed", "pending")}
    return {"rows": len(rows), "counts": counts, "estimate": est, "charged_this_run": spent, "stopped_on_budget": stopped, "results_csv": str(target), "state": str(state_path), "results": results}
