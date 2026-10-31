"""End-to-end CLI flows: local engine only, then against the real server
(registry search, hosted observer and Creem are faked at their network edge),
then the embedded agent team against a scripted model server."""

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from designaround_engine import api

from .conftest import ROOT, free_port


def test_local_flow(cli, home):
    code, body = cli("case", "create", "Local box", "--article", "storage box", "-j", "US", "-j", "CN")
    case = body["data"]["id"]
    code, body = cli("model", case, "--template", "storage_box")
    assert code == 0 and body["data"]["id"] == "v1" and body["data"]["kind"] == "original"
    assert Path(body["data"]["contact_sheet"]).exists()
    views = home / "cases" / case / "versions" / "v1" / "views"
    code, body = cli("prior-art", "add", case, "--number", "USD9000001S", "--office", "US", "-f", str(views / "front.lineart.png"))
    assert code == 0
    code, body = cli("prior-art", "rank", case)
    assert body["data"]["ranked"][0]["shape_score"] > 0.9
    assert body["next_actions"][0].startswith(f"designaround observe {case} v1 USD9000001S")

    code, body = cli("cad", "tweak", case, "v1", "--set", "handle_style=recessed", "--set", "rib_count=3", "--rationale", "drop the arched handle")
    assert code == 0 and body["data"]["id"] == "v2" and body["data"]["parent"] == "v1"
    code, body = cli("originality", case, "v2")
    assert body["data"]["pass"] is True
    code, body = cli("cad", "tweak", case, "v1", "--set", "corner_radius=30")
    code, body = cli("originality", case, "v3")
    assert body["data"]["pass"] is False  # edge treatment only
    code, body = cli("dfm", case, "v2")
    assert code == 0 and "checks" in body["data"]
    code, body = cli("evaluate", case, "v2")
    assert code == 0 and body["data"]["summary"]["level"] in ("high", "medium", "low", "not_seen")
    assert any("observe" in a for a in body["next_actions"])  # no observer score yet

    code, body = cli("viewpack", case, "v2", "--office", "US")
    assert code == 0 and body["data"]["watermark"] is True and body["data"]["plan"] == "free"

    # path B: import a STEP file as the original of another case
    step = home / "cases" / case / "versions" / "v1" / "model.step"
    code, body = cli("case", "create", "Imported box", "--article", "storage box")
    imported = body["data"]["id"]
    code, body = cli("model", imported, "--cad", str(step))
    assert code == 0, body
    assert body["data"]["kind"] == "original" and abs(body["data"]["bbox"]["zlen"] - json.loads((home / "cases" / case / "versions" / "v1" / "manifest.json").read_text())["bbox"]["zlen"]) < 0.5
    code, body = cli("cad", "tweak", imported, "v1", "--set", "length=300")
    assert code == 5 and body["kind"] == "invalid_input"  # imported B-rep has no parametric program
    code, body = cli("case", "decide", case, "chose v2", "--person", "Jane Doe")
    assert code == 0
    code, body = cli("evidence", "verify", case, "--offline")
    assert code == 0 and body["data"]["chain_ok"] is True

    # tampering with the log is detected
    log = home / "cases" / case / "events.jsonl"
    lines = log.read_text().splitlines()
    event = json.loads(lines[1])
    event["payload"]["tampered"] = True
    lines[1] = json.dumps(event)
    log.write_text("\n".join(lines) + "\n")
    code, body = cli("evidence", "verify", case, "--offline")
    assert code == 6 and body["kind"] == "evidence_mismatch"


def _admin_key(url: str, plan: str, credits: int) -> str:
    r = httpx.post(f"{url}/v1/admin/keys", json={"plan": plan, "credits": credits, "email": f"{plan}@example.com"}, headers={"Authorization": "Bearer admin-secret"})
    assert r.status_code == 200, r.text
    return r.json()["data"]["api_key"]


