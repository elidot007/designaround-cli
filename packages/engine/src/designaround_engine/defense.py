"""Complaints and lawsuits (FR-DEF-01..06).

* intake: the notice (PDF text is read; images are kept as they are), the complaint
  or case number, the asserted right. The right type and the channel follow from
  rules: Amazon APEX handles US utility patents only, so a design-patent complaint
  never goes there; a Schedule A / TRO lawsuit is a court matter with a 21-day
  answer period after service (FRCP 12(a)).
* pack: three-way comparison (asserted design, the seller's product, the closest
  earlier designs) with the observer findings on record, the earlier-design search
  record for counsel (validity after LKQ v. GM, Fed. Cir. 2024), independent-design
  evidence from the case's hash chain, and a platform appeal draft.

Nothing here is a legal conclusion: the pack prepares material for a registered
practitioner or litigation counsel (gate types platform_appeal / tro_response).
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from .errors import InvalidInput, NotFound
from .workspace import open_case

PLATFORMS = ("amazon", "temu", "walmart", "shein", "tiktok", "ebay", "aliexpress", "court", "other")
KINDS = ("design_patent", "utility_patent", "trademark", "copyright", "unknown")
KIND_ZH = {"design_patent": "外观设计专利", "utility_patent": "发明 / 实用新型专利", "trademark": "商标", "copyright": "版权", "unknown": "未识别"}
PLATFORM_ZH = {"amazon": "亚马逊", "temu": "Temu", "walmart": "沃尔玛", "shein": "SHEIN", "tiktok": "TikTok Shop", "ebay": "eBay", "aliexpress": "速卖通", "court": "美国法院", "other": "其他平台"}

US_DESIGN = re.compile(r"\b(?:US\s?)?D\s?(\d{1,3}(?:[,.]?\d{3}){1,2})(?:\s?S)?\b", re.I)  # D987,654 · D1,029,631 · USD1029631S
US_UTILITY = re.compile(r"\b(?:US\s?)?(\d{1,2}),?(\d{3}),?(\d{3})\s?(?:B\d)?\b")
CN_PATENT = re.compile(r"\b(?:ZL\s?)?(20\d{2})([123])(\d{7})\.?([\dX])\b", re.I)
EU_DESIGN = re.compile(r"\b(\d{9})-(\d{4})\b")


def classify(right: str | None, text: str = "", hint: str | None = None) -> dict:
    """Right type, office and normalised number from the asserted right and the notice text."""

    if hint in KINDS and hint != "unknown":
        kind = hint
    else:
        kind = "unknown"
    blob = f"{right or ''}\n{text or ''}"
    office, number = None, (right or "").strip() or None
    m = US_DESIGN.search(right or "") or (US_DESIGN.search(text) if not right else None)
    digits = re.sub(r"\D", "", m.group(1)) if m else ""
    if m and len(digits) in (6, 7):
        office, number = "US", f"USD{digits}S"
        kind = kind if kind != "unknown" else "design_patent"
    elif (c := CN_PATENT.search(right or "")) is not None:
        office, number = "CN", f"ZL{c.group(1)}{c.group(2)}{c.group(3)}.{c.group(4)}"
        kind = kind if kind != "unknown" else {"3": "design_patent", "1": "utility_patent", "2": "utility_patent"}[c.group(2)]
    elif EU_DESIGN.search(right or ""):
        office = "EU"
        kind = kind if kind != "unknown" else "design_patent"
    elif US_UTILITY.search(right or "") and kind == "unknown":
        office, kind = "US", "utility_patent"
    low = blob.lower()
    if kind == "unknown":
        if re.search(r"design patent|外观设计|外观专利|ornamental design", low):
            kind = "design_patent"
        elif re.search(r"utility patent|\bapex\b|发明专利|实用新型", low):
            kind = "utility_patent"
        elif re.search(r"trademark|商标|®|\breg\. no\b", low):
            kind = "trademark"
        elif re.search(r"copyright|版权|著作权", low):
            kind = "copyright"
    lawsuit = bool(re.search(r"schedule a|temporary restraining order|\btro\b|preliminary injunction|district court|civil action|case no\.?\s*\d|临时禁令|起诉", low))
    return {"kind": kind, "office": office, "number": number, "lawsuit": lawsuit}


def route(platform: str, info: dict) -> dict:
    """Channel and steps for the matter (FR-DEF-01: design patents never go through APEX)."""

    kind = info["kind"]
    if platform == "court" or info.get("lawsuit"):
        return {
            "route": "court_tro",
            "apex": False,
            "label_zh": "美国法院诉讼（常见为 Schedule A / TRO）",
            "label_en": "US court lawsuit (often Schedule A / TRO)",
            "steps_zh": ["立即联系美国诉讼律师：送达后 21 天内必须答辩（放弃送达为 60 天），法院命令中的日期优先", "确认平台资金是否被冻结、冻结范围", "把三方对比、在先设计检索记录和独立设计证据交给律师评估", "和原告律师的沟通、和解都通过自己的律师进行"],
            "steps_en": ["Contact US litigation counsel now: the answer is due 21 days after service (60 days if service was waived); court-ordered dates prevail", "Check whether and how much of the platform balance is frozen", "Give counsel the three-way comparison, the earlier-design search and the independent-design evidence", "Talk to the plaintiff's lawyers and settle only through your own counsel"],
            "gate": "tro_response",
        }
    if kind in ("trademark", "copyright"):
        return {"route": "out_of_scope", "apex": False, "label_zh": f"{KIND_ZH[kind]}投诉（不属于外观设计）", "label_en": f"{kind.replace('_', ' ')} complaint (not a design right)", "steps_zh": [f"{KIND_ZH[kind]}投诉需要的证据不同（例如授权、来源、使用证据），请找对应领域的律师", "DesignAround 只能提供设计过程记录作为独立创作的参考"], "steps_en": ["This needs different evidence (licences, sources, use); talk to a lawyer in that field", "DesignAround can only supply the design-process record as background"], "gate": "platform_appeal"}
    if platform == "amazon" and kind == "utility_patent":
        return {"route": "amazon_apex", "apex": True, "label_zh": "亚马逊 APEX（中立评估，只适用于美国发明专利）", "label_en": "Amazon APEX (neutral evaluation, US utility patents only)", "steps_zh": ["按 APEX 通知写明的期限选择参加或不参加；不回复时商品会被下架", "DesignAround 的外观比对不适用于发明专利的权利要求分析，请找专利律师"], "steps_en": ["Opt in or out within the period in the APEX notice; no reply means the listings come down", "Design comparisons do not answer utility-patent claims; ask a patent attorney"], "gate": "platform_appeal"}
    name = PLATFORM_ZH.get(platform, platform)
    return {
        "route": "platform_complaint",
        "apex": False,
        "label_zh": f"{name}知识产权投诉（外观专利不走 APEX）" if platform == "amazon" else f"{name}知识产权投诉",
        "label_en": f"{platform.title()} IP complaint" + (" (design patents do not go through APEX)" if platform == "amazon" else ""),
        "steps_zh": ["核对投诉的专利号、授权状态和保护的外观（图片）", "准备三方对比：权利人的外观、你的产品、更早的现有设计", "在平台的知识产权投诉记录里提交申诉；平台通常接受权利人撤诉、授权证明、法院判决或律师意见等材料，以平台当时的要求为准", "可以联系权利人说明差异并请求撤回投诉"],
        "steps_en": ["Check the patent number, its status and the protected design (figures)", "Prepare the three-way comparison: the asserted design, your product, earlier designs", "Appeal in the platform's IP-complaint record; platforms usually accept a retraction, a licence, a court decision or an attorney's letter (their current rules apply)", "You can ask the rights owner to retract, explaining the differences"],
        "gate": "platform_appeal",
    }


def _matters_dir(case) -> Path:
    path = case.root / "defense"
    path.mkdir(exist_ok=True)
    return path


def matters(case_id: str) -> list[dict]:
    case = open_case(case_id)
    rows = []
    for path in sorted(_matters_dir(case).glob("*/intake.json")):
        rows.append(json.loads(path.read_text(encoding="utf-8")))
    return rows


def matter(case_id: str, matter_id: str | None) -> dict:
    rows = matters(case_id)
    if not rows:
        raise NotFound("No complaint or lawsuit recorded on this case.", details={"next": f"designaround defense intake {case_id} --platform amazon --number <complaint-id> --right <patent-no>"})
    if not matter_id:
        return rows[-1]
    for r in rows:
        if r["id"] == matter_id or r["id"].startswith(matter_id):
            return r
    raise NotFound(f"No matter {matter_id}.", details={"matters": [r["id"] for r in rows]})


def _notice_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".txt", ".md", ".eml", ".html", ".htm"):
        return path.read_text(encoding="utf-8", errors="replace")[:200_000]
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader

            return "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages[:30])[:200_000]
        except Exception:  # noqa: BLE001 - scanned or broken PDF: keep the file, no text
            return ""
    return ""


def intake(case_id: str, *, platform: str, number: str | None, right: str | None, notice: str | None = None, kind: str | None = None, received: str | None = None, served: str | None = None, hearing: str | None = None, respond_by: str | None = None, listings: list[str] | None = None, note: str | None = None) -> dict:
    case = open_case(case_id)
    platform = (platform or "other").lower()
    if platform not in PLATFORMS:
        raise InvalidInput(f"platform must be one of {', '.join(PLATFORMS)}")
    text = ""
    mid = time.strftime("m%Y%m%d%H%M%S")
    folder = _matters_dir(case) / mid
    folder.mkdir(parents=True)
    saved = None
    if notice:
        src = Path(notice)
        if not src.is_file():
            raise NotFound(f"Notice file not found: {notice}")
        saved = folder / ("notice" + src.suffix.lower())
        shutil.copyfile(src, saved)
        text = _notice_text(saved)
    info = classify(right, text, kind)
    channel = route(platform, info)
    for label, value in (("received", received), ("served", served), ("hearing", hearing), ("respond_by", respond_by)):
        if value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise InvalidInput(f"{label} must be YYYY-MM-DD")
    record = {
        "id": mid,
        "platform": platform,
        "number": (number or "").strip() or None,
        "right": info["number"] or right,
        "right_kind": info["kind"],
        "right_office": info["office"],
        "lawsuit": info["lawsuit"] or platform == "court",
        "route": channel["route"],
        "apex": channel["apex"],
        "channel": channel,
        "received": received,
        "served": served,
        "hearing": hearing,
        "respond_by": respond_by,
        "listings": listings or [],
        "note": (note or "")[:500] or None,
        "notice_file": saved.relative_to(case.root).as_posix() if saved else None,
        "notice_text_chars": len(text),
        "created_at": time.time(),
        "prior_art_id": None,
    }
    (folder / "intake.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    case.event("defense.intake", {k: record[k] for k in ("id", "platform", "number", "right", "right_kind", "route", "apex", "received", "served")})
    return record


def link_prior_art(case_id: str, matter_id: str, prior_id: str) -> dict:
    case = open_case(case_id)
    m = matter(case_id, matter_id)
    case.prior_art_path(prior_id)
    m["prior_art_id"] = prior_id
    (_matters_dir(case) / m["id"] / "intake.json").write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    return m


# ---------------------------------------------------------------------- pack

TIMELINE = {
    "case.created": ("案卷建立", "Case created"),
    "photos.added": ("上传产品照片", "Product photos added"),
    "concept.created": ("生成设计方向", "Design concept generated"),
    "concept.reviewed": ("设计方向独立审核", "Concept reviewed"),
    "decision": ("人的决定", "Human decision"),
    "original.set": ("设定原始设计", "Original design set"),
    "version.created": ("保存设计版本", "Design version saved"),
    "agent.around.start": ("开始规避改型", "Design-around started"),
    "agent.around.end": ("规避改型结束", "Design-around finished"),
    "viewpack": ("生成专利视图", "Patent views made"),
    "proto_pack": ("生成打样包", "Prototype pack made"),
    "baseline.frozen": ("冻结提交基线", "Submission baseline frozen"),
    "filing.recorded": ("记录申请", "Filing recorded"),
}


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))


def _detail(kind: str, p: dict) -> str:
    if kind == "decision":
        return f"{p.get('person') or p.get('actor') or ''}: {p.get('decision', '')}"[:120]
    if kind == "version.created":
        return " ".join(str(x) for x in (p.get("version") or p.get("id"), p.get("kind"), p.get("created_by")) if x)[:120]
    if kind.startswith("concept."):
        return " ".join(str(x) for x in (p.get("concept"), p.get("title"), "pass" if p.get("pass") else "") if x)[:120]
    if kind == "filing.recorded":
        return f"{p.get('office')} {p.get('filed')} {p.get('number') or ''}"
    return ""


def independent_design_record(case) -> dict:
    events = case.log.read()
    ok, bad = case.log.verify()
    rows = [{"at": _iso(e["ts"]), "ts": e["ts"], "type": e["type"], "zh": TIMELINE[e["type"]][0], "en": TIMELINE[e["type"]][1], "detail": _detail(e["type"], e.get("payload") or {}), "hash": e["hash"][:16]} for e in events if e["type"] in TIMELINE]
    anchors = [e for e in events if e["type"] == "evidence.anchored"]
    return {"events": rows, "chain_ok": ok, "first_bad": bad, "total_events": len(events), "anchors": len(anchors), "earliest": rows[0]["at"] if rows else None, "earliest_ts": rows[0]["ts"] if rows else None}


def _cell(path: Path | None, size: tuple[int, int]):
    from PIL import Image, ImageOps

    w, h = size
    canvas = Image.new("RGB", (w, h), "white")
    if path and Path(path).exists():
        try:
            with Image.open(path) as im:
                im = ImageOps.contain(im.convert("RGB"), (w - 16, h - 16))
                canvas.paste(im, ((w - im.width) // 2, (h - im.height) // 2))
        except Exception:  # noqa: BLE001 - unreadable figure stays blank
            pass
    return canvas


def three_way_sheet(case, version: str, asserted: dict, priors: list[dict], out: Path, lang: str) -> Path:
    from PIL import Image, ImageDraw

    from .watermark import _font

    zh = lang != "en"
    views_dir = case.version_path(version) / "views"
    ours = [views_dir / f"{v}.lineart.png" for v in ("perspective", "front", "top")]
    columns = [(("你的产品 " if zh else "Your product ") + version, ours)]
    columns.append(((("被主张的外观 " if zh else "Asserted design ") + (asserted.get("number") or asserted["id"])), [case.root / f for f in asserted.get("figures", [])[:3]]))
    for p in priors[:2]:
        columns.append(((("更早的设计 " if zh else "Earlier design ") + (p.get("number") or p["id"]) + (f" ({p.get('date')})" if p.get("date") else "")), [case.root / f for f in p.get("figures", [])[:3]]))
    cw, ch, head = 360, 280, 46
    sheet = Image.new("RGB", (cw * len(columns), head + ch * 3), "white")
    draw = ImageDraw.Draw(sheet)
    font, _ = _font(17)
    for i, (title, paths) in enumerate(columns):
        draw.rectangle((i * cw, 0, (i + 1) * cw - 1, head - 1), fill=(244, 245, 247) if i else (232, 240, 255))
        draw.text((i * cw + 12, 12), title[:34], font=font, fill=(20, 22, 26))
        for r in range(3):
            sheet.paste(_cell(paths[r] if r < len(paths) else None, (cw, ch)), (i * cw, head + r * ch))
        draw.line(((i + 1) * cw - 1, 0, (i + 1) * cw - 1, sheet.height), fill=(220, 222, 226), width=1)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    return out


def _date_key(value) -> str:
    return str(value or "")[:10]


def pack(case_id: str, version: str, *, matter_id: str | None = None, prior_ids: list[str] | None = None, seller: str | None = None, lang: str = "zh") -> dict:
    from . import api

    lang = "en" if lang == "en" else "zh"
    zh = lang == "zh"
    case = open_case(case_id)
    case.version_path(version)
    m = matter(case_id, matter_id)
    if not m.get("prior_art_id"):
        right = m.get("right") or m.get("number") or "<patent number>"
        add = f"designaround prior-art add {case_id} --number '{right}' -f <drawing.png>"
        link = f"designaround defense link {case_id} <prior-art-id>"
        raise InvalidInput(
            "The asserted design is not on this case, so the pack has nothing to compare. "
            f"`defense intake --no-fetch` records the complaint without downloading drawings. "
            f"Add the asserted design yourself, then link it: `{add}` and `{link}`. "
            "For a US design patent you can also run intake again without --no-fetch and DesignAround will fetch the USPTO drawings.",
            details={"next": [add, link, "designaround defense intake <case>  # omit --no-fetch to fetch US drawings"]},
        )
    by_id = {p["id"]: p for p in case.prior_art()}
    asserted = by_id.get(m["prior_art_id"])
    if asserted is None:
        raise NotFound(f"Prior art {m['prior_art_id']} is gone from the case.")
    judged = api.stored_judgments(case_id, version)
    if prior_ids:
        priors = [by_id[p] for p in prior_ids if p in by_id and p != asserted["id"]]
    else:
        ranked = [r["id"] for r in api.shape_scores(case_id, version) if r["id"] != asserted["id"]]
        cutoff = _date_key(asserted.get("date"))
        earlier = [by_id[r] for r in ranked if not cutoff or (by_id[r].get("date") and _date_key(by_id[r]["date"]) < cutoff)]
        priors = earlier[:2]
    stamp = time.strftime("%Y%m%dT%H%M%S")
    out = _matters_dir(case) / m["id"] / f"pack-{stamp}"
    out.mkdir(parents=True)
    sheet = three_way_sheet(case, version, asserted, priors, out / "three_way.png", lang)
    record = independent_design_record(case)
    j_asserted = judged.get(asserted["id"]) or {}
    findings = [{"prior": p, "judgment": judged.get(p["id"])} for p in priors]
    search = sorted(({k: p.get(k) for k in ("id", "number", "office", "title", "date", "status", "source")} for p in case.prior_art() if p["id"] != asserted["id"]), key=lambda r: _date_key(r.get("date")) or "9999")
    draft = appeal_draft(m, asserted, j_asserted, priors, record, seller=seller)
    (out / "appeal_draft.txt").write_text(draft["en"] + "\n\n" + "-" * 60 + "\n\n" + draft["zh"], encoding="utf-8")
    _docx(out / "appeal_draft.docx", draft)
    report = out / ("应对材料.pdf" if zh else "defense_pack.pdf")
    _report_pdf(report, case, version, m, asserted, j_asserted, findings, search, record, sheet, draft, lang)
    from .evidence import manifest_for_dir

    files = manifest_for_dir(out)
    (out / "manifest.json").write_text(json.dumps({"matter": m["id"], "version": version, "asserted": asserted["id"], "priors": [p["id"] for p in priors], "files": files}, ensure_ascii=False, indent=2), encoding="utf-8")
    case.event("defense.pack", {"matter": m["id"], "version": version, "asserted": asserted["id"], "priors": [p["id"] for p in priors], "files": {f["path"]: f["sha256"] for f in files}})
    missing = [pid for pid in [asserted["id"], *[p["id"] for p in priors]] if pid not in judged]
    return {"dir": str(out), "report": str(report), "sheet": str(sheet), "appeal_draft": str(out / "appeal_draft.docx"), "matter": m, "asserted": asserted["id"], "priors": [p["id"] for p in priors], "observer_missing": missing, "earliest_record": record["earliest"], "chain_ok": record["chain_ok"], "gate": m["channel"]["gate"]}


def appeal_draft(m: dict, asserted: dict, j: dict, priors: list[dict], record: dict, *, seller: str | None) -> dict:
    platform = m["platform"]
    listings = ", ".join(m.get("listings") or []) or "[ASIN / listing IDs]"
    diffs = [d for d in (j.get("different") or []) if d][:5]
    diff_en = "\n".join(f"   - {d}" for d in diffs) or "   - [main visual differences — run the observer comparison first]"
    earlier = ", ".join(f"{p.get('number') or p['id']} ({p.get('date') or 'date unknown'})" for p in priors) or "[earlier designs, if any]"
    earliest = record.get("earliest") or "[date]"
    if m["route"] == "court_tro":
        en = f"""Notes for litigation counsel — {m.get('number') or '[case number]'}

