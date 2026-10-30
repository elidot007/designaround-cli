"""Result guarantee, client side (FR-ACCT-08).

A covered design-around run (standard or deep depth, at least three rounds, platform
models) that ends without any qualified candidate gets half of the credits the server
metered for it back. The CLI registers the run before it starts and files the claim
as soon as the run ends, together with a short account of where it got stuck; the
server refunds once per run. ``designaround run guarantee <run_id>`` files the claim
again when the automatic attempt could not reach the server.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from designaround_core.catalog import GUARANTEE

from . import config
from .client import Server
from .output import EXIT_INVALID, CliExit

ERROR_ENDS = {"budget_exceeded": "budget_exceeded", "insufficient_credits": "budget_exceeded", "internal": "interrupted", "runner_crashed": "interrupted"}


def covered(depth: str | None, rounds: int | None) -> bool:
    return depth in GUARANTEE["depths"] and int(rounds or 0) >= GUARANTEE["min_rounds"]


def candidate_rows(outcome: dict, events: list[dict]) -> list[dict]:
    rows = outcome.get("candidates") if outcome.get("type") == "result" else None
    if rows is None:
        rows = [e for e in events if e.get("type") == "candidate"]
    return [r for r in rows if isinstance(r, dict)]


def end_of(outcome: dict, events: list[dict]) -> str | None:
    """How a finished run ended, in guarantee terms, or None when it is not eligible."""

    if outcome.get("qualified") or any(r.get("qualified") for r in candidate_rows(outcome, events)):
        return None
    if outcome.get("type") == "result":
        return "no_qualified" if outcome.get("status") == "no_qualified" else None
    if outcome.get("type") == "error":
        return ERROR_ENDS.get(str(outcome.get("code") or ""))
    return None


def _still_sees(stuck: str, version: str) -> str | None:
    for line in (stuck or "").splitlines():
        if line.startswith(f"{version}:"):
            match = re.search(r"still sees: (.*?)(?:; already different|; other rights|; DFM errors|$)", line)
            if match and match.group(1).strip() not in ("", "n/a"):
                return match.group(1).strip()[:200]
    return None


def blockers(outcome: dict, events: list[dict], lang: str = "zh") -> list[str]:
    """Plain-language reasons the run produced no qualified candidate (说明卡点)."""

    zh = lang != "en"
    lines: list[str] = []
    if outcome.get("type") == "error":
        if outcome.get("code") in ("budget_exceeded", "insufficient_credits"):
            lines.append("预算或余额在运行中用完，闭环没有跑完" if zh else "The budget or the balance ran out before the loop finished")
        else:
            lines.append(("运行中断：" if zh else "The run stopped: ") + str(outcome.get("message") or outcome.get("code"))[:160])
    rows = candidate_rows(outcome, events)
    if not rows:
        lines.append("没有产生任何候选方案：设计师在锁定参数内找不到可改的地方" if zh else "No candidate was produced: the designer found nothing to change within the locked parameters")
        return lines
    scored = [r for r in rows if isinstance(r.get("risk"), (int, float))]
    unoriginal = [r for r in rows if not isinstance(r.get("risk"), (int, float))]
    dfm_failed = [r for r in scored if r.get("dfm_pass") is False]
    lines.append(f"{len(rows)} 个候选方案都没有达到合格线" if zh else f"None of the {len(rows)} candidates reached the threshold")
    if unoriginal:
        lines.append(f"{len(unoriginal)} 个改动太小或只改了锁定的特征，没过原创性检查" if zh else f"{len(unoriginal)} changed too little (or only locked features) and failed the originality check")
    if dfm_failed:
        lines.append(f"{len(dfm_failed)} 个没过可制造性检查（壁厚、拔模、倒扣等）" if zh else f"{len(dfm_failed)} failed the manufacturability check (walls, draft, undercuts)")
    if scored:
        best = min(scored, key=lambda r: r["risk"])
        lines.append(f"最接近的是 {best.get('version')}：最高风险 {best['risk']:.2f}（{best.get('level')}），仍在合格线以上" if zh else f"Closest: {best.get('version')}, highest risk {best['risk']:.2f} ({best.get('level')}), still above the threshold")
        sees = _still_sees(str(outcome.get("stuck") or ""), str(best.get("version")))
        if sees:
            lines.append(("观察者仍认为相似的地方：" if zh else "What the observer still finds similar: ") + sees)
    return lines


def _pending(run_id: str) -> Path:
    folder = config.home() / "runs" / "claims"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{re.sub(r'[^A-Za-z0-9._-]', '_', run_id)}.json"


def file_claim(run_id: str, end: str, candidates: int, lines: list[str]) -> dict:
    """POST the claim; never raises (the run result matters more than the refund call).

    When the server cannot be reached the claim is kept on disk (no secrets in it) so
    ``designaround run guarantee <run_id>`` can file it later.
    """

    body = {"end": end, "qualified": 0, "candidates": int(candidates), "blockers": lines}
    try:
        data = Server().post(f"/v1/runs/{run_id}/guarantee", body, timeout=20).get("data") or {}
    except CliExit as err:
        if err.code == EXIT_INVALID:  # the server decided: not covered, not eligible, not found
            _pending(run_id).unlink(missing_ok=True)
            return {"run_id": run_id, "status": "rejected", "code": err.kind, "message": err.message, "blockers": lines}
        _pending(run_id).write_text(json.dumps({"run_id": run_id, **body}, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"run_id": run_id, "status": "not_filed", "code": err.kind, "message": err.message, "blockers": lines}
    _pending(run_id).unlink(missing_ok=True)
    return {**data, "run_id": run_id, "blockers": lines}


def retry(run_id: str) -> dict:
    """File a claim kept on disk, or show the server's record of an earlier one."""

    path = _pending(run_id)
    if path.exists():
        body = json.loads(path.read_text(encoding="utf-8"))
        return file_claim(body["run_id"], body["end"], body.get("candidates", 0), body.get("blockers") or [])
    data = Server().get(f"/v1/runs/{run_id}", timeout=20).get("data") or {}
    if data.get("guarantee"):
        return {**data["guarantee"], "already_claimed": True}
    return {"run_id": run_id, "status": "none", "spent": data.get("spent"), "depth": data.get("depth"), "message": "No claim was filed for this run (it produced a qualified candidate, was not covered, or is still running)."}


