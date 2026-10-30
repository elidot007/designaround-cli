"""Three-way quotes: official fee + practitioner fee (paid directly) + platform fee."""

from __future__ import annotations

from . import catalog, fees


def filing_quote(
    offices: list[str],
    *,
    managed: bool = False,
    design_around: bool = True,
    entity: str = "small",
    fee_reduction: bool = False,
    similar_designs: int = 0,
) -> dict:
    offices = [o.upper() for o in offices]
    lines = []
    total_usd = 0.0
    for office in offices:
        official = fees.official_fees(office, entity=entity, fee_reduction=fee_reduction)
        lines.append({"kind": "official_fee", "office": office, "paid_to": "patent office via practitioner", "method": "via_practitioner", **official})
        total_usd += official.get("usd_now") or 0
    platform_usd = 0.0
    practitioner_usd = 0.0
    package = None
    if managed:
        key = "_".join(sorted(o.lower() for o in offices))
        package_id = {
            ("cn", True): "managed_cn_around_file",
            ("us", True): "managed_us_around_file",
            ("cn_us", True): "managed_cn_us_around_file",
            ("cn", False): "managed_cn_file_only",
            ("us", False): "managed_us_file_only",
        }.get((key, design_around))
        if package_id:
            package = {"id": package_id, **catalog.MANAGED_PACKAGES[package_id]}
            platform_usd = package["platform_usd"]
            practitioner_usd = package["practitioner_usd_est"]
    lines.append({"kind": "platform_fee", "usd": platform_usd, "paid_to": "DesignAround via Creem (Merchant of Record)", "method": "creem", "package": package})
    lines.append({"kind": "practitioner_fee", "usd_estimate": practitioner_usd, "paid_to": "registered practitioner / licensed agency", "method": "direct", "note": "Paid directly by the customer under the practitioner's engagement letter. Never routed through DesignAround."})
    total_usd += platform_usd + practitioner_usd
    if "EU" in offices and similar_designs:
        lines[offices.index("EU")] = {**lines[offices.index("EU")], **fees.eu_official_fees(similar_designs)}
        total_usd += fees.eu_official_fees(similar_designs)["usd_now"] - fees.eu_official_fees()["usd_now"]
    timeline = {
        "CN": "初步审查，通常约 4–8 个月授权（以 CNIPA 实际为准）",
        "US": "审查周期通常 18 个月以上；外观加急审查已于 2025 年取消",
        "EU": "EUIPO 只做形式审查，无缺陷时通常数周内注册",
    }
    notes = []
    if "CN" in offices and similar_designs:
        notes.append(f"CN 合案：主方案 + {similar_designs} 项相似设计（同一件申请，最多 10 项）。")
    if "CN" in offices and "US" in offices:
        notes.append(f"先报 CN，再在 {fees.PRIORITY_MONTHS_DESIGN} 个月内要求优先权报 US。")
    return {
        "fee_table_version": fees.FEE_TABLE_VERSION,
        "offices": offices,
        "lines": lines,
        "total_usd_estimate": round(total_usd, 2),
        "timeline": {o: timeline.get(o, "") for o in offices},
        "notes": notes,
    }
