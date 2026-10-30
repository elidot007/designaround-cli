"""CLI configuration. Secrets go to the OS keychain; config.json holds only
non-sensitive settings. Precedence for the API key:
DESIGNAROUND_API_KEY env > keychain > (none).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

SERVICE = "designaround"
DEFAULT_SERVER = "https://designaround.elidot.com"
SUPPORT_EMAIL = "support@elidot.com"


def home() -> Path:
    root = Path(os.environ.get("DESIGNAROUND_HOME") or Path.home() / ".designaround")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _config_path() -> Path:
    return home() / "config.json"


def load() -> dict:
    path = _config_path()
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return {}
    return {}


def save(data: dict) -> None:
    path = _config_path()
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def server_url() -> str:
    return (os.environ.get("DESIGNAROUND_BASE_URL") or load().get("server") or DEFAULT_SERVER).rstrip("/")


def _keyring():
    if os.environ.get("DESIGNAROUND_NO_KEYRING"):
        return None
    try:
        import keyring

        keyring.get_keyring()
        return keyring
    except Exception:  # noqa: BLE001 - headless/CI without a keychain
        return None


def _fallback_secrets() -> Path:
    return home() / "secrets.json"


def set_secret(name: str, value: str | None) -> str:
    kr = _keyring()
    if kr is not None:
        try:
            if value is None:
                try:
                    kr.delete_password(SERVICE, name)
                except Exception:  # noqa: BLE001
                    pass
            else:
                kr.set_password(SERVICE, name, value)
            return "keychain"
        except Exception:  # noqa: BLE001
            pass
    path = _fallback_secrets()
    data = json.loads(path.read_text()) if path.exists() else {}
    if value is None:
        data.pop(name, None)
    else:
        data[name] = value
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return "file (0600)"


def _lookup(name: str) -> tuple[str | None, str | None]:
    kr = _keyring()
    if kr is not None:
        try:
            value = kr.get_password(SERVICE, name)
            if value:
                return value, "keychain"
        except Exception:  # noqa: BLE001
            pass
    path = _fallback_secrets()
    if path.exists():
        value = json.loads(path.read_text()).get(name)
        if value:
            return value, "file"
    return None, None


def get_secret(name: str) -> str | None:
    return _lookup(name)[0]


def secret_source(name: str) -> str | None:
    """Where get_secret finds the value: "keychain", "file" (secrets.json, 0600) or None."""

    return _lookup(name)[1]


def keychain_key_present(name: str = "api_key") -> bool:
    """macOS: does the login Keychain hold a DesignAround secret, e.g. one a separately installed
    CLI stored there? Asks for attributes only, so it never shows an access prompt."""

    if sys.platform != "darwin":
        return False
    try:
        found = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", SERVICE, "-a", name], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return found.returncode == 0


def keychain_secret(name: str) -> str | None:
    """Read straight from the OS keychain, also when DESIGNAROUND_NO_KEYRING keeps this install's
    secrets in the file. macOS may ask once whether this program may use the item."""

    try:
        import keyring

        return keyring.get_password(SERVICE, name) or None
    except Exception:  # noqa: BLE001 - denied, locked or no keychain
        return None


def api_key() -> str | None:
    return os.environ.get("DESIGNAROUND_API_KEY") or get_secret("api_key")


def model_mode() -> str:
    return os.environ.get("DA_MODEL_MODE") or load().get("model_mode") or "platform"


def agent_mode() -> bool:
    """Non-interactive callers (agents, CI) must pass budgets explicitly."""

    if os.environ.get("DESIGNAROUND_AGENT") == "1":
        return True
    try:
        return not sys.stdin.isatty()
    except (AttributeError, ValueError):
        return True


def cached_entitlements() -> dict | None:
    path = home() / "entitlements.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    if time.time() - data.get("fetched_at", 0) > 7 * 86400:
        return None
    return data


def cache_entitlements(me: dict) -> None:
    (home() / "entitlements.json").write_text(json.dumps({**me, "fetched_at": time.time()}))
