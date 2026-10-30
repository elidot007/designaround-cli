"""Decision cards (FR-UX-02) and the "keep / redesign / abandon" overview (FR-UX-03).

A card is computed from the case's own records (concepts, versions, evaluations,
prior art, screens, filing packages, gates). It never calls a model, costs nothing
and is safe to show at any time. Wording rule: risk assessment, never a legal
conclusion; a registered practitioner confirms before filing or selling.
"""

from __future__ import annotations

import json
from pathlib import Path

from designaround_core import catalog
from designaround_core import quote as quote_mod

from . import concepts as concepts_mod
from .workspace import Case, open_case

DISCLAIMER = {
    "zh": "风险评估，不是法律意见；上架或提交前须由执业专利代理师确认。",
    "en": "Risk assessment only, not a legal opinion; a registered practitioner must confirm before you sell or file.",
}
LEVEL = {
    "zh": {"high": "高", "medium": "中", "low": "低", "not_seen": "未见"},
    "en": {"high": "high", "medium": "medium", "low": "low", "not_seen": "none seen"},
}
STAGES = ["start", "concepts", "build", "screen", "evaluate", "redesign", "file", "submit"]
STAGE_LABEL = {
    "zh": {"start": "需求", "concepts": "设计方向", "build": "3D 建模", "screen": "检索", "evaluate": "比对评估", "redesign": "改型", "file": "申请材料", "submit": "提交"},
    "en": {"start": "Requirements", "concepts": "Concepts", "build": "3D model", "screen": "Screening", "evaluate": "Assessment", "redesign": "Design-around", "file": "Filing package", "submit": "Submission"},
}
# Typical spend and time, from the price catalog and measured runs (2026-10).
TYPICAL = {
    "concepts": {"credits": 25 * 4 + 40, "minutes": "2–4"},
    "build": {"credits": 20, "max_credits": 3000, "minutes": "3–6"},
    "screen_quick": {"credits": catalog.ACTION_CREDITS["screen.quick"], "minutes": "1–3"},
    "screen_deep": {"credits": catalog.ACTION_CREDITS["screen.deep"], "minutes": "3–10"},
    "observe": {"credits": 10, "minutes": "1–2"},
    "around": {"credits": 1500, "max_credits": catalog.AROUND_DEPTHS["standard"]["max_credits"], "minutes": "20–40"},
}


def _usd(credits: int) -> float:
    return round(credits * catalog.CREDIT_USD, 2)


def _latest_screen(case: Case) -> dict | None:
    folder = case.root / "screens"
    files = sorted(folder.glob("*.json")) if folder.exists() else []
    if not files:
        return None
    data = json.loads(files[-1].read_text(encoding="utf-8"))
    return {
        "file": str(files[-1]),
        "at": files[-1].stem,
        "candidates": len(data.get("candidates") or []),
        "blind_spots": data.get("blind_spots") or [],
        "source_failures": data.get("source_failures") or [],
        "queries": data.get("queries") or [],
        "jurisdictions": data.get("jurisdictions") or case.meta.get("jurisdictions") or [],
    }


def _evaluation(case: Case, vid: str | None) -> dict | None:
    if not vid:
        return None
    path = case.evaluation_path(vid)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _filings(case: Case) -> list[dict]:
    root = case.root / "filings"
    rows = []
    if root.exists():
        for package in sorted(root.glob("*/*/package")):
            office, vid = package.parent.parent.name, package.parent.name
            checklist = package / "checklist.json"
            status = json.loads(checklist.read_text(encoding="utf-8")).get("status") if checklist.exists() else None
            rows.append({"office": office, "version": vid, "dir": str(package), "status": status})
    return rows


def _events(case: Case, kind: str) -> list[dict]:
    return [e for e in case.log.read() if e.get("type") == kind]