Asserted right: {asserted.get('number') or m.get('right')} ({KIND_ZH.get(m['right_kind'], m['right_kind'])} / {m['right_kind']})
Served: {m.get('served') or '[date of service]'} — answer due 21 days after service (FRCP 12(a)(1)(A)(i)) unless the court ordered otherwise.

1. Independent design: hash-chained design records start on {earliest} ({record['total_events']} events, chain {'verified' if record['chain_ok'] else 'NOT verified'}).
2. Visual differences noted by the isolated observer model (not a legal finding):
{diff_en}
3. Earlier designs for the ordinary-observer comparison in light of the prior art (Egyptian Goddess v. Swisa) and for validity (LKQ v. GM, 2024): {earlier}
4. Attached: three-way comparison sheet, earlier-design search record, design timeline.
"""
        zh = f"""给美国诉讼律师的要点 — {m.get('number') or '[案号]'}

被主张的权利：{asserted.get('number') or m.get('right')}（{KIND_ZH.get(m['right_kind'], m['right_kind'])}）
送达日：{m.get('served') or '[送达日期]'}；答辩期限为送达后 21 天（FRCP 12(a)(1)(A)(i)），法院命令中的日期优先。

1. 独立设计：设计记录从 {earliest} 开始，共 {record['total_events']} 条事件，哈希链{'校验通过' if record['chain_ok'] else '校验未通过'}。
2. 独立观察者模型看到的差异（不是法律结论）：
{chr(10).join(f'   - {d}' for d in diffs) or '   - [主要差异：先运行观察者比对]'}
3. 更早的设计（用于在现有设计背景下的普通观察者比较，以及 LKQ v. GM 之后的有效性评估）：{earlier}
4. 附件：三方对比图、在先设计检索记录、设计过程时间线。
"""
        return {"en": en, "zh": zh}
    who = seller or "[Seller name]"
    en = f"""Subject: Appeal of intellectual property complaint {m.get('number') or '[complaint ID]'} — {listings}

