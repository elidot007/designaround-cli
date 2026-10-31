"""End-to-end local flow: case -> original -> prior art -> evaluate -> candidate ->
originality -> evaluate -> viewpack -> filing pack -> proto -> evidence."""

import json
import zipfile
from pathlib import Path

import pytest

from designaround_engine import api
from designaround_engine.errors import FilingNotReady, InvalidInput, NotFound


@pytest.fixture()
def case_with_original():
    case = api.case_create("Folding storage box", article="storage box", jurisdictions=["US", "CN"])
    original = api.version_from_template(case["id"], "storage_box")
    return case["id"], original["id"]


def test_full_local_flow(case_with_original, tmp_path):
    cid, v1 = case_with_original
    assert api.case_show(cid)["evidence"]["chain_ok"]

    # Prior art = a figure of the original itself (worst case: identical design).
    fig = Path(api.version_show(cid, v1)["path"]) / "views" / "perspective.lineart.png"
    pa = api.prior_art_add(cid, number="US D1,000,001 S", office="US", title="Storage box", figures=[str(fig)], status="active")
    assert pa["id"] == "USD1000001S"
    ev1 = api.evaluate(cid, v1, judge={"USD1000001S": {"score": 9, "rationale": "same overall impression"}})
    assert ev1["per_prior_art"][0]["basis"] == "shape+observer"
    assert ev1["summary"]["level"] == "high"
    assert not ev1["summary"]["qualified"]

    # Cosmetic tweak is rejected by the originality gate.
    cosmetic = api.tweak(cid, v1, {"corner_radius": 20}, created_by="agent")
    gate = api.originality_check(cid, cosmetic["id"], target_features=["handle"])
    assert not gate["pass"]

    # Real design-around: recessed handle + ribs + domed lid.
    v3 = api.tweak(cid, v1, {"handle_style": "recessed", "rib_count": 3, "lid_dome": 10, "taper_deg": 6}, created_by="agent")
    gate = api.originality_check(cid, v3["id"], target_features=["handle"])
    assert gate["pass"], gate["reasons"]
    ev3 = api.evaluate(cid, v3["id"], judge={"USD1000001S": {"score": 3, "rationale": "different handle and side ribs"}})
    assert ev3["summary"]["max_risk"] < ev1["summary"]["max_risk"]

    dfm_result = api.dfm_check(cid, v3["id"])
    assert "checks" in dfm_result

    api.case_update(cid, applicant={"name": "Shenzhen Example Co., Ltd.", "domicile": "CN", "type": "juristic", "address": "Shenzhen", "entity_size": "small", "fee_reduction": True}, inventors=[{"name": "Li Wei", "residence": "Shenzhen, CN"}])
    api.decision(cid, "selected candidate", {"version": v3["id"]}, person="Li Wei")

    with pytest.raises(FilingNotReady) as caught:
        api.filing_pack(cid, v3["id"], "US")
    failed = caught.value
    assert "REP-US-001" in failed.message
    pkg = Path(failed.details["dir"])
    assert not (pkg / "specification.docx").exists()
    blocked = json.loads((pkg / "checklist.json").read_text(encoding="utf-8"))
    assert blocked["ready_for_practitioner"] is False
    assert "gates" in blocked

    us = api.filing_pack(cid, v3["id"], "US", gates=[{"id": "g1", "type": "filing_submission", "status": "approved"}])
    files = set(us["files"])
    assert {"specification.docx", "ADS_AIA14_prefill.json", "IDS_SB08_draft.csv", "human_contribution.md"} <= files
    assert us["checklist"]["requires_practitioner_gate"]  # foreign-domiciled applicant, recorded even after the gate
    assert us["checklist"]["gates"][0]["status"] == "approved"
    assert (Path(us["dir"]) / "specification.docx").exists()
    vp = json.loads((Path(us["viewpack_dir"]) / "viewpack.json").read_text())
    assert vp["rules"]["status"] in ("ok", "warning")
    assert len(vp["figures"]) == 7

    cn = api.filing_pack(cid, v3["id"], "CN")
    brief = (Path(cn["dir"]) / "简要说明.md").read_text(encoding="utf-8")
    assert "收纳箱" in brief and "设计要点" in brief

    proto = api.proto_pack(cid, v3["id"])
    assert any(f["path"] == "model.stl" for f in proto["files"])

    exported = api.evidence_export(cid, str(tmp_path / "e.zip"))
    assert exported["chain_ok"]
    with zipfile.ZipFile(exported["zip"]) as zf:
        assert "events.jsonl" in zf.namelist()


def test_evidence_tamper_detected(case_with_original):
    cid, _v1 = case_with_original
    from designaround_engine.workspace import open_case

    case = open_case(cid)
    path = case.root / "events.jsonl"
    lines = path.read_text().splitlines()
    record = json.loads(lines[0])
    record["payload"]["name"] = "tampered"
    lines[0] = json.dumps(record)
    path.write_text("\n".join(lines) + "\n")
    ok, bad = case.log.verify()
    assert not ok and bad == 0


def test_disclosure_blocks_cn_before_filing(case_with_original):
    cid, _ = case_with_original
    result = api.disclose(cid, "listing", "2099-01-01")
    assert any(f["rule_id"] == "TL-CN-001" and f["severity"] == "block" for f in result["findings"]["CN"])
    assert not any(f["severity"] == "block" for f in result["findings"]["US"])


def test_tweak_requires_program():
    cid = api.case_create("x")["id"]
    with pytest.raises(NotFound):
        api.tweak(cid, "v9", {"a": 1})
    with pytest.raises(InvalidInput):
        api.case_update(cid, nonsense=1)
