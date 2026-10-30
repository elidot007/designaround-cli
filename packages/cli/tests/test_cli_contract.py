"""Output contract, exit codes and budget rules (no server, no CAD builds)."""

import json

from designaround_cli.main import normalize_argv, parse_kv
from designaround_core import SCHEMA_VERSION


def _case(cli) -> str:
    code, body = cli("case", "create", "Contract box", "--article", "storage box", "-j", "US")
    assert code == 0 and body["ok"]
    return body["data"]["id"]


def test_envelope_and_invalid_input(cli):
    code, body = cli("case", "show", "missing-case")
    assert code == 5
    assert body["schema_version"] == SCHEMA_VERSION
    assert body["ok"] is False and body["kind"] == "not_found"
    assert set(body) == {"schema_version", "kind", "ok", "data", "next_actions", "cost"}


def test_global_flags_anywhere():
    assert normalize_argv(["case", "list", "--json"]) == ["--json", "case", "list"]
    assert normalize_argv(["cad", "run", "c", "p.py", "--", "--json"]) == ["cad", "run", "c", "p.py", "--", "--json"]
    assert parse_kv(["a=1", "b=true", "c=recessed", "d=2.5"]) == {"a": 1, "b": True, "c": "recessed", "d": 2.5}


def test_agents_must_state_budgets(cli, monkeypatch):
    case = _case(cli)
    code, body = cli("around", case)
    assert code == 3 and body["kind"] == "budget_required"
    assert body["data"]["details"]["suggested"] == 5900  # the standard depth's cap
    code, body = cli("observe", case, "v1", "X")
    assert code == 3 and body["kind"] == "budget_required"
    monkeypatch.setenv("DA_MODEL_MODE", "host")
    code, body = cli("observe", case, "v1", "X", "--max-credits", "50")
    assert code == 3 and body["kind"] == "budget_too_low" and body["data"]["details"]["price"] == 120
    code, body = cli("screen", case, "--deep", "--max-credits", "50")
    assert code == 3 and body["kind"] == "budget_too_low"


def test_host_mode_around_needs_the_host(cli, monkeypatch):
    case = _case(cli)
    monkeypatch.setenv("DA_MODEL_MODE", "host")
    code, body = cli("around", case, "--max-credits", "1000")
    assert code == 2 and body["kind"] == "host_mode"
    assert any("observe" in step for step in body["data"]["details"]["loop"])


def test_auth_and_plan_gates(cli):
    case = _case(cli)
    code, body = cli("screen", case, "--max-credits", "50")
    assert code == 4 and body["kind"] == "auth_required"
    code, body = cli("filing-pack", case, "v1", "--office", "US")
    assert code == 3 and body["kind"] == "plan_required"
    assert any("billing checkout --plan pro" in a for a in body["next_actions"])


def test_byok_config_is_validated_before_spawning(cli, monkeypatch):
    case = _case(cli)
    monkeypatch.setenv("DA_MODEL_MODE", "byok")
    code, body = cli("around", case, "--max-credits", "1000")
    assert code == 4 and body["kind"] == "byok_incomplete"


def test_byok_key_goes_to_secret_store_not_config(cli, home):
    code, body = cli("config", "byok", "--provider", "anthropic", "--model", "claude-test", "--key-stdin", input="sk-secret-123\n")
    assert code == 0 and body["data"]["model_mode"] == "byok"
    assert "sk-secret-123" not in (home / "config.json").read_text()
    assert (home / "secrets.json").stat().st_mode & 0o777 == 0o600
    code, body = cli("config", "show")
    assert body["data"]["byok"]["key_stored"] is True and "sk-secret" not in json.dumps(body)


def test_billing_choice_validation(cli):
    code, body = cli("billing", "checkout", "--plan", "pro", "--credits", "5k")
    assert code == 5 and body["kind"] == "choose_one"
    code, body = cli("billing", "checkout", "--package", "managed_moon")
    assert code == 5 and body["kind"] == "bad_package"


def test_quote_is_local(cli):
    code, body = cli("quote", "-o", "US", "-o", "CN", "--managed")
    assert code == 0 and body["kind"] == "quote"
    assert {line["office"] for line in body["data"]["lines"] if "office" in line} >= {"US", "CN"}


def test_skill_install_into_project(cli, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, body = cli("skill", "install", "kiro", "--project")
    assert code == 0
    files = body["data"]["files"]
    assert "SKILL.md" in files and "references/workflow.md" in files and "scripts/da.py" in files
    skill = (tmp_path / ".kiro" / "skills" / "designaround" / "SKILL.md").read_text()
    assert skill.startswith("---\nname: designaround\n")
    code, body = cli("skill", "install", "kiro", "--project")
    assert code == 5 and body["kind"] == "exists"


def test_assistant_needs_a_model_mode(cli, monkeypatch):
    monkeypatch.setenv("DA_MODEL_MODE", "host")
    code, body = cli("chat", "-m", "hi")
    assert code == 2 and body["kind"] == "host_mode"
    code, body = cli("ui", "--no-browser")
    assert code == 2 and body["kind"] == "host_mode"
    monkeypatch.setenv("DA_MODEL_MODE", "platform")
    code, body = cli("chat", "-m", "hi")
    assert code == 4 and body["kind"] == "auth_required"


def test_outdated_runtime_is_detected(tmp_path, monkeypatch):
    from designaround_cli import bundles

    source = tmp_path / "bundle" / "agents"
    (source / "src").mkdir(parents=True)
    (source / "ui").mkdir()
    (source / "src" / "main.ts").write_text("new")
    (source / "ui" / "app.js").write_text("ui")
    installed = tmp_path / "runtime"
    (installed / "src").mkdir(parents=True)
    (installed / "src" / "main.ts").write_text("old")
    monkeypatch.setattr(bundles, "PKG", tmp_path)
    assert bundles.agents_outdated(installed) is True
    (installed / "src" / "main.ts").write_text("new")
    (installed / "ui").mkdir()
    (installed / "ui" / "app.js").write_text("ui")
    assert bundles.agents_outdated(installed) is False
