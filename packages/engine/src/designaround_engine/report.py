"""Risk assessment report PDF, Chinese or English (FR-RISK-07, FR-RISK-05).

Built from the case's own records: the latest evaluation of a version (fused shape and
observer scores), the prior art and its figures, the latest screening (queries,
sources, blind spots) and the decision card. No model calls; the same inputs give
the same report. Every report carries the standard blind-spot statement and the
"risk assessment, not a legal opinion" wording.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import advisor
from .evidence import sha256_file
from .workspace import open_case

T = {
    "zh": {
        "title": "外观设计风险评估报告",
        "case": "案卷", "article": "产品", "version": "评估的版本", "markets": "目标法域", "generated": "生成时间", "screened": "检索时间",
        "conclusion": "结论", "design": "评估的设计（着色视图）", "table": "逐件评估",
        "cols": ["#", "在先设计", "法域", "状态", "轮廓相似", "观察者", "风险", "等级"],
        "closest": "最接近的在先设计", "similar": "相同点", "different": "不同点", "rationale": "观察者理由",
        "method": "方法与数据来源", "blind": "盲区声明", "next": "下一步",
        "method_lines": [
            "检索：DesignAround 服务器调用 ClearComply 注册库检索（{queries}）；美国外观从 USPTO 公开检索服务取整页图纸。",
            "排序：本地 CPU 比较多视角剪影，只用于挑出要细看的在先设计。",
            "评分：独立观察者模型（看不到设计理由）按固定评分细则逐件多视图打分，取 3 次中位数；风险 = 0.1 × 轮廓 + 0.9 × 观察者，融合版本 {fusion}，合格线 {threshold}。",
            "权重尚未用执业代理人标注数据校准。",
        ],
        "blind_lines": [
            "在审未公开的申请、未注册的外观、商标和版权不在本次检索范围内。",
            "检索结果取决于各数据源当时的覆盖范围；中国外观部分记录没有附图，无法按形状比较。",
            "检索时间之后公开或授权的设计不在报告内；上架前建议复检。",
        ],
        "none": "没有已评估的在先设计。",
        "page": "第 {n} 页",
    },
    "en": {
        "title": "Design-patent risk assessment",
        "case": "Case", "article": "Article", "version": "Version assessed", "markets": "Markets", "generated": "Generated", "screened": "Screened",
        "conclusion": "Conclusion", "design": "Design assessed (shaded views)", "table": "Prior designs assessed",
        "cols": ["#", "Prior design", "Office", "Status", "Outline", "Observer", "Risk", "Level"],
        "closest": "Closest prior designs", "similar": "Shared", "different": "Different", "rationale": "Observer's reasoning",
        "method": "Method and sources", "blind": "Blind spots", "next": "Next steps",
        "method_lines": [
            "Search: the DesignAround server queries registered designs through ClearComply ({queries}); US designs get full drawing sheets from the USPTO public search service.",
            "Ranking: local CPU comparison of multi-view silhouettes, used only to pick which prior designs to look at closely.",
            "Scoring: an isolated observer model (it never sees the design rationale) scores each prior design over several views with a fixed rubric, median of 3; risk = 0.1 x outline + 0.9 x observer, fusion {fusion}, threshold {threshold}.",
            "The weights are not yet calibrated against practitioner labels.",
        ],
        "blind_lines": [
            "Unpublished pending applications, unregistered designs, trademarks and copyright are outside this search.",
            "Results depend on each source's coverage at search time; some Chinese design records have no figures and cannot be compared by shape.",
            "Designs published or granted after the search date are not included; re-screen before you sell.",
        ],
        "none": "No prior design has been assessed.",
        "page": "Page {n}",
    },
}


def _font() -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    try:
        pdfmetrics.getFont("STSong-Light")
    except KeyError:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    return "STSong-Light"


def _when(stamp: str | None) -> str:
    """20261027T174200 -> 2026-10-27 17:42."""

    if not stamp or len(stamp) < 13:
        return stamp or "–"
    return f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[9:11]}:{stamp[11:13]}"


def _esc(text) -> str:
    return str(text if text is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def risk_report(case_id: str, vid: str | None = None, *, lang: str = "zh", out: str | None = None, filing_packs: bool = False) -> dict:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        Image,
        KeepTogether,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    from . import api

    lang = "en" if lang == "en" else "zh"
    t = T[lang]
    case = open_case(case_id)
    vid = vid or case.original_version()
    if not vid:
        from .errors import InvalidInput

        raise InvalidInput("The case has no design version to assess yet.")
    ev_path = case.evaluation_path(vid)
    evaluation = json.loads(ev_path.read_text(encoding="utf-8")) if ev_path.exists() else None
    facts = advisor.facts(case_id)
    card = advisor.decision_card(case_id, lang=lang, filing_packs=filing_packs)
    screen = facts.get("screen") or {}
    font = _font()
    base = ParagraphStyle("b", fontName=font, fontSize=9.5, leading=14)
    small = ParagraphStyle("s", parent=base, fontSize=8, leading=11, textColor=colors.HexColor("#555555"))
    h1 = ParagraphStyle("h1", parent=base, fontSize=18, leading=24, spaceAfter=6)
    h2 = ParagraphStyle("h2", parent=base, fontSize=12.5, leading=18, spaceBefore=10, spaceAfter=4)
    verdict = ParagraphStyle("v", parent=base, fontSize=13, leading=18, textColor=colors.HexColor("#B4232A"))

    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    story = [Paragraph(_esc(t["title"]), h1)]
    info = [
        [t["case"], f"{facts['name'] or ''} ({facts['case']})"],
        [t["article"], facts.get("article") or ""],
        [t["version"], vid],
        [t["markets"], ", ".join(facts["jurisdictions"])],
        [t["screened"], _when(screen.get("at"))],
        [t["generated"], stamp],
    ]
    story.append(Table([[Paragraph(_esc(a), small), Paragraph(_esc(b), base)] for a, b in info], colWidths=[32 * mm, 140 * mm], style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)])))

    story += [Paragraph(_esc(t["conclusion"]), h2), Paragraph(_esc(card["headline"]), verdict), Paragraph(_esc(card["summary"]), base)]
    for reason in card["reasons"][:5]:
        story.append(Paragraph("· " + _esc(reason), base))

    sheet = api.contact_sheet(case.version_path(vid), kind="render")
    story += [Paragraph(_esc(t["design"]), h2), Image(sheet, width=172 * mm, height=172 * mm * 0.53)]

    story.append(Paragraph(_esc(t["table"]), h2))
    rows = sorted((evaluation or {}).get("per_prior_art") or [], key=lambda r: -r.get("risk", 0))
    prior = {p["id"]: p for p in case.prior_art()}
    if rows:
        data = [[Paragraph(_esc(c), small) for c in t["cols"]]]
        for i, r in enumerate(rows[:25], start=1):
            pa = prior.get(r["prior_art_id"], {})
            obs = r.get("observer") or {}
            data.append([
                Paragraph(str(i), small),
                Paragraph(_esc(f"{r.get('number') or pa.get('number', '')} {pa.get('title') or ''}")[:90], small),
                Paragraph(_esc(pa.get("office") or ""), small),
                Paragraph(_esc(pa.get("status") or ""), small),
                Paragraph(f"{(r.get('shape') or {}).get('score', 0):.2f}", small),
                Paragraph(f"{obs.get('score')}/10" if obs.get("score") is not None else "–", small),
                Paragraph(f"{r.get('risk', 0):.2f}", small),
                Paragraph(_esc(advisor.LEVEL[lang].get(r.get("level"), r.get("level"))), small),
            ])
        table = Table(data, colWidths=[8 * mm, 62 * mm, 14 * mm, 20 * mm, 17 * mm, 17 * mm, 15 * mm, 17 * mm], repeatRows=1)
        table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#BBBBBB")), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F2F0EC")), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
        story.append(table)
    else:
        story.append(Paragraph(_esc(t["none"]), base))

    closest = [r for r in rows if prior.get(r["prior_art_id"], {}).get("figures")][:3]
    if closest:
        story += [PageBreak(), Paragraph(_esc(t["closest"]), h2)]
        for r in closest:
            obs = r.get("observer") or {}
            try:
                pair = api.pair_image(case_id, vid, r["prior_art_id"])["path"]
            except Exception:  # noqa: BLE001 - a missing figure must not stop the report
                pair = None
            block = [Paragraph(_esc(f"{r.get('number') or r['prior_art_id']} · {advisor.LEVEL[lang].get(r.get('level'), r.get('level'))} · {r.get('risk', 0):.2f}"), base)]
            if pair:
                block.append(Image(pair, width=172 * mm, height=172 * mm / 3 * 1.06))
            for key, label in (("similar", t["similar"]), ("different", t["different"])):
                notes = advisor.phrases_for_lang(obs.get(key) or [], lang)
                if notes:
                    block.append(Paragraph(_esc(f"{label}: " + "; ".join(notes)[:400]), small))
            rationale = advisor.phrases_for_lang([obs["rationale"]], lang) if obs.get("rationale") else []
            if rationale:
                block.append(Paragraph(_esc(f"{t['rationale']}: {rationale[0]}")[:600], small))
            block.append(Spacer(1, 8))
            story.append(KeepTogether(block))

    if card["next"]:
        story.append(Paragraph(_esc(t["next"]), h2))
        for step in card["next"]:
            phrase = advisor.step_cost_phrase(step, lang)
            cost = (" · " + phrase) if phrase else ""
            story.append(Paragraph("· " + _esc(step["label"] + cost) + (f"  <font size=7>{_esc(step['command'])}</font>" if step.get("command") else ""), base))

    observer_models = sorted({(r.get("observer") or {}).get("model") for r in rows if (r.get("observer") or {}).get("model")})
    story.append(Paragraph(_esc(t["method"]), h2))
    queries = "; ".join(q if isinstance(q, str) else f"{q.get('query')} [{', '.join(q.get('sources') or [])}]" for q in (screen.get("queries") or [])[:4]) or "–"
    fusion = (rows[0].get("fusion") if rows else None) or "–"
    threshold = ((evaluation or {}).get("summary") or {}).get("threshold", 0.4)
    for line in t["method_lines"]:
        story.append(Paragraph("· " + _esc(line.format(queries=queries, fusion=fusion, threshold=threshold)), small))
    if observer_models:
        story.append(Paragraph("· " + _esc(("观察者模型：" if lang == "zh" else "Observer model: ") + ", ".join(observer_models)), small))

    story.append(Paragraph(_esc(t["blind"]), h2))
    for line in list(t["blind_lines"]) + list(screen.get("blind_spots") or [])[:6] + [f"source failure: {s}" for s in (screen.get("source_failures") or [])[:3]]:
        story.append(Paragraph("· " + _esc(line if isinstance(line, str) else json.dumps(line, ensure_ascii=False)), small))
    story += [Spacer(1, 10), Paragraph(_esc(advisor.DISCLAIMER[lang]), ParagraphStyle("d", parent=base, textColor=colors.HexColor("#B4232A")))]

    folder = case.root / "reports"
    folder.mkdir(exist_ok=True)
    target = Path(out) if out else folder / f"risk-{vid}-{lang}-{time.strftime('%Y%m%dT%H%M%S')}.pdf"

    pages = {"n": 0}

    def footer(canvas, doc):
        pages["n"] = doc.page
        canvas.saveState()
        canvas.setFont(font, 7.5)
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.drawString(18 * mm, 10 * mm, f"DesignAround · {facts['case']} · {vid} · {advisor.DISCLAIMER[lang]}"[:150])
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, t["page"].format(n=doc.page))
        canvas.restoreState()

    doc = SimpleDocTemplate(str(target), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=18 * mm, title=t["title"], author="DesignAround")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    digest = sha256_file(target)
    case.event("report", {"kind": "risk", "version": vid, "lang": lang, "file": str(target.relative_to(case.root)) if target.is_relative_to(case.root) else str(target), "sha256": digest, "evaluated": evaluation is not None})
    return {"path": str(target), "sha256": digest, "version": vid, "lang": lang, "pages": pages["n"], "verdict": card["verdict"], "headline": card["headline"]}