def facts(case_id: str) -> dict:
    """Everything the card and the report need, gathered once."""

    case = open_case(case_id)
    meta = case.meta
    versions = case.versions()
    original = case.original_version()
    concept_rows = concepts_mod.concepts(case_id)
    evaluation = _evaluation(case, original)
    candidates = []
    for v in versions:
        if v.get("kind") != "candidate":
            continue
        ev = _evaluation(case, v["id"])
        candidates.append({"id": v["id"], "label": v.get("label"), "risk": v.get("risk"), "level": v.get("risk_level"), "qualified": bool(v.get("qualified")), "originality": (v.get("originality") or {}).get("pass"), "evaluated": ev is not None})
    spent = sum(int((e.get("payload") or {}).get("charged") or 0) for e in _events(case, "screen"))
    return {
        "case": case.id,
        "name": meta.get("name"),
        "article": meta.get("article_zh") or meta.get("article"),
        "jurisdictions": meta.get("jurisdictions") or [],
        "brief": meta.get("brief") or {},
        "photos": bool((case.root / "photos").exists() and any((case.root / "photos").iterdir())),
        "concepts": [{"id": c["id"], "title": c.get("title"), "pass": (c.get("review") or {}).get("pass")} for c in concept_rows],
        "chosen_concept": meta.get("chosen_concept"),
        "original": original,
        "drafts": [{"id": v["id"], "match": (v.get("review") or {}).get("match")} for v in versions if v.get("kind") == "draft"],
        "candidates": candidates,
        "prior_art": len(case.prior_art()),
        "prior_art_with_figures": sum(1 for p in case.prior_art() if p.get("figures")),
        "screen": _latest_screen(case),
        "evaluation": evaluation,
        "filings": _filings(case),
        "gates": [e.get("payload") or {} for e in _events(case, "gate.created")],
        "baselines": [e.get("payload") or {} for e in _events(case, "baseline.frozen")],
        "disclosures": case.disclosures(),
        "screen_credits_spent": spent,
    }


def _filing_quote(offices: list[str], managed: bool = False) -> dict:
    offices = [o for o in offices if o in ("US", "CN", "EU")] or ["US"]
    return quote_mod.filing_quote(offices, managed=managed)


def _managed(offices: list[str], redesign: bool) -> dict | None:
    key = {
        (True, ("CN",)): "managed_cn_around_file",
        (True, ("US",)): "managed_us_around_file",
        (True, ("CN", "US")): "managed_cn_us_around_file",
        (False, ("CN",)): "managed_cn_file_only",
        (False, ("US",)): "managed_us_file_only",
    }.get((redesign, tuple(sorted(o for o in offices if o in ("US", "CN")))))
    if not key:
        return None
    pkg = catalog.MANAGED_PACKAGES[key]
    return {"package": key, "platform_usd": pkg["platform_usd"], "practitioner_usd_est": pkg["practitioner_usd_est"], "official_fees": "extra"}


def _step(lang: str, label_zh: str, label_en: str, *, command: str | None = None, prompt_zh: str | None = None, prompt_en: str | None = None, credits: int | None = None, max_credits: int | None = None, minutes: str | None = None, free: bool = False) -> dict:
    step = {"label": label_zh if lang == "zh" else label_en, "command": command, "prompt": (prompt_zh if lang == "zh" else prompt_en) or (label_zh if lang == "zh" else label_en)}
    if credits is not None:
        step["credits"] = credits
        step["usd"] = _usd(credits)
    if max_credits is not None:
        step["max_credits"] = max_credits
    if minutes:
        step["minutes"] = minutes
    if free:
        step["free"] = True
    return step


def _risk_reasons(f: dict, lang: str, limit: int = 3) -> list[str]:
    ev = f.get("evaluation") or {}
    # Observer-scored rights decide; outline-only rows are listed after them.
    rows = sorted(ev.get("per_prior_art") or [], key=lambda r: (r.get("basis") != "shape+observer", -r.get("risk", 0)))
    out = []
    for r in rows[:limit]:
        obs = r.get("observer") or {}
        similar = "；".join(obs.get("similar") or [])[:120] if lang == "zh" else "; ".join(obs.get("similar") or [])[:160]
        lvl = LEVEL[lang].get(r.get("level"), r.get("level"))
        if lang == "zh":
            text = f"{r.get('number') or r.get('prior_art_id')}：风险 {r.get('risk', 0):.2f}（{lvl}）" + (f"，观察者 {obs.get('score')}/10" if obs.get("score") is not None else "，只有轮廓分") + (f"；相同点：{similar}" if similar else "")
        else:
            text = f"{r.get('number') or r.get('prior_art_id')}: risk {r.get('risk', 0):.2f} ({lvl})" + (f", observer {obs.get('score')}/10" if obs.get("score") is not None else ", outline score only") + (f"; shared: {similar}" if similar else "")
        out.append(text)
    return out


