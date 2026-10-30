"""MCP server on stdio (FR-API-02): screen, design_around, run_status, viewpack, compare.

``designaround mcp`` speaks newline-delimited JSON-RPC 2.0 on stdin/stdout and is
dual-era:

* modern (2026-07-28): no handshake; every request carries
  ``_meta["io.modelcontextprotocol/protocolVersion"]``; ``server/discover``
  advertises versions and capabilities; results carry ``resultType``; list
  results carry ``ttlMs`` / ``cacheScope``.
* legacy (2025-11-25 and earlier): ``initialize`` / ``notifications/initialized``
  handshake and ``ping``.

Every tool runs the CLI itself (``python -m designaround_cli --json``) with
``DESIGNAROUND_AGENT=1``, so budgets (``max_credits`` is required for paid tools),
plan checks and the evidence log are exactly those of the CLI. Long agent runs
start detached and are followed with ``run_status``. Logs go to stderr only.
"""

from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from . import __version__

MODERN = ["2026-07-28"]
LEGACY = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
SERVER_INFO = {"name": "designaround", "title": "DesignAround", "version": __version__}
INSTRUCTIONS = (
    "DesignAround checks a physical product's design against registered designs (design patents) and designs it around them. "
    "Work on a case: case_list, then screen (registry search, paid) -> decision_card (where the case stands and the next step) -> design_around (agent run in the background, paid; follow it with run_status) -> compare (original vs candidate) -> viewpack (patent drawings). "
    "Paid tools need max_credits: state a budget the user agreed to. Results are risk assessments, not legal opinions; a registered practitioner confirms before filing."
)
VERSION_RE = r"^v\d+$"

