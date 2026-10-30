"""Output contract (FR-CLI-03) and exit codes (FR-CLI-06)."""

from __future__ import annotations

import json
import sys
from typing import Any

from designaround_core import SCHEMA_VERSION

EXIT_OK = 0
EXIT_NEEDS_HUMAN = 2
EXIT_BUDGET = 3
EXIT_AUTH = 4
EXIT_INVALID = 5
EXIT_ERROR = 6

STATE = {"json": False}


class CliExit(Exception):
    def __init__(self, code: int, kind: str, message: str, *, data: Any = None, next_actions: list[str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.kind = kind
        self.message = message
        self.data = data
        self.next_actions = next_actions or []


def envelope(kind: str, data: Any, *, next_actions: list[str] | None = None, cost: dict | None = None, ok: bool = True) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "ok": ok,
        "data": data,
        "next_actions": next_actions or [],
        "cost": cost or {"estimated": 0, "charged": 0},
    }


def emit(kind: str, data: Any, *, markdown: str | None = None, next_actions: list[str] | None = None, cost: dict | None = None) -> None:
    if STATE["json"]:
        sys.stdout.write(json.dumps(envelope(kind, data, next_actions=next_actions, cost=cost), ensure_ascii=False, default=str) + "\n")
        sys.stdout.flush()
        return
    from rich.console import Console
    from rich.markdown import Markdown

    console = Console()
    if markdown:
        console.print(Markdown(markdown))
    else:
        console.print_json(json.dumps(data, ensure_ascii=False, default=str))
    if cost and (cost.get("charged") or cost.get("estimated")):
        console.print(f"[dim]credits: estimated {cost.get('estimated', 0)}, charged {cost.get('charged', 0)}[/dim]")
    for action in next_actions or []:
        console.print(f"[cyan]next:[/cyan] {action}")


def fail(exc: CliExit) -> int:
    if STATE["json"]:
        body = envelope(exc.kind, {"message": exc.message, **({"details": exc.data} if exc.data is not None else {})}, next_actions=exc.next_actions, ok=False)
        sys.stdout.write(json.dumps(body, ensure_ascii=False, default=str) + "\n")
        sys.stdout.flush()
    else:
        from rich.console import Console

        console = Console(stderr=True)
        console.print(f"[red]{exc.kind}[/red]: {exc.message}")
        if exc.data:
            console.print_json(json.dumps(exc.data, ensure_ascii=False, default=str)[:4000])
        for action in exc.next_actions:
            console.print(f"[cyan]next:[/cyan] {action}")
    return exc.code


def md_table(rows: list[dict], columns: list[str]) -> str:
    if not rows:
        return "_(none)_"
    head = "| " + " | ".join(columns) + " |\n|" + "---|" * len(columns)
    body = "\n".join("| " + " | ".join(str(r.get(c, "")) for c in columns) + " |" for r in rows)
    return head + "\n" + body
