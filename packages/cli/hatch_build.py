"""Bundle the Skill and the agent runtime sources into the CLI distribution.

Sources live outside this package (repo-level skills/ and agents/). When
building from the repository they are read from there; when building a wheel
from an sdist they are read from the sdist's bundle/ directory.
"""

from __future__ import annotations

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

ITEMS = {
    "skills/designaround": "skill",
    "agents/src": "agents/src",
    "agents/ui": "agents/ui",
    "agents/package.json": "agents/package.json",
    "agents/package-lock.json": "agents/package-lock.json",
}


class BundleHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        root = Path(self.root)
        repo = root.parent.parent
        prefix = "bundle" if self.target_name == "sdist" else "designaround_cli/bundle"
        for src, dest in ITEMS.items():
            source = repo / src if (repo / src).exists() else root / "bundle" / dest
            if not source.exists():
                raise RuntimeError(f"Bundle source missing: {src} (looked in {repo} and {root / 'bundle'})")
            build_data["force_include"][str(source)] = f"{prefix}/{dest}"