def _options(f: dict, lang: str, *, best_candidate: dict | None) -> list[dict]:
    """FR-UX-03: keep as is / redesign / abandon, side by side."""

    offices = f["jurisdictions"] or ["US"]
    q = _filing_quote(offices)
    fees = round(q["total_usd_estimate"], 0)
    ev = f.get("evaluation") or {}
    summary = ev.get("summary") or {}
    level = LEVEL[lang].get(summary.get("level"), summary.get("level"))
    around = TYPICAL["around"]
    zh = lang == "zh"
    keep = {
        "key": "keep",
        "label": "不改，直接推进" if zh else "Keep the design",
        "risk": f"{summary.get('max_risk', 0):.2f}（{level}）" if zh else f"{summary.get('max_risk', 0):.2f} ({level})",
        "platform_credits": 0,
        "filing_usd_estimate": fees,
        "minutes": None,
        "note": "风险不变；适合风险低的情况" if zh else "Risk stays as assessed; suits low-risk results",
    }
    redesign = {
        "key": "redesign",
        "label": "改型后推进" if zh else "Design around first",
        "risk": (f"目标 < {summary.get('threshold', 0.4):.2f}" if zh else f"target < {summary.get('threshold', 0.4):.2f}") if not best_candidate or best_candidate.get("risk") is None else (f"{best_candidate['id']} 已到 {best_candidate['risk']:.2f}" if zh else f"{best_candidate['id']} reached {best_candidate['risk']:.2f}"),
        "platform_credits": around["credits"],
        "platform_usd": _usd(around["credits"]),
        "max_credits": around["max_credits"],
        "filing_usd_estimate": fees,
        "minutes": around["minutes"],
        "note": "另需评估改模成本；标准或深度改型没有任何达标方案时，自动退回实际花费的 50%" if zh else "Mould changes to be assessed; a standard or deep run with no qualified candidate refunds 50% of its spend automatically",
    }
    abandon = {
        "key": "abandon",
        "label": "放弃这个款" if zh else "Drop the product",
        "risk": "无" if zh else "none",
        "platform_credits": 0,
        "filing_usd_estimate": 0,
        "minutes": None,
        "note": (f"已花费检索 {f['screen_credits_spent']} 积分" if zh else f"Spent so far on screening: {f['screen_credits_spent']} credits"),
    }
    return [keep, redesign, abandon]


def _next_deadline(case_id: str) -> dict | None:
    """The next hard deadline from recorded filings, notices and disclosures (FR-FILE-12)."""

    from datetime import date

    from designaround_core import deadlines as dl

    from . import api

    case = open_case(case_id)
    data = api.prosecution(case_id)
    disclosures = case.disclosures()
    matters = api._matters(case)
    if not (data["filings"] or data["notices"] or disclosures or matters):
        return None
    today = date.today()
    for d in dl.compute(offices=case.meta.get("jurisdictions") or [], filings=data["filings"], notices=data["notices"], disclosures=disclosures, matters=matters):
        if d.hard and d.due >= today:
            return d.to_dict(today)
    return None


