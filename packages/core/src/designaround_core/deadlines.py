"""Deadline calendar for design filings (FR-FILE-12).

Pure date rules, computed from what the person recorded on the case: filings
(office, filing date, route), official notices (office action, allowance or grant
notice, registration) and public disclosures. Every deadline carries its legal
basis. Official notices state their own time limit; when the person records it
(``due``), that date wins over the computed one. Non-working days are not moved
(the Paris Convention and the offices extend a period that ends on a closed day
to the next working day); practitioners confirm every date.

Rules (2026-10):

* Priority (Paris Convention Art. 4C; 35 U.S.C. 172; CN Patent Law Art. 29;
  EUDR Art. 41; Hague via Paris): 6 months from the first filing.
* Own-disclosure grace period: US 1 year (35 U.S.C. 102(b)(1)), EU 12 months
  (EUDR Art. 7(2)), JP 1 year; CN has no general grace period.
* US office action: 3-month shortened statutory period, extendable with fees to
  the 6-month statutory limit (35 U.S.C. 133, 37 CFR 1.134, 1.136(a)).
  Notice of allowance: issue fee within 3 months, not extendable (35 U.S.C. 151,
  37 CFR 1.311). Term: 15 years from grant (35 U.S.C. 173); no maintenance fees.
* CN notice (补正通知书 / 审查意见通知书): 2 months from receipt; a mailed notice
  is deemed received 15 days after dispatch, an electronic one on dispatch
  (专利法实施细则第四条). Extension can be requested before expiry (第六条).
  Grant notice (办理登记手续通知书): 2 months to complete registration and pay
  the annual fee of the grant year. Patent years run from the filing date; each
  later year is paid before the previous one ends (the day before the filing
  anniversary), or within 6 months late with a surcharge. Term: 15 years from filing
  (专利法第四十二条, applications filed from 2021-06-01).
* EU (RCD): protection 5 years from filing, renewable to 25. Renewal within the 6
  months ending on the last day of the month in which protection ends, then 6
  more months with a surcharge (EUDR Art. 13). Deficiency letters: the period
  stated in the letter, usually 2 months.
* Hague (WIPO): standard publication 12 months after the international
  registration (Common Regulations Rule 17); designated offices may refuse
  within 6 months of publication (12 months for offices that declared it, such
  as CN and US); irregularities are remedied within 3 months (Rule 14);
  renewal every 5 years.
"""

from __future__ import annotations

import calendar
import hashlib
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

PRIORITY_MONTHS = 6
GRACE_MONTHS = {"US": 12, "EU": 12, "JP": 12}
REMINDER_DAYS = (30, 14, 7, 1)
OFFICES = ("US", "CN", "EU", "JP", "HAGUE")
NOTICE_KINDS = ("office_action", "allowance", "granted")
HAGUE_LONG_REFUSAL = {"CN", "US", "JP", "KR", "MX", "RU", "SY", "VN", "IL"}


def add_months(d: date, months: int) -> date:
    """Same day number ``months`` later; the month's last day when that day does not exist."""

    total = d.month - 1 + months
    year, month = d.year + total // 12, total % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def end_of_month(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


@dataclass
class Deadline:
    id: str
    office: str
    kind: str
    due: date
    title_zh: str
    title_en: str
    basis: str
    note_zh: str = ""
    note_en: str = ""
    hard: bool = True  # missing it loses a right (vs. an informational milestone)
    source: dict = field(default_factory=dict)

    def to_dict(self, today: date | None = None) -> dict:
        out = asdict(self)
        out["due"] = self.due.isoformat()
        if today is not None:
            out["days_left"] = (self.due - today).days
        return out


