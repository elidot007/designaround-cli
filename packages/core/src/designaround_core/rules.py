"""Versioned legal-process rules: representation, disclosure timeline, titles.

Each rule carries an id and a basis so every decision written into a case can
be traced to the rule version that produced it. Rule text must be approved by a
partner practitioner before production use (开发清单 T0-24).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

RULEPACK_VERSION = "2026.10-1"

# --------------------------------------------------------------- representation

US_FOREIGN_COUNSEL_EFFECTIVE = date(2026, 7, 20)


@dataclass
class Finding:
    rule_id: str
    severity: str  # block | error | warning | info
    message: str
    basis: str
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"rule_id": self.rule_id, "severity": self.severity, "message": self.message, "basis": self.basis, **({"data": self.data} if self.data else {})}


def representation_requirements(office: str, applicant: dict, *, on: date | None = None) -> list[Finding]:
    """Who must represent the applicant. applicant = {domicile: 'CN', type: 'natural'|'juristic'}."""

    on = on or date.today()
    office = office.upper()
    domicile = (applicant.get("domicile") or "").upper()
    kind = applicant.get("type", "juristic")
    out: list[Finding] = []
    if office == "US":
        if domicile and domicile != "US" and on >= US_FOREIGN_COUNSEL_EFFECTIVE:
            out.append(Finding("REP-US-001", "block", "Applicants domiciled outside the US must be represented by a USPTO registered practitioner.", "37 CFR 1.31(a)(2), effective 2026-07-20"))
        if kind == "juristic":
            out.append(Finding("REP-US-002", "block", "A juristic entity applicant must be represented by a registered practitioner.", "37 CFR 1.31"))
    elif office == "CN":
        if domicile and domicile != "CN":
            out.append(Finding("REP-CN-001", "block", "Foreign applicants without a habitual residence or business office in China must appoint a licensed patent agency.", "Patent Law Art. 18"))
        else:
            out.append(Finding("REP-CN-002", "info", "Domestic applicants may file themselves; using a licensed agency is recommended.", "Patent Law Art. 18"))
    elif office == "EU":
        if domicile and domicile not in EU_EEA:
            out.append(Finding("REP-EU-001", "block", "Applicants without domicile or establishment in the EEA must be represented before EUIPO.", "EUDR Art. 77(2)"))
    return out


EU_EEA = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT",
    "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "IS", "LI", "NO",
}

# --------------------------------------------------------------------- timeline

GRACE_DAYS = {"US": 365, "EU": 365, "CN": 0, "JP": 365}


def disclosure_findings(office: str, disclosures: list[dict], filing_date: date | None) -> list[Finding]:
    """Check public disclosures (listing, fair, crowdfunding, social, sale) against the filing date.

    disclosures: [{"kind": "listing", "date": "2026-11-01", "by_applicant": True, "exception": None}]
    filing_date: planned or actual filing date; None means "not filed yet" (today is used).
    """

    office = office.upper()
    ref = filing_date or date.today()
    out: list[Finding] = []
    grace = GRACE_DAYS.get(office, 0)
    for item in disclosures:
        when = date.fromisoformat(str(item["date"]))
        if when >= ref and filing_date is not None:
            continue
        days_before = (ref - when).days
        by_applicant = bool(item.get("by_applicant", True))
        planned = when > date.today()
        label = f"{item.get('kind', 'disclosure')} on {when.isoformat()}"
        if office == "CN":
            if item.get("exception"):
                out.append(Finding("TL-CN-002", "warning", f"{label}: claimed statutory exception '{item['exception']}' must be declared and proven when filing.", "Patent Law Art. 24"))
            elif planned and filing_date is None:
                out.append(Finding("TL-CN-001", "block", f"{label} is planned before a CN filing exists. File first, then disclose.", "Patent Law Art. 23-24 (no general grace period)"))
            elif when < ref:
                out.append(Finding("TL-CN-001", "block", f"{label} precedes the CN filing date; it can destroy novelty of your own design.", "Patent Law Art. 23-24 (no general grace period)"))
        else:
            if not by_applicant and when < ref:
                out.append(Finding(f"TL-{office}-003", "error", f"{label} by a third party precedes filing.", "novelty"))
            elif days_before > grace:
                out.append(Finding(f"TL-{office}-001", "block", f"{label} is more than {grace} days before filing; the grace period is exceeded.", "35 U.S.C. 102(b)(1)" if office == "US" else "EUDR Art. 7(2)"))
            elif days_before > 0:
                out.append(Finding(f"TL-{office}-002", "warning", f"{label} relies on the {grace}-day grace period; record evidence of the disclosure.", "grace period"))
    return out


# ------------------------------------------------------------------------ titles

MARKETING_WORDS = re.compile(r"\b(best|premium|ultimate|new|improved|amazing|pro|max|deluxe|luxury|super|™|®)\b", re.I)


def title_findings(office: str, title: str) -> list[Finding]:
    out: list[Finding] = []
    if not title or not title.strip():
        return [Finding("TTL-001", "error", "Article title is empty.", "MPEP 1503.01 / CN Guidelines Part I Ch.3")]
    if office.upper() == "US":
        if MARKETING_WORDS.search(title):
            out.append(Finding("TTL-US-002", "error", "Title contains marketing language; use the generic article name.", "MPEP 1503.01(I)"))
        if any(ch.isdigit() for ch in title):
            out.append(Finding("TTL-US-003", "warning", "Model numbers in the title are discouraged.", "MPEP 1503.01(I)"))
    if office.upper() == "CN":
        if re.search(r"(新型|最佳|豪华|升级版|专用)", title):
            out.append(Finding("TTL-CN-002", "error", "产品名称不应包含宣传性或含糊词语。", "专利审查指南 第一部分第三章"))
        if re.search(r"[A-Za-z]{3,}\d|\d{3,}", title):
            out.append(Finding("TTL-CN-003", "warning", "产品名称一般不应包含型号。", "专利审查指南 第一部分第三章"))
    return out


def worst(findings: list[Finding]) -> str:
    order = {"block": 3, "error": 2, "warning": 1, "info": 0}
    if not findings:
        return "ok"
    return max(findings, key=lambda f: order.get(f.severity, 0)).severity
