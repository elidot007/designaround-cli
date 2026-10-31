"""Supervisor for a detached agent run (FR-CLI-05).

Started by ``runner.detach`` in its own session when ``--detach`` is given:

    python -m designaround_cli.detached <runs>/<run_id>.json

Its own environment is the runner environment (it carries the API key, which is never
written to the run file). It starts the agent runner with stdout going to
``<run_id>.ndjson``, waits, records the exit code and sends the completion
notification. ``designaround run watch`` reads the same NDJSON file, so progress
survives the terminal that started it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _write(meta_path: Path, meta: dict) -> None:
    tmp = meta_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    tmp.replace(meta_path)


def main() -> int:
    meta_path = Path(sys.argv[1])
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    log = Path(meta["log"])
    err = Path(meta["stderr"])
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with log.open("w", encoding="utf-8") as out, err.open("w", encoding="utf-8") as errf:
        proc = subprocess.Popen(meta["args"], cwd=meta["cwd"], env=env, stdout=out, stderr=errf, stdin=subprocess.DEVNULL, creationflags=creation)
        meta.update({"agent_pid": proc.pid, "supervisor_pid": os.getpid(), "status": "running"})
        _write(meta_path, meta)
        code = proc.wait()
    events = []
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    outcome = next((e for e in reversed(events) if e.get("type") in ("result", "error")), None)
    current = json.loads(meta_path.read_text(encoding="utf-8"))
    status = "aborted" if current.get("status") == "aborted" else "finished"
    if outcome is not None and status != "aborted":
        # Result guarantee (FR-ACCT-08): filed here so it also works when nobody watches.
        from .runner import apply_server_spend, finish_run

        outcome.setdefault("run_id", meta["run_id"])
        apply_server_spend(outcome)
        finish_run(meta["task"], meta.get("run") or {}, outcome, events, meta.get("lang") or "zh")
        if outcome.get("guarantee"):
            meta["guarantee"] = outcome["guarantee"]
    meta.update({"status": status, "exit_code": code, "finished": time.time(), "outcome_type": (outcome or {}).get("type"), "outcome_status": (outcome or {}).get("status")})
    _write(meta_path, meta)
    if status != "aborted":
        try:
            from .notify import describe_outcome, notify

            title, text = describe_outcome(meta["task"], meta["case"], code, outcome or {"type": "error", "message": "the agent runner exited without a result"})
            notify(title, text, {"run_id": meta["run_id"], "case": meta["case"], "task": meta["task"], "exit_code": code}, force_desktop=True)
        except Exception:  # noqa: BLE001 - notifications are best effort
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
