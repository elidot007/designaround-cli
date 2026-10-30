"""Re-running screens with their differences (FR-CASE-07) and case monitoring (FR-MON-01).

``rescreen`` repeats a recorded screen with its original parameters and lists what
changed: new registrations, records that dropped out, and changed status, date,
owner or classes. ``monitor`` subscribes a case: every N days the same screen runs
again (when the workbench starts or ``designaround monitor run`` is called, for
example from cron), new hits are added as prior art, ranked by silhouette against
the design being sold, filtered to the case's Locarno class when the registry
gives classes, and pushed to the configured chat-bot webhook / desktop.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from designaround_engine import api
from designaround_engine.workspace import open_case

from . import config
from .output import EXIT_INVALID, CliExit

FIELDS = ("status", "date", "title", "owner", "classes")
VISUAL_CLOSE = 0.70  # silhouette score from which a new hit is called visually close


def _norm(number: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (number or "").upper())


def screen_records(case) -> list[dict]:
    folder = case.root / "screens"
    rows = []
    for path in sorted(folder.glob("*.json")) if folder.exists() else []:
        if path.name.endswith(".diff.json"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        rows.append({"id": data.get("id") or path.stem, "path": path, "data": data})
    return rows


def _event_params(case, file_rel: str) -> dict:
    """Parameters of screens recorded before 0.1.3 (kept only in the evidence event)."""

    for event in reversed(case.log.read()):
        payload = event.get("payload") or {}
        if event.get("type") == "screen" and payload.get("file") == file_rel:
            return {"query": payload.get("query"), "deep": bool(payload.get("deep")), "jurisdictions": payload.get("jurisdictions") or [], "image": None, "version": None, "fetch": 10, "figures_per": 4, "hires": 5}
    return {}


def params_of(case, record: dict) -> dict:
    params = record["data"].get("params") or _event_params(case, record["path"].relative_to(case.root).as_posix())
    if not params.get("jurisdictions"):
        params["jurisdictions"] = record["data"].get("jurisdictions") or case.meta.get("jurisdictions") or ["US"]
    return params


def pick(case, screen_id: str | None) -> dict:
    records = screen_records(case)
    if not records:
        raise CliExit(EXIT_INVALID, "no_screens", f"Case {case.id} has no screening record to re-run.", next_actions=[f"designaround screen {case.id} --max-credits 60"])
    if not screen_id or screen_id == "latest":
        return records[-1]
    for r in records:
        if r["id"] == screen_id or r["id"].startswith(screen_id):
            return r
    raise CliExit(EXIT_INVALID, "not_found", f"No screen {screen_id} on case {case.id}.", data={"screens": [r["id"] for r in records]})


def diff(old: dict, new: dict) -> dict:
    before = {_norm(c.get("number")): c for c in old.get("candidates") or [] if c.get("number")}
    after = {_norm(c.get("number")): c for c in new.get("candidates") or [] if c.get("number")}
    added = [after[k] for k in after if k not in before]
    gone = [before[k] for k in before if k not in after]
    changed = []
    for k in after.keys() & before.keys():
        delta = {f: {"from": before[k].get(f), "to": after[k].get(f)} for f in FIELDS if (before[k].get(f) or None) != (after[k].get(f) or None)}
        if delta:
            changed.append({"number": after[k].get("number"), "office": after[k].get("office"), "changes": delta})
    slim = lambda c: {k: c.get(k) for k in ("number", "office", "title", "status", "date", "owner", "classes", "detail_url")}  # noqa: E731
    return {"new": [slim(c) for c in added], "gone": [slim(c) for c in gone], "changed": changed, "unchanged": len(after.keys() & before.keys()) - len(changed)}


def _locarno_main(value) -> set[str]:
    text = json.dumps(value, ensure_ascii=False) if value is not None else ""
    return {m.group(1) for m in re.finditer(r"(?<!\d)(\d{2})[-.](\d{2})(?!\d)", text)}


def target_version(case) -> str | None:
    """The design that is being sold or filed: a frozen baseline, else a qualified candidate, else the original."""

    try:
        baselines = api.baseline_list(case.id)
    except Exception:  # noqa: BLE001
        baselines = []
    if baselines:
        return baselines[-1]["version"]
    qualified = [v["id"] for v in api.versions(case.id) if v.get("qualified")]
    return qualified[-1] if qualified else case.original_version()


def rescreen(case_id: str, screen_id: str | None = None, *, lang: str = "zh") -> dict:
    from .screening import run_screen

    case = open_case(case_id)
    base = pick(case, screen_id)
    params = params_of(case, base)
    image = None
    note = None
    if params.get("image"):
        candidate = case.root / params["image"] if not Path(params["image"]).is_absolute() else Path(params["image"])
        if candidate.exists():
            image = candidate
        else:
            note = f"The original image {params['image']} is gone; the default image was used."
    result = run_screen(case_id, image=image, query=params.get("query"), deep=bool(params.get("deep")), jurisdictions=params["jurisdictions"], fetch=int(params.get("fetch") or 10), figures_per=int(params.get("figures_per") or 4), version=params.get("version"), hires=int(params.get("hires") or 5))
    new_record = json.loads(Path(result["raw"]).read_text(encoding="utf-8"))
    changes = diff(base["data"], new_record)
    vid = target_version(case)
    ranked = {r["number"]: r for r in api_rank(case_id, vid)}
    top3 = [r["number"] for r in sorted(ranked.values(), key=lambda r: -r["shape_score"])[:3]]
    want = _locarno_main(case.meta.get("locarno"))
    for hit in changes["new"]:
        r = ranked.get(hit["number"]) or next((x for n, x in ranked.items() if _norm(n) == _norm(hit["number"])), None)
        hit["shape_score"] = r["shape_score"] if r else None
        hit["prior_art_id"] = r["id"] if r else None
        hit["top3"] = hit["number"] in top3
        hit["visually_close"] = bool(r and r["shape_score"] >= VISUAL_CLOSE)
        classes = _locarno_main(hit.get("classes"))
        hit["same_class"] = None if not (want and classes) else bool(want & {c for c in classes})
    changes["new"].sort(key=lambda h: -(h.get("shape_score") or 0))
    out_path = Path(result["raw"]).with_suffix(".diff.json")
    record = {"base": base["id"], "rerun": new_record.get("id"), "params": params, "version": vid, **changes, "note": note}
    out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    case.event("screen.rerun", {"base": base["id"], "rerun": new_record.get("id"), "new": len(changes["new"]), "gone": len(changes["gone"]), "changed": len(changes["changed"]), "file": out_path.relative_to(case.root).as_posix()})
    return {**record, "case": case_id, "diff_file": str(out_path), "charged": result["charged"], "source_failures": result["source_failures"], "candidates": result["candidates"]}


def api_rank(case_id: str, vid: str | None) -> list[dict]:
    if not vid:
        return []
    return [{"id": r["id"], "number": r.get("number"), "shape_score": r["shape"]["score"]} for r in api.shape_scores(case_id, vid)]


# ---------------------------------------------------------------- monitoring


def _monitor_path(case) -> Path:
    return case.root / "monitor.json"


def subscribe(case_id: str, *, every_days: int, max_credits: int) -> dict:
    case = open_case(case_id)
    pick(case, None)  # needs at least one screen to repeat
    data = {"every_days": every_days, "max_credits": max_credits, "since": time.time(), "last_run": None, "last_status": None, "runs": 0}
    path = _monitor_path(case)
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        data.update({k: old.get(k) for k in ("since", "last_run", "last_status", "runs") if old.get(k) is not None})
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    case.event("monitor.subscribed", {"every_days": every_days, "max_credits": max_credits})
    return {"case": case_id, **data, "next_run": (data["last_run"] or time.time()) + (every_days * 86400 if data["last_run"] else 0)}


def unsubscribe(case_id: str) -> dict:
    case = open_case(case_id)
    path = _monitor_path(case)
    existed = path.exists()
    path.unlink(missing_ok=True)
    if existed:
        case.event("monitor.removed", {})
    return {"case": case_id, "removed": existed}


def monitors() -> list[dict]:
    rows = []
    for path in sorted((config.home() / "cases").glob("*/monitor.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        last = data.get("last_run")
        due_at = (last + data["every_days"] * 86400) if last else data.get("since", 0)
        rows.append({"case": path.parent.name, **data, "next_run": due_at, "due": due_at <= time.time()})
    return rows


def _price(deep: bool) -> int:
    from designaround_core import catalog

    return catalog.ACTION_CREDITS["screen.deep" if deep else "screen.quick"]


def run(*, due_only: bool, budget: int, lang: str = "zh") -> dict:
    """Re-run the monitored cases' latest screens within ``budget`` credits; push new hits."""

    zh = lang != "en"
    results, spent = [], 0
    for m in monitors():
        if due_only and not m["due"]:
            continue
        case = open_case(m["case"])
        params = params_of(case, pick(case, None))
        price = _price(bool(params.get("deep")))
        if price > m.get("max_credits", price) or spent + price > budget:
            results.append({"case": m["case"], "status": "skipped_budget", "price": price})
            continue
        path = _monitor_path(case)
        state = json.loads(path.read_text(encoding="utf-8"))
        try:
            r = rescreen(m["case"], None, lang=lang)
            spent += int(r.get("charged") or 0)
            status = "failed_source" if r["source_failures"] and not r["candidates"] else "ok"
            results.append({"case": m["case"], "name": case.meta.get("name"), "status": status, "new": r["new"], "gone": len(r["gone"]), "changed": len(r["changed"]), "charged": r.get("charged"), "diff_file": r["diff_file"]})
        except CliExit as err:
            results.append({"case": m["case"], "status": "error", "code": err.kind, "message": err.message})
            status = "error"
        now = time.time()
        # A failed run (registry down, quota) is retried the next day, not a whole period later.
        last = now if status != "error" else now - max(0, state["every_days"] - 1) * 86400
        state.update({"last_run": last, "last_status": status, "runs": int(state.get("runs") or 0) + 1})
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    hits = [(r, h) for r in results if r.get("status") == "ok" for h in r.get("new") or []]
    pushed = None
    if hits:
        from .notify import notify

        close = [(r, h) for r, h in hits if h.get("visually_close") or h.get("top3")]
        title = (f"DesignAround 监控：{len(hits)} 件新的外观设计" + (f"，{len(close)} 件外形接近" if close else "")) if zh else (f"DesignAround monitor: {len(hits)} new designs" + (f", {len(close)} look close" if close else ""))
        lines = [f"{r.get('name') or r['case']}: {h['number']} {h.get('title') or ''} " + (f"(外形 {h['shape_score']:.2f})" if zh and h.get("shape_score") is not None else f"(shape {h['shape_score']:.2f})" if h.get("shape_score") is not None else "") for r, h in (close or hits)[:5]]
        pushed = notify(title, "；".join(lines), {"monitor": [{"case": r["case"], **h} for r, h in hits[:20]]}, force_desktop=True)
    return {"results": results, "spent": spent, "budget": budget, "new_hits": len(hits), "notified": pushed}
