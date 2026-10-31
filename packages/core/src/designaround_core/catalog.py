"""Price catalog (USD). 1 credit = $0.01.

Lean-cost model (decision after v1.2): heavy work runs on the user's machine,
so the subscription unlocks local features and credits only pay for work that
costs us money on the server:

* server-side registry screening (ClearComply + image fetch),
* platform-proxied LLM/VLM tokens (metered: upstream cost x markup),
* hosted observer scoring when the user runs in host mode.

BYOK (bring your own model key) and host mode (Claude Code / Codex / Cursor
supplies the model) consume no LLM credits.
"""

from __future__ import annotations

from dataclasses import dataclass

CREDIT_USD = 0.01


@dataclass(frozen=True)
class Plan:
    id: str
    name: str
    monthly_usd: float
    annual_usd: float
    monthly_credits: int
    seats: int
    max_active_cases: int | None
    viewpack_watermark: bool
    filing_packs: bool
    batch_limit: int
    monitors: int | None
    # One-time credits granted at signup. Paid plans grant monthly_credits instead.
    starter_credits: int = 0


PLANS: dict[str, Plan] = {
    # None on cases and monitors means no cap. Nothing in the server or CLI reads these
    # two fields to refuse a case or a monitor; the free plan does not advertise a limit.
    "free": Plan("free", "Free", 0, 0, 0, 1, None, True, False, 20, None, starter_credits=1_500),
    "pro": Plan("pro", "Pro", 59, 590, 10_000, 1, 50, False, True, 200, 20),
    "team": Plan("team", "Team", 189, 1890, 40_000, 5, None, False, True, 1000, 100),
    "enterprise": Plan("enterprise", "Enterprise", 1000, 12_000, 150_000, 25, None, False, True, 10_000, 1000),
}

CREDIT_PACKS: dict[str, dict] = {
    "credits_5k": {"credits": 5_000, "usd": 50},
    "credits_21k": {"credits": 21_000, "usd": 200},
    "credits_55k": {"credits": 55_000, "usd": 500},
    "credits_240k": {"credits": 240_000, "usd": 2_000},
}

# Fixed-price server actions (credits).
ACTION_CREDITS: dict[str, int] = {
    "screen.quick": 50,
    "screen.deep": 350,
    "screen.deep.batch": 200,  # per item when >= 100 items
    "observe.hosted": 120,  # one hosted observer scoring call (host mode)
    "concept.image": 25,  # one 2D concept image (GPT Image), charged only when delivered
    "evidence.anchor": 0,
    "gate.create": 0,
}

# Free monthly allowance of quick screens.
FREE_QUICK_SCREENS_PER_MONTH = 5

# Design-around depth presets (CLI `around --depth`). Credits are what the run is
# capped at by default; platform runs are metered and usually spend less.
AROUND_DEPTHS: dict[str, dict] = {
    "quick": {"rounds": 1, "per_round": 3, "target": 1, "max_credits": 2_900},
    "standard": {"rounds": 3, "per_round": 3, "target": 2, "max_credits": 5_900},
    "deep": {"rounds": 6, "per_round": 4, "target": 3, "max_credits": 14_900},
}

# Result guarantee (FR-ACCT-08): a standard or deep design-around run that ends
# without any qualified candidate gets this share of its spent credits back,
# automatically and once per run. View packs are rendered locally and never
# charged, so re-rendering one an agent returned for format reasons is free.
GUARANTEE: dict = {
    "rate": 0.5,
    "depths": ["standard", "deep"],
    "min_rounds": 3,
    "claim_window_days": 7,
    "max_claims_per_month": 20,
    "max_credits_per_month": 20_000,
}

# Platform LLM proxy: credits = ceil(upstream_usd * LLM_MARKUP / CREDIT_USD)
LLM_MARKUP = 1.25

# Managed packages: platform fee is sold via Creem; practitioner fees are paid
# by the customer directly to the practitioner (never through the platform).
MANAGED_PACKAGES: dict[str, dict] = {
    "managed_cn_around_file": {"platform_usd": 419, "practitioner_usd_est": 140, "offices": ["CN"]},
    "managed_us_around_file": {"platform_usd": 819, "practitioner_usd_est": 850, "offices": ["US"]},
    "managed_cn_us_around_file": {"platform_usd": 1099, "practitioner_usd_est": 990, "offices": ["CN", "US"]},
    "managed_cn_file_only": {"platform_usd": 99, "practitioner_usd_est": 140, "offices": ["CN"]},
    "managed_us_file_only": {"platform_usd": 249, "practitioner_usd_est": 850, "offices": ["US"]},
    "managed_risk_opinion": {"platform_usd": 249, "practitioner_usd_est": 420, "offices": []},
}


def credits_for_usd(usd: float) -> int:
    import math

    return max(0, math.ceil(round(usd / CREDIT_USD, 6)))


def plan(plan_id: str) -> Plan:
    return PLANS.get(plan_id, PLANS["free"])


def public_catalog() -> dict:
    return {
        "credit_usd": CREDIT_USD,
        "plans": {k: vars(v) for k, v in PLANS.items()},
        "credit_packs": CREDIT_PACKS,
        "actions": ACTION_CREDITS,
        "llm_markup": LLM_MARKUP,
        "managed_packages": MANAGED_PACKAGES,
        "free_quick_screens_per_month": FREE_QUICK_SCREENS_PER_MONTH,
        "around_depths": AROUND_DEPTHS,
        "guarantee": GUARANTEE,
    }