def decision_card(case_id: str, *, lang: str = "zh") -> dict:
    """One sentence verdict, reasons, next steps with cost and time, and the managed option."""

    lang = "en" if lang == "en" else "zh"
    zh = lang == "zh"
    f = facts(case_id)
    case = f["case"]
    offices = f["jurisdictions"] or ["US"]
    card: dict = {"case": case, "lang": lang, "reasons": [], "next": [], "options": None, "managed": None, "blind_spots": [], "wording": DISCLAIMER[lang]}

    def done(stage: str, verdict: str, headline: str, summary: str) -> dict:
        card.update({"stage": stage, "stage_label": STAGE_LABEL[lang][stage], "stage_index": STAGES.index(stage), "stages": [STAGE_LABEL[lang][s] for s in STAGES], "verdict": verdict, "headline": headline, "summary": summary})
        if f.get("screen"):
            card["blind_spots"] = f["screen"]["blind_spots"][:6]
            card["screened_at"] = f["screen"]["at"]
        nd = _next_deadline(case)
        if nd:
            card["deadline"] = nd
            if nd["days_left"] <= 30:
                card["reasons"].insert(0, f"期限提醒：{nd['title_zh']}，{nd['due']}（还有 {nd['days_left']} 天）" if zh else f"Deadline: {nd['title_en']} on {nd['due']} ({nd['days_left']} days left)")
        return card

    original = f["original"]
    if not original:
        if f["drafts"]:
            d = f["drafts"][-1]
            card["reasons"].append(f"3D 草稿 {d['id']} 与概念图一致度 {d['match']}/10，没有通过独立审核" if zh else f"3D draft {d['id']} matched the concept {d['match']}/10 and did not pass the independent review")
            card["next"] += [
                _step(lang, "重新建模", "Build again", command=f"designaround design build {case} --max-credits {TYPICAL['build']['max_credits']}", prompt_zh="再建一次 3D 模型", prompt_en="Build the 3D model again", credits=TYPICAL["build"]["credits"], max_credits=TYPICAL["build"]["max_credits"], minutes=TYPICAL["build"]["minutes"]),
                _step(lang, f"我确认采用草稿 {d['id']}", f"Accept draft {d['id']} myself", command=f'designaround design accept {case} {d["id"]} --person "<name>"', prompt_zh=f"我看过了，采用草稿 {d['id']} 作为原始设计", prompt_en=f"I reviewed it; use draft {d['id']} as the original design", free=True),
            ]
            return done("build", "pending", "3D 草稿等你确认" if zh else "3D draft needs your decision", "建模没有通过审核，可以重建、简化方向，或你确认后采用。" if zh else "The build did not pass review: build again, simplify the concept, or accept the draft yourself.")
        if f["chosen_concept"]:
            card["next"].append(_step(lang, f"把方向 {f['chosen_concept']} 建成 3D", f"Build concept {f['chosen_concept']} in 3D", command=f"designaround design build {case} --max-credits {TYPICAL['build']['max_credits']}", prompt_zh=f"把方向 {f['chosen_concept']} 建成 3D 模型", prompt_en=f"Build concept {f['chosen_concept']} in 3D", credits=TYPICAL["build"]["credits"], max_credits=TYPICAL["build"]["max_credits"], minutes=TYPICAL["build"]["minutes"]))
            return done("build", "pending", "已选定方向，下一步建 3D" if zh else "Concept chosen; build it in 3D", "CAD Agent 会从零建参数化模型，独立审核对照概念图。" if zh else "The CAD Agent builds a parametric model; an independent reviewer compares it with the concept.")
        passed = [c for c in f["concepts"] if c.get("pass")]
        if passed:
            card["reasons"].append(("通过审核的方向：" if zh else "Concepts that passed review: ") + ", ".join(f"{c['id']} {c['title']}" for c in passed[:6]))
            card["next"] += [
                _step(lang, "选一个方向", "Choose a concept", command=f'designaround design choose {case} <concept> --person "<name>"', prompt_zh="我选这个方向：", prompt_en="I choose this concept: ", free=True),
                _step(lang, "按我的意见改一个方向", "Refine a concept with my feedback", command=f'designaround design concepts {case} --base <concept> --feedback "..." --max-credits 300', prompt_zh="把这个方向改一下：", prompt_en="Refine this concept: ", credits=90, minutes="2–3"),
            ]
            return done("concepts", "pending", "挑一个设计方向" if zh else "Pick a design direction", "看概念图，选一个，或者说要改哪里。" if zh else "Look at the concepts and pick one, or say what to change.")
        if f["brief"] or f["concepts"]:
            card["next"].append(_step(lang, "生成 4 个设计方向", "Generate 4 design directions", command=f"designaround design concepts {case} --count 4 --max-credits 680", prompt_zh="按我的需求出 4 个设计方向", prompt_en="Generate 4 design directions from my requirements", credits=TYPICAL["concepts"]["credits"], max_credits=680, minutes=TYPICAL["concepts"]["minutes"]))
            return done("concepts", "pending", "需求已记下，下一步出设计方向" if zh else "Requirements recorded; next, design directions", "每张概念图都由独立审核员检查。" if zh else "Every concept image is checked by an independent reviewer.")
        if f["photos"]:
            card["next"].append(_step(lang, "按照片建 3D 样品", "Build the 3D sample from the photos", command=f"designaround model {case} --agent --known length=<mm> --max-credits 1500", prompt_zh="按照片建 3D 样品，已知尺寸：", prompt_en="Build the 3D sample from the photos; known size: ", credits=30, max_credits=1500, minutes="3–6"))
            return done("build", "pending", "照片已上传，下一步建样品" if zh else "Photos uploaded; next, the 3D sample", "需要一个真实尺寸，例如长度。" if zh else "One real dimension is needed, for example the length.")
        card["next"] += [
            _step(lang, "我有一个产品想法", "I have a product idea", prompt_zh="我想做一款新产品：", prompt_en="I want to design a new product: ", free=True),
            _step(lang, "我有照片或 CAD 文件", "I have photos or a CAD file", prompt_zh="我上传了产品照片/CAD 文件，帮我查风险", prompt_en="I uploaded photos / a CAD file; check the risk", free=True),
        ]
        return done("start", "pending", "先告诉我你手上有什么" if zh else "Tell me what you have", "想法、照片、STEP/STL 文件，或收到的专利号都可以。" if zh else "An idea, photos, a STEP/STL file or a patent number all work.")

    # From here the case has an original design (version) to assess.
    if not f["screen"] and not f["prior_art"]:
        card["next"] += [
            _step(lang, "深度检索（以图搜图）", "Deep screening (image search)", command=f"designaround screen {case} --deep --max-credits 400", prompt_zh="帮我做深度外观专利检索", prompt_en="Run a deep design-patent screening", credits=TYPICAL["screen_deep"]["credits"], minutes=TYPICAL["screen_deep"]["minutes"]),
            _step(lang, "快速检索（每月前 5 次免费）", "Quick screening (5 free a month)", command=f"designaround screen {case} --max-credits 60", prompt_zh="先做一次快速检索", prompt_en="Run a quick screening first", credits=TYPICAL["screen_quick"]["credits"], minutes=TYPICAL["screen_quick"]["minutes"]),
        ]
        return done("screen", "pending", "设计已就绪，下一步检索" if zh else "Design ready; next, screening", f"检索 {', '.join(offices)} 已公开的外观设计。" if zh else f"Search published designs in {', '.join(offices)}.")

    ev = f["evaluation"]
    summary = (ev or {}).get("summary") or {}
    if not ev or summary.get("pending_observer"):
        pending = summary.get("pending_observer") or []
        card["reasons"].append((f"找到 {f['prior_art']} 件在先设计（{f['prior_art_with_figures']} 件有图）" if zh else f"{f['prior_art']} prior designs found ({f['prior_art_with_figures']} with figures)") + ((f"；待观察者评分：{', '.join(pending)}" if zh else f"; observer score pending: {', '.join(pending)}") if pending else ""))
        command = f"designaround observe {case} {original} {pending[0]} --max-credits 120" if pending else f"designaround evaluate {case} {original}"
        card["next"].append(_step(lang, "让独立观察者逐件比对", "Have the isolated observer compare the closest ones", command=command, prompt_zh="让观察者比对最像的几件，并给出风险", prompt_en="Have the observer compare the closest prior designs and give the risk", credits=TYPICAL["observe"]["credits"] * max(1, len(pending) or 3), minutes=TYPICAL["observe"]["minutes"]))
        return done("evaluate", "pending", "检索完成，下一步比对" if zh else "Screening done; next, the comparison", "轮廓排序只用来挑候选，结论以观察者评分为准。" if zh else "Outline ranking only picks candidates; the observer score decides.")

    level = summary.get("level")
    qualified_candidates = sorted([c for c in f["candidates"] if c["qualified"] and c["originality"] is not False], key=lambda c: c["risk"] if c["risk"] is not None else 1)
    best = qualified_candidates[0] if qualified_candidates else None
    if best:
        lvl = LEVEL[lang].get(best.get("level"), best.get("level"))
        card["reasons"].append(f"改型方案 {best['id']}：最高风险 {best['risk']:.2f}（{lvl}），已过原创性检查，低于合格线" if zh else f"Candidate {best['id']}: highest risk {best['risk']:.2f} ({lvl}), passed the originality check, below the threshold")
        card["reasons"] += [("原款 " if zh else "Original ") + r for r in _risk_reasons(f, lang, limit=2)]
    else:
        card["reasons"] += _risk_reasons(f, lang)
    card["options"] = _options(f, lang, best_candidate=best)
    target = best["id"] if best else original

    filed = [x for x in f["filings"] if x["version"] == target]
    if filed:
        card["reasons"].append((f"{target} 的申请材料已生成：" if zh else f"Filing package for {target}: ") + ", ".join(f"{x['office']} {x['status'] or ''}".strip() for x in filed))
        gate_open = any((g.get("type") == "filing_submission") for g in f["gates"])
        frozen = [b for b in f["baselines"] if b.get("version") == target]
        if frozen:
            card["reasons"].append(f"提交基线 {frozen[-1]['baseline']} 已冻结" if zh else f"Submission baseline {frozen[-1]['baseline']} frozen")
        else:
            card["next"].append(_step(lang, "冻结提交基线", "Freeze the submission baseline", command=f"designaround baseline freeze {case} {target} --office {filed[0]['office']}", prompt_zh=f"冻结 {target} 的提交基线", prompt_en=f"Freeze the submission baseline of {target}", free=True))
        if not gate_open:
            card["next"].append(_step(lang, "交给执业代理师审核提交", "Send to a registered practitioner", command=f"designaround gate create {case} --type filing_submission --reviewer-email <practitioner>", prompt_zh="把申请材料发给代理师审核", prompt_en="Send the filing package to a practitioner for review", free=True))
        from . import api as _api

        if not _api.prosecution(case)["filings"]:
            card["next"].append(_step(lang, "拿到申请号后记录申请日（自动算优先权和各项期限）", "Record the filing date once filed (priority and deadlines follow)", command=f"designaround filing record {case} --office {offices[0]} --filed <YYYY-MM-DD> --number <application-no>", prompt_zh="我已经提交了申请，申请日和申请号是：", prompt_en="I have filed; the filing date and number are: ", free=True))
        card["managed"] = _managed(offices, redesign=False)
        return done("submit", "proceed", "申请材料已备好，交代理师提交" if zh else "Filing package ready for the practitioner", "拿到申请号之后再上架或公开。" if zh else "Sell or publish only after you have the application number.")

    if best or level in ("low", "not_seen"):
        verdict_text = (f"改型方案 {best['id']} 达标，可以准备申请" if zh else f"Candidate {best['id']} qualifies; prepare the filing") if best else ("风险低，可以推进" if zh else "Low risk; you can proceed")
        card["next"] += [
            _step(lang, f"生成 {'/'.join(offices)} 申请材料包", f"Build the {'/'.join(offices)} filing package", command=f"designaround filing-pack {case} {target} --office {offices[0]}", prompt_zh=f"给 {target} 准备 {'、'.join(offices)} 的申请材料", prompt_en=f"Prepare the {', '.join(offices)} filing package for {target}", free=True),
            _step(lang, "打样包（STEP/STL + 中文说明）", "Prototype pack (STEP/STL + factory notes)", command=f"designaround proto {case} {target}", prompt_zh=f"给 {target} 出打样包", prompt_en=f"Make the prototype pack for {target}", free=True),
        ]
        card["managed"] = _managed(offices, redesign=False)
        return done("file", "proceed", verdict_text, ("申请前别公开；代理师确认后提交。" if zh else "Do not publish before filing; a practitioner confirms and files."))

    tried = [c for c in f["candidates"] if c["evaluated"]]
    if level == "high" and len(tried) >= 6 and not qualified_candidates:
        card["next"] += [
            _step(lang, "换一个整体造型方向", "Try a different overall form", command=f'designaround design concepts {case} --count 4 --max-credits 680', prompt_zh="换几个整体造型完全不同的方向", prompt_en="Give me directions with a completely different overall form", credits=TYPICAL["concepts"]["credits"], minutes=TYPICAL["concepts"]["minutes"]),
            _step(lang, "看托管报价（代理师参与）", "Managed service quote (with a practitioner)", command="designaround quote " + " ".join(f"--office {o}" for o in offices if o in ("US", "CN")) + " --managed", prompt_zh="我想了解托管服务的报价", prompt_en="Show me the managed service quote", free=True),
        ]
        card["managed"] = _managed(offices, redesign=True)
        return done("redesign", "abandon", "建议放弃或大改方向" if zh else "Consider dropping it or changing direction", (f"已评估 {len(tried)} 个改型方案，都没有降到合格线以下。" if zh else f"{len(tried)} assessed candidates; none came below the threshold."))

    card["next"] += [
        _step(lang, "自动改型（Agent 团队）", "Automatic design-around (agent team)", command=f"designaround around {case} --depth standard --max-credits {TYPICAL['around']['max_credits']}", prompt_zh="帮我做改型，保留主要功能", prompt_en="Design around it and keep the main function", credits=TYPICAL["around"]["credits"], max_credits=TYPICAL["around"]["max_credits"], minutes=TYPICAL["around"]["minutes"]),
        _step(lang, "自己调参数后快速复检", "Adjust parameters and re-check quickly", command=f"designaround cad tweak {case} {original} --set <param>=<value>", prompt_zh="我想自己调几个参数", prompt_en="I want to adjust some parameters myself", free=True),
    ]
    card["managed"] = _managed(offices, redesign=True)
    return done("redesign", "redesign", "建议改了再做" if zh else "Redesign before you sell", (f"最高风险 {summary.get('max_risk', 0):.2f}（{LEVEL[lang].get(level, level)}），高于合格线 {summary.get('threshold', 0.4):.2f}。" if zh else f"Highest risk {summary.get('max_risk', 0):.2f} ({LEVEL[lang].get(level, level)}), above the {summary.get('threshold', 0.4):.2f} threshold."))


