#!/usr/bin/env python3
"""Run the DesignAround CLI with --json and print a compact envelope.

Usage: python3 scripts/da.py <designaround args...>

Keeps the CLI exit code. Long lists in `data` are cut to 20 items and long
strings to 2000 characters so agent hosts with small output windows still see
`ok`, `kind`, `next_actions` and `cost`. Standard library only.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

MAX_ITEMS = 20
MAX_STR = 2000


def _trim(value, depth=0):
    if isinstance(value, str):
        return value if len(value) <= MAX_STR else value[:MAX_STR] + f"… (+{len(value) - MAX_STR} chars)"
    if isinstance(value, list):
        head = [_trim(v, depth + 1) for v in value[:MAX_ITEMS]]
        return head + ([f"… (+{len(value) - MAX_ITEMS} more)"] if len(value) > MAX_ITEMS else [])
    if isinstance(value, dict):
        return {k: _trim(v, depth + 1) for k, v in value.items()}
    return value


def main(argv: list[str]) -> int:
    exe = shutil.which("designaround") or shutil.which("da")
    if exe is None:
        sys.stdout.write(json.dumps({"ok": False, "kind": "cli_missing", "data": {"message": "designaround CLI not found on PATH"}, "next_actions": ["pip install designaround  (or: uv tool install designaround)"]}) + "\n")
        return 6
    proc = subprocess.run([exe, "--json", *argv], capture_output=True, text=True, check=False)
    out = proc.stdout.strip().splitlines()
    try:
        envelope = json.loads(out[-1]) if out else None
    except ValueError:
        envelope = None
    if envelope is None:
        envelope = {"ok": proc.returncode == 0, "kind": "raw", "data": {"stdout": proc.stdout[-MAX_STR:], "stderr": proc.stderr[-MAX_STR:]}}
    sys.stdout.write(json.dumps(_trim(envelope), ensure_ascii=False, indent=1) + "\n")
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
