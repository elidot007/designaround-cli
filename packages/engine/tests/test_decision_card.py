"""Paid next steps on the decision card are labelled with the plan, never free."""

import json

from designaround_engine import api
from designaround_engine.advisor import card_markdown, decision_card, step_cost_phrase


def _paid(steps):
    return [s for s in steps if s.get("requires_plan")]


def test_filing_pack_and_baseline_are_not_free():
    case = api.case_create("Card", article="storage box", jurisdictions=["US"])
    cid = case["id"]
    vid = api.version_from_template(cid, "storage_box")["id"]
    opened = api.open_case(cid)
    screen = opened.root / "screens"
    screen.mkdir()
    (screen / "t.json").write_text(json.dumps({"candidates": [], "blind_spots": []}), encoding="utf-8")
    ev = opened.evaluation_path(vid)
    ev.parent.mkdir(parents=True, exist_ok=True)
    ev.write_text(json.dumps({"summary": {"level": "not_seen", "max_risk": 0, "qualified": False, "pending_observer": []}}), encoding="utf-8")

    card = decision_card(cid, lang="zh", filing_packs=False)
    paid = _paid(card["next"])
    assert any("filing-pack" in (s.get("command") or "") for s in paid)
    assert all("free" not in s for s in paid)
    md = card_markdown(card)
    assert "需要 Pro 套餐" in md
    assert "designaround billing checkout --plan pro" in md
    assert "免费" not in "".join(line for line in md.splitlines() if "filing-pack" in line or "Pro" in line)

    included = decision_card(cid, lang="en", filing_packs=True)
    step = next(s for s in included["next"] if "filing-pack" in (s.get("command") or ""))
    assert step["included"] is True and "free" not in step
    assert "Included in Pro" in card_markdown(included)

    package = opened.root / "filings" / "US" / vid / "package"
    package.mkdir(parents=True)
    (package / "checklist.json").write_text(json.dumps({"status": "draft"}), encoding="utf-8")
    filed = decision_card(cid, lang="zh", filing_packs=False)
    freeze = next(s for s in filed["next"] if "baseline freeze" in (s.get("command") or ""))
    assert freeze["requires_plan"] == "pro" and "free" not in freeze
    assert "需要 Pro 套餐" in card_markdown(filed)
    phrase = step_cost_phrase(step, "en")
    assert phrase.startswith("Included in Pro")
    paid_phrase = step_cost_phrase(freeze, "en")
    assert "Needs the Pro plan" in paid_phrase and "checkout --plan pro" in paid_phrase
