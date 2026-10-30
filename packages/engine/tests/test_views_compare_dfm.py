import numpy as np
import pytest
from PIL import Image

from designaround_engine import compare, dfm, viewpack, views
from designaround_engine.camera import STANDARD_VIEWS
from designaround_engine.geometry import load_version_parts
from designaround_engine.lineart import project_lines


def test_views_are_consistent(built_box):
    parts = load_version_parts(built_box)
    drawings = {k: project_lines(parts, STANDARD_VIEWS[k]) for k in STANDARD_VIEWS}
    report = viewpack._consistency(drawings)
    assert report["checked"] >= 5
    assert report["mismatches"] == []
    fw, fh = drawings["front"].extent()
    assert fw == pytest.approx(352.9, abs=1.0)  # body flares by the taper + rim


def test_self_similarity_is_high_and_ranks_first(built_box, tmp_path):
    ours = views.view_masks(built_box)
    same = compare.normalise(compare.auto_mask(compare.load_gray(built_box / "views" / "front.lineart.png")))
    circle = np.zeros((300, 300), dtype=np.uint8) + 255
    yy, xx = np.ogrid[:300, :300]
    ring = np.abs(np.hypot(yy - 150, xx - 150) - 100) < 2
    circle[ring] = 0
    other = compare.normalise(compare.auto_mask(circle))
    ranked = compare.rank_candidates(ours, [{"id": "circle", "masks": [other]}, {"id": "self", "masks": [same]}])
    assert ranked[0]["id"] == "self"
    assert ranked[0]["shape"]["score"] > 0.85
    assert ranked[1]["shape"]["score"] < ranked[0]["shape"]["score"] - 0.1


def test_lineart_mask_fills_outline():
    img = np.full((200, 200), 255, dtype=np.uint8)
    img[40, 40:160] = 0
    img[160, 40:160] = 0
    img[40:161, 40] = 0
    img[40:161, 160] = 0
    mask = compare.mask_from_lineart(img)
    assert mask[100, 100]
    assert not mask[10, 10]


def test_photo_mask_white_background():
    img = Image.new("L", (400, 300), 250)
    for x in range(120, 280):
        for y in range(80, 220):
            img.putpixel((x, y), 90)
    mask = compare.mask_from_photo(np.asarray(img))
    assert 0.15 < mask.mean() < 0.25


def test_dfm_box(built_box):
    result = dfm.check_version(built_box, "injection")
    ids = {c["rule_id"]: c for c in result["checks"]}
    assert ids["DFM-GEN-001"]["pass"]
    wall = ids["DFM-WALL-001"]["value"]
    assert wall["p05_mm"] is not None and wall["p05_mm"] >= 1.0
    printable = dfm.check_version(built_box, "print3d")
    assert any(c["rule_id"] == "DFM-OVERHANG-001" for c in printable["checks"])
