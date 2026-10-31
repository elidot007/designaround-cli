"""Measured drawing rules: unique ids, and passes that come from the files that were written."""

from designaround_engine import api


def _ids(vp: dict) -> list[str]:
    return [finding["rule_id"] for finding in vp["rules"]["findings"]]


def _by_id(vp: dict) -> dict[str, dict]:
    rows = vp["rules"]["findings"]
    assert len(rows) == len({finding["rule_id"] for finding in rows})
    return {finding["rule_id"]: finding for finding in rows}


def _assert_modes(vp: dict) -> None:
    assert vp["ruleset_version"] == "drawing-2026.10-2"
    assert vp["rules"]["ruleset_version"] == "drawing-2026.10-2"
    assert vp["rules"]["passed"] == sum(bool(finding["pass"]) for finding in vp["rules"]["findings"])
    for finding in vp["rules"]["findings"]:
        assert finding["mode"] in ("auto", "confirm")
        if finding["mode"] == "confirm":
            assert finding["pass"] is False
            assert finding["severity"] == "confirm"


def test_measured_drawing_rules():
    cid = api.case_create("Measured box", article="storage box", jurisdictions=["US", "CN", "EU"])["id"]
    api.version_from_template(cid, "storage_box")
    us = api.make_viewpack(cid, "v1", "US")
    cn = api.make_viewpack(cid, "v1", "CN")
    eu = api.make_viewpack(cid, "v1", "EU")
    eu_mark = api.make_viewpack(cid, "v1", "EU", watermark=True)

    for pack in (us, cn, eu, eu_mark):
        _assert_modes(pack)

    us_ids = _ids(us)
    assert len(us_ids) == 7
    assert len(_ids(cn)) == 6
    assert len(_ids(eu)) == 6
    assert len(_ids(eu_mark)) == 6
    assert us_ids.count("US-DRW-015") == 1
    assert "US-DRW-005" not in us_ids
    us_rules = _by_id(us)
    assert us_rules["US-DRW-020"]["pass"] is True and us_rules["US-DRW-020"]["mode"] == "auto"
    assert us_rules["US-DRW-021"]["pass"] is True and us_rules["US-DRW-021"]["mode"] == "auto"

    cn_ids = _ids(cn)
    assert cn_ids.count("CN-DRW-015") == 1
    assert cn_ids.count("CN-DRW-005") == 1
    labels = _by_id(cn)["CN-DRW-003"]
    assert labels["mode"] in ("auto", "confirm")
    if labels["mode"] == "confirm":
        assert labels["pass"] is False
    assert not (labels["pass"] is True and labels.get("mode") not in ("auto", "confirm"))

    eu_rules = _by_id(eu)
    assert "EU-DRW-004" in eu_rules and "EU-DRW-015" not in eu_rules
    assert eu_rules["EU-DRW-006"]["pass"] is True and eu_rules["EU-DRW-006"]["mode"] == "auto"
    marked = _by_id(eu_mark)["EU-DRW-006"]
    assert marked["pass"] is False and marked["mode"] == "auto" and marked["severity"] == "error"