def after_run(task: str, run: dict | None, outcome: dict, events: list[dict], lang: str = "zh") -> dict | None:
    """Called when an agent run ends: file the claim when the run is covered and eligible."""

    if task != "around" or not run or not run.get("registered") or config.model_mode() != "platform":
        return None
    if not covered(run.get("depth"), run.get("rounds")):
        return None
    end = end_of(outcome, events)
    if end is None:
        return None
    lines = blockers(outcome, events, lang)
    return file_claim(outcome.get("run_id") or run["run_id"], end, len(candidate_rows(outcome, events)), lines)


def describe(claim: dict | None, lang: str = "zh") -> str | None:
    """One line for the result markdown and notifications."""

    if not claim:
        return None
    zh = lang != "en"
    status = claim.get("status")
    refunded = int(claim.get("refunded") or 0)
    spent = int(claim.get("spent") or 0)
    if status in ("refunded", "partial") and refunded:
        if zh:
            return f"结果保障：这次没有达标方案，已退回 {refunded} 点（本次实际花费 {spent} 点的 {int(GUARANTEE['rate'] * 100)}%" + ("，已到本月上限" if status == "partial" else "") + "）。"
        return f"Result guarantee: no qualified candidate, {refunded} credits refunded ({int(GUARANTEE['rate'] * 100)}% of the {spent} spent" + (", monthly limit reached" if status == "partial" else "") + ")."
    if status == "capped":
        return "结果保障：本月的退点次数或额度已用完，这次没有退点。" if zh else "Result guarantee: this month's refund limit is used up; nothing refunded this time."
    if status == "nothing_spent":
        return "结果保障：这次运行没有产生费用，无需退点。" if zh else "Result guarantee: the run spent nothing, so there is nothing to refund."
    run_id = claim.get("run_id") or "<run_id>"
    if status == "not_filed":
        return f"结果保障：退点申请没有提交成功（{claim.get('message')}），稍后运行 `designaround run guarantee {run_id}` 重试。" if zh else f"Result guarantee: the claim could not be filed ({claim.get('message')}); retry later with `designaround run guarantee {run_id}`."
    if status == "rejected":
        return f"结果保障：这次运行不在保障范围内（{claim.get('message')}）。" if zh else f"Result guarantee: this run is not covered ({claim.get('message')})."
    return None
