from pathlib import Path

from PIL import Image

from designaround_engine import api, concepts

BOX = '''
import cadquery as cq

PARAMS = {"w": {"default": 120.0, "min": 60, "max": 300, "unit": "mm"}}


def build(p):
    return {"body": cq.Workplane("XY").box(p["w"], 80, 60)}
'''


def _png(path: Path, color) -> str:
    Image.new("RGB", (256, 256), color).save(path)
    return str(path)


def test_concepts_lifecycle(tmp_path):
    case = api.case_create("Cosmetic box", article="cosmetic organizer", jurisdictions=["US"])
    cid = case["id"]
    a = concepts.concept_save(cid, _png(tmp_path / "a.png", (220, 60, 40)), title="Arched lid", idea="soft arch", prompt="p1", features=["arched lid"])
    b = concepts.concept_save(cid, _png(tmp_path / "b.png", (40, 60, 220)), title="Stepped", idea="steps", prompt="p2")
    assert (a["id"], b["id"]) == ("c1", "c2")
    assert len(a["sha256"]) == 64 and Path(a["image"]).exists()

    good = concepts.concept_review(cid, "c1", {"fit": 8, "manufacturable": 7, "has_text": False, "single_product": True, "issues": []})
    bad = concepts.concept_review(cid, "c2", {"fit": 5, "manufacturable": 8, "has_text": False, "issues": ["does not hold brushes"]})
    assert good["status"] == "passed" and good["review"]["pass"] is True
    assert bad["status"] == "needs_revision" and bad["review"]["pass"] is False
    # text in the image fails regardless of scores
    assert concepts.review_passes({"fit": 9, "manufacturable": 9, "has_text": True}) is False
    # A blocking issue (a string, or severity major/block/error) cannot pass on scores alone.
    assert concepts.review_passes({"fit": 9, "manufacturable": 9, "issues": ["与简单圆柱不符，脱模有风险"]}) is False
    assert concepts.review_passes({"fit": 9, "manufacturable": 9, "issues": [{"issue": "slightly tight corner", "severity": "detail"}]}) is True

    c3 = concepts.concept_save(cid, _png(tmp_path / "c.png", (40, 200, 60)), title="Stepped v2", idea="steps, brush slots", prompt="p3", parent="c2", round=2)
    concepts.concept_supersede(cid, "c2", by=c3["id"])
    assert [c["id"] for c in concepts.concepts(cid)] == ["c1", "c3"]
    assert len(concepts.concepts(cid, include_superseded=True)) == 3

    chosen = concepts.concept_choose(cid, "c1", person="Alex Chen")
    assert chosen["chosen"] is True
    sheet = concepts.concept_sheet(cid)
    assert Path(sheet["path"]).exists() and sheet["concepts"] == ["c1", "c3"]

    events = [e["type"] for e in api.open_case(cid).log.read()]
    assert events.count("concept.created") == 3 and events.count("concept.reviewed") == 2 and "decision" in events
    assert api.open_case(cid).log.verify()[0]


def test_build_from_concept_replaces_the_original(tmp_path):
    case = api.case_create("Box", article="box", jurisdictions=["US"])
    cid = case["id"]
    first = api.version_from_program(cid, BOX)
    assert first["kind"] == "original"
    built = api.version_from_program(cid, BOX, {"w": 150}, kind="original", concept="c1", review={"match": 8})
    assert built["kind"] == "original" and built["concept"] == "c1"
    assert api.case_show(cid)["original"] == built["id"]
    # later candidates default to "candidate" and the new original stays the baseline
    third = api.version_from_program(cid, BOX, {"w": 200})
    assert third["kind"] == "candidate"
    assert api.open_case(cid).original_version() == built["id"]


def test_try_program_renders_for_review():
    out = api.try_program(BOX, {}, renders=True)
    assert Path(out["render_sheet"]).exists() and Path(out["contact_sheet"]).exists()
