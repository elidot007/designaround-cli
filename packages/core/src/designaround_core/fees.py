"""Official design-application fees and terms (versioned data, review quarterly).

Sources (2026): USPTO design fees per UpCounsel/Patsnap summaries of the
current fee schedule; CNIPA design application fee 500 CNY with up to 85% fee
reduction. Practitioners must confirm before filing.
"""

from __future__ import annotations

FEE_TABLE_VERSION = "2026-10"
CNY_PER_USD = 7.1  # display reference only

US_DESIGN = {
    # filing + search + examination
    "application": {"large": 1300, "small": 520, "micro": 260},
    "issue": {"large": 1300, "small": 520, "micro": 260},
    "currency": "USD",
}

CN_DESIGN = {
    "application": 500,
    "fee_reduction_rate": 0.85,
    "currency": "CNY",
}
# EUIPO since 1 May 2025 (EUDR as amended): one application fee that replaced
# registration + publication fees; EUR 125 for each additional design in a
# multiple application; deferment of publication from EUR 40.
EU_DESIGN = {"application": 350, "additional_design": 125, "deferment": 40, "currency": "EUR"}
USD_PER_EUR = 1.08  # display reference only

TERMS = {
    "US": {"years": 15, "from": "grant"},
    "CN": {"years": 15, "from": "filing"},
    "JP": {"years": 25, "from": "filing"},
    "EU": {"years": 25, "from": "filing", "note": "renewable every 5 years"},
}

PRIORITY_MONTHS_DESIGN = 6


def us_official_fees(entity: str = "small") -> dict:
    entity = entity if entity in US_DESIGN["application"] else "large"
    return {
        "office": "US",
        "currency": "USD",
        "entity": entity,
        "application": US_DESIGN["application"][entity],
        "issue_on_allowance": US_DESIGN["issue"][entity],
        "usd_now": US_DESIGN["application"][entity],
    }


def cn_official_fees(fee_reduction: bool = False) -> dict:
    amount = CN_DESIGN["application"] * (1 - CN_DESIGN["fee_reduction_rate"]) if fee_reduction else CN_DESIGN["application"]
    return {
        "office": "CN",
        "currency": "CNY",
        "fee_reduction": fee_reduction,
        "application": round(amount, 2),
        "usd_now": round(amount / CNY_PER_USD, 2),
    }


def eu_official_fees(additional_designs: int = 0) -> dict:
    amount = EU_DESIGN["application"] + EU_DESIGN["additional_design"] * max(0, additional_designs)
    return {"office": "EU", "currency": "EUR", "application": amount, "additional_designs": max(0, additional_designs), "usd_now": round(amount * USD_PER_EUR, 2)}


def official_fees(office: str, *, entity: str = "small", fee_reduction: bool = False) -> dict:
    office = office.upper()
    if office == "US":
        return us_official_fees(entity)
    if office == "CN":
        return cn_official_fees(fee_reduction)
    if office == "EU":
        return eu_official_fees()
    return {"office": office, "currency": None, "application": None, "usd_now": None, "note": "fee table not available yet"}