def test_server_flow(cli, home, live_server, monkeypatch):
    url, app = live_server["url"], live_server["app"]
    monkeypatch.setenv("DESIGNAROUND_BASE_URL", url)

    # trial key via onboard: stored, not printed
    code, body = cli("onboard", "--agent", "pytest")
    assert code == 0 and body["data"]["api_key"].endswith("…") and body["data"]["stored_in"] == "file (0600)"
    code, body = cli("auth", "status")
    assert body["data"]["key_source"] == "file" and body["data"]["me"]["plan"] == "free"

    # switch to a Pro key
    pro = _admin_key(url, "pro", 5000)
    code, body = cli("auth", "login", input=pro + "\n")
    assert code == 0 and body["data"]["plan"] == "pro"

    code, body = cli("case", "create", "Server box", "--article", "storage box", "-j", "US")
    case = body["data"]["id"]
    cli("model", case, "--template", "storage_box")
    figure = home / "cases" / case / "versions" / "v1" / "views" / "front.lineart.png"

    # Registry search (ClearComply) faked at the server's network edge, in the
    # shape the live service returns: PPUBS numbers like D1000001 with inline
    # data:image thumbnails, Google Patents CN designs without figures.
    import base64

    thumb = "data:image/png;base64," + base64.b64encode(figure.read_bytes()).decode()
    seen: list[dict] = []

    async def fake_search(**kw):
        seen.append(kw)
        if kw["sources"] == ["google_patents"]:
            return {"source_results": [{"source_id": "google_patents", "success": True, "items": [{"publication_number": "CN306500365S", "title": "收纳箱", "jurisdiction": "CN", "image_urls": []}]}]}
        return {"source_results": [
            {"source_id": "uspto_patent", "success": True, "items": [
                {"publication_number": "D1000001", "title": "Storage box", "jurisdiction": "US", "status": "granted", "image_urls": [thumb]},
                {"publication_number": "D1000002", "title": "Container lid", "jurisdiction": "US", "image_urls": ["https://img.example/d2.png"]},
                {"publication_number": "11223344", "title": "Utility patent, not a design", "jurisdiction": "US", "image_urls": [thumb]},
            ]},
            {"source_id": "dpma", "success": False, "error": {"code": "SOURCE_BLOCKED"}, "items": []},
        ]}

    monkeypatch.setattr(app.clearcomply, "search", fake_search)
    from designaround_cli import screening

    monkeypatch.setattr(screening, "_fetch_figure", lambda url, dest: Path(shutil.copyfile(figure, dest)))
    # full-size USPTO drawing sheet: one sheet with the figure in the middle and a "FIG. 1" label below
    from PIL import Image as _Image
    from PIL import ImageDraw as _Draw

    sheet = _Image.new("L", (1700, 2200), 255)
    fig = _Image.open(figure).convert("L").resize((900, 900))
    sheet.paste(fig, (400, 500))
    _Draw.Draw(sheet).text((800, 1600), "FIG. 1", fill=0)
    import io as _io

    buf = _io.BytesIO()
    sheet.save(buf, format="PNG")
    fetched = []
    monkeypatch.setattr(screening, "UsptoFigures", None)

    def fake_sheets(number, max_sheets):
        fetched.append(number)
        return [buf.getvalue()]

    orig_upgrade = screening.upgrade_us_figures
    monkeypatch.setattr(screening, "upgrade_us_figures", lambda case_id, ids, **kw: orig_upgrade(case_id, ids, sheets_fn=fake_sheets, **kw))
    code, body = cli("case", "set", case, "--article-zh", "收纳箱", "-j", "US", "-j", "CN")
    code, body = cli("screen", case, "--max-credits", "50")
    assert code == 0, body
    queries = {q["query"]: q["sources"] for q in body["data"]["queries"]}
    assert queries == {"ornamental design for a storage box": ["uspto_patent"], "收纳箱 外观设计": ["google_patents"]}
    assert all(not kw.get("image_base64") for kw in seen)  # quick screens are text-only
    numbers = {a["number"] for a in body["data"]["added"]}
    assert numbers == {"USD1000001S", "USD1000002S", "CN306500365S"}  # canonical numbers, utility patent dropped
    assert body["data"]["without_figures"] == ["CN306500365S"]
    assert body["data"]["source_failures"] == [{"query": "ornamental design for a storage box", "mode": "text", "source": "dpma", "error": "SOURCE_BLOCKED"}]
    assert set(fetched) == {"USD1000001S", "USD1000002S"}
    assert all(u["ok"] and u["figures"] == 1 for u in body["data"]["full_drawings"])
    assert body["data"]["ranked"][0]["id"] in {"USD1000001S", "USD1000002S"} and body["data"]["ranked"][0]["shape_score"] > 0.9
    pa = next(p for p in api.prior_art_list(case) if p["id"] == "USD1000001S")
    assert pa["figure_source"] == "uspto_ppubs" and len(pa["figures"]) == 1
    assert body["data"]["blind_spots"] and body["cost"]["charged"] == 0  # within the free monthly allowance

    # hosted observer (host mode), upstream model faked; median of 3 judgements
    replies = iter([7.0, 9.0, 7.5])

    async def fake_complete(model, messages, **kw):
        assert "independent visual reviewer" in messages[0]["content"]
        assert "CANDIDATE" in messages[0]["content"] and "Product type: storage box" in messages[1]["content"][0]["text"]
        return json.dumps({"score": next(replies), "similar": ["arched handle"], "different": [], "distinctive_features_of_prior_art": ["arched handle"]}), {"prompt_tokens": 900, "completion_tokens": 120}

    monkeypatch.setattr(app.llm, "complete_once", fake_complete)
    monkeypatch.setenv("DA_MODEL_MODE", "host")
    code, body = cli("observe", case, "v1", "USD1000001S", "--max-credits", "120")
    assert code == 0, body
    assert body["data"]["score"] == 7.5 and sorted(body["data"]["samples"]) == [7.0, 7.5, 9.0] and body["cost"]["charged"] == 120
    assert body["data"]["view"] == "multi"
    code, body = cli("evaluate", case, "v1")
    row = next(r for r in body["data"]["per_prior_art"] if r["prior_art_id"] == "USD1000001S")
    assert row["observer"]["score"] == 7.5 and body["data"]["summary"]["qualified"] is False

    code, body = cli("credits")
    assert body["data"]["balance"] == 5000 - 120

    # Pro: no watermark, filing package allowed
    code, body = cli("viewpack", case, "v1", "--office", "US")
    assert body["data"]["watermark"] is False
    code, body = cli("case", "set", case, "--applicant", "ACME Ltd", "--applicant-domicile", "cn", "--applicant-type", "juristic", "--inventor", "Jane Doe;Shenzhen, CN")
    assert body["data"]["applicant"] == {"name": "ACME Ltd", "domicile": "CN", "type": "juristic"}
    code, body = cli("filing-pack", case, "v1", "--office", "US")
    assert code != 0, body
    assert body["kind"] == "filing_not_ready"
    assert "REP-US-001" in body["data"]["message"]
    details = body["data"]["details"]
    checklist = details["checklist"]
    assert Path(details["dir"], "checklist.json").exists()
    assert not Path(details["dir"], "specification.docx").exists()
    assert {"REP-US-001", "REP-US-002"} <= {f["rule_id"] for f in checklist["findings"]}  # foreign juristic applicant
    assert checklist["requires_practitioner_gate"] is True
    assert checklist["ready_for_practitioner"] is False
    assert "applicant.entity_size (large/small/micro)" in checklist["missing"]

    # evidence anchoring and verification against the server
    code, body = cli("evidence", "anchor", case)
    assert code == 0 and body["data"]["receipt"]
    code, body = cli("evidence", "verify", case)
    assert code == 0 and body["data"]["anchors_ok"] is True and len(body["data"]["anchors"]) == 1

    # practitioner gate
    code, body = cli("gate", "create", case, "--type", "filing_submission", "--reviewer-email", "agent@lawfirm.example")
    assert code == 0 and body["data"]["review_url"].startswith(url + "/gate/")
    gate_id = body["data"]["id"]
    code, body = cli("gate", "status", gate_id)
    assert body["data"]["status"] == "pending"

    # checkout: URL only, managed packages carry the direct-payment note
    async def fake_checkout(**kw):
        return {"id": "ch_test", "checkout_url": f"https://checkout.creem.io/{kw['order_id']}"}

    monkeypatch.setattr(app.creem, "create_checkout", fake_checkout)
    # checkout_ready requires this SKU's product id, not only the API key.
    monkeypatch.setenv("CREEM_PRODUCT_DA_CREDITS_5K", "prod_test_credits_5k")
    code, body = cli("billing", "checkout", "--credits", "5k")
    assert code == 0 and body["data"]["sku"] == "credits_5k" and body["data"]["checkout_url"].startswith("https://checkout.creem.io/")
    code, body = cli("billing", "order", body["data"]["order_id"])
    assert body["data"]["status"] == "pending"
    code, body = cli("billing", "plans")
    assert body["data"]["plans"]["pro"]["monthly_usd"] == 59


