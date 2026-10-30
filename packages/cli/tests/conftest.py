import importlib
import json
import socket
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[3]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Isolated CLI environment: temp DESIGNAROUND_HOME, no keychain, agent (non-interactive) mode."""

    root = tmp_path / "da-home"
    monkeypatch.setenv("DESIGNAROUND_HOME", str(root))
    monkeypatch.setenv("DESIGNAROUND_NO_KEYRING", "1")
    monkeypatch.setenv("DESIGNAROUND_AGENT", "1")
    monkeypatch.setenv("DESIGNAROUND_BASE_URL", "http://127.0.0.1:9")
    for name in ("DESIGNAROUND_API_KEY", "DA_MODEL_MODE", "DA_BYOK_KEY", "DA_BYOK_PROVIDER", "DA_BYOK_MODEL"):
        monkeypatch.delenv(name, raising=False)
    return root


@pytest.fixture()
def cli(home):
    from designaround_cli.main import app

    runner = CliRunner()

    def invoke(*args: str, input: str | None = None):
        result = runner.invoke(app, ["--json", *args], input=input, catch_exceptions=False)
        lines = [ln for ln in result.stdout.splitlines() if ln.strip().startswith("{")]
        body = json.loads(lines[-1]) if lines else None
        return result.exit_code, body

    return invoke


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    """The real FastAPI server on a local port (uvicorn in a thread)."""

    import uvicorn

    mp = pytest.MonkeyPatch()
    data = tmp_path_factory.mktemp("server")
    port = free_port()
    env = {
        "DA_DATA_DIR": str(data),
        "APP_API_KEY": "admin-secret",
        "PUBLIC_BASE_URL": f"http://127.0.0.1:{port}",
        "CREEM_API_KEY": "creem_test_dummy",
        "CREEM_WEBHOOK_SECRET": "whsec_test",
        "CREEM_PRODUCT_DA_PRO_MONTHLY": "prod_test_pro_monthly",
        "LLM_UPSTREAM_BASE_URL": "http://upstream.invalid/v1",
        "LLM_UPSTREAM_API_KEY": "sk-upstream",
    }
    for key, value in env.items():
        mp.setenv(key, value)
    from designaround_server import config as server_config
    from designaround_server import db

    server_config.get_settings.cache_clear()
    db._engine = None
    db._Session = None
    db.init_engine()
    from designaround_server import app as app_module

    importlib.reload(app_module)
    server = uvicorn.Server(uvicorn.Config(app_module.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "server did not start"
    yield {"url": f"http://127.0.0.1:{port}", "app": app_module, "mp": mp}
    server.should_exit = True
    thread.join(timeout=10)
    mp.undo()
    server_config.get_settings.cache_clear()