Dear {platform.title()} Seller Performance / IP team,

We received complaint {m.get('number') or '[complaint ID]'} alleging that the listing(s) above infringe {asserted.get('number') or m.get('right')}. We respectfully ask you to review it and reinstate the listing(s):

1. Independent design. Our product was designed independently. Our design records begin on {earliest} and document each design step and decision with time-stamped, hash-chained entries; we can provide them on request.
2. Visual differences. Compared with the asserted design, our product differs in:
{diff_en}
3. Earlier designs. Designs published before the asserted patent show the features the two products share: {earlier}.

Attached: a three-way comparison of the asserted design, our product and the earlier designs. [Attach an attorney's letter if you have one.]

Sincerely,
{who}

[Draft prepared with DesignAround. Have it reviewed by an attorney and adapt it to the platform's current appeal form. Do not state anything you cannot document.]
"""
    zh = f"""主题：针对知识产权投诉 {m.get('number') or '[投诉编号]'} 的申诉 — {listings}

{PLATFORM_ZH.get(platform, platform)}卖家绩效 / 知识产权团队：

我们收到投诉 {m.get('number') or '[投诉编号]'}，称上述商品侵犯 {asserted.get('number') or m.get('right')}。请复核并恢复商品：

1. 独立设计：我们的产品是独立设计的。设计记录从 {earliest} 开始，每一步设计和决定都有带时间戳、哈希链接的记录，可以按要求提供。
2. 外观差异：和被主张的外观相比，我们的产品在以下方面不同：
{chr(10).join(f'   - {d}' for d in diffs) or '   - [主要差异：先运行观察者比对]'}
3. 更早的设计：在被主张专利之前已公开的设计，已经具有两件产品的共同特征：{earlier}。

附件：被主张的外观、我们的产品和更早设计的三方对比图。[如有律师函请一并附上。]

此致
{who}

[本草稿由 DesignAround 生成，提交前请律师审阅，并按平台当时的申诉表格调整；不要写没有证据支持的内容。]
"""
    return {"en": en, "zh": zh}


def _docx(path: Path, draft: dict) -> None:
    from docx import Document

    doc = Document()
    for part in (draft["en"], draft["zh"]):
        for line in part.splitlines():
            doc.add_paragraph(line)
        doc.add_page_break()
    doc.save(str(path))


def _report_pdf(path: Path, case, version: str, m: dict, asserted: dict, j: dict, findings: list[dict], search: list[dict], record: dict, sheet: Path, draft: dict, lang: str) -> None:
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    from .viewpack import _ensure_cjk

    zh = lang == "zh"
    font = _ensure_cjk()
    h1 = ParagraphStyle("h1", fontName=font, fontSize=16, leading=22, spaceAfter=8)
    h2 = ParagraphStyle("h2", fontName=font, fontSize=12.5, leading=18, spaceBefore=10, spaceAfter=4)
    body = ParagraphStyle("b", fontName=font, fontSize=9.5, leading=14.5)
    small = ParagraphStyle("s", fontName=font, fontSize=8, leading=12, textColor=colors.HexColor("#5b6068"))
    P = lambda t, s=body: Paragraph(escape(str(t)).replace("\n", "<br/>"), s)  # noqa: E731
    story = [P(("投诉 / 诉讼应对材料（供律师审阅）" if zh else "Complaint / lawsuit response pack (for counsel)"), h1)]
    story.append(P(f"{case.meta.get('name') or case.id} · {version} · {m['id']} · {time.strftime('%Y-%m-%d')}", small))
    ch = m["channel"]
    story += [P("事项与通道" if zh else "Matter and channel", h2)]
    rows = [[("平台" if zh else "Platform"), PLATFORM_ZH.get(m["platform"], m["platform"]) if zh else m["platform"]], [("投诉 / 案件编号" if zh else "Complaint / case no."), m.get("number") or "-"], [("被主张的权利" if zh else "Asserted right"), f"{m.get('right') or '-'} · {KIND_ZH.get(m['right_kind']) if zh else m['right_kind']}"], [("通道" if zh else "Channel"), ch["label_zh"] if zh else ch["label_en"]], [("是否走 APEX" if zh else "APEX"), ("是" if m["apex"] else "否") if zh else ("yes" if m["apex"] else "no")]]
    t = Table([[P(a), P(b)] for a, b in rows], colWidths=[38 * mm, 132 * mm])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d9dce1")), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(t)
    for i, step in enumerate(ch["steps_zh"] if zh else ch["steps_en"], 1):
        story.append(P(f"{i}. {step}"))
    story += [P("三方对比" if zh else "Three-way comparison", h2), Image(str(sheet), width=170 * mm, height=170 * mm * _ratio(sheet))]
    story.append(P("美国外观专利侵权按普通观察者标准，在现有设计的背景下比较（Egyptian Goddess v. Swisa, 2008）：与更早设计共有的特征，权利人主张的范围更窄。以下评分来自独立的视觉模型，仅作参考，不是侵权结论。" if zh else "US design-patent infringement uses the ordinary-observer test in light of the prior art (Egyptian Goddess v. Swisa, 2008): features shared with earlier designs narrow what the owner can claim. Scores below come from an isolated vision model; they are not an infringement finding.", small))
    jrows = [[("比较" if zh else "Comparison"), ("评分 0-10" if zh else "Score 0-10"), ("相似" if zh else "Similar"), ("不同" if zh else "Different")]]
    jrows.append([f"{version} vs {asserted.get('number') or asserted['id']}", str(j.get("score", "-" if j else ("待评分" if zh else "pending"))), "; ".join(j.get("similar") or [])[:300], "; ".join(j.get("different") or [])[:300]])
    for f in findings:
        jj = f["judgment"] or {}
        jrows.append([f"{version} vs {f['prior'].get('number') or f['prior']['id']}", str(jj.get("score", "待评分" if zh else "pending")), "; ".join(jj.get("similar") or [])[:300], "; ".join(jj.get("different") or [])[:300]])
    t = Table([[P(c) for c in r] for r in jrows], colWidths=[40 * mm, 20 * mm, 55 * mm, 55 * mm])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d9dce1")), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f3f5")), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(t)
    story += [P("在先设计检索记录" if zh else "Earlier-design search record", h2)]
    story.append(P(("有效性 / 显而易见性按 LKQ v. GM（美国联邦巡回上诉法院全院，2024）的标准评估，由律师判断。被主张专利日期：" if zh else "Validity / obviousness is assessed under LKQ v. GM (Fed. Cir. en banc, 2024) by counsel. Asserted patent date: ") + str(asserted.get("date") or "-"), small))
    srows = [[("号码" if zh else "Number"), ("法域" if zh else "Office"), ("名称" if zh else "Title"), ("日期" if zh else "Date"), ("来源" if zh else "Source")]] + [[r.get("number") or r["id"], r.get("office") or "", (r.get("title") or "")[:40], r.get("date") or "", (r.get("source") or "")[:24]] for r in search[:40]]
    t = Table([[P(c) for c in r] for r in srows], colWidths=[34 * mm, 14 * mm, 62 * mm, 24 * mm, 36 * mm])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d9dce1")), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f3f5"))]))
    story.append(t)
    story += [P("独立设计证据（设计过程时间线）" if zh else "Independent-design evidence (design timeline)", h2)]
    story.append(P(f"共 {record['total_events']} 条记录，哈希链{'校验通过' if record['chain_ok'] else '校验未通过（第 ' + str(record['first_bad']) + ' 条起）'}；服务器时间戳锚点 {record['anchors']} 个。最早的设计记录：{record['earliest'] or '-'}；被主张专利日期：{asserted.get('date') or '-'}。" if zh else f"{record['total_events']} records, hash chain {'verified' if record['chain_ok'] else 'NOT verified (from record ' + str(record['first_bad']) + ')'}; {record['anchors']} server time-stamp anchors. Earliest design record: {record['earliest'] or '-'}; asserted patent date: {asserted.get('date') or '-'}."))
    trows = [[("时间" if zh else "Time"), ("事件" if zh else "Event"), ("内容" if zh else "Detail"), ("哈希" if zh else "Hash")]] + [[e["at"], e["zh"] if zh else e["en"], e["detail"], e["hash"]] for e in record["events"][:60]]
    t = Table([[P(c, small) for c in r] for r in trows], colWidths=[34 * mm, 32 * mm, 74 * mm, 30 * mm])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d9dce1")), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f3f5"))]))
    story.append(t)
    story += [PageBreak(), P(("给律师的要点" if m["route"] == "court_tro" else "平台申诉草稿") if zh else ("Notes for counsel" if m["route"] == "court_tro" else "Platform appeal draft"), h2), P(draft["zh"] if zh else draft["en"])]
    story += [Spacer(1, 8), P("本材料用于准备应对，不构成法律意见，也不是侵权或不侵权的结论；请交执业律师或专利代理人审阅后再使用。" if zh else "This pack prepares a response. It is not legal advice and not a finding of infringement or non-infringement; have an attorney or registered practitioner review it before use.", small)]
    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm, title="DesignAround defense pack").build(story)


def _ratio(path: Path) -> float:
    from PIL import Image

    with Image.open(path) as im:
        return min(1.3, im.height / im.width)
