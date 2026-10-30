"""Filing packages (FR-FILE-01..04, 08..11, 13, 14).

The engine drafts; a registered practitioner reviews, signs and files. Nothing
here submits anything to a patent office.
"""

from __future__ import annotations

import csv
import json
import os
from datetime import date
from pathlib import Path

from designaround_core import fees, quote, rules

from .evidence import manifest_for_dir

DISCLAIMER_EN = "Draft prepared by software for review by a registered patent practitioner. Not legal advice."
DISCLAIMER_ZH = "本文件由软件起草，须经执业专利代理师审核后方可提交，不构成法律意见。"


def _ordinal_views(viewpack: dict) -> list[dict]:
    return sorted(viewpack["figures"], key=lambda f: f["number"])


def us_specification(article: str, viewpack: dict, *, applicant_name: str | None, has_unclaimed: bool, photos: bool = False) -> dict:
    article_l = article.strip().lower()
    title = article.strip().upper()
    figs = _ordinal_views(viewpack)
    descriptions = []
    for index, fig in enumerate(figs):
        if index == 0:
            descriptions.append(f"FIG. {fig['number']} is a {fig['us_label']} of a {article_l} showing our new design;")
        else:
            descriptions.append(f"FIG. {fig['number']} is a {fig['us_label']} thereof;")
    if descriptions:
        descriptions[-1] = descriptions[-1].rstrip(";") + "."
        if len(descriptions) > 1:
            descriptions[-2] = descriptions[-2].rstrip(";") + "; and"
    statements = []
    if has_unclaimed:
        statements.append(f"The broken lines shown in the drawings depict portions of the {article_l} that form no part of the claimed design.")
    omitted = viewpack.get("omitted_views") or []
    if omitted:
        statements.append("[Practitioner to confirm] The omitted views are not required for a complete disclosure of the claimed design.")
    preamble = f"Be it known that {applicant_name or '[APPLICANT NAME]'} {'has' if applicant_name else 'have'} invented a new, original, and ornamental design for a {article_l}, of which the following is a specification, reference being had to the accompanying drawings{' (photographs)' if photos else ''}, forming a part thereof."
    claim = f"The ornamental design for a {article_l}, as shown and described." if statements else f"The ornamental design for a {article_l}, as shown."
    return {
        "title": title,
        "preamble": preamble,
        "cross_reference": "[If claiming priority to a CN application, insert: This application claims priority to Chinese Design Application No. ____, filed ____.]",
        "federal_research": "Not applicable.",
        "figure_descriptions": descriptions,
        "feature_description": statements,
        "claim": claim,
    }


def cn_brief_description(article_zh: str, use_zh: str, viewpack: dict, *, design_points: str, has_unclaimed: bool, similar_count: int = 0) -> dict:
    figs = _ordinal_views(viewpack)
    labels = {f["view"]: f["cn_label"] for f in figs}
    best = labels.get("perspective", figs[0]["cn_label"] if figs else "立体图")
    omitted = viewpack.get("omitted_views") or []
    items = [
        f"1. 本外观设计产品的名称：{article_zh}。",
        f"2. 本外观设计产品的用途：{use_zh}。",
        f"3. 本外观设计产品的设计要点：{design_points}。",
        f"4. 最能表明设计要点的图片或照片：{best}{'（设计1）' if similar_count else ''}。",
    ]
    n = 5
    if omitted:
        from .camera import STANDARD_VIEWS

        names = "、".join(STANDARD_VIEWS[o].cn_label for o in omitted)
        items.append(f"{n}. 省略视图：【请代理师确认理由】{names}为使用时不常见或与其他视图对称，故省略。")
        n += 1
    if similar_count:
        items.append(f"{n}. 本申请包含 {similar_count + 1} 项相似外观设计，设计1为基本设计。")
        n += 1
    if has_unclaimed:
        items.append(f"{n}. 视图中虚线所示部分为不要求保护的部分（局部外观设计）。")
    return {"items": items, "text": "\n".join(items)}


