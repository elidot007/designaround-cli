"""Bridge to the embedded Pi Agent team (agents/src/main.ts).

The runner is a child Node process. It gets everything through its
environment, never through ~/.pi:

* DESIGNAROUND_HOME   -> Pi config lives in $DESIGNAROUND_HOME/pi-agent
* DA_PYTHON           -> this interpreter, so the agents call the same engine
* DA_MODEL_MODE       -> platform | byok
* platform: DA_SERVER_URL + DESIGNAROUND_API_KEY (server meters and caps the run)
* byok:     DA_BYOK_PROVIDER / DA_BYOK_MODEL / DA_BYOK_KEY (key stays in memory)

The runner prints NDJSON events on stdout; the last ``result`` or ``error``
event is the outcome. Exit codes follow the CLI contract (0/2/3/4/5/6).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import bundles, config
from .output import EXIT_AUTH, EXIT_BUDGET, EXIT_ERROR, EXIT_INVALID, STATE, CliExit

MIN_NODE = (22, 19)

# Windows: helper processes whose stdio is piped never need a console window. Without
# this flag every child of the desktop app (which has no console) would flash one.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def console_flags() -> int:
    """creationflags for children that inherit our stdio: hide them only when we have no console."""

    if sys.platform != "win32":
        return 0
    import ctypes

    return 0 if ctypes.windll.kernel32.GetConsoleWindow() else NO_WINDOW


def node_version(node: str) -> tuple[int, int, int]:
    proc = subprocess.run([node, "--version"], capture_output=True, text=True, check=False, creationflags=NO_WINDOW)
    match = re.match(r"v(\d+)\.(\d+)\.(\d+)", proc.stdout.strip())
    if not match:
        raise CliExit(EXIT_ERROR, "node_version", f"Cannot read Node.js version from {node}")
    return tuple(int(x) for x in match.groups())  # type: ignore[return-value]


def check_runtime() -> dict:
    node = bundles.node_bin()
    version = node_version(node)
    if version[:2] < MIN_NODE:
        raise CliExit(EXIT_ERROR, "node_too_old", f"Node.js {'.'.join(map(str, version))} found; agent runs need >= {MIN_NODE[0]}.{MIN_NODE[1]}.", next_actions=["Upgrade Node.js, or use host mode (`designaround config mode host`)."])
    root = bundles.agents_dir()
    main = root / "src" / "main.ts"
    pi = root / "node_modules" / "@earendil-works" / "pi-coding-agent"
    if main.exists() and pi.exists() and bundles.agents_outdated(root):
        raise CliExit(EXIT_ERROR, "agents_outdated", f"The agent runtime at {root} is older than this CLI.", next_actions=["designaround agent setup"])
    if not main.exists() or not pi.exists():
        raise CliExit(EXIT_ERROR, "agents_missing", f"Agent runtime not installed at {root}.", next_actions=["designaround agent setup"])
    return {"node": node, "node_version": ".".join(map(str, version)), "agents_dir": str(root), "main": str(main)}


def child_env(mode: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PI_")}
    env["DESIGNAROUND_HOME"] = str(config.home())
    env["DA_PYTHON"] = sys.executable
    env["DA_MODEL_MODE"] = mode
    env["NODE_NO_WARNINGS"] = "1"
    if mode == "platform":
        key = config.api_key()
        if not key:
            raise CliExit(EXIT_AUTH, "auth_required", "Platform model mode needs an API key.", next_actions=["designaround onboard", "designaround auth login --key da_...", "designaround config mode byok"])
        env["DESIGNAROUND_API_KEY"] = key
        env["DA_SERVER_URL"] = config.server_url()
    elif mode == "byok":
        settings = config.load().get("byok") or {}
        key = os.environ.get("DA_BYOK_KEY") or config.get_secret("byok_key")
        provider = os.environ.get("DA_BYOK_PROVIDER") or settings.get("provider")
        model = os.environ.get("DA_BYOK_MODEL") or settings.get("model")
        if not (key and provider and model):
            raise CliExit(EXIT_AUTH, "byok_incomplete", "BYOK mode needs a provider, a model and a key.", next_actions=["designaround config byok --provider anthropic --model <model-id>"])
        env.update({"DA_BYOK_KEY": key, "DA_BYOK_PROVIDER": provider, "DA_BYOK_MODEL": model})
        for name, field in (("DA_BYOK_OBSERVER_MODEL", "observer_model"), ("DA_BYOK_BASE_URL", "base_url")):
            value = os.environ.get(name) or settings.get(field)
            if value:
                env[name] = value
        env.pop("DESIGNAROUND_API_KEY", None)
    else:
        raise CliExit(EXIT_INVALID, "bad_mode", f"Agent runs need platform or byok model mode, not {mode}.")
    return env


def _progress(event: dict) -> None:
    """Short progress lines on stderr; stdout stays reserved for the result."""

    kind = event.get("type")
    text = None
    if kind == "run_start":
        text = f"run {event.get('run_id')} · {event.get('task')} · mode {event.get('mode')} · cap {event.get('max_credits')} credits"
    elif kind == "round_start":
        text = f"round {event.get('round')}"
    elif kind == "candidate":
        status = "qualified" if event.get("qualified") else "not qualified"
        text = f"  {event.get('version')}: {status}" + (f" · risk {event.get('risk')} ({event.get('level')})" if event.get("risk") is not None else "")
    elif kind == "observer":
        text = f"  observer {event.get('version')} vs {event.get('prior_art')}: {event.get('score')}/10"
    elif kind == "cost":
        text = f"  cost +{event.get('credits')} ({event.get('spent')}/{event.get('max')})"
    elif kind == "version":
        text = f"  saved {event.get('version')} ({event.get('kind')})" + (f" from {event.get('parent')}" if event.get("parent") else "")
    elif kind == "evaluation":
        summary = event.get("summary") or {}
        text = f"  {event.get('version')}: max risk {summary.get('max_risk')} ({summary.get('level')})"
    elif kind == "baseline_start":
        text = f"baseline {event.get('original')} vs {', '.join(event.get('prior_art') or [])}"
    elif kind == "round_end":
        text = f"round {event.get('round')} done · {event.get('qualified')} qualified"
    elif kind == "concepts_planned":
        text = f"planned {event.get('count')} directions: {', '.join(event.get('titles') or [])}"
    elif kind == "concept":
        verdict = "passed review" if event.get("pass") else "failed review: " + "; ".join(event.get("issues") or [])[:160]
        text = f"  {event.get('concept')} {event.get('title')}: fit {event.get('fit')}/10, {verdict}"
    elif kind == "diversity":
        pairs = event.get("too_similar") or []
        text = "  directions differ" if not pairs else f"  too similar: {', '.join('/'.join(p) for p in pairs)} (redrawing)"
    elif kind == "build_plan":
        if event.get("error"):
            text = "  modelling plan unavailable; the CAD Agent works from the concept alone"
        else:
            env = event.get("envelope") or {}
            size = "×".join(str(env.get(k, "?")) for k in ("width", "depth", "height"))
            text = f"  modelling plan: {size} mm, parts {', '.join(event.get('parts') or [])}, {event.get('features')} features"
    elif kind == "build_starter":
        text = f"  starter form from the plan ({event.get('variant')})" if event.get("ok") else "  no starter form; the CAD Agent starts from scratch"
    elif kind == "cad_try":
        text = f"  CAD try {event.get('n')}" + ("" if event.get("ok", True) else f" failed: {event.get('error', '')[:120]}")
    elif kind == "build_review":
        verdict = "accepted" if event.get("pass") else "rejected: " + "; ".join((event.get("mismatches") or []) + (event.get("geometry") or []))[:200]
        majors = f", {event.get('majors')} major difference(s)" if event.get("majors") is not None else ""
        text = f"  3D review {event.get('submission')}: match {event.get('match')}/10{majors}, {verdict}"
    elif kind == "model_retry":
        text = f"  model call failed ({event.get('message', '')[:120]}); retry {event.get('attempt')}/{event.get('max')}"
    elif kind == "model_error":
        text = f"  model error ({event.get('role')}): {event.get('message', '')[:160]}"
    elif kind == "stalled":
        text = f"  the model stopped responding for {event.get('seconds')} s; " + ("asking again" if event.get("retry") else "giving up")
    elif kind == "budget_exceeded":
        text = f"budget exceeded: {event.get('spent')}/{event.get('max')}"
    elif kind == "error":
        text = f"error: {event.get('message')}"
    if text:
        sys.stderr.write(text + "\n")
        sys.stderr.flush()


def script(name: str) -> tuple[dict, str]:
    """(runtime info, absolute path) of an agents/src entry script."""

    info = check_runtime()
    path = Path(info["agents_dir"]) / "src" / name
    if not path.exists():
        raise CliExit(EXIT_ERROR, "agents_outdated", f"{path} is missing; the agent runtime is older than this CLI.", next_actions=["designaround agent setup"])
    return info, str(path)


def register_run(run_id: str, task: str, max_credits: int, mode: str, details: dict) -> dict:
    """Tell the server what this run is before it spends anything (metering, result guarantee)."""

    run = {"run_id": run_id, "task": task, "registered": False, **{k: v for k, v in details.items() if v is not None}}
    if mode != "platform":
        return run
    from .client import Server

    body = {"run_id": run_id, "task": task, "max_credits": int(max_credits), **{k: details[k] for k in ("depth", "rounds", "per_round", "target") if details.get(k) is not None}}
    try:
        Server().post("/v1/runs", body, timeout=10)
        run["registered"] = True
    except CliExit:
        pass  # an older server or a network blip: the run still works, it is just not covered
    return run


def finish_run(task: str, run: dict, outcome: dict, events: list[dict], lang: str) -> None:
    """Result guarantee claim when the run is covered and produced nothing qualified."""

    try:
        from .guarantee import after_run

        claim = after_run(task, run, outcome, events, lang)
    except Exception as exc:  # noqa: BLE001 - never lose the run result over the refund call
        claim = {"run_id": run.get("run_id"), "status": "not_filed", "message": str(exc)[:200]}
    if claim:
        outcome["guarantee"] = claim


def run(task: str, case_id: str, *, max_credits: int, mode: str, extra: dict | None = None, details: dict | None = None, lang: str = "zh") -> tuple[int, dict, list[dict]]:
    """Run one agent task. Returns (exit_code, outcome_event, all_events).

    ``details`` (depth, rounds, per_round, target) is declared to the server with the run.
    """

    env = child_env(mode)
    info = check_runtime()
    run_id = f"run-{uuid.uuid4()}"
    env["DA_RUN_ID"] = run_id
    args = [info["node"], info["main"], task, "--case", case_id, "--max-credits", str(int(max_credits))]
    for key, value in (extra or {}).items():
        if value is None or value is False:
            continue
        args += [f"--{key}", str(value)]
    run_info = register_run(run_id, task, max_credits, mode, details or {})
    if STATE.get("detach"):
        detach(task, case_id, run_id, args, env, Path(info["agents_dir"]), max_credits, run_info=run_info, lang=lang)
    events: list[dict] = []
    started = time.time()
    proc = subprocess.Popen(args, cwd=Path(info["agents_dir"]), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, creationflags=NO_WINDOW)
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            events.append(event)
            if not STATE.get("quiet"):
                _progress(event)
        code = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait(timeout=10)
        raise
    stderr = proc.stderr.read() if proc.stderr else ""
    outcome = next((e for e in reversed(events) if e.get("type") in ("result", "error")), None)
    if outcome is None:
        outcome = {"type": "error", "code": "runner_crashed", "message": (stderr or "agent runner exited without a result")[-2000:]}
        code = code or EXIT_ERROR
    outcome.setdefault("run_id", run_id)
    apply_server_spend(outcome)
    finish_run(task, run_info, outcome, events, lang)
    if time.time() - started >= 60:
        # Long runs: tell the person (chat-bot webhook / desktop notification) when done.
        try:
            from .notify import describe_outcome, notify

            title, text = describe_outcome(task, case_id, code, outcome)
            notify(title, text, {"run_id": run_id, "case": case_id, "task": task, "exit_code": code})
        except Exception:  # noqa: BLE001 - best effort
            pass
    return code, outcome, events


def apply_server_spend(outcome: dict) -> int | None:
    """Overwrite the agent's local ``spent`` with the server ledger when we have one.

    The envelope, the run result and the notification must show this number.
    Zero is a real charge (a free quick screen) and must not fall through to the local estimate.
    """

    run_id = outcome.get("run_id")
    if not run_id or config.model_mode() != "platform":
        return None
    try:
        from .client import Server

        data = Server().get(f"/v1/runs/{run_id}/spend", timeout=10).get("data")
    except Exception:  # noqa: BLE001 - a missing ledger must not fail the run
        return None
    if not isinstance(data, dict) or not isinstance(data.get("spent"), (int, float)):
        return None
    spent = int(data["spent"])
    outcome["spent"] = spent
    return spent


def runs_dir() -> Path:
    path = config.home() / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def detach(task: str, case_id: str, run_id: str, args: list[str], env: dict, cwd: Path, max_credits: int, *, run_info: dict | None = None, lang: str = "zh") -> None:
    """Start the run under the detached supervisor, print the run id and stop."""

    from .output import emit

    folder = runs_dir()
    meta_path = folder / f"{run_id}.json"
    # The run file holds no secrets: the supervisor gets the runner environment (with the
    # API key) as its own environment and passes it on to the agent process.
    meta = {"run_id": run_id, "task": task, "case": case_id, "max_credits": max_credits, "started": time.time(), "status": "starting", "log": str(folder / f"{run_id}.ndjson"), "stderr": str(folder / f"{run_id}.stderr"), "args": args, "cwd": str(cwd), "run": run_info or {}, "lang": lang}
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    sup = subprocess.Popen([sys.executable, "-m", "designaround_cli.detached", str(meta_path)], env={**env, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}, **kwargs)
    emit("run_detached", {"run_id": run_id, "task": task, "case": case_id, "supervisor_pid": sup.pid, "log": meta["log"]}, markdown=f"Run **{run_id}** started in the background.", next_actions=[f"designaround run watch {run_id}", f"designaround run abort {run_id}", "designaround run list"])
    raise SystemExit(0)


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        if sys.platform == "win32":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, check=False, creationflags=NO_WINDOW).stdout
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def run_records() -> list[dict]:
    rows = []
    for path in sorted(runs_dir().glob("run-*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        status = meta.get("status")
        if status in ("starting", "running") and not _alive(meta.get("agent_pid")) and time.time() - meta.get("started", 0) > 30:
            status = "died"
        rows.append({"run_id": meta["run_id"], "task": meta.get("task"), "case": meta.get("case"), "status": status, "outcome": meta.get("outcome_status") or meta.get("outcome_type"), "exit_code": meta.get("exit_code"), "started": meta.get("started"), "log": meta.get("log"), "agent_pid": meta.get("agent_pid")})
    return rows


def run_record(run_id: str) -> dict:
    for row in run_records():
        if row["run_id"] == run_id or row["run_id"].startswith(run_id):
            return row
    raise CliExit(EXIT_INVALID, "not_found", f"No detached run {run_id}.", next_actions=["designaround run list"])


def watch(run_id: str, *, follow: bool = True, poll: float = 1.0) -> tuple[int, dict]:
    """Print progress of a detached run until it ends; returns (exit_code, outcome)."""

    row = run_record(run_id)
    log = Path(row["log"])
    seen = 0
    while True:
        lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        for line in lines[seen:]:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not STATE.get("quiet"):
                _progress(event)
            if event.get("type") in ("result", "error"):
                code = 0 if event.get("type") == "result" and event.get("status") not in ("needs_input", "gate_failed") else (2 if event.get("type") == "result" else EXIT_ERROR)
                return code, _with_guarantee(row["run_id"], row.get("task"), event)
        seen = len(lines)
        current = run_record(row["run_id"])
        if current["status"] in ("finished", "died", "aborted") or not follow:
            return (current.get("exit_code") or EXIT_ERROR), {"type": "error", "code": current["status"], "message": f"run {current['status']} without a result"}
        time.sleep(poll)


PROGRESS_TYPES = {"run_start", "round_start", "round_end", "candidate", "baseline_start", "model_done", "concept", "build_try", "build_review", "model_retry", "model_error"}


def status(run_id: str, *, tail: int = 8) -> dict:
    """Snapshot of a detached run without waiting: state, recent progress, outcome when done."""

    row = run_record(run_id)
    log = Path(row["log"]) if row.get("log") else None
    events = []
    if log and log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    outcome = next((e for e in reversed(events) if e.get("type") in ("result", "error")), None)
    meta_path = runs_dir() / f"{row['run_id']}.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    if outcome is not None and meta.get("guarantee"):
        outcome = {**outcome, "guarantee": meta["guarantee"]}
    progress = [e for e in events if e.get("type") in PROGRESS_TYPES][-tail:]
    return {**row, "elapsed_s": round(time.time() - (row.get("started") or time.time())), "events": len(events), "progress": progress, "outcome": outcome, "done": outcome is not None or row["status"] in ("finished", "died", "aborted")}


def _with_guarantee(run_id: str, task: str | None, outcome: dict, wait: float = 30.0) -> dict:
    """The supervisor files the result-guarantee claim after the agent exits; pick it up."""

    if task != "around":
        return outcome
    meta_path = runs_dir() / f"{run_id}.json"
    deadline = time.time() + wait
    while True:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return outcome
        if meta.get("status") not in ("starting", "running") or time.time() > deadline:
            return {**outcome, "guarantee": meta["guarantee"]} if meta.get("guarantee") else outcome
        time.sleep(0.5)


def abort(run_id: str) -> dict:
    row = run_record(run_id)
    pid = row.get("agent_pid")
    if row["status"] not in ("starting", "running") or not _alive(pid):
        return {"run_id": row["run_id"], "aborted": False, "status": row["status"]}
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False, creationflags=NO_WINDOW)
    else:
        import signal

        os.kill(pid, signal.SIGTERM)
    meta_path = runs_dir() / f"{row['run_id']}.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.update({"status": "aborted", "aborted_at": time.time()})
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return {"run_id": row["run_id"], "aborted": True}


def require_budget(max_credits: int | None, default: int, what: str) -> int:
    """FR-CLI-04: agents must state a budget; people confirm a default."""

    if max_credits is not None:
        if max_credits <= 0:
            raise CliExit(EXIT_BUDGET, "budget_required", "--max-credits must be a positive integer.")
        return max_credits
    if config.agent_mode():
        raise CliExit(EXIT_BUDGET, "budget_required", f"{what} spends credits; non-interactive callers must pass --max-credits.", data={"suggested": default}, next_actions=[f"Re-run with --max-credits {default}"])
    import typer

    if not typer.confirm(f"{what}: cap this run at {default} credits (${default / 100:.2f})?", default=True, err=True):
        raise CliExit(EXIT_BUDGET, "budget_declined", "Run cancelled; pass --max-credits to choose a cap.")
    return default
