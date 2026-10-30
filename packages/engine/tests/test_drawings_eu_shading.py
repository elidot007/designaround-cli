"""US surface shading, EU view packs, and HLR fallback consistency."""

import json
from pathlib import Path

from PIL import Image

from designaround_engine import api


def _case(template: str, offices=("US",)) -> str:
    cid = api.case_create(f"{template} drawings", article="tumbler" if template == "tumbler" else "storage box", jurisdictions=list(offices))["id"]
    api.version_from_template(cid, template)
    return cid


def test_views_consistent_for_curved_template():
    # the exact HLR drops the torus handle outline in the top view; the polygonal fallback restores it
    cid = _case("tumbler")
    for office in ("US", "CN", "EU"):
        vp = api.make_viewpack(cid, "v1", office)
        errors = [f for f in vp["rules"]["findings"] if f["severity"] == "error"]
        assert not errors, (office, errors)


def test_us_shading_on_curved_surfaces_only():
    cid = _case("tumbler")
    vp = api.make_viewpack(cid, "v1", "US")
    lines = vp["shading"]["lines_per_view"]
    assert vp["shading"]["enabled"] and lines["front"] > 10 and lines["perspective"] > 10
    finding = next(f for f in vp["rules"]["findings"] if f["rule_id"] == "US-DRW-014")
    assert finding["pass"] and "Surface shading generated" in finding["message"]
    svg = (Path(vp["dir"]) / "figures" / "fig2_front.svg").read_text()
    assert 'class="shading"' in svg
    # CN packs carry no shading by default (CN line drawings are without shading)
    cn = api.make_viewpack(cid, "v1", "CN")
    assert cn["shading"]["enabled"] is False


def test_unclaimed_parts_get_no_shading():
    cid = _case("tumbler")
    api.set_claimed(cid, "v1", "handle", False)
    vp = api.make_viewpack(cid, "v1", "US")
    from designaround_engine.camera import STANDARD_VIEWS
    from designaround_engine.geometry import combined_mesh, load_version_parts
    from designaround_engine.lineart import project_lines
    from designaround_engine.shading import add_shading

    parts = load_version_parts(Path(api.version_show(cid, "v1")["path"]))
    V, F, _ = combined_mesh(parts)
    drawings = {"front": project_lines(parts, STANDARD_VIEWS["front"])}
    add_shading(drawings, parts, V, F, STANDARD_VIEWS)
    assert all(ln.part != "handle" for ln in drawings["front"].lines if ln.kind == "shading")
    assert vp["rules"]["status"] != "error"


def test_eu_viewpack_and_filing_pack():
    cid = _case("storage_box", offices=("EU",))
    api.case_update(cid, applicant={"name": "ACME GmbH", "domicile": "DE", "address": "Berlin", "type": "juristic"}, inventors=[{"name": "Jane Doe", "residence": "Berlin, DE"}], locarno="06-04")
    vp = api.make_viewpack(cid, "v1", "EU")
    assert vp["office"] == "EU" and vp["rules"]["status"] in ("ok", "warning")
    assert [f["number"] for f in vp["figures"]] == ["1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.7"]
    first = Path(vp["dir"]) / vp["figures"][0]["jpg"]
    with Image.open(first) as im:
        assert im.format == "JPEG" and min(im.size) >= 800
    assert all(f["bytes"] <= 2 * 1024 * 1024 for f in vp["figures"])
    pack = api.filing_pack(cid, "v1", "EU")
    form = json.loads((Path(pack["dir"]) / "euipo_application.json").read_text())
    assert form["locarno_class"] == "06-04" and len(form["views"]) == 7 and form["designers"][0]["name"] == "Jane Doe"
    assert pack["checklist"]["quote"]["lines"][0]["currency"] == "EUR"


def test_eu_quote_multiple_designs():
    q = api.quote(["EU"], similar_designs=2)
    line = q["lines"][0]
    assert line["application"] == 350 + 2 * 125 and line["currency"] == "EUR"