def _write_docx(path: Path, title: str, paragraphs: list[str], disclaimer: str) -> None:
    from docx import Document

    doc = Document()
    doc.add_heading(title, level=1)
    for para in paragraphs:
        doc.add_paragraph(para)
    doc.add_paragraph("")
    doc.add_paragraph(disclaimer).italic = True
    doc.save(str(path))


def ids_rows(prior_art: list[dict]) -> list[dict]:
    rows = []
    for item in prior_art:
        office = (item.get("office") or "").upper()
        rows.append(
            {
                "section": "US patent documents" if office == "US" else "Foreign patent documents" if office else "Non-patent literature",
                "number": item.get("number") or item.get("id"),
                "office": office or None,
                "date": item.get("date") or item.get("grant_date") or item.get("publication_date"),
                "owner": item.get("owner"),
                "title": item.get("title"),
                "source": item.get("source"),
                "relevance": item.get("relevance") or "Considered during design-around screening.",
                "evaluated": bool(item.get("evaluated")),
            }
        )
    return rows


def write_ids(prior_art: list[dict], outdir: Path) -> dict:
    rows = ids_rows(prior_art)
    with open(outdir / "IDS_SB08_draft.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else ["section", "number"])
        writer.writeheader()
        writer.writerows(rows)
    lines = ["# Information Disclosure Statement — draft list (PTO/SB/08)", "", DISCLAIMER_EN, "", "The practitioner decides what to cite. Items come from this case's screening and design-around records.", ""]
    for section in ("US patent documents", "Foreign patent documents", "Non-patent literature"):
        sub = [r for r in rows if r["section"] == section]
        if not sub:
            continue
        lines += [f"## {section}", "", "| Number | Office | Date | Owner | Title | Relevance |", "|---|---|---|---|---|---|"]
        lines += [f"| {r['number']} | {r['office'] or ''} | {r['date'] or ''} | {r['owner'] or ''} | {r['title'] or ''} | {r['relevance']} |" for r in sub]
        lines.append("")
    (outdir / "IDS_SB08_draft.md").write_text("\n".join(lines), encoding="utf-8")
    return {"count": len(rows), "files": ["IDS_SB08_draft.csv", "IDS_SB08_draft.md"]}


def applicant_gaps(office: str, applicant: dict, inventors: list[dict]) -> list[str]:
    gaps = []
    for key in ("name", "domicile", "address", "type"):
        if not applicant.get(key):
            gaps.append(f"applicant.{key}")
    if not inventors:
        gaps.append("inventors (natural persons only)")
    for i, inv in enumerate(inventors):
        for key in ("name", "residence"):
            if not inv.get(key):
                gaps.append(f"inventors[{i}].{key}")
    if office == "US" and not applicant.get("entity_size"):
        gaps.append("applicant.entity_size (large/small/micro)")
    if office == "CN" and "fee_reduction" not in applicant:
        gaps.append("applicant.fee_reduction (费减资格)")
    return gaps


def build_filing_package(
    *,
    office: str,
    outdir: str | os.PathLike,
    case_meta: dict,
    viewpack: dict,
    prior_art: list[dict],
    decisions: list[dict],
    similar_versions: list[str] | None = None,
    has_unclaimed: bool = False,
    disclosures: list[dict] | None = None,
    planned_filing_date: str | None = None,
) -> dict:
    office = office.upper()
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    applicant = case_meta.get("applicant") or {}
    if isinstance(applicant, str):  # hand-edited case.json
        applicant = {"name": applicant}
    inventors = [{"name": i} if isinstance(i, str) else i for i in case_meta.get("inventors") or []]
    article = case_meta.get("article") or case_meta["name"]
    findings = []
    findings += rules.representation_requirements(office, applicant)
    findings += rules.title_findings(office, article if office == "US" else (case_meta.get("article_zh") or article))
    filing_date = date.fromisoformat(planned_filing_date) if planned_filing_date else None
    findings += rules.disclosure_findings(office, disclosures or [], filing_date)
    gaps = applicant_gaps(office, applicant, inventors)
    files = []

    human = human_contribution(decisions, inventors)
    (outdir / "human_contribution.md").write_text(human["markdown"], encoding="utf-8")
    files.append("human_contribution.md")

    if office == "US":
        spec = us_specification(article, viewpack, applicant_name=applicant.get("name"), has_unclaimed=has_unclaimed, photos=viewpack.get("kind") == "photo_viewpack")
        paragraphs = [
            spec["preamble"], "CROSS-REFERENCE TO RELATED APPLICATIONS", spec["cross_reference"],
            "STATEMENT REGARDING FEDERALLY SPONSORED RESEARCH OR DEVELOPMENT", spec["federal_research"],
            "DESCRIPTION OF THE DRAWINGS", *spec["figure_descriptions"], *spec["feature_description"],
            "CLAIM", f"What is claimed is: {spec['claim']}",
        ]
        _write_docx(outdir / "specification.docx", spec["title"], paragraphs, DISCLAIMER_EN)
        (outdir / "specification.md").write_text(f"# {spec['title']}\n\n" + "\n\n".join(paragraphs) + f"\n\n_{DISCLAIMER_EN}_\n", encoding="utf-8")
        files += ["specification.docx", "specification.md"]
        ads = {
            "form": "PTO/AIA/14 (Application Data Sheet) — prefill draft",
            "title_of_invention": spec["title"],
            "application_type": "Design",
            "applicant": applicant,
            "inventors": inventors,
            "correspondence": "[practitioner customer number]",
            "representative": "[USPTO registration number — required for foreign-domiciled applicants]",
            "domestic_benefit_foreign_priority": "[CN application no. / date if priority is claimed]",
            "entity_status": applicant.get("entity_size", "[large/small/micro]"),
            "related_forms": ["PTO/AIA/01 declaration (signed by each inventor)", "PTO/AIA/18 design transmittal", "PTO/SB/17 fee transmittal", "PTO/SB/08 IDS"],
            "missing_fields": gaps,
        }
        (outdir / "ADS_AIA14_prefill.json").write_text(json.dumps(ads, indent=2, ensure_ascii=False), encoding="utf-8")
        files.append("ADS_AIA14_prefill.json")
        ids = write_ids(prior_art, outdir)
        files += ids["files"]
        q = quote.filing_quote(["US"], entity=applicant.get("entity_size", "small"))
    elif office == "CN":
        article_zh = case_meta.get("article_zh") or article
        use_zh = case_meta.get("use_zh") or f"用于{article_zh}的日常使用"
        points = case_meta.get("design_points_zh") or "在于产品的形状"
        brief = cn_brief_description(article_zh, use_zh, viewpack, design_points=points, has_unclaimed=has_unclaimed, similar_count=len(similar_versions or []))
        _write_docx(outdir / "简要说明.docx", "简要说明", brief["items"], DISCLAIMER_ZH)
        (outdir / "简要说明.md").write_text("# 简要说明\n\n" + brief["text"] + f"\n\n_{DISCLAIMER_ZH}_\n", encoding="utf-8")
        files += ["简要说明.docx", "简要说明.md"]
        request = {
            "表格": "外观设计专利请求书 — 预填草稿",
            "使用外观设计的产品名称": article_zh,
            "设计人": [i.get("name") for i in inventors],
            "申请人": applicant,
            "专利代理机构": "[代理机构名称、代码]（外国申请人必须委托）",
            "相似设计": len(similar_versions or []) + 1 if similar_versions else 1,
            "要求优先权声明": "[如有]",
            "费用减缴": applicant.get("fee_reduction"),
            "缺项": gaps,
        }
        (outdir / "请求书_预填.json").write_text(json.dumps(request, indent=2, ensure_ascii=False), encoding="utf-8")
        files.append("请求书_预填.json")
        q = quote.filing_quote(["CN"], fee_reduction=bool(applicant.get("fee_reduction")), similar_designs=len(similar_versions or []))
    elif office == "EU":
        locarno = case_meta.get("locarno") or applicant.get("locarno")
        form = {
            "form": "EUIPO EU design application — pre-filled draft (e-filing data)",
            "indication_of_product": article,
            "locarno_class": locarno or "[Locarno class, e.g. 06-04]",
            "number_of_designs": 1 + len(similar_versions or []),
            "views": [{"number": f["number"], "view": f["view"], "file": f["jpg"]} for f in viewpack.get("figures", [])],
            "applicant": applicant,
            "designers": [{"name": i.get("name"), "waiver": False} for i in inventors],
            "representative": "[EUIPO-registered representative — required when the applicant has no EEA domicile or establishment]",
            "deferment_of_publication": "[optional: up to 30 months from filing/priority]",
            "priority": "[earlier filing (office, number, date) within 6 months, if claimed]",
            "disclaimer": DISCLAIMER_EN,
        }
        if not locarno:
            gaps.append("locarno class of the product (e.g. 06-04) for the indication of product")
        if not form["views"]:
            gaps.append("views (build the EU view pack first)")
        form["missing_fields"] = gaps
        (outdir / "euipo_application.json").write_text(json.dumps(form, indent=2, ensure_ascii=False), encoding="utf-8")
        files.append("euipo_application.json")
        q = quote.filing_quote(["EU"], similar_designs=len(similar_versions or []))
    else:
        raise ValueError("office must be US, CN or EU")

    status = rules.worst(findings)
    checklist = {
        "office": office,
        "rulepack_version": rules.RULEPACK_VERSION,
        "fee_table_version": fees.FEE_TABLE_VERSION,
        "status": status,
        "ready_for_practitioner": status != "error" and not gaps,
        "requires_practitioner_gate": any(f.rule_id.startswith("REP-") and f.severity == "block" for f in findings),
        "findings": [f.to_dict() for f in findings],
        "missing": gaps,
        "similar_versions": similar_versions or [],
        "quote": q,
        "disclaimer": DISCLAIMER_ZH if office == "CN" else DISCLAIMER_EN,
    }
    (outdir / "checklist.json").write_text(json.dumps(checklist, indent=2, ensure_ascii=False), encoding="utf-8")
    files.append("checklist.json")
    return {"office": office, "files": files, "checklist": checklist, "manifest": manifest_for_dir(outdir)}


def human_contribution(decisions: list[dict], inventors: list[dict]) -> dict:
    """Summary of human conception and choices (USPTO 2025-11 guidance: only natural persons are inventors)."""

    lines = [
        "# Human contribution record / 人的贡献记录",
        "",
        "AI tools were used to explore and draw candidate designs. The design choices below were made by natural persons.",
        "",
        "| Time (UTC) | Person | Decision | Detail |",
        "|---|---|---|---|",
    ]
    count = 0
    for item in decisions:
        if item.get("actor") != "user":
            continue
        count += 1
        when = item.get("ts_iso", item.get("ts"))
        lines.append(f"| {when} | {item.get('person', '[inventor]')} | {item.get('decision')} | {json.dumps(item.get('detail', {}), ensure_ascii=False)} |")
    if not count:
        lines.append("| – | – | No user decisions recorded yet | Record constraint, selection and tweak decisions before filing. |")
    lines += ["", f"Named inventors: {', '.join(i.get('name', '?') for i in inventors) or '[to be completed]'}", "", DISCLAIMER_EN]
    return {"count": count, "markdown": "\n".join(lines)}


def suggest_similar_designs(main: str, candidates: list[dict], *, band=(0.62, 0.97), limit: int = 9) -> list[dict]:
    """candidates: [{"version": vid, "qualified": bool, "similarity_to_main": float}] -> picked list."""

    picked = [c for c in candidates if c["version"] != main and c.get("qualified") and band[0] <= c["similarity_to_main"] <= band[1]]
    picked.sort(key=lambda c: c["similarity_to_main"])
    return picked[:limit]