def _wait_port(port: int, timeout: float = 15) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise AssertionError("fake model server did not start")


@pytest.mark.skipif(not shutil.which("node") or not (ROOT / "agents" / "node_modules" / "@earendil-works" / "pi-coding-agent").exists(), reason="Node.js and agents/node_modules are needed")
def test_around_with_embedded_agent_team(cli, home, monkeypatch):
    """CLI -> isolated Pi runtime -> engine, with a scripted model server standing in for /v1/llm."""

    port = free_port()
    fake = subprocess.Popen(["node", str(ROOT / "agents" / "test" / "fake-llm.mjs"), str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_port(port)
        monkeypatch.setenv("DESIGNAROUND_BASE_URL", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("DESIGNAROUND_API_KEY", "da_test_key")
        monkeypatch.setenv("DA_MODEL_MODE", "platform")
        monkeypatch.setenv("PI_CODING_AGENT_DIR", "/should/not/be/used")
        code, body = cli("case", "create", "Agent box", "--article", "storage box", "-j", "US")
        case = body["data"]["id"]
        cli("model", case, "--template", "storage_box")
        figure = home / "cases" / case / "versions" / "v1" / "views" / "front.lineart.png"
        cli("prior-art", "add", case, "--number", "US D1,000,001 S", "--office", "US", "-f", str(figure))

        code, body = cli("around", case, "--max-credits", "6000", "--rounds", "1", "--per-round", "1", "--target", "1", "--top", "1")
        assert code == 0, body
        data = body["data"]
        assert data["status"] == "ok" and data["qualified"] == ["v2"]
        assert body["cost"]["estimated"] == 6000 and 0 < body["cost"]["charged"] <= 6000
        assert (home / "pi-agent" / "models.json").exists()  # Pi config inside DESIGNAROUND_HOME
        code, body = cli("cad", "show", case, "v2")
        assert body["data"]["created_by"] == "agent" and body["data"]["evaluation"]["summary"]["qualified"] is True

        code, body = cli("agent", "check")
        assert code == 0 and body["data"]["isolated"] is True
        assert body["data"]["isolation"]["pi_agent_dir"] == str(home / "pi-agent")
    finally:
        fake.terminate()
        fake.wait(timeout=10)
    assert not os.environ.get("DA_BYOK_KEY")