def card_markdown(card: dict) -> str:
    zh = card.get("lang") != "en"
    lines = [f"**{card['headline']}** · {card['stage_label']}", "", card["summary"]]
    if card["reasons"]:
        lines += [""] + [f"- {r}" for r in card["reasons"]]
    if card["next"]:
        lines += ["", "**下一步**" if zh else "**Next**"]
        for s in card["next"]:
            cost = "免费" if zh and s.get("free") else ("free" if s.get("free") else (f"约 {s['credits']} 积分（${s['usd']}）" if zh else f"~{s['credits']} credits (${s['usd']})") if s.get("credits") is not None else "")
            when = (f"，{s['minutes']} 分钟" if zh else f", {s['minutes']} min") if s.get("minutes") else ""
            lines.append(f"- {s['label']}{'：' if zh else ': '}{cost}{when}" + (f" — `{s['command']}`" if s.get("command") else ""))
    if card.get("options"):
        lines += ["", "| " + (" | ".join(["选择", "风险", "平台费", "申请费（估）", "时间"] if zh else ["Option", "Risk", "Platform", "Filing (est.)", "Time"])) + " |", "|---|---|---|---|---|"]
        for o in card["options"]:
            platform = f"{o['platform_credits']} 积分" if zh else f"{o['platform_credits']} credits"
            lines.append(f"| {o['label']} | {o['risk']} | {platform} | ${o['filing_usd_estimate']:.0f} | {o.get('minutes') or '–'} |")
    if card.get("managed"):
        m = card["managed"]
        lines += ["", (f"托管：平台费 ${m['platform_usd']} + 代理费约 ${m['practitioner_usd_est']}（直付代理师），官费另计" if zh else f"Managed: platform ${m['platform_usd']} + practitioner ~${m['practitioner_usd_est']} (paid to them directly), official fees extra")]
    if card.get("blind_spots"):
        lines += ["", ("盲区：" if zh else "Blind spots: ") + "；".join(card["blind_spots"][:3])]
    lines += ["", f"_{card['wording']}_"]
    return "\n".join(lines)


def card_path(case_id: str) -> Path:
    return open_case(case_id).root / "card.json"
