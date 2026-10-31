"""A failed photo reconstruction is stored as a draft, not the original."""

from designaround_engine import api


def test_failed_photo_gate_demotes_the_original():
    case = api.case_create("Photo sample", article="lamp", jurisdictions=["US"])
    cid = case["id"]
    original = api.version_from_template(cid, "storage_box")
    vid = original["id"]
    opened = api.open_case(cid)
    assert opened.original_version() == vid
    opened.update(original=vid)

    out = api.demote_failed_gate(cid, vid, {"pass": False, "worst": 0.11, "min_score": 0.72, "advice": "fix the front view"})
    assert out["kind"] == "draft" and out["previous_kind"] == "original"
    again = api.open_case(cid)
    meta = again.version_meta(vid)
    assert meta["kind"] == "draft"
    assert meta["reconstruction_gate"]["pass"] is False
    assert again.meta.get("original") is None
    assert again.original_version() != vid
    assert any(e["type"] == "version.demoted" for e in again.log.read())
