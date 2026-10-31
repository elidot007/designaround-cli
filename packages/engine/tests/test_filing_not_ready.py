"""Export of a filing package stops while block or error findings remain."""

import json
from pathlib import Path

import pytest

from designaround_engine.errors import FilingNotReady
from designaround_engine.filing import build_filing_package


def _viewpack(findings=None, status="ok"):
    return {
        "figures": [{"number": 1, "view": "front", "us_label": "front view", "jpg": "views/1.jpg"}],
        "rules": {"status": status, "findings": findings or []},
    }


def _pack(outdir, **kw):
    applicant = kw.pop("applicant", None) or {
        "name": "Shenzhen Example Co., Ltd.",
        "domicile": "CN",
        "type": "juristic",
        "address": "Shenzhen",
        "entity_size": "small",
    }
    meta = {
        "name": "Box",
        "article": kw.pop("article", "storage box"),
        "applicant": applicant,
        "inventors": kw.pop("inventors", [{"name": "Li Wei", "residence": "Shenzhen, CN"}]),
    }
    return build_filing_package(
        office=kw.pop("office", "US"),
        outdir=outdir,
        case_meta=meta,
        viewpack=kw.pop("viewpack", None) or _viewpack(),
        prior_art=[],
        decisions=[],
        **kw,
    )


def test_未通过仍导出则失败(tmp_path):
    blocked = tmp_path / "blocked"
    with pytest.raises(FilingNotReady, match="REP-US-001") as caught:
        _pack(blocked)
    err = caught.value
    assert "REP-US-002" in err.message
    assert {p.name for p in blocked.iterdir()} == {"checklist.json"}
    checklist = json.loads((blocked / "checklist.json").read_text(encoding="utf-8"))
    assert checklist["ready_for_practitioner"] is False
    assert checklist["requires_practitioner_gate"] is True
    assert checklist["gates"] == []
    assert {r["rule_id"] for r in checklist["blocked_reasons"]} == {"REP-US-001", "REP-US-002"}
    assert err.details["reasons"] == checklist["blocked_reasons"]
    assert Path(err.details["dir"]) == blocked

    pending = tmp_path / "pending"
    with pytest.raises(FilingNotReady, match="REP-US-001"):
        _pack(pending, gates=[{"id": "g0", "type": "filing_submission", "status": "pending"}])
    assert not (pending / "specification.docx").exists()

    other = tmp_path / "other"
    with pytest.raises(FilingNotReady, match="REP-US-001"):
        _pack(other, gates=[{"id": "g0", "type": "design_review", "status": "approved"}])
    assert not (other / "specification.docx").exists()

    approved = tmp_path / "approved"
    gate = {"id": "g1", "type": "filing_submission", "status": "approved"}
    result = _pack(approved, gates=[gate])
    assert (approved / "specification.docx").exists()
    assert "human_contribution.md" in result["files"]
    assert result["checklist"]["ready_for_practitioner"] is True
    assert result["checklist"]["requires_practitioner_gate"] is True
    assert result["checklist"]["blocked_reasons"] == []
    assert result["checklist"]["gates"] == [gate]

    represented = tmp_path / "represented"
    result = _pack(represented, gates=[{"id": "g2", "type": "representation", "status": "approved"}])
    assert (represented / "specification.docx").exists()
    assert result["checklist"]["ready_for_practitioner"] is True


def test_non_rep_blocks_are_not_lifted(tmp_path):
    applicant = {"name": "Ada", "domicile": "US", "type": "natural", "address": "Austin", "entity_size": "small"}
    out = tmp_path / "title"
    with pytest.raises(FilingNotReady, match="TTL-US-002"):
        _pack(out, article="best storage box", applicant=applicant, gates=[{"id": "g1", "type": "filing_submission", "status": "approved"}])
    assert not (out / "specification.docx").exists()
    checklist = json.loads((out / "checklist.json").read_text(encoding="utf-8"))
    assert checklist["requires_practitioner_gate"] is False

    drawn = tmp_path / "views"
    viewpack = _viewpack(status="error", findings=[{"rule_id": "US-DRW-010", "pass": False, "severity": "error", "message": "Unclaimed parts must be broken lines."}])
    with pytest.raises(FilingNotReady, match="US-DRW-010"):
        _pack(drawn, applicant=applicant, viewpack=viewpack, gates=[{"id": "g1", "type": "filing_submission", "status": "approved"}])
    assert {p.name for p in drawn.iterdir()} == {"checklist.json"}
    assert json.loads((drawn / "checklist.json").read_text(encoding="utf-8"))["ready_for_practitioner"] is False


def test_gaps_do_not_refuse_export(tmp_path):
    out = tmp_path / "gaps"
    applicant = {"name": "Ada", "domicile": "US", "type": "natural", "address": "Austin"}
    result = _pack(out, applicant=applicant)
    assert (out / "specification.docx").exists()
    assert result["checklist"]["ready_for_practitioner"] is False
    assert "applicant.entity_size (large/small/micro)" in result["checklist"]["missing"]
    assert result["checklist"]["blocked_reasons"] == []
    assert result["checklist"]["gates"] == []
