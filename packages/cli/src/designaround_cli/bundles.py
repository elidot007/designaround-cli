"""Locate the Skill bundle and the embedded agent runtime (repo checkout or wheel)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from . import config
from .output import EXIT_ERROR, EXIT_INVALID, CliExit

PKG = Path(__file__).resolve().parent


def _repo_root() -> Path | None:
    for parent in PKG.parents:
        if (parent / "agents" / "package.json").exists() and (parent / "skills" / "designaround").exists():
            return parent
    return None


def skill_dir() -> Path:
    bundled = PKG / "bundle" / "skill"
    if bundled.exists():
        return bundled
    repo = _repo_root()
    if repo:
        return repo / "skills" / "designaround"
    raise CliExit(EXIT_ERROR, "bundle_missing", "Skill bundle not found in this installation.")


SKILL_TARGETS = {
    "claude-code": ".claude/skills/designaround",
    "cursor": ".cursor/skills/designaround",
    "codex": ".codex/skills/designaround",
    "gemini-cli": ".gemini/skills/designaround",
    "opencode": ".opencode/skills/designaround",
    "openclaw": ".openclaw/workspace/skills/designaround",
    "kiro": ".kiro/skills/designaround",
}


def install_skill(target: str, *, project: bool, overwrite: bool) -> dict:
    if target not in SKILL_TARGETS:
        raise CliExit(EXIT_INVALID, "unknown_target", f"Unknown target {target}", data={"targets": sorted(SKILL_TARGETS)})
    base = Path.cwd() if project else Path.home()
    dest = base / SKILL_TARGETS[target]
    if dest.exists():
        if not overwrite:
            raise CliExit(EXIT_INVALID, "exists", f"{dest} exists; pass --overwrite to replace it.")
        shutil.rmtree(dest)
    shutil.copytree(skill_dir(), dest, ignore=shutil.ignore_patterns("__pycache__"))
    return {"target": target, "path": str(dest), "files": sorted(str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file())}


def agents_dir() -> Path:
    """Runtime directory of the embedded Pi Agent team (node_modules installed, isolated)."""

    override = os.environ.get("DESIGNAROUND_AGENTS_DIR")
    if override:
        return Path(override)
    repo = _repo_root()
    if repo and (repo / "agents" / "node_modules" / "@earendil-works" / "pi-coding-agent").exists():
        return repo / "agents"
    return config.home() / "agent-runtime"


def agents_outdated(root: Path) -> bool:
    """An installed runtime (~/.designaround/agent-runtime) whose sources differ from the bundle."""

    source = PKG / "bundle" / "agents"
    if not source.exists() or root.resolve() == source.resolve() or not (root / "src").exists():
        return False
    for path in list((source / "src").glob("*.ts")) + [p for p in (source / "ui").rglob("*") if p.is_file()]:
        rel = path.relative_to(source)
        installed = root / rel
        if not installed.exists() or installed.read_bytes() != path.read_bytes():
            return True
    return False


def node_bin() -> str:
    node = os.environ.get("DA_NODE") or shutil.which("node")
    if not node:
        raise CliExit(EXIT_ERROR, "node_missing", "Node.js >= 22.19 is required for agent runs (CAD Agent, Designer, Observer).", next_actions=["Install Node.js 22+ or use host mode inside Claude Code / Codex."])
    return node


def setup_agents() -> dict:
    """Install the pinned agent runtime into ~/.designaround/agent-runtime (never touches ~/.pi)."""

    target = config.home() / "agent-runtime"
    source = PKG / "bundle" / "agents"
    if not source.exists():
        repo = _repo_root()
        if repo is None:
            raise CliExit(EXIT_ERROR, "bundle_missing", "Agent bundle not found.")
        source = repo / "agents"
    target.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "package-lock.json"):
        shutil.copyfile(source / name, target / name)
    for folder in ("src", "ui"):
        if (target / folder).exists():
            shutil.rmtree(target / folder)
        if (source / folder).exists():
            shutil.copytree(source / folder, target / folder)
    npm = shutil.which("npm")
    if not npm:
        raise CliExit(EXIT_ERROR, "npm_missing", "npm is required to install the agent runtime.")
    proc = subprocess.run([npm, "ci", "--omit=dev", "--no-audit", "--no-fund"], cwd=target, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise CliExit(EXIT_ERROR, "npm_failed", "npm ci failed", data={"stderr": proc.stderr[-2000:]})
    return {"path": str(target)}