OFFICES = {"type": "string", "enum": ["US", "CN", "EU"]}
TOOLS = [
    {
        "name": "case_list",
        "title": "List cases",
        "description": "Free. The cases on this machine (one product each) with their status.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "decision_card",
        "title": "Decision card",
        "description": "Free. Where a case stands: verdict (proceed / redesign / abandon / pending), reasons from the evaluations, next steps with credits and time, and the nearest deadline.",
        "inputSchema": {"type": "object", "properties": {"case_id": {"type": "string"}, "lang": {"type": "string", "enum": ["zh", "en"], "default": "en"}}, "required": ["case_id"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "screen",
        "title": "Screen registered designs",
        "description": "Paid. Search registered designs (US, CN, EU) for the case's product on the DesignAround server, add the candidates as prior art and rank them by silhouette locally. Quick screen 50 credits (5 free a month), deep (image + text) 350. Takes 1-3 minutes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "case_id": {"type": "string"},
                "max_credits": {"type": "integer", "minimum": 1, "description": "Budget the user agreed to; must cover the price."},
                "deep": {"type": "boolean", "default": False},
                "jurisdictions": {"type": "array", "items": OFFICES, "description": "Default: the case's offices."},
                "query": {"type": "string", "description": "Text query (default: the case's article)."},
            },
            "required": ["case_id", "max_credits"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    },
    {
        "name": "design_around",
        "title": "Design-around run",
        "description": "Paid. Starts the agent team in the background: a designer proposes candidates, originality and manufacturability gates check them, an isolated observer scores them against the closest prior art, and it repeats. Returns a run_id at once; follow it with run_status (runs take 10-60 minutes). depth: quick (1 round) | standard (3 rounds, default) | deep (6 rounds); standard and deep refund 50% of the spend automatically when no candidate qualifies.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "case_id": {"type": "string"},
                "max_credits": {"type": "integer", "minimum": 1, "description": "Hard cap for the whole run (standard default 5900)."},
                "depth": {"type": "string", "enum": ["quick", "standard", "deep"], "default": "standard"},
                "top": {"type": "integer", "minimum": 1, "maximum": 8, "description": "How many prior-art rights to design around (default 3)."},
            },
            "required": ["case_id", "max_credits"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    },
    {
        "name": "run_status",
        "title": "Run status",
        "description": "Free. State of a background run started by design_around: status, recent progress events and, when done, the outcome (qualified candidates, where it got stuck, result guarantee refund).",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "viewpack",
        "title": "Patent drawings",
        "description": "Free, local. Patent line drawings of a version for US (with surface shading), CN or EU, with the office's rule checks. Free plan: watermarked.",
        "inputSchema": {"type": "object", "properties": {"case_id": {"type": "string"}, "version": {"type": "string", "pattern": VERSION_RE}, "office": OFFICES}, "required": ["case_id", "version", "office"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "compare",
        "title": "Compare two versions",
        "description": "Free, local. Same-scale overlay of two versions in every view, with changed parameters, size and risk; returns the comparison sheet as an image.",
        "inputSchema": {"type": "object", "properties": {"case_id": {"type": "string"}, "a": {"type": "string", "pattern": VERSION_RE}, "b": {"type": "string", "pattern": VERSION_RE}}, "required": ["case_id", "a", "b"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
]


def _argv(name: str, a: dict) -> list[str]:
    if name == "case_list":
        return ["case", "list"]
    if name == "decision_card":
        return ["case", "card", a["case_id"], "--lang", "zh" if a.get("lang") == "zh" else "en"]
    if name == "screen":
        argv = ["screen", a["case_id"], "--max-credits", str(int(a["max_credits"]))]
        if a.get("deep"):
            argv.append("--deep")
        for office in a.get("jurisdictions") or []:
            argv += ["-j", office]
        if a.get("query"):
            argv += ["--query", a["query"]]
        return argv
    if name == "design_around":
        argv = ["--detach", "around", a["case_id"], "--max-credits", str(int(a["max_credits"])), "--depth", a.get("depth") or "standard"]
        if a.get("top"):
            argv += ["--top", str(int(a["top"]))]
        return argv
    if name == "run_status":
        return ["run", "status", a["run_id"]]
    if name == "viewpack":
        return ["viewpack", a["case_id"], a["version"], "--office", a["office"]]
    if name == "compare":
        return ["cad", "compare", a["case_id"], a["a"], a["b"]]
    raise KeyError(name)


def _validate(tool: dict, args: dict) -> str | None:
    schema = tool["inputSchema"]
    if not isinstance(args, dict):
        return "arguments must be an object"
    for key in schema.get("required", []):
        if key not in args or args[key] in (None, ""):
            return f"missing required argument: {key}"
    props = schema.get("properties", {})
    for key, value in args.items():
        if key not in props:
            return f"unknown argument: {key}"
        spec = props[key]
        kind = spec.get("type")
        if kind == "string" and not isinstance(value, str):
            return f"{key} must be a string"
        if kind == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
            return f"{key} must be an integer"
        if kind == "boolean" and not isinstance(value, bool):
            return f"{key} must be a boolean"
        if kind == "array" and not isinstance(value, list):
            return f"{key} must be an array"
        if "enum" in spec and value not in spec["enum"]:
            return f"{key} must be one of {', '.join(spec['enum'])}"
        if kind == "array" and "enum" in spec.get("items", {}) and any(v not in spec["items"]["enum"] for v in value):
            return f"{key} items must be one of {', '.join(spec['items']['enum'])}"
        if kind == "integer" and ("minimum" in spec and value < spec["minimum"] or "maximum" in spec and value > spec["maximum"]):
            return f"{key} is out of range"
        if "pattern" in spec:
            import re

            if not re.fullmatch(spec["pattern"], value):
                return f"{key} does not match {spec['pattern']}"
    return None


def _image_block(path: str | None) -> dict | None:
    if not path or not os.path.isfile(path):
        return None
    try:
        from PIL import Image

        with Image.open(path) as im:
            im.thumbnail((1600, 1600))
            buf = io.BytesIO()
            im.convert("RGB").save(buf, format="JPEG", quality=85)
        if buf.tell() > 1_500_000:
            return None
        return {"type": "image", "data": base64.b64encode(buf.getvalue()).decode("ascii"), "mimeType": "image/jpeg"}
    except Exception:  # noqa: BLE001 - the text result is enough
        return None


def run_tool(name: str, args: dict) -> dict:
    """Run one tool through the CLI; returns an MCP CallToolResult (without resultType)."""

    env = {**os.environ, "DESIGNAROUND_AGENT": "1", "PYTHONUNBUFFERED": "1"}
    proc = subprocess.run([sys.executable, "-m", "designaround_cli", "--json", "--quiet", *_argv(name, args)], capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=3600, check=False)
    line = next((ln for ln in reversed(proc.stdout.strip().splitlines()) if ln.strip().startswith("{")), None)
    try:
        envelope = json.loads(line) if line else None
    except ValueError:
        envelope = None
    if envelope is None:
        return {"content": [{"type": "text", "text": f"designaround exited with code {proc.returncode}: {(proc.stderr or proc.stdout)[-1500:]}"}], "isError": True}
    ok = bool(envelope.get("ok", proc.returncode == 0))
    data = envelope.get("data")
    text = envelope.get("markdown") or ""
    summary = {k: envelope.get(k) for k in ("kind", "ok", "cost", "next_actions") if envelope.get(k) is not None}
    body = (text + "\n\n" if text else "") + json.dumps({**summary, "data": data}, ensure_ascii=False, default=str)[:60_000]
    content = [{"type": "text", "text": body}]
    if ok and name == "compare" and isinstance(data, dict):
        block = _image_block(data.get("sheet") or data.get("path"))
        if block:
            content.append(block)
    result = {"content": content, "isError": not ok}
    if isinstance(data, dict):
        result["structuredContent"] = {"kind": envelope.get("kind"), "ok": ok, "data": data, "cost": envelope.get("cost"), "next_actions": envelope.get("next_actions") or []}
    elif data is not None:
        result["structuredContent"] = {"kind": envelope.get("kind"), "ok": ok, "data": data}
    return result


class Server:
    def __init__(self, out=None) -> None:
        self.out = out or sys.stdout
        self.lock = threading.Lock()
        self.legacy_version: str | None = None
        self.pool = ThreadPoolExecutor(max_workers=4)

    def send(self, message: dict) -> None:
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self.lock:
            self.out.write(line + "\n")
            self.out.flush()

    def error(self, rid, code: int, message: str, data=None) -> None:
        err = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        self.send({"jsonrpc": "2.0", "id": rid, "error": err})

    def result(self, rid, result: dict, *, modern: bool) -> None:
        if modern:
            result = {**result, "resultType": "complete", "_meta": {**(result.get("_meta") or {}), "io.modelcontextprotocol/serverInfo": SERVER_INFO}}
        self.send({"jsonrpc": "2.0", "id": rid, "result": result})

    def handle(self, msg: dict) -> None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
            if isinstance(msg, dict) and "id" in msg and "method" not in msg:
                return  # a response to something we never sent
            self.error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
            return
        method, rid, params = msg["method"], msg.get("id"), msg.get("params") or {}
        if rid is None:  # notifications: initialized, cancelled, ... nothing to answer
            return
        meta = params.get("_meta") or {}
        requested = meta.get("io.modelcontextprotocol/protocolVersion")
        modern = requested is not None
        if modern and requested not in MODERN:
            if requested in LEGACY and self.legacy_version:
                modern = False  # a legacy client that also sends the field
            else:
                self.error(rid, -32022, f"Unsupported protocol version {requested}", {"supported": MODERN, "requested": requested})
                return
        if method == "initialize":
            asked = params.get("protocolVersion")
            self.legacy_version = asked if asked in LEGACY else LEGACY[0]
            self.result(rid, {"protocolVersion": self.legacy_version, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS}, modern=False)
            return
        if method == "ping":
            self.result(rid, {}, modern=modern)
            return
        if method == "server/discover":
            self.result(rid, {"supportedVersions": MODERN, "capabilities": {"tools": {"listChanged": False}}, "instructions": INSTRUCTIONS, "ttlMs": 3_600_000, "cacheScope": "public"}, modern=True)
            return
        if method == "tools/list":
            self.result(rid, {"tools": TOOLS, "ttlMs": 3_600_000, "cacheScope": "public"}, modern=modern)
            return
        if method == "tools/call":
            name = params.get("name")
            tool = next((t for t in TOOLS if t["name"] == name), None)
            if tool is None:
                self.error(rid, -32602, f"Unknown tool: {name}")
                return
            args = params.get("arguments") or {}
            problem = _validate(tool, args)
            if problem:
                self.result(rid, {"content": [{"type": "text", "text": f"Invalid arguments: {problem}"}], "isError": True}, modern=modern)
                return
            self.pool.submit(self._call, rid, name, args, modern)
            return
        if method in ("resources/list", "prompts/list"):
            self.result(rid, {method.split("/")[0]: [], "ttlMs": 3_600_000, "cacheScope": "public"}, modern=modern)
            return
        self.error(rid, -32601, f"Method not found: {method}")

    def _call(self, rid, name: str, args: dict, modern: bool) -> None:
        try:
            result = run_tool(name, args)
        except subprocess.TimeoutExpired:
            result = {"content": [{"type": "text", "text": "The tool took longer than an hour and was stopped."}], "isError": True}
        except Exception as exc:  # noqa: BLE001 - report, never crash the server
            result = {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}], "isError": True}
        self.result(rid, result, modern=modern)

    def serve(self, stdin=None) -> None:
        stream = stdin or sys.stdin
        for raw in stream:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                self.error(None, -32700, "Parse error")
                continue
            if isinstance(msg, list):  # JSON-RPC batches are not part of MCP
                self.error(None, -32600, "Batches are not supported")
                continue
            try:
                self.handle(msg)
            except Exception as exc:  # noqa: BLE001
                sys.stderr.write(f"designaround mcp: {type(exc).__name__}: {exc}\n")
                if isinstance(msg, dict) and msg.get("id") is not None:
                    self.error(msg["id"], -32603, "Internal error")
        self.pool.shutdown(wait=True)


def host_configs() -> dict:
    """Ready-to-paste snippets for common agent hosts."""

    exe = "designaround"
    return {
        "claude_code": f"claude mcp add designaround -- {exe} mcp",
        "codex": f"[mcp_servers.designaround]\ncommand = \"{exe}\"\nargs = [\"mcp\"]",
        "cursor": json.dumps({"mcpServers": {"designaround": {"command": exe, "args": ["mcp"]}}}, indent=2),
        "claude_desktop": json.dumps({"mcpServers": {"designaround": {"command": exe, "args": ["mcp"]}}}, indent=2),
        "vscode": json.dumps({"servers": {"designaround": {"type": "stdio", "command": exe, "args": ["mcp"]}}}, indent=2),
    }


def main() -> None:
    Server().serve()