def _d(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _uid(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


def compute(*, offices: list[str], filings: list[dict], notices: list[dict], disclosures: list[dict], matters: list[dict] | None = None) -> list[Deadline]:
    """All deadlines for one case, sorted by date."""

    offices = [o.upper() for o in offices]
    out: list[Deadline] = []

    # Complaints and lawsuits (FR-DEF-06): a US court answer is due 21 days after service.
    for m in matters or []:
        ref = {"matter": m.get("id"), "number": m.get("number")}
        if m.get("route") == "court_tro" and m.get("served"):
            served = _d(m["served"])
            out.append(Deadline(_uid("answer", m.get("id"), served), "US", "court_answer", served + timedelta(days=21), f"诉讼答辩期限（{m.get('number') or '案件'}，送达后 21 天）", f"Answer due ({m.get('number') or 'lawsuit'}, 21 days after service)", "FRCP 12(a)(1)(A)(i)", "放弃送达（waiver）时为发出请求后 60 天；法院命令里的日期优先。请立即联系美国诉讼律师。", "60 days after the waiver request was sent if service was waived; dates in a court order prevail. Contact US litigation counsel now.", source=ref))
        if m.get("hearing"):
            out.append(Deadline(_uid("hearing", m.get("id"), m["hearing"]), "US" if m.get("route") == "court_tro" else (m.get("platform") or "").upper() or "US", "hearing", _d(m["hearing"]), f"听证 / 回复期限（{m.get('number') or ''}）", f"Hearing / response date ({m.get('number') or ''})", "court order / notice", "以法院命令或平台通知写明的日期为准。", "The date in the court order or platform notice applies.", source=ref))
        if m.get("respond_by"):
            out.append(Deadline(_uid("respond", m.get("id"), m["respond_by"]), (m.get("platform") or "platform").upper(), "complaint_response", _d(m["respond_by"]), f"平台投诉回复期限（{m.get('number') or ''}）", f"Platform complaint response ({m.get('number') or ''})", "platform notice", "以平台通知写明的期限为准。", "The time limit in the platform notice applies.", source=ref))
    by_office = {}
    for f in sorted(filings, key=lambda f: _d(f["filed"])):
        by_office.setdefault(f["office"].upper(), f)
    filed_offices = set(by_office)

    # Priority: 6 months from the first filing anywhere, for every target office not filed yet.
    if filings:
        first = min(filings, key=lambda f: _d(f["filed"]))
        first_date = _d(first["filed"])
        missing = [o for o in offices if o not in filed_offices]
        due = add_months(first_date, PRIORITY_MONTHS)
        out.append(Deadline(
            _uid("priority", first_date), first["office"].upper(), "priority", due,
            f"优先权期限：{'、'.join(missing) if missing else '其他法域'}要求 {first['office'].upper()} 首次申请的优先权",
            f"Priority deadline: file {', '.join(missing) if missing else 'other offices'} claiming the {first['office'].upper()} first filing",
            "Paris Convention Art. 4C; 35 U.S.C. 172; CN Patent Law Art. 29; EUDR Art. 41",
            "6 个月内在其他法域申请才能享有优先权；最后一天遇官方非工作日顺延到下一个工作日。" + ("（目标法域都已申请）" if not missing else ""),
            "File within 6 months to keep the first filing date; a closed office day moves the end to the next working day." + (" (All target offices are filed.)" if not missing else ""),
            hard=bool(missing), source={"filing": first.get("number") or first_date.isoformat()},
        ))

    # Own disclosures: file-by dates inside the grace period.
    own = [x for x in disclosures if x.get("by_applicant", True)]
    if own:
        earliest = min(own, key=lambda x: _d(x["date"]))
        when = _d(earliest["date"])
        for office in offices:
            if office in filed_offices:
                continue
            if office in GRACE_MONTHS:
                out.append(Deadline(
                    _uid("grace", office, when), office, "grace", add_months(when, GRACE_MONTHS[office]),
                    f"{office} 宽限期内最晚申请日（自己 {when.isoformat()} 的公开）",
                    f"{office}: last filing day inside the grace period (own disclosure on {when.isoformat()})",
                    "35 U.S.C. 102(b)(1)" if office == "US" else "EUDR Art. 7(2)" if office == "EU" else "JP Design Act Art. 4",
                    "超过这一天，自己的公开会破坏新颖性。", "After this day your own disclosure destroys novelty.",
                    source={"disclosure": earliest.get("kind"), "date": when.isoformat()},
                ))

    # Term and renewal milestones from filings.
    for office, f in by_office.items():
        filed = _d(f["filed"])
        if office == "EU":
            for n in range(1, 5):
                ends = add_months(filed, 60 * n)
                last = end_of_month(ends)
                out.append(Deadline(
                    _uid("eu-renew", filed, n), "EU", "renewal", last,
                    f"欧盟外观第 {n} 次续展截止（保护到 {ends.isoformat()}）",
                    f"EU design renewal {n} (protection ends {ends.isoformat()})",
                    "EUDR Art. 13",
                    f"续展窗口 {add_months(date(last.year, last.month, 1), -5)} 起；之后 6 个月内可加滞纳金补办（到 {end_of_month(add_months(last, 6))}）。",
                    f"Window opens {add_months(date(last.year, last.month, 1), -5)}; 6 more months with a surcharge (to {end_of_month(add_months(last, 6))}).",
                    source={"filing": f.get("number") or filed.isoformat()},
                ))
        if office == "CN":
            out.append(Deadline(_uid("cn-term", filed), "CN", "term", add_months(filed, 180) - timedelta(days=1), "中国外观专利权期满（申请日起 15 年）", "CN design term ends (15 years from filing)", "CN Patent Law Art. 42", hard=False, source={"filing": f.get("number") or filed.isoformat()}))
        if office == "HAGUE" or f.get("route") == "hague":
            reg = filed
            pub = add_months(reg, 12)
            out.append(Deadline(_uid("hague-pub", reg), "HAGUE", "publication", pub, "海牙国际注册标准公布日（国际注册日起 12 个月）", "Hague standard publication (12 months after international registration)", "Hague Common Regulations Rule 17", "可以申请立即公布或延迟公布（最长 30 个月）。", "Immediate or deferred publication (up to 30 months) can be requested.", hard=False))
            designated = [o for o in (f.get("designated") or offices) if o != "HAGUE"]
            long = [o for o in designated if o in HAGUE_LONG_REFUSAL]
            short = [o for o in designated if o not in HAGUE_LONG_REFUSAL]
            if short:
                out.append(Deadline(_uid("hague-ref6", reg), "HAGUE", "refusal_period", add_months(pub, 6), f"海牙：{'、'.join(short)} 驳回期届满（公布后 6 个月）", f"Hague: refusal period ends for {', '.join(short)} (6 months after publication)", "Hague Agreement (1999) Art. 12; Rule 18", "期满没有收到驳回，视为在该国获得保护。", "No refusal by then: protection in that office.", hard=False))
            if long:
                out.append(Deadline(_uid("hague-ref12", reg), "HAGUE", "refusal_period", add_months(pub, 12), f"海牙：{'、'.join(long)} 驳回期届满（公布后 12 个月）", f"Hague: refusal period ends for {', '.join(long)} (12 months after publication)", "Hague Agreement (1999) Art. 12; Rule 18(1)(b)", "这些国家声明了 12 个月的驳回期。", "These offices declared a 12-month refusal period.", hard=False))
            for n in range(1, 3):
                out.append(Deadline(_uid("hague-renew", reg, n), "HAGUE", "renewal", add_months(reg, 60 * n), f"海牙国际注册第 {n} 次续展（每 5 年）", f"Hague renewal {n} (every 5 years)", "Hague Agreement (1999) Art. 17", "到期后 6 个月内可加滞纳金续展。", "Up to 6 months late with a surcharge.", source={"registration": f.get("number") or reg.isoformat()}))

    # Official notices.
    for n in notices:
        office = n["office"].upper()
        kind = n["kind"]
        issued = _d(n["date"])
        mailed = bool(n.get("mailed"))
        received = issued + timedelta(days=15) if (office == "CN" and mailed) else issued
        given = _d(n["due"]) if n.get("due") else None
        ref = {"notice": n.get("number") or kind, "date": issued.isoformat()}
        filed = _d(by_office[office]["filed"]) if office in by_office else None
        if kind == "office_action":
            if office == "US":
                out.append(Deadline(_uid("us-oa3", issued), "US", "response", given or add_months(issued, 3), "美国审查意见答复（3 个月，不交延期费）", "US office action reply (3 months, no extension fee)", "37 CFR 1.134, 1.136(a)", "可交延期费延到 6 个月。", "Extendable with fees up to 6 months.", source=ref))
                out.append(Deadline(_uid("us-oa6", issued), "US", "response_final", add_months(issued, 6), "美国审查意见答复法定最晚期限（6 个月，需延期费）", "US office action statutory limit (6 months, with extension fees)", "35 U.S.C. 133", "过期不答复，申请视为放弃。", "Missing it abandons the application.", source=ref))
            elif office == "CN":
                out.append(Deadline(_uid("cn-oa", issued), "CN", "response", given or add_months(received, 2), "中国审查意见 / 补正通知答复", "CN office action / correction reply", "专利法实施细则第四条、第六条", ("邮寄送达按发文日加 15 天推定收到；" if mailed else "电子送达以发文日为收到日；") + "可以在期满前请求延长期限。", ("Mailed: deemed received 15 days after dispatch; " if mailed else "Electronic: received on dispatch; ") + "an extension can be requested before expiry.", source=ref))
            elif office == "HAGUE":
                out.append(Deadline(_uid("hg-irr", issued), "HAGUE", "response", given or add_months(issued, 3), "海牙：WIPO 不规范通知答复（3 个月）", "Hague: WIPO irregularity reply (3 months)", "Hague Common Regulations Rule 14", source=ref))
            else:
                out.append(Deadline(_uid(office.lower() + "-oa", issued), office, "response", given or add_months(issued, 2), f"{office} 官方通知答复", f"{office} official notice reply", "period stated in the notice", "以通知书写明的期限为准。", "The period stated in the notice applies.", source=ref))
        elif kind == "allowance":
            if office == "US":
                out.append(Deadline(_uid("us-issue", issued), "US", "issue_fee", given or add_months(issued, 3), "美国授权费（Issue fee）缴纳截止", "US issue fee due", "35 U.S.C. 151; 37 CFR 1.311", "3 个月，不能延期；过期申请视为放弃。", "3 months, not extendable; missing it abandons the application.", source=ref))
            elif office == "CN":
                out.append(Deadline(_uid("cn-reg", issued), "CN", "registration", given or add_months(received, 2), "中国办理登记手续并缴纳第一年年费", "CN registration formalities and first annual fee", "专利法实施细则（办理登记手续）", "期满未办理视为放弃取得专利权。", "Missing it forfeits the patent.", source=ref))
            else:
                out.append(Deadline(_uid(office.lower() + "-allow", issued), office, "registration", given or add_months(issued, 2), f"{office} 授权后续手续", f"{office} post-allowance step", "period stated in the notice", source=ref))
        elif kind == "granted":
            if office == "US":
                out.append(Deadline(_uid("us-term", issued), "US", "term", add_months(issued, 180), "美国外观专利权期满（授权日起 15 年，无维持费）", "US design term ends (15 years from grant; no maintenance fees)", "35 U.S.C. 173", hard=False, source=ref))
            elif office == "CN" and filed:
                year = 2
                anniversary = add_months(filed, 12 * (year - 1))
                while anniversary <= issued:  # fees for the grant year are paid at registration
                    year += 1
                    anniversary = add_months(filed, 12 * (year - 1))
                for k in range(3):
                    # Patent years run from the filing date; year N is paid before year N-1 ends.
                    due = add_months(filed, 12 * (year - 1 + k)) - timedelta(days=1)
                    if due >= add_months(filed, 180) - timedelta(days=1):
                        break
                    out.append(Deadline(_uid("cn-annual", filed, year + k), "CN", "annual_fee", due, f"中国外观第 {year + k} 年年费", f"CN design annual fee, year {year + k}", "专利法实施细则（年费）", f"可在 6 个月内加滞纳金补缴（到 {add_months(due, 6)}）。", f"Payable late within 6 months with a surcharge (to {add_months(due, 6)}).", source=ref))
    return sorted(out, key=lambda d: (d.due, d.office, d.kind))


# ------------------------------------------------------------------- iCalendar


def _ics_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n")


def _fold(line: str) -> list[str]:
    """RFC 5545 3.1: lines longer than 75 octets continue on the next line after a space."""

    out, current = [], ""
    for ch in line:
        if len((current + ch).encode("utf-8")) > (75 if not out else 74):
            out.append(current)
            current = ch
        else:
            current += ch
    out.append(current)
    return [out[0]] + [" " + part for part in out[1:]]


def to_ics(deadlines: list[Deadline], *, calendar_name: str, lang: str = "zh", reminders: tuple[int, ...] = REMINDER_DAYS, now: datetime | None = None) -> str:
    zh = lang != "en"
    stamp = (now or datetime.utcnow()).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//DesignAround//Deadlines//" + ("ZH" if zh else "EN"), "CALSCALE:GREGORIAN", "METHOD:PUBLISH", f"X-WR-CALNAME:{_ics_text(calendar_name)}"]
    for d in deadlines:
        title = d.title_zh if zh else d.title_en
        note = d.note_zh if zh else d.note_en
        desc = "\n".join(x for x in [note, ("依据：" if zh else "Basis: ") + d.basis, "请执业代理人确认期限；这不是法律意见。" if zh else "Have a registered practitioner confirm; not legal advice."] if x)
        lines += [
            "BEGIN:VEVENT",
            f"UID:{d.id}@designaround",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{d.due.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{(d.due + timedelta(days=1)).strftime('%Y%m%d')}",
            f"SUMMARY:{_ics_text(('[' + d.office + '] ') + title)}",
            f"DESCRIPTION:{_ics_text(desc)}",
            "TRANSP:TRANSPARENT",
            f"CATEGORIES:DesignAround,{d.office},{d.kind}",
        ]
        if d.hard:
            for days in reminders:
                lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_ics_text(title)}", f"TRIGGER:-P{days}D", "END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(part for line in lines for part in _fold(line)) + "\r\n"
