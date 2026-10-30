"""`designaround` / `da` command line.

Local commands (case, cad, views, DFM, filing packages, evidence) run on this
machine with the bundled engine and cost nothing. Server commands (screening,
hosted observer, platform models, billing, anchors, gates) need an API key.

Output contract: with --json (or when stdout is not a terminal) every command
prints one JSON envelope {schema_version, kind, ok, data, next_actions, cost}.
Exit codes: 0 ok, 2 needs human input, 3 budget/plan, 4 auth, 5 invalid input,
6 error.
"""

from __future__ import annotations

import base64
import functools
import json
import os
import re
import sys
import time
from datetime import date
from enum import Enum
from pathlib import Path

import typer

from designaround_core import catalog
from designaround_engine import api
from designaround_engine.errors import EngineError
from designaround_engine.workspace import open_case

from . import __version__, bundles, config, runner
from . import guarantee as guarantee_mod
from .client import Server
from .output import (
    EXIT_AUTH,
    EXIT_BUDGET,
    EXIT_ERROR,
    EXIT_INVALID,
    EXIT_NEEDS_HUMAN,
    STATE,
    CliExit,
    emit,
    fail,
    md_table,
)

app = typer.Typer(name="designaround", help="DesignAround: design-patent screening, design-around, patent views and filing packages.", no_args_is_help=True, add_completion=False, pretty_exceptions_enable=False, rich_markup_mode=None)
auth_app = typer.Typer(help="API key for the DesignAround server.", no_args_is_help=True)
config_app = typer.Typer(help="Server URL, model mode (platform | byok | host) and BYOK settings.", no_args_is_help=True)
case_app = typer.Typer(help="Cases: one product, its versions, prior art and evidence.", no_args_is_help=True)
cad_app = typer.Typer(help="CAD versions: run programs, tweak parameters, claim scope, compare.", no_args_is_help=True)
prior_app = typer.Typer(help="Prior art on a case.", no_args_is_help=True)
photos_app = typer.Typer(help="Labelled product photos.", no_args_is_help=True)
evidence_app = typer.Typer(help="Hash-chained evidence: export, anchor, verify.", no_args_is_help=True)
gate_app = typer.Typer(help="Practitioner review gates.", no_args_is_help=True)
baseline_app = typer.Typer(help="Submission baselines: freeze what was filed, verify it, compare later versions.", no_args_is_help=True)
batch_app = typer.Typer(help="Batch work from a CSV: many products at once, resumable.", no_args_is_help=True)
run_app = typer.Typer(help="Agent runs started with --detach: list, watch, abort; result guarantee.", no_args_is_help=True)
share_app = typer.Typer(help="Read-only share links for a factory or a practitioner: expiry, watermark, access log.", no_args_is_help=True)
filing_app = typer.Typer(help="Record filings and official notices; deadlines follow from them (see `deadlines`).", no_args_is_help=True)
monitor_app = typer.Typer(help="Watch cases for newly published designs: periodic re-screens, new hits pushed to you.", no_args_is_help=True)
defense_app = typer.Typer(help="Complaints and lawsuits: intake and channel, three-way comparison, independent-design evidence, appeal draft.", no_args_is_help=True)
billing_app = typer.Typer(help="Plans and credit packs (a person pays in the browser), billing portal.", no_args_is_help=True)
skill_app = typer.Typer(help="Install the DesignAround Skill into agent hosts.", no_args_is_help=True)
agent_app = typer.Typer(help="Embedded agent team runtime (isolated Pi Agent).", no_args_is_help=True)
design_app = typer.Typer(help="Design a new product from requirements: 2D concepts, independent reviews, the chosen concept built in 3D.", no_args_is_help=True)

for sub, name in ((auth_app, "auth"), (config_app, "config"), (case_app, "case"), (cad_app, "cad"), (prior_app, "prior-art"), (photos_app, "photos"), (evidence_app, "evidence"), (gate_app, "gate"), (billing_app, "billing"), (skill_app, "skill"), (agent_app, "agent"), (design_app, "design"), (baseline_app, "baseline"), (batch_app, "batch"), (run_app, "run"), (share_app, "share"), (filing_app, "filing"), (monitor_app, "monitor"), (defense_app, "defense")):
    app.add_typer(sub, name=name)


class Office(str, Enum):
    US = "US"
    CN = "CN"
    EU = "EU"


class Lang(str, Enum):
    zh = "zh"
    en = "en"


class Mode(str, Enum):
    platform = "platform"
    byok = "byok"
    host = "host"


class Process(str, Enum):
    injection = "injection"
    print3d = "print3d"


# ------------------------------------------------------------------ helpers


def guarded(fn):
    """Map every failure to the exit-code contract and the JSON error envelope."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (typer.Exit, typer.Abort, typer.TyperException):
            raise
        except CliExit as exc:
            raise typer.Exit(fail(exc)) from None
        except EngineError as exc:
            raise typer.Exit(fail(CliExit(EXIT_INVALID, exc.code, exc.message, data=exc.details or None))) from None
        except KeyboardInterrupt:
            raise typer.Exit(130) from None
        except Exception as exc:  # noqa: BLE001 - last-resort contract
            raise typer.Exit(fail(CliExit(EXIT_ERROR, "internal_error", f"{type(exc).__name__}: {exc}"))) from None

    return wrapper


def command(target: typer.Typer, name: str | None = None, **kw):
    def deco(fn):
        target.command(name, **kw)(guarded(fn))
        return fn

    return deco


def parse_kv(items: list[str] | None, what: str = "value") -> dict:
    """k=v pairs; values are parsed as JSON when possible (numbers, booleans)."""

    out: dict = {}
    for item in items or []:
        if "=" not in item:
            raise CliExit(EXIT_INVALID, "bad_pair", f"Expected name=value for {what}, got '{item}'")
        key, raw = item.split("=", 1)
        try:
            out[key.strip()] = json.loads(raw)
        except ValueError:
            out[key.strip()] = raw
    return out


def parse_date(text: str | None, flag: str) -> str | None:
    if text is None:
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise CliExit(EXIT_INVALID, "bad_date", f"{flag} must be YYYY-MM-DD, got '{text}'") from exc


def actor() -> str:
    return "agent" if config.agent_mode() else "user"


def fixed_price(action: str, max_credits: int | None) -> int:
    """Server actions with a fixed price: agents must state a budget that covers it."""

    price = catalog.ACTION_CREDITS[action]
    if max_credits is not None and max_credits < price:
        raise CliExit(EXIT_BUDGET, "budget_too_low", f"{action} costs {price} credits; --max-credits is {max_credits}.", data={"price": price})
    if max_credits is None and config.agent_mode() and price > 0:
        raise CliExit(EXIT_BUDGET, "budget_required", f"{action} costs up to {price} credits; non-interactive callers must pass --max-credits.", data={"price": price}, next_actions=[f"Re-run with --max-credits {price}"])
    return price


def entitlements() -> dict:
    """Plan entitlements: fresh from the server, cached copy when offline, free plan without a key."""

    free = {"plan": "free", "entitlements": vars(catalog.PLANS["free"]), "source": "default"}
    if not config.api_key():
        return {**free, "source": "no_key"}
    try:
        me = Server().get("/v1/me", timeout=8)["data"]
        config.cache_entitlements(me)
        return {**me, "source": "server"}
    except CliExit as exc:
        if exc.code == EXIT_AUTH:
            raise
        cached = config.cached_entitlements()
        return {**cached, "source": "cache"} if cached else {**free, "source": "offline"}


def findings_md(findings: list[dict]) -> str:
    rows = [{"rule": f.get("rule_id"), "ok": "✓" if f.get("pass", f.get("severity") in ("ok", "info")) else "✗", "severity": f.get("severity"), "message": f.get("message")} for f in findings]
    return md_table(rows, ["rule", "ok", "severity", "message"])


def save_judgment(case_id: str, vid: str, prior_id: str, result: dict, source: str) -> dict:
    return api.save_judgment(case_id, vid, prior_id, result, source)


def agent_outcome(code: int, outcome: dict, *, what: str) -> dict:
    """Turn a runner outcome into data, or raise with the runner's exit code."""

    if outcome.get("type") == "error":
        kind = outcome.get("code") or "agent_error"
        hints = {
            "budget_exceeded": ["Raise --max-credits, or narrow the run (--rounds 1 --per-round 2)."],
            "insufficient_credits": ["designaround credits", "designaround billing checkout --credits 5k"],
            "auth": ["designaround auth status"],
            "config": ["designaround config show"],
        }
        raise CliExit(code or EXIT_ERROR, kind, outcome.get("message") or f"{what} failed", data={k: v for k, v in outcome.items() if k not in ("type", "message")}, next_actions=hints.get(kind, []))
    if code == EXIT_NEEDS_HUMAN:
        raise CliExit(EXIT_NEEDS_HUMAN, outcome.get("status") or "needs_input", outcome.get("reason") or f"{what} needs human input.", data=outcome)
    if code:
        raise CliExit(code, "agent_error", f"{what} exited with code {code}", data=outcome)
    return outcome


def server_spend(run_id: str | None) -> int | None:
    if not run_id or config.model_mode() != "platform":
        return None
    try:
        data = Server().get(f"/v1/runs/{run_id}/spend", timeout=10).get("data")
    except CliExit:
        return None
    if data is None:
        return 0
    return int(data["spent"]) if isinstance(data, dict) and isinstance(data.get("spent"), (int, float)) else None


HOST_LOOP = [
    "designaround --json prior-art rank <case>  # CPU ranking; pick the top 3",
    "designaround --json observe <case> <original> <prior> --max-credits 120  # hosted isolated observer, per prior art",
    "designaround --json evaluate <case> <original>",
    "designaround --json cad params <case> <original>  # features, locked parameters",
    "designaround --json cad tweak <case> <original> --set name=value --rationale '...'  # or cad run <case> program.py --parent <original>",
    "designaround --json originality <case> <vN>; designaround --json dfm <case> <vN>",
    "designaround --json observe <case> <vN> <prior> --max-credits 120; designaround --json evaluate <case> <vN>",
    "Repeat until 2 candidates are qualified, then show them to the user to choose.",
]


# --------------------------------------------------------------- top level


def _version_cb(value: bool) -> None:
    if value:
        typer.echo(f"designaround {__version__}")
        raise typer.Exit(0)


@app.callback()
def main_callback(
    json_out: bool = typer.Option(False, "--json", help="Print one JSON envelope (default when stdout is not a terminal)."),
    human: bool = typer.Option(False, "--human", help="Force human-readable output."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="No progress lines on stderr."),
    detach: bool = typer.Option(False, "--detach", help="Agent runs (around, design build/concepts, model --agent): start in the background and print the run id."),
    version: bool = typer.Option(False, "--version", callback=_version_cb, is_eager=True, help="Show the version."),
) -> None:
    auto = os.environ.get("DESIGNAROUND_JSON") == "1" or not sys.stdout.isatty()
    STATE["json"] = json_out or (auto and not human)
    STATE["quiet"] = quiet
    STATE["detach"] = detach


@command(app, "onboard")
def onboard(email: str | None = typer.Option(None, help="Optional contact email."), agent: str | None = typer.Option(None, help="Name of the calling agent host."), print_key: bool = typer.Option(False, "--print-key", help="Also print the raw key (it is stored in the keychain either way).")) -> None:
    """Get a trial key (free plan, trial credits) and store it in the keychain."""

    body = Server(api_key="").post("/v1/onboard", {"email": email, "agent": agent or os.environ.get("DESIGNAROUND_AGENT_NAME")}, auth=False)
    data = body["data"]
    where = config.set_secret("api_key", data["api_key"])
    shown = {**data, "api_key": data["api_key"] if print_key else data["api_key"][:16] + "…", "stored_in": where}
    emit("onboard", shown, markdown=f"Trial key stored in **{where}**. Plan **{data['plan']}**, {data['credits']} credits.", next_actions=["designaround case create \"My product\" --article \"storage box\"", "designaround templates"])


@command(app, "templates")
def templates_cmd() -> None:
    """Parametric CAD templates shipped with the engine."""

    rows = api.templates()
    emit("templates", rows, markdown=md_table([{"id": r.get("id"), "article": r.get("article"), "params": len(r.get("params") or {})} for r in rows], ["id", "article", "params"]))


@command(app, "credits")
def credits_cmd() -> None:
    """Credit balance and recent ledger entries."""

    data = Server().get("/v1/credits")["data"]
    history = data.get("history") or []
    emit("credits", data, markdown=f"Balance: **{data['balance']}** credits (${data['balance'] / 100:.2f})\n\n" + md_table([{**h, "at": time.strftime("%Y-%m-%d %H:%M", time.gmtime(h["at"])) if isinstance(h.get("at"), (int, float)) else h.get("at")} for h in history[:15]], ["at", "kind", "delta", "ref"]))


@command(app, "quote")
def quote_cmd(
    office: list[Office] = typer.Option(..., "--office", "-o", help="US and/or CN (repeat)."),
    managed: bool = typer.Option(False, help="Managed package (platform fee + practitioner fee paid directly to the practitioner)."),
    design_around: bool = typer.Option(True, "--design-around/--file-only"),
    entity: str = typer.Option("small", help="US entity status: large | small | micro."),
    fee_reduction: bool = typer.Option(False, help="CN fee reduction (85%) applies."),
    similar: int = typer.Option(0, help="CN similar designs beyond the first."),
) -> None:
    """Official fees + platform + practitioner estimate (no network)."""

    if entity not in ("large", "small", "micro"):
        raise CliExit(EXIT_INVALID, "bad_entity", "entity must be large, small or micro")
    data = api.quote([o.value for o in office], managed=managed, design_around=design_around, entity=entity, fee_reduction=fee_reduction, similar_designs=similar)
    emit("quote", data)


# ------------------------------------------------------------------- auth


@command(auth_app, "login")
def auth_login(key: str | None = typer.Option(None, "--key", help="API key (da_...). Omit to read it from stdin or a hidden prompt.")) -> None:
    """Store an API key in the OS keychain after checking it with the server."""

    if key is None or key == "-":
        key = sys.stdin.readline().strip() if not sys.stdin.isatty() else typer.prompt("API key", hide_input=True, err=True)
    key = (key or "").strip()
    if not key.startswith("da_"):
        raise CliExit(EXIT_INVALID, "bad_key", "API keys start with da_.")
    me = Server(api_key=key).get("/v1/me")["data"]
    where = config.set_secret("api_key", key)
    config.cache_entitlements(me)
    emit("auth", {"stored_in": where, "server": config.server_url(), "plan": me.get("plan"), "balance": me.get("balance")}, markdown=f"Logged in to {config.server_url()} · plan **{me.get('plan')}** · {me.get('balance')} credits · key in {where}.")


@command(auth_app, "status")
def auth_status() -> None:
    """Which key is used and what it is entitled to."""

    source = "env" if os.environ.get("DESIGNAROUND_API_KEY") else config.secret_source("api_key")
    data: dict = {"server": config.server_url(), "key_source": source, "model_mode": config.model_mode(), "support_email": config.SUPPORT_EMAIL}
    if source:
        key = config.api_key() or ""
        data["key_prefix"] = key[:12]
        data["me"] = entitlements()
    elif config.keychain_key_present():
        # A key this install cannot read yet (another install keeps it in the Keychain): use it
        # rather than starting a second trial account.
        data["keychain_key"] = True
    adopt = ["designaround auth from-keychain"] if data.get("keychain_key") else []
    emit("auth_status", data, next_actions=[] if source else [*adopt, "designaround onboard", "designaround auth login --key da_..."])


@command(auth_app, "from-keychain")
def auth_from_keychain() -> None:
    """Use the key another DesignAround install (e.g. the CLI) keeps in the OS keychain; macOS may ask once to allow access."""

    key = config.keychain_secret("api_key")
    if not key:
        raise CliExit(EXIT_AUTH, "keychain_unavailable", "No readable DesignAround key in the keychain (none stored, or access was not allowed).", next_actions=["designaround auth login --key da_...", "designaround onboard"])
    me = Server(api_key=key).get("/v1/me")["data"]
    where = config.set_secret("api_key", key)
    config.cache_entitlements(me)
    emit("auth", {"stored_in": where, "server": config.server_url(), "plan": me.get("plan"), "balance": me.get("balance"), "from": "keychain"}, markdown=f"Using the key from the keychain · plan **{me.get('plan')}** · {me.get('balance')} credits · key in {where}.")


@command(auth_app, "logout")
def auth_logout() -> None:
    """Remove the stored API key."""

    config.set_secret("api_key", None)
    cache = config.home() / "entitlements.json"
    if cache.exists():
        cache.unlink()
    emit("auth", {"logged_out": True})


# ----------------------------------------------------------------- config


@command(config_app, "notify")
def config_notify(
    webhook: str | None = typer.Option(None, help="Chat-bot webhook (Feishu/Lark, WeCom, DingTalk, Slack, or any https URL). Pass '' to remove."),
    desktop: bool | None = typer.Option(None, "--desktop/--no-desktop", help="Desktop notification when a long run ends."),
    test: bool = typer.Option(False, "--test", help="Send a test message now."),
) -> None:
    """Where to tell you that a long run (design-around, 3D build, batch) finished or needs you."""

    from . import notify

    data = config.load()
    current = dict(data.get("notify") or {})
    if webhook is not None:
        if webhook and not webhook.startswith("https://"):
            raise CliExit(EXIT_INVALID, "bad_webhook", "The webhook must be an https URL.")
        current["webhook"] = webhook or None
    if desktop is not None:
        current["desktop"] = desktop
    data["notify"] = current
    config.save(data)
    sent = notify.notify("DesignAround 通知测试", "通知已配置：长任务完成或需要你确认时会发到这里。", force_desktop=bool(current.get("desktop"))) if test else None
    shown = {"webhook": (current.get("webhook") or "")[:40] + ("…" if current.get("webhook") and len(current["webhook"]) > 40 else ""), "desktop": bool(current.get("desktop")), "test": sent}
    emit("notify_config", shown, markdown=f"Webhook: {shown['webhook'] or '—'} · desktop: {shown['desktop']}" + (f" · test: {sent}" if sent is not None else ""))


@command(app, "notify", hidden=True)
def notify_cmd(title: str = typer.Argument(...), text: str = typer.Argument(""), desktop: bool = typer.Option(False, "--desktop")) -> None:
    """Send a notification through the configured channels (used by the workbench)."""

    from . import notify

    emit("notify", notify.notify(title[:80], text[:180], force_desktop=desktop))


@command(config_app, "show")
def config_show() -> None:
    data = config.load()
    byok = data.get("byok") or {}
    emit("config", {"server": config.server_url(), "model_mode": config.model_mode(), "byok": {**byok, "key_stored": bool(config.get_secret("byok_key"))}, "notify": data.get("notify") or {}, "home": str(config.home()), "agents_dir": str(bundles.agents_dir())})


@command(config_app, "server")
def config_server(url: str = typer.Argument(..., help="Base URL, e.g. https://designaround.elidot.com")) -> None:
    if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        raise CliExit(EXIT_INVALID, "insecure_url", "Use an https:// URL (plain http only for localhost).")
    data = config.load()
    data["server"] = url.rstrip("/")
    config.save(data)
    emit("config", {"server": data["server"]})


@command(config_app, "mode")
def config_mode(mode: Mode = typer.Argument(..., help="platform: our metered models · byok: your own key · host: your agent host drives the CLI")) -> None:
    data = config.load()
    data["model_mode"] = mode.value
    config.save(data)
    notes = {
        "platform": "LLM calls go through the DesignAround server and are metered in credits.",
        "byok": "LLM calls go straight to your provider with your key; no LLM credits.",
        "host": "Your agent host (Claude Code, Codex, Cursor, …) is the designer; observer scoring uses the hosted observer (120 credits per call).",
    }
    emit("config", {"model_mode": mode.value, "note": notes[mode.value]}, markdown=notes[mode.value])


@command(config_app, "byok")
def config_byok(
    provider: str = typer.Option(..., help="Pi provider id (anthropic, openai, google, openrouter, …) or a label when --base-url is set."),
    model: str = typer.Option(..., help="Designer / CAD Agent model id (vision-capable)."),
    observer_model: str | None = typer.Option(None, help="Observer model id (defaults to --model)."),
    base_url: str | None = typer.Option(None, help="OpenAI-compatible endpoint for custom providers."),
    key_stdin: bool = typer.Option(False, "--key-stdin", help="Read the provider key from stdin (otherwise a hidden prompt; never pass keys as arguments)."),
    no_key: bool = typer.Option(False, "--no-key", help="Do not store a key (use DA_BYOK_KEY at run time)."),
) -> None:
    """Bring your own model key. The key goes to the OS keychain and is passed to the agent runtime in memory only."""

    data = config.load()
    data["byok"] = {k: v for k, v in {"provider": provider, "model": model, "observer_model": observer_model, "base_url": base_url}.items() if v}
    data["model_mode"] = "byok"
    config.save(data)
    stored = None
    if not no_key:
        key = sys.stdin.readline().strip() if key_stdin else typer.prompt(f"{provider} API key", hide_input=True, err=True)
        if not key:
            raise CliExit(EXIT_INVALID, "empty_key", "No key provided.")
        stored = config.set_secret("byok_key", key)
    emit("config", {"model_mode": "byok", "byok": data["byok"], "key_stored_in": stored})


# ------------------------------------------------------------------ cases


@command(case_app, "create")
def case_create(
    name: str = typer.Argument(...),
    article: str | None = typer.Option(None, help="Article of manufacture (English), e.g. 'storage box'."),
    article_zh: str | None = typer.Option(None, help="产品名称（中文）"),
    category: str | None = typer.Option(None),
    jurisdiction: list[Office] = typer.Option([Office.US], "--jurisdiction", "-j", help="Target offices (repeat)."),
    platform: list[str] = typer.Option([], "--platform", help="Sales platforms (amazon, temu, …)."),
) -> None:
    """Create a case."""

    data = api.case_create(name, article=article, article_zh=article_zh, category=category, jurisdictions=[j.value for j in jurisdiction], platforms=platform)
    emit("case", data, markdown=f"Case **{data['id']}** created.", next_actions=[f"designaround case set {data['id']} --brief-json '{{\"use\": \"...\", \"size\": \"240x160x90mm\"}}'", f"designaround design concepts {data['id']} --count 4 --max-credits 600", f"designaround model {data['id']} --cad product.step", f"designaround model {data['id']} --photo front=front.jpg --agent --max-credits 3000"])


@command(case_app, "list")
def case_list() -> None:
    rows = api.case_list()
    emit("cases", rows, markdown=md_table(rows, ["id", "name", "article", "jurisdictions"]))


@command(case_app, "show")
def case_show(case_id: str = typer.Argument(...)) -> None:
    data = api.case_show(case_id)
    md = f"## {data.get('name')} ({data['id']})\n\nArticle: {data.get('article') or '-'} · offices: {', '.join(data.get('jurisdictions') or [])}\n\n### Versions\n" + md_table(data["versions"], ["id", "kind", "label", "created_by", "risk", "qualified"]) + "\n\n### Prior art\n" + md_table(data["prior_art"], ["id", "office", "number", "title", "status"]) + f"\n\nEvidence: {data['evidence']['events']} events, chain {'OK' if data['evidence']['chain_ok'] else 'BROKEN'}"
    emit("case", data, markdown=md)


@command(case_app, "set")
def case_set(
    case_id: str = typer.Argument(...),
    article: str | None = typer.Option(None),
    article_zh: str | None = typer.Option(None),
    applicant: str | None = typer.Option(None, help="Applicant name."),
    applicant_domicile: str | None = typer.Option(None, help="Applicant country, ISO code (US, CN, DE, …)."),
    applicant_address: str | None = typer.Option(None),
    applicant_type: str | None = typer.Option(None, help="natural | juristic"),
    entity_size: str | None = typer.Option(None, help="US entity status: large | small | micro"),
    fee_reduction: bool | None = typer.Option(None, "--fee-reduction/--no-fee-reduction", help="CN fee reduction eligibility (费减)."),
    inventor: list[str] = typer.Option([], "--inventor", help="Inventor 'Name;Residence' (natural person; repeat)."),
    jurisdiction: list[Office] = typer.Option([], "--jurisdiction", "-j"),
    known_dimension: str | None = typer.Option(None, help="One real dimension, e.g. 'length=320mm'."),
    locarno: str | None = typer.Option(None, help="Locarno class, e.g. 06-04 (EU indication of product)."),
    description: str | None = typer.Option(None),
    constraints: Path | None = typer.Option(None, help="Constraints YAML (locked_params, target_features, interfaces, …)."),
    use_zh: str | None = typer.Option(None, help="用途（简要说明）"),
    design_point_zh: list[str] = typer.Option([], "--design-point-zh", help="设计要点（repeat）"),
    brief_json: str | None = typer.Option(None, "--brief-json", help='Design requirements as JSON: {"use", "size", "users", "style", "process", "price", "must_have": [...], "avoid": [...], "notes"}.'),
) -> None:
    """Update case fields."""

    fields: dict = {}
    for key, value in (("article", article), ("article_zh", article_zh), ("known_dimension", known_dimension), ("locarno", locarno), ("description", description), ("use_zh", use_zh)):
        if value is not None:
            fields[key] = value
    if applicant_type not in (None, "natural", "juristic"):
        raise CliExit(EXIT_INVALID, "bad_applicant_type", "applicant type must be natural or juristic")
    if entity_size not in (None, "large", "small", "micro"):
        raise CliExit(EXIT_INVALID, "bad_entity", "entity size must be large, small or micro")
    updates = {k: v for k, v in (("name", applicant), ("domicile", applicant_domicile.upper() if applicant_domicile else None), ("address", applicant_address), ("type", applicant_type), ("entity_size", entity_size), ("fee_reduction", fee_reduction)) if v is not None}
    if updates:
        current = open_case(case_id).meta.get("applicant") or {}
        fields["applicant"] = {**({"name": current} if isinstance(current, str) else current), **updates}
    if inventor:
        rows = []
        for item in inventor:
            name, _, residence = item.partition(";")
            if not name.strip():
                raise CliExit(EXIT_INVALID, "bad_inventor", "Inventor needs a name: 'Name;Residence'.")
            rows.append({"name": name.strip(), **({"residence": residence.strip()} if residence.strip() else {})})
        fields["inventors"] = rows
    if jurisdiction:
        fields["jurisdictions"] = [j.value for j in jurisdiction]
    if design_point_zh:
        fields["design_points_zh"] = design_point_zh
    if constraints is not None:
        import yaml

        if not constraints.exists():
            raise CliExit(EXIT_INVALID, "not_found", f"{constraints} not found")
        loaded = yaml.safe_load(constraints.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise CliExit(EXIT_INVALID, "bad_constraints", "Constraints file must be a YAML mapping.")
        fields["constraints"] = loaded
    if brief_json is not None:
        try:
            brief = json.loads(brief_json)
        except ValueError as exc:
            raise CliExit(EXIT_INVALID, "bad_brief", f"--brief-json is not valid JSON: {exc}") from None
        if not isinstance(brief, dict):
            raise CliExit(EXIT_INVALID, "bad_brief", "--brief-json must be a JSON object.")
        fields["brief"] = {**(open_case(case_id).meta.get("brief") or {}), **brief}
    if not fields:
        raise CliExit(EXIT_INVALID, "nothing_to_set", "Pass at least one field to set.")
    data = api.case_update(case_id, **fields)
    if constraints is not None or inventor or brief_json is not None:
        api.decision(case_id, "case settings", {k: fields[k] for k in fields if k in ("constraints", "inventors", "brief")})
    emit("case", data)


@command(case_app, "card")
def case_card(case_id: str = typer.Argument(...), lang: Lang = typer.Option(Lang.zh, "--lang", help="zh | en")) -> None:
    """Decision card: verdict, reasons, next steps with cost and time, keep / redesign / abandon."""

    card = api.decision_card(case_id, lang=lang.value)
    emit("decision_card", card, markdown=card["markdown"], next_actions=[s["command"] for s in card["next"] if s.get("command")])


@command(app, "report")
def report_cmd(
    case_id: str = typer.Argument(...),
    version: str | None = typer.Argument(None, help="Version to report on (default: the original design)."),
    lang: Lang = typer.Option(Lang.zh, "--lang", help="zh | en"),
    out: Path | None = typer.Option(None, help="Output PDF path."),
) -> None:
    """Risk assessment report (PDF) with the conclusion, every assessed prior design, method and blind spots."""

    data = api.risk_report(case_id, version, lang=lang.value, out=str(out) if out else None)
    emit("risk_report", data, markdown=f"**{data['headline']}** → `{data['path']}`")


@command(case_app, "decide")
def case_decide(case_id: str = typer.Argument(...), text: str = typer.Argument(..., help="What the person decided, e.g. 'chose v4 as the main design'."), person: str | None = typer.Option(None, help="Who decided (natural person).")) -> None:
    """Record a human decision (feeds the human-contribution record for filings)."""

    emit("decision", api.decision(case_id, text, person=person))


# ------------------------------------------------------------------ model


@command(app, "model")
def model_cmd(
    case_id: str = typer.Argument(...),
    template: str | None = typer.Option(None, help="Build from a parametric template (local, free)."),
    param: list[str] = typer.Option([], "--param", "-p", help="Template parameter name=value (repeat)."),
    cad: Path | None = typer.Option(None, "--cad", help="Import a CAD file (STEP/IGES/BREP) or a mesh (STL/OBJ/GLB/glTF/PLY/3MF, repaired on import) as the original."),
    units: str | None = typer.Option(None, "--units", help="Mesh units when the file does not say: mm (default), cm, m, inch."),
    up: str | None = typer.Option(None, "--up", help="Mesh up axis: z, or y (default for glTF/GLB)."),
    photo: list[str] = typer.Option([], "--photo", help="Labelled photo view=path (front, rear, left, right, top, bottom, perspective)."),
    agent: bool = typer.Option(False, "--agent", help="Let the CAD Agent build a parametric model from photos/description."),
    known: str | None = typer.Option(None, help="One real dimension for scale, e.g. 'length=320mm'."),
    description: str | None = typer.Option(None),
    label: str | None = typer.Option(None),
    max_credits: int | None = typer.Option(None, help="Hard cap for agent runs."),
) -> None:
    """Create the original digital sample (template, CAD import, or CAD Agent from photos)."""

    if cad and (template or agent):
        raise CliExit(EXIT_INVALID, "conflict", "--cad cannot be combined with --template or --agent.")
    if cad and not cad.exists():
        raise CliExit(EXIT_INVALID, "not_found", f"{cad} not found")
    added = api.case_add_photos(case_id, {k: str(v) for k, v in parse_kv(photo, "--photo").items()}) if photo else None
    if known or description:
        api.case_update(case_id, **{k: v for k, v in (("known_dimension", known), ("description", description)) if v})
    if template and not agent:
        data = api.version_from_template(case_id, template, parse_kv(param, "--param"), created_by=actor(), label=label)
    elif cad:
        if not cad.is_file():
            raise CliExit(EXIT_INVALID, "not_found", f"{cad} not found")
        data = api.import_model_file(case_id, str(cad), label=label, units=units, up=up)
    elif agent:  # --template with --agent is a hint for the CAD Agent
        mode = config.model_mode()
        if mode == "host":
            raise CliExit(EXIT_NEEDS_HUMAN, "host_mode", "In host mode you (the host agent) write the CadQuery program.", next_actions=[f"designaround --json cad run {case_id} model.py --kind original --try  # iterate", f"designaround --json cad run {case_id} model.py --kind original", f"designaround --json photos verify {case_id} v1"])
        budget = runner.require_budget(max_credits, 3000, "CAD Agent modelling")
        code, outcome, _ = runner.run("model", case_id, max_credits=budget, mode=mode, extra={"known": known, "template": template, "description": description})
        outcome = agent_outcome(code, outcome, what="CAD Agent")
        data = {**outcome, "version_info": api.version_show(case_id, outcome["version"]) if outcome.get("version") else None}
        emit("model", data, next_actions=[f"designaround screen {case_id} --max-credits 50", f"designaround cad tweak {case_id} {outcome.get('version')} --set name=value"], cost={"estimated": budget, "charged": server_spend(outcome.get("run_id")) or outcome.get("spent", 0)})
        return
    else:
        if added:
            raise CliExit(EXIT_NEEDS_HUMAN, "photos_added", "Photos stored. Build the model with --agent, a --template, a --cad file, or `cad run` in host mode.", data=added)
        raise CliExit(EXIT_INVALID, "nothing_to_do", "Pass --template, --cad, --agent or --photo.", next_actions=["designaround templates"])
    sheet = api.contact_sheet_for(case_id, data["id"])
    md = f"Saved **{data['id']}** ({data['kind']}). Bounding box {data['bbox']['xlen']:.1f} × {data['bbox']['ylen']:.1f} × {data['bbox']['zlen']:.1f} mm. Views: {sheet['path']}"
    rep = data.get("repair")
    if rep:
        fixes = [f"{rep[k]} {label_}" for k, label_ in (("merged_vertices", "duplicate vertices merged"), ("degenerate_removed", "degenerate triangles removed"), ("duplicates_removed", "duplicate triangles removed"), ("loose_pieces_removed", "loose pieces removed"), ("holes_filled", "holes filled"), ("faces_flipped", "triangles re-oriented")) if rep.get(k)]
        md += f"\n\nMesh: {rep['triangles_in']} → {rep['triangles_out']} triangles, {rep['pieces']} piece(s), {'watertight' if rep['watertight'] else 'NOT watertight'}" + (f"; repaired: {', '.join(fixes)}" if fixes else "; no repair needed") + f". Units: {data['source'].get('unit_source')}" + (f"; up axis {data['source']['up']}" if data["source"].get("up") else "") + "."
    for w in data.get("warnings") or []:
        md += f"\n\n> {w}"
    nxt = [f"designaround screen {case_id} --max-credits 50"]
    nxt.append(f"designaround dfm {case_id} {data['id']} --process print3d" if rep else f"designaround cad params {case_id} {data['id']}")
    emit("version", {**data, "contact_sheet": sheet["path"], "photos": added}, markdown=md, next_actions=nxt)


# -------------------------------------------------------------------- cad


@command(cad_app, "run")
def cad_run(
    case_id: str = typer.Argument(...),
    program: Path = typer.Argument(..., help="CadQuery program (sandboxed: only cadquery and math imports)."),
    param: list[str] = typer.Option([], "--param", "-p"),
    parent: str | None = typer.Option(None, help="Version this one derives from."),
    kind: str | None = typer.Option(None, help="original | candidate (default: original if the case has none)."),
    label: str | None = typer.Option(None),
    rationale: str | None = typer.Option(None, help="Why this change (design-around reasoning)."),
    try_only: bool = typer.Option(False, "--try", help="Build and render without saving a version."),
) -> None:
    """Build a CadQuery program (host mode: the host agent writes it)."""

    if not program.exists():
        raise CliExit(EXIT_INVALID, "not_found", f"{program} not found")
    if kind not in (None, "original", "candidate"):
        raise CliExit(EXIT_INVALID, "bad_kind", "kind must be original or candidate")
    source = program.read_text(encoding="utf-8")
    params = parse_kv(param, "--param")
    if try_only:
        open_case(case_id)
        data = api.try_program(source, params)
        emit("cad_try", data, markdown=f"Built OK. Look at the views: {data['contact_sheet']}", next_actions=[f"designaround cad run {case_id} {program}" + (f" --parent {parent}" if parent else "")])
        return
    data = api.version_from_program(case_id, source, params, parent=parent, kind=kind, created_by=actor(), label=label, rationale=rationale)
    sheet = api.contact_sheet_for(case_id, data["id"])
    nxt = [f"designaround originality {case_id} {data['id']}", f"designaround dfm {case_id} {data['id']}"] if data["kind"] == "candidate" else [f"designaround screen {case_id} --max-credits 50"]
    emit("version", {**data, "contact_sheet": sheet["path"]}, markdown=f"Saved **{data['id']}** ({data['kind']}). Views: {sheet['path']}", next_actions=nxt)


@command(cad_app, "tweak")
def cad_tweak(case_id: str = typer.Argument(...), version: str = typer.Argument(...), set_: list[str] = typer.Option(..., "--set", "-s", help="name=value (repeat)."), label: str | None = typer.Option(None), rationale: str | None = typer.Option(None)) -> None:
    """New version with changed parameters."""

    data = api.tweak(case_id, version, parse_kv(set_, "--set"), created_by=actor(), label=label, rationale=rationale)
    sheet = api.contact_sheet_for(case_id, data["id"])
    emit("version", {**data, "contact_sheet": sheet["path"]}, markdown=f"Saved **{data['id']}** from {version}. Views: {sheet['path']}", next_actions=[f"designaround originality {case_id} {data['id']}", f"designaround dfm {case_id} {data['id']}"])


@command(cad_app, "claim")
def cad_claim(case_id: str = typer.Argument(...), version: str = typer.Argument(...), part: str = typer.Argument(...), unclaimed: bool = typer.Option(False, "--unclaimed", help="Show this part in broken lines (not claimed).")) -> None:
    """Set which parts are claimed (solid lines) or unclaimed (broken lines)."""

    emit("version", api.set_claimed(case_id, version, part, not unclaimed))


@command(cad_app, "versions")
def cad_versions(case_id: str = typer.Argument(...)) -> None:
    rows = api.versions(case_id)
    emit("versions", rows, markdown=md_table(rows, ["id", "kind", "parent", "label", "created_by", "risk", "risk_level", "qualified"]))


@command(cad_app, "show")
def cad_show(case_id: str = typer.Argument(...), version: str = typer.Argument(...)) -> None:
    emit("version", api.version_show(case_id, version))


@command(cad_app, "params")
def cad_params(case_id: str = typer.Argument(...), version: str = typer.Argument(...)) -> None:
    """Parameters, their ranges, feature groups and default-locked parameters."""

    emit("params", api.params_schema(case_id, version))


@command(cad_app, "sheet")
def cad_sheet(case_id: str = typer.Argument(...), version: str = typer.Argument(...), kind: str = typer.Option("lineart", help="lineart | render")) -> None:
    """Seven-view contact sheet image (for people and vision models)."""

    if kind not in ("lineart", "render"):
        raise CliExit(EXIT_INVALID, "bad_kind", "kind must be lineart or render")
    emit("contact_sheet", api.contact_sheet_for(case_id, version, kind))


@command(cad_app, "pair")
def cad_pair(case_id: str = typer.Argument(...), version: str = typer.Argument(...), prior: str = typer.Argument(...), view: str | None = typer.Option(None)) -> None:
    """Side-by-side image: candidate | prior art | overlay."""

    emit("pair_image", api.pair_image(case_id, version, prior, view=view))


@command(cad_app, "sheet-vs")
def cad_sheet_vs(case_id: str = typer.Argument(...), version: str = typer.Argument(...), prior: str = typer.Argument(...)) -> None:
    """Multi-view comparison sheet (candidate views over prior-art figures), as the observer sees it."""

    emit("comparison_image", api.comparison_image(case_id, version, prior))


@command(cad_app, "compare")
def cad_compare(case_id: str = typer.Argument(...), a: str = typer.Argument(...), b: str = typer.Argument(...)) -> None:
    """Changed parameters, size, same-scale overlay in every view and risk of two versions."""

    data = api.compare_sheet(case_id, a, b)
    size = data["size_mm"]
    md = f"**{a} → {b}** · size {size['xlen'][0]}×{size['ylen'][0]}×{size['zlen'][0]} → {size['xlen'][1]}×{size['ylen'][1]}×{size['zlen'][1]} mm\n\n" + md_table([{"view": k, "overlap": r["iou"], "area_change": r["area_change"]} for k, r in data["overlap"].items()], ["view", "overlap", "area_change"]) + f"\n\nOverlay: `{data['sheet']}`"
    emit("compare", data, markdown=md)


# -------------------------------------------------------------- prior art


@command(prior_app, "add")
def prior_add(
    case_id: str = typer.Argument(...),
    number: str = typer.Option(..., help="Registration / patent number, e.g. USD1012345S."),
    office: str | None = typer.Option(None),
    title: str | None = typer.Option(None),
    figure: list[Path] = typer.Option([], "--figure", "-f", help="Figure image (repeat)."),
    status: str | None = typer.Option(None),
    date_: str | None = typer.Option(None, "--date"),
    owner: str | None = typer.Option(None),
    url: str | None = typer.Option(None),
    scope_notes: str | None = typer.Option(None, help="Claimed scope (solid vs broken lines) if known."),
    design_point: list[str] = typer.Option([], "--design-point"),
) -> None:
    """Add a known design right (e.g. from a complaint or a competitor)."""

    data = api.prior_art_add(case_id, number=number, office=office, title=title, figures=[str(f) for f in figure], status=status, date=date_, owner=owner, source="user", url=url, scope_notes=scope_notes, design_points=design_point)
    emit("prior_art", data, next_actions=[f"designaround prior-art rank {case_id}"])


@command(prior_app, "list")
def prior_list(case_id: str = typer.Argument(...)) -> None:
    rows = api.prior_art_list(case_id)
    emit("prior_art", rows, markdown=md_table(rows, ["id", "office", "number", "title", "status", "source"]))


@command(prior_app, "rank")
def prior_rank(case_id: str = typer.Argument(...), version: str | None = typer.Option(None, help="Default: the original.")) -> None:
    """CPU silhouette ranking of prior art against a version (free, local)."""

    vid = version or open_case(case_id).original_version()
    if not vid:
        raise CliExit(EXIT_INVALID, "no_version", "The case has no versions yet.", next_actions=[f"designaround model {case_id} --template storage_box"])
    rows = [{"id": r["id"], "number": r.get("number"), "title": r.get("title"), "shape_score": r["shape"]["score"], "view": r["shape"].get("view"), "figure": r["shape"].get("figure")} for r in api.shape_scores(case_id, vid)]
    top = [r["id"] for r in rows[:3]]
    emit("ranking", {"version": vid, "ranked": rows}, markdown=md_table(rows, ["id", "number", "title", "shape_score", "view"]), next_actions=[f"designaround observe {case_id} {vid} {pid} --max-credits 120" for pid in top])


# ---------------------------------------------------------------- screen


@command(app, "screen")
def screen_cmd(
    case_id: str = typer.Argument(...),
    image: Path | None = typer.Argument(None, help="Product image (default: the original version's perspective render, else a photo)."),
    query: str | None = typer.Option(None, help="Text query (default: the case article)."),
    deep: bool = typer.Option(False, "--deep", help="Image + text search across sources (350 credits)."),
    jurisdiction: list[Office] = typer.Option([], "--jurisdiction", "-j", help="Default: the case offices."),
    fetch: int = typer.Option(10, help="How many candidates to download figures for and add as prior art."),
    figures: int = typer.Option(4, help="Figures per candidate."),
    version: str | None = typer.Option(None, help="Version to rank against (default: the original)."),
    hires: int = typer.Option(5, help="Fetch full-size USPTO drawing sheets for this many top-ranked US designs (0 = off)."),
    max_credits: int | None = typer.Option(None, help="Spending cap (required for non-interactive callers)."),
) -> None:
    """Registry screening on the server, then local ranking of the results."""

    from .screening import run_screen

    price = fixed_price("screen.deep" if deep else "screen.quick", max_credits)
    offices = [j.value for j in jurisdiction] or (open_case(case_id).meta.get("jurisdictions") or ["US"])
    data = run_screen(case_id, image=image, query=query, deep=deep, jurisdictions=offices, fetch=max(0, fetch), figures_per=max(1, figures), version=version, hires=max(0, hires))
    md = f"{data['candidates']} candidates · {len(data['added'])} added as prior art · {sum(1 for u in data['full_drawings'] if u.get('ok'))} with full drawings · charged {data['charged']} credits\n\n" + md_table(data["ranked"][:10], ["id", "number", "title", "shape_score", "view"]) + "\n\nBlind spots:\n" + "\n".join(f"- {b}" for b in data["blind_spots"])
    if data["source_failures"]:
        md += "\n\nSource failures:\n" + "\n".join(f"- {f.get('source') or f.get('sources')}: {f.get('error')}" for f in data["source_failures"])
    top = [r["id"] for r in data["ranked"][:3]]
    nxt = [f"designaround observe {case_id} {data['version']} {pid} --max-credits 120" for pid in top] if data["version"] else [f"designaround model {case_id} --cad product.step", f"designaround model {case_id} --agent --known length=<mm> --max-credits 1500", f"designaround case card {case_id}"]
    if top and config.model_mode() != "host":
        nxt.insert(0, f"designaround around {case_id} --max-credits 6000")
    emit("screen", data, markdown=md, next_actions=nxt, cost={"estimated": price, "charged": data["charged"]})


def _diff_md(data: dict, lang: str) -> str:
    zh = lang != "en"
    new = [{"number": h["number"], "office": h.get("office"), "title": (h.get("title") or "")[:40], "date": h.get("date"), "shape": h.get("shape_score"), "close": ("前 3" if zh else "top 3") if h.get("top3") else ("接近" if zh else "close") if h.get("visually_close") else "", "class": {True: "same", False: "other", None: "?"}[h.get("same_class")]} for h in data["new"]]
    head = (f"复跑 {data['base']} → {data['rerun']}：新增 **{len(data['new'])}**，消失 {len(data['gone'])}，变化 {len(data['changed'])}，未变 {data['unchanged']}" if zh else f"Re-run {data['base']} → {data['rerun']}: **{len(data['new'])}** new, {len(data['gone'])} gone, {len(data['changed'])} changed, {data['unchanged']} unchanged")
    parts = [head]
    if new:
        parts.append(md_table(new, ["number", "office", "title", "date", "shape", "close", "class"]))
    if data["changed"]:
        parts.append("\n".join(f"- {c['number']}: " + "; ".join(f"{k} {v['from']} → {v['to']}" for k, v in c["changes"].items()) for c in data["changed"][:20]))
    if data["gone"]:
        parts.append(("不再出现：" if zh else "No longer returned: ") + ", ".join(g["number"] for g in data["gone"][:20]))
    if data.get("note"):
        parts.append(data["note"])
    return "\n\n".join(parts)


@command(app, "rescreen")
def rescreen_cmd(
    case_id: str = typer.Argument(...),
    screen_id: str | None = typer.Argument(None, help="Screen record to repeat (default: the latest; see `rescreen <case> --list`)."),
    list_: bool = typer.Option(False, "--list", help="List the screen records instead of re-running."),
    max_credits: int | None = typer.Option(None, help="Spending cap (required for non-interactive callers)."),
    lang: Lang = typer.Option(Lang.zh, "--lang"),
) -> None:
    """Re-run a recorded screen with its original parameters and list the differences (FR-CASE-07)."""

    from . import monitor

    case = open_case(case_id)
    if list_:
        rows = [{"screen": r["id"], "query": monitor.params_of(case, r).get("query"), "deep": monitor.params_of(case, r).get("deep"), "offices": ",".join(monitor.params_of(case, r).get("jurisdictions") or []), "candidates": len(r["data"].get("candidates") or [])} for r in monitor.screen_records(case)]
        emit("screens", rows, markdown=md_table(rows, ["screen", "query", "deep", "offices", "candidates"]) if rows else "No screens yet.")
        return
    params = monitor.params_of(case, monitor.pick(case, screen_id))
    price = fixed_price("screen.deep" if params.get("deep") else "screen.quick", max_credits)
    data = monitor.rescreen(case_id, screen_id, lang=lang.value)
    nxt = [f"designaround observe {case_id} {data['version']} {h['prior_art_id']} --max-credits 120" for h in data["new"] if h.get("top3") and h.get("prior_art_id") and data.get("version")][:3]
    nxt.append(f"designaround monitor add {case_id} --every 30 --max-credits {price}")
    emit("rescreen", data, markdown=_diff_md(data, lang.value), next_actions=nxt, cost={"estimated": price, "charged": data["charged"]})


@command(monitor_app, "add")
def monitor_add(
    case_id: str = typer.Argument(...),
    every: int = typer.Option(30, "--every", min=1, max=365, help="Days between re-screens."),
    max_credits: int = typer.Option(..., help="Most credits one re-screen of this case may cost (quick 50, 5 free a month; deep 350)."),
) -> None:
    """Watch a case: its latest screen is repeated every N days and new hits are pushed to you."""

    from . import monitor

    data = monitor.subscribe(case_id, every_days=every, max_credits=max_credits)
    emit("monitor", data, markdown=f"Watching **{case_id}** every {every} days (≤ {max_credits} credits a run). Runs when the workbench starts or with `designaround monitor run --due-only`; new hits go to your chat-bot webhook / desktop notification (`designaround config notify`).", next_actions=["designaround monitor run --due-only --max-credits 500", "designaround config notify --webhook <url>"])


@command(monitor_app, "list")
def monitor_list() -> None:
    from . import monitor

    rows = monitor.monitors()
    table = [{"case": r["case"], "every": r["every_days"], "max_credits": r["max_credits"], "last": _when(r.get("last_run")), "status": r.get("last_status") or "-", "next": _when(r["next_run"]), "due": r["due"]} for r in rows]
    emit("monitors", rows, markdown=md_table(table, ["case", "every", "max_credits", "last", "status", "next", "due"]) if table else "No monitored cases.")


@command(monitor_app, "remove")
def monitor_remove(case_id: str = typer.Argument(...)) -> None:
    from . import monitor

    emit("monitor", monitor.unsubscribe(case_id))


@command(monitor_app, "run")
def monitor_run(
    due_only: bool = typer.Option(False, "--due-only", help="Only cases whose period has passed."),
    max_credits: int | None = typer.Option(None, help="Cap for all re-screens of this run (default: the sum of the per-case caps set with `monitor add`)."),
    lang: Lang = typer.Option(Lang.zh, "--lang"),
) -> None:
    """Re-screen the watched cases now and push new hits (for cron: --due-only)."""

    from . import monitor

    rows = [r for r in monitor.monitors() if r["due"] or not due_only]
    if not rows:
        emit("monitor_run", {"results": [], "spent": 0, "new_hits": 0}, markdown="Nothing due.")
        return
    if max_credits is not None and max_credits <= 0:
        raise CliExit(EXIT_BUDGET, "budget_required", "--max-credits must be a positive integer.")
    # Without --max-credits the caps the person set per case (`monitor add --max-credits`) apply.
    budget = max_credits if max_credits is not None else sum(int(r.get("max_credits") or 0) for r in rows)
    data = monitor.run(due_only=due_only, budget=budget, lang=lang.value)
    table = [{"case": r["case"], "status": r["status"], "new": len(r.get("new") or []), "gone": r.get("gone", ""), "changed": r.get("changed", ""), "charged": r.get("charged", "")} for r in data["results"]]
    emit("monitor_run", data, markdown=md_table(table, ["case", "status", "new", "gone", "changed", "charged"]) + f"\n\n{data['new_hits']} new hits · spent {data['spent']} credits", cost={"estimated": budget, "charged": data["spent"]})


# --------------------------------------------------------------- observer


@command(app, "observe")
def observe_cmd(
    case_id: str = typer.Argument(...),
    version: str = typer.Argument(...),
    prior: str = typer.Argument(..., help="Prior art id (see `prior-art list`)."),
    view: str | None = typer.Option(None, help="Compare a single view instead of the multi-view sheet (host mode only)."),
    max_credits: int | None = typer.Option(None),
) -> None:
    """Ordinary-observer similarity score (0-10) from an isolated vision model."""

    mode = config.model_mode()
    if mode == "host":
        price = fixed_price("observe.hosted", max_credits)
        pair = api.pair_image(case_id, version, prior, view=view) if view else api.comparison_image(case_id, version, prior)
        prompt = api.observer_prompt(case_id, version, prior, pair["view"])
        image_b64 = base64.b64encode(Path(pair["path"]).read_bytes()).decode("ascii")
        body = Server().post("/v1/observe", {"image_base64": image_b64, "image_mime_type": "image/png", "system": prompt["system"], "user": prompt["user"], "rubric_version": prompt["rubric_version"]}, timeout=180)
        result = {**body["data"], "view": pair["view"], "rubric_version": prompt["rubric_version"]}
        record = save_judgment(case_id, version, prior, result, "hosted")
        cost = {"estimated": price, "charged": (body.get("cost") or {}).get("charged", price)}
    else:
        budget = runner.require_budget(max_credits, 300, "Observer scoring")
        code, outcome, _ = runner.run("observe", case_id, max_credits=budget, mode=mode, extra={"version": version, "prior": prior})
        outcome = agent_outcome(code, outcome, what="Observer")
        record = save_judgment(case_id, version, prior, outcome["observer"], f"agent:{mode}")
        cost = {"estimated": budget, "charged": server_spend(outcome.get("run_id")) or outcome.get("spent", 0)}
    md = f"Observer score **{record.get('score')}/10** ({version} vs {prior}, view {record.get('view')})\n\nSimilar: " + "; ".join(record.get("similar") or []) + "\n\nDifferent: " + "; ".join(record.get("different") or [])
    emit("observer_score", record, markdown=md, next_actions=[f"designaround evaluate {case_id} {version}"], cost=cost)


@command(app, "evaluate")
def evaluate_cmd(case_id: str = typer.Argument(...), version: str = typer.Argument(...), judge_file: Path | None = typer.Option(None, help="Extra judgments JSON {prior_id: {score, similar, different, rationale}}.")) -> None:
    """Fuse CPU shape scores and observer judgments into a risk level (not a legal opinion)."""

    judge: dict = {}
    if judge_file is not None:
        judge = json.loads(judge_file.read_text(encoding="utf-8"))
        if not isinstance(judge, dict):
            raise CliExit(EXIT_INVALID, "bad_judge_file", "Judge file must map prior-art ids to judgments.")
    data = api.evaluate(case_id, version, judge=judge, judge_source="file")
    rows = [{"prior_art": r["prior_art_id"], "number": r.get("number"), "shape": r["shape"]["score"], "observer": (r.get("observer") or {}).get("score"), "risk": r["risk"], "level": r["level"]} for r in data["per_prior_art"]]
    summary = data["summary"]
    missing = summary.get("pending_observer") or []
    nxt = [f"designaround observe {case_id} {version} {pid} --max-credits 120" for pid in missing]
    if summary.get("qualified"):
        nxt += [f"designaround originality {case_id} {version}", f"designaround viewpack {case_id} {version} --office US"]
    elif not missing:
        nxt.append(f"designaround around {case_id} --max-credits 6000" if config.model_mode() != "host" else f"designaround cad tweak {case_id} {version} --set name=value")
    md = f"Max risk **{summary['max_risk']}** ({summary['level']}) · qualified: **{summary['qualified']}**" + (f" · observer pending: {', '.join(missing)}" if missing else "") + f" · {summary.get('screened_out', 0)} screened out by silhouette\n\n" + md_table(rows, ["prior_art", "number", "shape", "observer", "risk", "level"]) + f"\n\n_{data['wording']}_"
    emit("evaluation", data, markdown=md, next_actions=nxt)


class Depth(str, Enum):
    quick = "quick"
    standard = "standard"
    deep = "deep"


@command(app, "around")
def around_cmd(
    case_id: str = typer.Argument(...),
    max_credits: int | None = typer.Option(None, help="Hard cap for the whole run (default: the depth's cap)."),
    depth: Depth = typer.Option(Depth.standard, "--depth", "--mode", help="quick (1 round) | standard (3 rounds) | deep (6 rounds). Standard and deep carry the result guarantee."),
    rounds: int | None = typer.Option(None, min=1, max=8, help="Override the depth's rounds."),
    per_round: int | None = typer.Option(None, min=1, max=6, help="Override the depth's candidates per round."),
    target: int | None = typer.Option(None, min=1, max=6, help="Stop after this many qualified candidates."),
    top: int = typer.Option(3, min=1, max=8, help="How many prior-art rights to design around."),
    process: Process = typer.Option(Process.injection),
    lang: Lang = typer.Option(Lang.zh, "--lang", help="Language of the stuck-point explanation: zh | en"),
) -> None:
    """Agent design-around: Designer proposes, gates check, isolated Observer scores, repeat.

    Result guarantee: a standard or deep run (platform models) that ends without any
    qualified candidate gets half of its credits back automatically, with the reasons
    it got stuck.
    """

    mode = config.model_mode()
    if mode == "host":
        raise CliExit(EXIT_NEEDS_HUMAN, "host_mode", "Host mode: you (the host agent) run the design-around loop with the CLI; follow the Skill workflow.", data={"loop": [s.replace("<case>", case_id) for s in HOST_LOOP]}, next_actions=[s.replace("<case>", case_id) for s in HOST_LOOP[:3]])
    preset = catalog.AROUND_DEPTHS[depth.value]
    rounds = rounds or preset["rounds"]
    per_round = per_round or preset["per_round"]
    target = target or preset["target"]
    budget = runner.require_budget(max_credits, preset["max_credits"], f"Design-around run ({depth.value})")
    details = {"depth": depth.value, "rounds": rounds, "per_round": per_round, "target": target}
    code, outcome, _ = runner.run("around", case_id, max_credits=budget, mode=mode, extra={"rounds": rounds, "per-round": per_round, "target": target, "top": top, "process": process.value}, details=details, lang=lang.value)
    claim = outcome.get("guarantee")
    if claim and outcome.get("type") == "error":
        outcome = {**outcome, "message": f"{outcome.get('message')} | " + (guarantee_mod.describe(claim, lang.value) or "")}
    outcome = agent_outcome(code, outcome, what="Design-around")
    rows = outcome.get("candidates") or []
    md = f"Status **{outcome.get('status')}** ({depth.value}: {rounds} rounds × {per_round}) · baseline risk {(outcome.get('baseline') or {}).get('max_risk')} · qualified: {', '.join(outcome.get('qualified') or []) or 'none'}\n\n" + md_table(rows, ["version", "round", "qualified", "risk", "level", "dfm_pass", "reason"])
    if claim:
        md += "\n\n" + (guarantee_mod.describe(claim, lang.value) or "")
        if claim.get("blockers"):
            md += "\n\n" + ("卡在哪里：" if lang.value == "zh" else "Where it got stuck:") + "\n\n" + "\n".join(f"- {b}" for b in claim["blockers"])
    elif outcome.get("stuck"):
        md += f"\n\nWhere it got stuck:\n\n{outcome['stuck']}"
    if outcome.get("status") != "ok" and not guarantee_mod.covered(depth.value, rounds) and mode == "platform":
        md += "\n\n" + ("（快速模式或少于 3 轮的运行不在结果保障范围内。）" if lang.value == "zh" else "(Quick runs and runs under 3 rounds are not covered by the result guarantee.)")
    nxt = [f"designaround cad compare {case_id} {outcome.get('original')} {v}" for v in (outcome.get("qualified") or [])[:2]]
    nxt += [f"designaround case decide {case_id} 'chose <version> as the main design'"]
    if outcome.get("status") != "ok":
        nxt.append(f"designaround case set {case_id} --constraints constraints.yaml  # loosen locked parameters, then re-run")
        nxt.append(f"designaround design concepts {case_id} --max-credits 600  # or try a different design direction")
    if claim and claim.get("status") == "not_filed":
        nxt.insert(0, f"designaround run guarantee {outcome.get('run_id')}")
    charged = server_spend(outcome.get("run_id"))
    cost = {"estimated": budget, "charged": charged if charged is not None else outcome.get("spent", 0)}
    if claim and claim.get("refunded"):
        cost["refunded"] = int(claim["refunded"])
    emit("around", outcome, markdown=md, next_actions=nxt, cost=cost)


# ---------------------------------------------------------------- design


def _design_budget(max_credits: int | None, default: int, what: str) -> int:
    mode = config.model_mode()
    if mode != "platform":
        raise CliExit(EXIT_NEEDS_HUMAN, "platform_mode_needed", f"{what} uses hosted image and review models: switch to platform mode.", next_actions=["designaround config mode platform"])
    return runner.require_budget(max_credits, default, what)


def _concept_rows(rows: list[dict]) -> list[dict]:
    return [{"id": r["id"], "title": r.get("title"), "status": r.get("status"), "fit": (r.get("review") or {}).get("fit"), "made": (r.get("review") or {}).get("manufacturable"), "chosen": "yes" if r.get("chosen") else ""} for r in rows]


@command(design_app, "concepts")
def design_concepts(
    case_id: str = typer.Argument(...),
    count: int = typer.Option(4, min=1, max=6, help="How many distinct directions."),
    base: str | None = typer.Option(None, help="Refine this concept (e.g. c2) instead of starting fresh."),
    feedback: str | None = typer.Option(None, help="What the person wants changed (with --base)."),
    max_credits: int | None = typer.Option(None, help="Hard cap for images and reviews."),
) -> None:
    """2D concepts from the case requirements: one image per direction, each checked by an independent reviewer."""

    if feedback and not base:
        raise CliExit(EXIT_INVALID, "base_required", "--feedback needs --base <concept>.")
    budget = _design_budget(max_credits, 120 * count + 200, "Concept generation")
    code, outcome, _ = runner.run("concepts", case_id, max_credits=budget, mode=config.model_mode(), extra={"count": count, "base": base, "feedback": feedback})
    outcome = agent_outcome(code, outcome, what="Concept generation")
    rows = outcome.get("concepts") or []
    md = md_table([{"id": r["id"], "title": r.get("title"), "pass": "yes" if r.get("pass") else "no", "fit": r.get("fit"), "issues": "; ".join(r.get("issues") or [])[:120]} for r in rows], ["id", "title", "pass", "fit", "issues"])
    passed = outcome.get("passed") or []
    nxt = [f"designaround design choose {case_id} {passed[0] if passed else '<concept>'} --person 'Name'", f"designaround design concepts {case_id} --base {passed[0] if passed else '<concept>'} --feedback '...'"]
    emit("concepts", outcome, markdown=md + (f"\n\nSheet: {outcome.get('sheet')}" if outcome.get("sheet") else ""), next_actions=nxt, cost={"estimated": budget, "charged": server_spend(outcome.get("run_id")) or outcome.get("spent", 0)})


@command(design_app, "list")
def design_list(case_id: str = typer.Argument(...), all_: bool = typer.Option(False, "--all", help="Include replaced concepts.")) -> None:
    from designaround_engine import concepts

    rows = concepts.concepts(case_id, include_superseded=all_)
    emit("concepts", rows, markdown=md_table(_concept_rows(rows), ["id", "title", "status", "fit", "made", "chosen"]))


@command(design_app, "show")
def design_show(case_id: str = typer.Argument(...), concept: str = typer.Argument(...)) -> None:
    from designaround_engine import concepts

    emit("concept", concepts.concept_show(case_id, concept))


@command(design_app, "sheet")
def design_sheet(case_id: str = typer.Argument(...), concept: list[str] = typer.Argument(None, help="Concept ids (default: all current).")) -> None:
    """Image grid of concepts, to look at or show a person."""

    from designaround_engine import concepts

    emit("concept_sheet", concepts.concept_sheet(case_id, concept or None))


@command(design_app, "choose")
def design_choose(case_id: str = typer.Argument(...), concept: str = typer.Argument(...), person: str | None = typer.Option(None, help="Who chose (natural person)."), note: str | None = typer.Option(None)) -> None:
    """Record the person's choice of a design direction (the human contribution)."""

    from designaround_engine import concepts

    data = concepts.concept_choose(case_id, concept, person=person, note=note)
    emit("concept", data, markdown=f"Chose **{concept}** ({data.get('title')}).", next_actions=[f"designaround design build {case_id} --concept {concept} --max-credits 3000"])


@command(design_app, "accept")
def design_accept(
    case_id: str = typer.Argument(...),
    version: str = typer.Argument(..., help="Version to accept, e.g. the draft a 3D build returned."),
    person: str | None = typer.Option(None, help="Who accepts it (recorded as a human decision)."),
    note: str | None = typer.Option(None),
) -> None:
    """Accept a version as the case's original design yourself (for example a 3D draft the reviewer did not accept)."""

    data = api.accept_version(case_id, version, person=person, note=note)
    review = data.get("review") or {}
    md = f"**{version}** is now the original design" + (f" (reviewer match was {review.get('match')}/10)" if review.get("match") is not None else "")
    emit("design_accept", data, markdown=md, next_actions=[f"designaround screen {case_id} --max-credits 50", f"designaround dfm {case_id} {version}"])


@command(design_app, "build")
def design_build(
    case_id: str = typer.Argument(...),
    concept: str | None = typer.Option(None, help="Concept to build (default: the chosen one)."),
    known: str | None = typer.Option(None, help="Required size, e.g. '240x160x90mm' or 'length=240mm'."),
    rounds: int = typer.Option(4, min=1, max=6, help="Review submissions before asking the person."),
    process: Process = typer.Option(Process.injection),
    max_credits: int | None = typer.Option(None, help="Hard cap for the CAD Agent and reviews."),
) -> None:
    """The CAD Agent builds the concept in 3D from scratch; an independent reviewer and geometry checks must accept it."""

    budget = _design_budget(max_credits, 3000, "3D build")
    code, outcome, _ = runner.run("build", case_id, max_credits=budget, mode=config.model_mode(), extra={"concept": concept, "known": known, "rounds": rounds, "process": process.value})
    if code == EXIT_NEEDS_HUMAN and outcome.get("type") != "error":
        draft, cid = outcome.get("draft"), outcome.get("concept") or "<concept>"
        steps = [f"designaround cad sheet {case_id} {draft} --kind render", f'designaround design accept {case_id} {draft} --person "<name>"'] if draft else []
        steps += [f"designaround design build {case_id} --max-credits {budget}", f'designaround design concepts {case_id} --base {cid} --feedback "simpler form" --max-credits 300']
        raise CliExit(EXIT_NEEDS_HUMAN, "needs_input", outcome.get("reason") or "3D build needs human input.", data=outcome, next_actions=steps)
    outcome = agent_outcome(code, outcome, what="3D build")
    vid = outcome.get("version")
    review = outcome.get("review") or {}
    md = f"Built **{vid}** from {outcome.get('concept')} · reviewer match {review.get('match')}/10 · {outcome.get('submissions')} submission(s)"
    emit("design_build", outcome, markdown=md, next_actions=[f"designaround cad sheet {case_id} {vid} --kind render", f"designaround screen {case_id} --max-credits 50", f"designaround dfm {case_id} {vid}"], cost={"estimated": budget, "charged": server_spend(outcome.get("run_id")) or outcome.get("spent", 0)})


# ----------------------------------------------------------------- gates


@command(app, "originality")
def originality_cmd(case_id: str = typer.Argument(...), version: str = typer.Argument(...), base: str | None = typer.Option(None), target_feature: list[str] = typer.Option([], "--target-feature")) -> None:
    """Originality gate: the candidate must change the features that matter, not just scale."""

    data = api.originality_check(case_id, version, base=base, target_features=target_feature or None)
    emit("originality", data, markdown=f"Originality: **{'pass' if data['pass'] else 'fail'}**\n\n" + "\n".join(f"- {r}" for r in data["reasons"]))


@command(app, "dfm")
def dfm_cmd(case_id: str = typer.Argument(...), version: str = typer.Argument(...), process: Process = typer.Option(Process.injection)) -> None:
    """Manufacturability checks (wall thickness, draft, interfaces)."""

    data = api.dfm_check(case_id, version, process.value)
    rows = [{"rule": c["rule_id"], "severity": c["severity"], "message": c["message"]} for c in data["checks"]]
    emit("dfm", data, markdown=f"DFM ({process.value}): **{'pass' if data['pass'] else 'fail'}**\n\n" + md_table(rows, ["rule", "severity", "message"]))


# ----------------------------------------------------------- deliverables


@command(app, "viewpack")
def viewpack_cmd(case_id: str = typer.Argument(...), version: str = typer.Argument(...), office: Office = typer.Option(..., "--office", "-o"), omit: list[str] = typer.Option([], "--omit", help="View to omit (practitioner must confirm).")) -> None:
    """Patent line drawings (US sheets / CN views) with rule checks."""

    ent = entitlements()
    watermark = bool(ent["entitlements"].get("viewpack_watermark", True))
    data = api.make_viewpack(case_id, version, office.value, omit=omit or None, watermark=watermark)
    rules = data["rules"]
    nxt = [f"designaround filing-pack {case_id} {version} --office {office.value}"]
    if watermark:
        nxt.insert(0, "designaround billing checkout --plan pro  # removes the PREVIEW watermark")
    emit("viewpack", {**data, "watermark": watermark, "plan": ent.get("plan")}, markdown=f"View pack **{rules['status']}** ({rules['passed']}/{rules['total']} rules) → {data['dir']}" + (" · PREVIEW watermark (free plan)" if watermark else "") + "\n\n" + findings_md(rules["findings"]), next_actions=nxt)


@command(app, "photo-viewpack")
def photo_viewpack_cmd(case_id: str = typer.Argument(...), office: Office = typer.Option(..., "--office", "-o"), photo: list[str] = typer.Option([], "--photo", help="view=path (default: the case photos).")) -> None:
    """View pack from labelled product photos (path A, no CAD)."""

    photos = parse_kv(photo, "--photo") or api.case_photos(case_id)
    if not photos:
        raise CliExit(EXIT_INVALID, "no_photos", "No photos on the case.", next_actions=[f"designaround photos add {case_id} front=front.jpg perspective=persp.jpg"])
    ent = entitlements()
    watermark = bool(ent["entitlements"].get("viewpack_watermark", True))
    data = api.make_photo_viewpack(case_id, {k: str(v) for k, v in photos.items()}, office.value, watermark=watermark)
    emit("viewpack", {**data, "watermark": watermark}, markdown=f"Photo view pack → {data['dir']}\n\n" + findings_md(data["rules"]["findings"]))


@command(app, "filing-pack")
def filing_pack_cmd(
    case_id: str = typer.Argument(...),
    version: str = typer.Argument(...),
    office: Office = typer.Option(..., "--office", "-o"),
    similar: str | None = typer.Option(None, help="CN similar designs: 'auto' or comma-separated versions."),
    planned_date: str | None = typer.Option(None, help="Planned filing date YYYY-MM-DD (grace-period and priority checks)."),
) -> None:
    """Filing package for a practitioner: specification, forms, IDS, checklist, human-contribution record."""

    ent = entitlements()
    if not ent["entitlements"].get("filing_packs"):
        raise CliExit(EXIT_BUDGET, "plan_required", "Filing packages need the Pro plan or higher.", data={"plan": ent.get("plan")}, next_actions=["designaround billing checkout --plan pro  (a person completes payment)", f"designaround viewpack {case_id} {version} --office {office.value}  # free preview"])
    sim = None if similar is None else ("auto" if similar == "auto" else [s.strip() for s in similar.split(",") if s.strip()])
    data = api.filing_pack(case_id, version, office.value, similar=sim, planned_filing_date=parse_date(planned_date, "--planned-date"), watermark=False)
    checklist = data["checklist"]
    md = f"Filing package **{checklist['status']}** → {data['dir']}\n\nReady for practitioner: **{checklist['ready_for_practitioner']}**" + (" · practitioner gate required" if checklist.get("requires_practitioner_gate") else "") + "\n\n" + findings_md(checklist["findings"])
    if checklist.get("missing"):
        md += "\n\nMissing:\n" + "\n".join(f"- {m}" for m in checklist["missing"])
    nxt = [f"designaround gate create {case_id} --type filing_submission --reviewer-email <practitioner>"] if checklist.get("requires_practitioner_gate") or office == Office.US else []
    nxt.append(f"designaround evidence anchor {case_id}")
    emit("filing_pack", data, markdown=md, next_actions=nxt)


@command(baseline_app, "freeze")
def baseline_freeze(
    case_id: str = typer.Argument(...),
    version: str = typer.Argument(...),
    office: Office = typer.Option(..., "--office", "-o"),
    application_number: str | None = typer.Option(None, help="Application number once known."),
    note: str | None = typer.Option(None),
) -> None:
    """Freeze the filing package, view pack and 3D version as submitted (hashes, read-only, evidence event)."""

    data = api.baseline_freeze(case_id, version, office.value, note=note, application_number=application_number)
    emit("baseline", data, markdown=f"Baseline **{data['id']}** frozen: {data['files']} files, record sha256 `{data['sha256'][:16]}…`", next_actions=[f"designaround baseline verify {case_id} {data['id']}"])


@command(baseline_app, "list")
def baseline_list(case_id: str = typer.Argument(...)) -> None:
    rows = api.baseline_list(case_id)
    emit("baselines", rows, markdown=md_table(rows, ["id", "office", "version", "frozen_at", "application_number", "files"]))


@command(baseline_app, "verify")
def baseline_verify(case_id: str = typer.Argument(...), baseline_id: str = typer.Argument(...)) -> None:
    """Check every frozen file against its recorded SHA-256 and the evidence event."""

    data = api.baseline_verify(case_id, baseline_id)
    if not data["ok"]:
        details = "; ".join(f"{k}: {', '.join(data[k][:10])}" for k in ("changed", "missing", "added") if data[k])
        raise CliExit(EXIT_ERROR, "baseline_changed", f"The frozen baseline no longer matches its record ({details or 'record differs from the evidence event'}).", data=data)
    emit("baseline_verify", data, markdown=f"Baseline **{baseline_id}**: unchanged ({data['files']} files)")


@command(baseline_app, "compare")
def baseline_compare(case_id: str = typer.Argument(...), baseline_id: str = typer.Argument(...), version: str = typer.Argument(..., help="Version to compare with the baseline, e.g. a corrected design.")) -> None:
    """How far a version departs from what was filed, view by view (input for the practitioner's scope check)."""

    data = api.baseline_compare(case_id, baseline_id, version)
    rows = [{"view": k, "overlap": r["iou"], "area_change": r["area_change"], "changed": "✗" if k in data["changed_views"] else ""} for k, r in data["per_view"].items()]
    md = (f"**{version}** shows the same design as baseline {baseline_id} in every view." if data["same_design_shown"] else f"**{version}** differs from baseline {baseline_id} in: {', '.join(data['changed_views'])}.") + f"\n\n{data['rule']}\n\n" + md_table(rows, ["view", "overlap", "area_change", "changed"]) + f"\n\nOverlay: `{data['sheet']}`\n\n_{data['wording']}_"
    emit("baseline_compare", data, markdown=md)


@command(batch_app, "screen")
def batch_screen(
    csv_file: Path = typer.Argument(..., help="CSV with name, article[, article_zh, jurisdictions, image, cad, case]."),
    deep: bool = typer.Option(False, "--deep", help="Deep screening (image + text) for every row."),
    max_credits: int | None = typer.Option(None, help="Cap for the whole batch (required for non-interactive callers)."),
    concurrency: int = typer.Option(2, min=1, max=6),
    out: Path | None = typer.Option(None, help="Results CSV (default: <csv>-results.csv)."),
) -> None:
    """Screen many products from a CSV; a second run resumes and retries failed rows."""

    from . import batch

    rows = batch.read_rows(csv_file)
    state = csv_file.with_suffix(csv_file.suffix + ".state.json")
    done = {k for k, v in (json.loads(state.read_text(encoding="utf-8"))["rows"] if state.exists() else {}).items() if v.get("status") == "done"}
    est = batch.estimate(rows, deep, done)
    budget = runner.require_budget(max_credits, max(est, 1), f"Batch screening of {len(rows) - len(done)} rows (estimate {est} credits)")
    data = batch.run_batch(csv_file, deep=deep, max_credits=budget, concurrency=concurrency, out=out)
    c = data["counts"]
    md = f"Batch: **{c['done']}** done · **{c['failed']}** failed · {c['pending']} pending · charged {data['charged_this_run']} credits\n\n" + md_table(data["results"][:50], ["name", "case", "status", "candidates", "top", "error"]) + f"\n\nResults: `{data['results_csv']}` · resume state: `{data['state']}`"
    nxt = [f"designaround batch screen {csv_file} --max-credits {budget}"] if c["failed"] or c["pending"] else []
    emit("batch_screen", data, markdown=md, next_actions=nxt, cost={"estimated": est, "charged": data["charged_this_run"]})


@command(run_app, "list")
def run_list() -> None:
    """Detached agent runs on this machine, newest first."""

    rows = runner.run_records()
    emit("runs", rows, markdown=md_table(rows[:30], ["run_id", "task", "case", "status", "outcome", "exit_code"]))


@command(run_app, "watch")
def run_watch(run_id: str = typer.Argument(..., help="Run id (a unique prefix is enough).")) -> None:
    """Follow a detached run until it ends; exits with the run's own exit code."""

    code, outcome = runner.watch(run_id)
    row = runner.run_record(run_id)
    data = {**{k: v for k, v in outcome.items() if k != "type"}, "run": row}
    if code == 0:
        emit("run_result", data, markdown=f"Run **{row['run_id']}** finished: {outcome.get('status') or outcome.get('type')}")
        return
    if outcome.get("type") == "result":
        raise CliExit(EXIT_NEEDS_HUMAN, outcome.get("status") or "needs_input", outcome.get("reason") or "The run needs your input.", data=data)
    raise CliExit(code if code in (3, 4, 5, 6) else EXIT_ERROR, outcome.get("code") or "run_failed", outcome.get("message") or "The run failed.", data=data)


@command(run_app, "status")
def run_status_cmd(run_id: str = typer.Argument(..., help="Run id (a unique prefix is enough).")) -> None:
    """A detached run's state right now (no waiting): recent progress, and the outcome when it is done."""

    data = runner.status(run_id)
    last = data["progress"][-1] if data["progress"] else {}
    md = f"Run **{data['run_id']}** · {data['task']} · {data['status']} · {data['elapsed_s']} s" + (f" · last: {last.get('type')}" if last else "") + (f"\n\nOutcome: {(data['outcome'] or {}).get('status') or (data['outcome'] or {}).get('code')}" if data["outcome"] else "")
    emit("run_status", data, markdown=md, next_actions=[] if data["done"] else [f"designaround run status {data['run_id']}", f"designaround run abort {data['run_id']}"])


@command(run_app, "abort")
def run_abort(run_id: str = typer.Argument(...)) -> None:
    """Stop a detached run (the server keeps what was already charged)."""

    data = runner.abort(run_id)
    emit("run_abort", data, markdown=f"Run **{data['run_id']}**: " + ("aborted" if data["aborted"] else f"not running ({data.get('status')})"))


@command(run_app, "guarantee")
def run_guarantee(run_id: str = typer.Argument(..., help="Run id from the run result (run-…)."), lang: Lang = typer.Option(Lang.zh, "--lang")) -> None:
    """Result guarantee for a design-around run: file a claim that could not be sent, or show the refund."""

    if config.model_mode() != "platform":
        raise CliExit(EXIT_INVALID, "platform_mode_needed", "The result guarantee refunds platform credits; BYOK and host runs spend none.")
    data = guarantee_mod.retry(run_id)
    line = guarantee_mod.describe(data, lang.value) or data.get("message") or ""
    if data.get("already_claimed") and data.get("refunded"):
        line = (f"结果保障：这次运行已经退回 {data['refunded']} 点。" if lang.value == "zh" else f"Result guarantee: {data['refunded']} credits were already refunded for this run.")
    md = line + ("\n\n" + "\n".join(f"- {b}" for b in data.get("blockers") or []) if data.get("blockers") else "")
    emit("guarantee", data, markdown=md, next_actions=["designaround credits"])


class FilingOffice(str, Enum):
    US = "US"
    CN = "CN"
    EU = "EU"
    JP = "JP"
    HAGUE = "HAGUE"


class NoticeKind(str, Enum):
    office_action = "office_action"
    allowance = "allowance"
    granted = "granted"


def _deadline_md(data: dict, lang: str, limit: int = 20) -> str:
    zh = lang != "en"
    rows = [d for d in data["deadlines"] if d["days_left"] >= -30][:limit]
    table = [{"due": d["due"], "left": d["days_left"], "office": d["office"], "what": d["title_zh"] if zh else d["title_en"], "basis": d["basis"][:40]} for d in rows]
    head = ""
    if data.get("next"):
        n = data["next"]
        head = (f"最近的期限：**{n['title_zh']}**，{n['due']}（还有 {n['days_left']} 天）\n\n" if zh else f"Next deadline: **{n['title_en']}** on {n['due']} ({n['days_left']} days)\n\n")
    tail = (f"\n\n日历文件（每个期限提前 {'/'.join(map(str, data['reminders_days']))} 天提醒）：`{data['ics']}`。期限以官方通知书为准，请执业代理人确认。" if zh else f"\n\nCalendar file (reminders {'/'.join(map(str, data['reminders_days']))} days before): `{data['ics']}`. Official notices prevail; have a practitioner confirm.")
    return head + (md_table(table, ["due", "left", "office", "what", "basis"]) if table else ("还没有期限：先记录申请日（`designaround filing record`）或公开日（`designaround disclose`）。" if zh else "No deadlines yet: record a filing (`designaround filing record`) or a disclosure (`designaround disclose`).")) + tail


@command(filing_app, "record")
def filing_record_cmd(
    case_id: str = typer.Argument(...),
    office: FilingOffice = typer.Option(..., "--office", "-o", help="US | CN | EU | JP | HAGUE (international application via WIPO)."),
    filed: str = typer.Option(..., "--filed", help="Filing date YYYY-MM-DD (for HAGUE: the international registration date)."),
    number: str | None = typer.Option(None, "--number", help="Application number."),
    version: str | None = typer.Option(None, "--version", help="Design version that was filed."),
    designate: str | None = typer.Option(None, "--designate", help="HAGUE only: designated offices, e.g. US,EU,CN (default: the case's jurisdictions)."),
    lang: Lang = typer.Option(Lang.zh, "--lang"),
) -> None:
    """Record a filing; priority, grace, renewal and fee deadlines are recomputed."""

    data = api.filing_record(case_id, office.value, filed, number=number, version=version, designated=[x.strip() for x in designate.split(",") if x.strip()] if designate else None)
    emit("deadlines", data, markdown=_deadline_md(data, lang.value), next_actions=[f"designaround filing notice {case_id} --office {office.value} --kind office_action --date <YYYY-MM-DD>", f"designaround deadlines {case_id}"])


@command(filing_app, "notice")
def filing_notice_cmd(
    case_id: str = typer.Argument(...),
    office: FilingOffice = typer.Option(..., "--office", "-o"),
    kind: NoticeKind = typer.Option(..., "--kind", help="office_action (审查意见/补正/irregularity), allowance (US notice of allowance, CN 办理登记手续通知书), granted (grant or registration date)."),
    issued: str = typer.Option(..., "--date", help="Date on the notice (dispatch / mailing date) YYYY-MM-DD."),
    due: str | None = typer.Option(None, "--due", help="The time limit printed on the notice; it overrides the computed one."),
    mailed: bool = typer.Option(False, "--mailed", help="CN: the notice came by post (deemed received 15 days after dispatch)."),
    number: str | None = typer.Option(None, "--number", help="Notice or patent number."),
    note: str | None = typer.Option(None),
    lang: Lang = typer.Option(Lang.zh, "--lang"),
) -> None:
    """Record an official notice; its response or fee deadline is added to the calendar."""

    data = api.notice_record(case_id, office.value, kind.value, issued, due=due, mailed=mailed, number=number, note=note)
    emit("deadlines", data, markdown=_deadline_md(data, lang.value), next_actions=[f"designaround deadlines {case_id}", f"designaround gate create {case_id} --type filing_submission --reviewer-email <practitioner>"])


@command(filing_app, "list")
def filing_list_cmd(case_id: str = typer.Argument(...)) -> None:
    """Filings and notices recorded on the case."""

    data = api.prosecution(case_id)
    md = md_table(data["filings"], ["office", "filed", "number", "version", "route"]) + "\n\n" + (md_table(data["notices"], ["office", "kind", "date", "due", "mailed", "number"]) if data["notices"] else "No notices recorded.")
    emit("prosecution", data, markdown=md, next_actions=[f"designaround deadlines {case_id}"])


@command(app, "deadlines")
def deadlines_cmd(
    case_id: str | None = typer.Argument(None, help="Case; leave out with --within to check every case."),
    ics: Path | None = typer.Option(None, "--ics", help="Where to write the calendar (default: <case>/calendar/deadlines.ics)."),
    within: int | None = typer.Option(None, "--within", min=1, max=365, help="All cases: hard deadlines in the next N days."),
    notify_: bool = typer.Option(False, "--notify", help="With --within: send them to the configured chat-bot webhook / desktop notification."),
    lang: Lang = typer.Option(Lang.zh, "--lang"),
) -> None:
    """Deadline calendar (FR-FILE-12): priority, grace periods, office-action replies, fees, renewals, Hague milestones; .ics with reminders."""

    if case_id is None:
        days = within or 30
        rows = api.upcoming_deadlines(days)
        zh = lang.value != "en"
        table = [{"due": r["due"], "left": r["days_left"], "case": r["case"], "office": r["office"], "what": r["title_zh"] if zh else r["title_en"]} for r in rows]
        sent = None
        if notify_ and rows:
            from .notify import notify

            title = f"DesignAround：{len(rows)} 个期限在 {days} 天内" if zh else f"DesignAround: {len(rows)} deadlines within {days} days"
            text = "；".join(f"{r['name'] or r['case']} {r['office']} {r['title_zh'] if zh else r['title_en']} {r['due']}（{r['days_left']} 天）" for r in rows[:5])
            sent = notify(title, text, {"deadlines": rows[:20]}, force_desktop=True)
        emit("upcoming_deadlines", {"within_days": days, "deadlines": rows, "notified": sent}, markdown=md_table(table, ["due", "left", "case", "office", "what"]) if table else (f"{days} 天内没有期限。" if zh else f"No deadlines in the next {days} days."))
        return
    data = api.deadlines(case_id, lang=lang.value, ics=str(ics) if ics else None)
    emit("deadlines", data, markdown=_deadline_md(data, lang.value), next_actions=[f"designaround filing record {case_id} --office CN --filed <YYYY-MM-DD> --number <no>"] if not data["filings"] else [])


class Platform(str, Enum):
    amazon = "amazon"
    temu = "temu"
    walmart = "walmart"
    shein = "shein"
    tiktok = "tiktok"
    ebay = "ebay"
    aliexpress = "aliexpress"
    court = "court"
    other = "other"


class RightKind(str, Enum):
    design_patent = "design_patent"
    utility_patent = "utility_patent"
    trademark = "trademark"
    copyright = "copyright"
    unknown = "unknown"


@command(defense_app, "intake")
def defense_intake_cmd(
    case_id: str = typer.Argument(...),
    platform: Platform = typer.Option(..., help="Where the complaint came from (court = a US lawsuit such as a Schedule A TRO)."),
    number: str | None = typer.Option(None, "--number", help="Complaint ID or court case number."),
    right: str | None = typer.Option(None, "--right", help="Asserted right, e.g. 'US D1,029,631 S'."),
    notice: Path | None = typer.Option(None, "--notice", help="The notice (PDF text is read; images are kept)."),
    kind: RightKind | None = typer.Option(None, "--kind", help="Override the detected right type."),
    received: str | None = typer.Option(None, help="Date received YYYY-MM-DD."),
    served: str | None = typer.Option(None, help="Lawsuits: date of service YYYY-MM-DD (answer due 21 days later)."),
    hearing: str | None = typer.Option(None, help="Hearing date from a court order YYYY-MM-DD."),
    respond_by: str | None = typer.Option(None, "--respond-by", help="Response deadline stated in a platform notice YYYY-MM-DD."),
    listing: list[str] = typer.Option(None, "--listing", help="Affected ASIN / listing ID (repeatable)."),
    note: str | None = typer.Option(None),
    fetch: bool = typer.Option(True, "--fetch/--no-fetch", help="US design patents: add the asserted patent as prior art with its USPTO drawings."),
    lang: Lang = typer.Option(Lang.zh, "--lang"),
) -> None:
    """Record a complaint or lawsuit; the right type and channel are determined (design patents never go through APEX)."""

    data = api.defense_intake(case_id, platform=platform.value, number=number, right=right, notice=str(notice) if notice else None, kind=kind.value if kind else None, received=received, served=served, hearing=hearing, respond_by=respond_by, listings=list(listing or []), note=note)
    linked = None
    if fetch and data["right_office"] == "US" and data["right_kind"] == "design_patent" and data.get("right"):
        from .screening import upgrade_us_figures

        existing = next((p for p in api.prior_art_list(case_id) if re.sub(r"[^A-Z0-9]", "", (p.get("number") or "").upper()) == data["right"]), None)
        pa = existing or api.prior_art_add(case_id, number=data["right"], office="US", title=None, figures=[], source="complaint")
        if not pa.get("figures"):
            upgrade_us_figures(case_id, [pa["id"]], max_sheets=4)
        data = api.defense_link(case_id, pa["id"], data["id"])
        linked = pa["id"]
    zh = lang.value == "zh"
    ch = data["channel"]
    md = f"**{ch['label_zh'] if zh else ch['label_en']}** · {data['right'] or '-'} ({data['right_kind']})" + ("" if not data["apex"] else " · APEX") + "\n\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(ch["steps_zh"] if zh else ch["steps_en"], 1))
    if data["notice_file"] and not data["notice_text_chars"]:
        md += "\n\n" + ("（通知是图片或扫描件，没能读取文字；权利类型按你填的专利号判断。）" if zh else "(The notice is an image or a scan; no text was read, the type follows the number you gave.)")
    target = target_version_for(case_id)
    nxt = []
    if linked and target:
        nxt.append(f"designaround observe {case_id} {target} {linked} --max-credits 120")
        nxt.append(f"designaround defense pack {case_id} {target}")
    elif not linked:
        nxt.append(f"designaround prior-art add {case_id} --number '<asserted no.>' -f figure.png  # then: designaround defense link {case_id} <prior-art-id>")
    nxt.append(f"designaround gate create {case_id} --type {ch['gate']} --reviewer-email <attorney>")
    emit("defense_intake", data, markdown=md, next_actions=nxt)


def target_version_for(case_id: str) -> str | None:
    from .monitor import target_version

    return target_version(open_case(case_id))


@command(defense_app, "link")
def defense_link_cmd(case_id: str = typer.Argument(...), prior: str = typer.Argument(..., help="Prior-art id of the asserted design (with figures)."), matter: str | None = typer.Option(None, help="Matter id (default: the latest).")) -> None:
    """Mark which prior-art record is the asserted design of a complaint."""

    emit("defense_matter", api.defense_link(case_id, prior, matter))


@command(defense_app, "list")
def defense_list_cmd(case_id: str = typer.Argument(...)) -> None:
    rows = api.defense_matters(case_id)
    emit("defense_matters", rows, markdown=md_table([{"matter": r["id"], "platform": r["platform"], "number": r.get("number"), "right": r.get("right"), "kind": r["right_kind"], "route": r["route"], "asserted": r.get("prior_art_id")} for r in rows], ["matter", "platform", "number", "right", "kind", "route", "asserted"]) if rows else "No complaints recorded.")


@command(defense_app, "pack")
def defense_pack_cmd(
    case_id: str = typer.Argument(...),
    version: str = typer.Argument(..., help="The version you sell (the accused product)."),
    matter: str | None = typer.Option(None, help="Matter id (default: the latest)."),
    prior: list[str] = typer.Option(None, "--prior", help="Earlier designs for the comparison (default: the closest ones dated before the asserted patent)."),
    seller: str | None = typer.Option(None, help="Seller name for the appeal draft."),
    lang: Lang = typer.Option(Lang.zh, "--lang"),
) -> None:
    """Response pack for counsel: three-way comparison, earlier-design search record, design timeline, appeal draft (PDF + DOCX)."""

    data = api.defense_pack(case_id, version, matter_id=matter, prior_ids=list(prior or []) or None, seller=seller, lang=lang.value)
    zh = lang.value == "zh"
    md = f"{'应对材料' if zh else 'Response pack'} → `{data['report']}`\n\n" + (f"{'三方对比' if zh else 'Three-way sheet'}: `{data['sheet']}` · {'申诉草稿' if zh else 'appeal draft'}: `{data['appeal_draft']}`") + (f"\n\n{'最早的设计记录' if zh else 'Earliest design record'}: {data['earliest_record']} · {'哈希链' if zh else 'chain'} {'OK' if data['chain_ok'] else 'BROKEN'}")
    if data["observer_missing"]:
        md += "\n\n" + ("还没有观察者评分：" if zh else "Observer score missing for: ") + ", ".join(data["observer_missing"])
    nxt = [f"designaround observe {case_id} {version} {pid} --max-credits 120" for pid in data["observer_missing"]][:3]
    nxt += [f"designaround gate create {case_id} --type {data['gate']} --reviewer-email <attorney>", f"designaround share create {case_id} {version} --to '<attorney>' --purpose other --file {data['report']}"]
    emit("defense_pack", data, markdown=md, next_actions=nxt)


class Purpose(str, Enum):
    factory = "factory"
    agent = "agent"
    other = "other"


def _when(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


@command(share_app, "create")
def share_create(
    case_id: str = typer.Argument(...),
    version: str | None = typer.Argument(None, help="Version to share (default: the newest non-draft version)."),
    to: str = typer.Option(..., "--to", help="Who receives it, e.g. '东莞某模具厂' or the practitioner's name."),
    purpose: Purpose = typer.Option(Purpose.factory, help="factory: prototype pack (STEP/STL + notes); agent: drawings and filing package; other: reports and views."),
    days: int = typer.Option(7, min=1, max=30, help="The link stops working after this many days."),
    note: str | None = typer.Option(None, help="A line shown on the share page."),
    file: list[Path] = typer.Option(None, "--file", help="Extra file of this case (repeatable). With --purpose other, only these files."),
    watermark: bool = typer.Option(True, "--watermark/--no-watermark", help="Watermark images and PDFs with the recipient and share id (3D files cannot be marked)."),
) -> None:
    """Upload chosen case files and get a read-only link that expires; every view is logged."""

    from . import share as share_mod

    data = share_mod.create(case_id, version, to=to, purpose=purpose.value, days=days, note=note, extra=list(file or []), watermark=watermark)
    rows = [{"file": f["shared_name"], "from": f["source"], "watermark": f["watermarked"], "KB": round(f["size"] / 1024)} for f in data["files"]]
    md = f"Share **{data['share_id']}** for **{to}** · expires {_when(data['expires_at'])}\n\n{data['url']}\n\n" + md_table(rows, ["file", "from", "watermark", "KB"]) + "\n\n_The link works for anyone who has it until it expires; send it only to the recipient. Views and downloads are logged._"
    emit("share", data, markdown=md, next_actions=[f"designaround share show {data['share_id']}", f"designaround share revoke {data['share_id']}"])


@command(share_app, "list")
def share_list(case_id: str | None = typer.Option(None, "--case", help="Only shares made from this case (with their links).")) -> None:
    """Your share links with status, expiry and how often they were opened."""

    rows = Server().get("/v1/shares", timeout=20)["data"]
    if case_id:
        folder = open_case(case_id).root / "shares"
        local = {}
        for path in folder.glob("*/share.json") if folder.exists() else []:
            record = json.loads(path.read_text(encoding="utf-8"))
            local[record["share_id"]] = record
        rows = [{**r, "url": local[r["share_id"]].get("url"), "version": local[r["share_id"]].get("version")} for r in rows if r["share_id"] in local]
    table = [{"share": r["share_id"], "title": r["title"], "to": r["recipient"], "status": r["status"], "expires": _when(r["expires_at"]), "views": r["views"], "downloads": r["downloads"], "last": _when(r["last_access"])} for r in rows]
    emit("shares", rows, markdown=md_table(table, ["share", "title", "to", "status", "expires", "views", "downloads", "last"]) if table else "No shares yet.")


@command(share_app, "show")
def share_show(share_id: str = typer.Argument(...)) -> None:
    """One share with its access log (time, view or download, truncated address, country)."""

    from . import share as share_mod

    data = Server().get(f"/v1/shares/{share_id}", timeout=20)["data"]
    local = share_mod.local_record(share_id)
    if local:
        data["url"] = local.get("url")
        data["case"] = local.get("case")
    log = [{"at": _when(a["at"]), "kind": a["kind"], "file": a.get("file") or "", "from": a.get("ip_prefix") or "", "country": a.get("country") or ""} for a in data.get("access") or []]
    md = f"Share **{data['share_id']}** · {data['status']} · for {data['recipient']} · expires {_when(data['expires_at'])} · {data['views']} views, {data['downloads']} downloads\n\n" + (md_table(log[:50], ["at", "kind", "file", "from", "country"]) if log else "Not opened yet.")
    emit("share", data, markdown=md, next_actions=[f"designaround share revoke {share_id}"] if data["status"] == "active" else [])


@command(share_app, "revoke")
def share_revoke(share_id: str = typer.Argument(...)) -> None:
    """Close a share now; its files are deleted from the server."""

    from . import share as share_mod

    data = share_mod.revoke(share_id)
    emit("share", data, markdown=f"Share **{share_id}** is {data['status']}; the link no longer works.")


@command(app, "mcp")
def mcp_cmd(print_config: bool = typer.Option(False, "--print-config", help="Show how to add this server to Claude Code, Codex, Cursor, Claude Desktop and VS Code.")) -> None:
    """MCP server on stdio for agent hosts (screen, design_around, run_status, viewpack, compare, case_list, decision_card)."""

    from . import mcp

    if print_config:
        cfg = mcp.host_configs()
        md = "\n\n".join(f"**{host}**\n\n```\n{snippet}\n```" for host, snippet in cfg.items())
        emit("mcp_config", cfg, markdown=md + "\n\nPaid tools need max_credits; the server runs with your CLI login (`designaround auth status`).")
        return
    mcp.main()


@command(app, "sample")
def sample_cmd(lang: Lang = typer.Option(Lang.zh, "--lang")) -> None:
    """Create a free sample case (illustrative prior art and scores) to try the whole flow without credits."""

    data = api.sample_case(lang=lang.value)
    card = api.decision_card(data["case"], lang=lang.value)
    emit("sample_case", {**data, "card": card}, markdown=f"Sample case **{data['case']}** created ({data['note']})\n\n{card['markdown']}", next_actions=[f"designaround case card {data['case']}", f"designaround report {data['case']} {data['candidate']}", f"designaround cad compare {data['case']} {data['original']} {data['candidate']}"])


@command(app, "files")
def files_cmd(case_id: str = typer.Argument(...)) -> None:
    """Deliverables of a case: reports, filing packages, view packs, prototype packs, baselines, evidence."""

    rows = api.deliverables(case_id)
    emit("deliverables", rows, markdown=md_table([{**r, "size": f"{r['bytes'] / 1e6:.1f} MB"} for r in rows], ["kind", "title", "files", "size", "path"]))


@command(app, "ids")
def ids_cmd(case_id: str = typer.Argument(...)) -> None:
    """US Information Disclosure Statement rows from the case prior art."""

    rows = api.ids_list(case_id)
    emit("ids", rows, markdown=md_table(rows, list(rows[0].keys()) if rows else ["number"]))


@command(app, "disclose")
def disclose_cmd(
    case_id: str = typer.Argument(...),
    kind: str = typer.Option(..., help="listing | social | exhibition | sale | crowdfunding | other"),
    when: str = typer.Option(..., "--date", help="First public disclosure date YYYY-MM-DD."),
    third_party: bool = typer.Option(False, "--third-party", help="Disclosed by someone else."),
    exception: str | None = typer.Option(None, help="Claimed exception (e.g. CN exhibition)."),
    note: str | None = typer.Option(None),
) -> None:
    """Record a public disclosure; returns grace-period findings per office."""

    data = api.disclose(case_id, kind, parse_date(when, "--date"), by_applicant=not third_party, exception=exception, note=note)
    emit("timeline", data)


@command(app, "timeline")
def timeline_cmd(case_id: str = typer.Argument(...), planned_date: str | None = typer.Option(None)) -> None:
    """Disclosure and grace-period findings per office."""

    data = api.timeline(case_id, parse_date(planned_date, "--planned-date"))
    md = "\n\n".join(f"### {office}\n" + findings_md(rows) for office, rows in data["findings"].items()) or "_(no offices)_"
    emit("timeline", data, markdown=md)


@command(app, "proto")
def proto_cmd(case_id: str = typer.Argument(...), version: str = typer.Argument(...), process: Process = typer.Option(Process.injection)) -> None:
    """Prototype pack for a factory: STEP/STL/3MF + Chinese PDF summary."""

    data = api.proto_pack(case_id, version, process.value)
    emit("proto", data, markdown=f"Prototype pack → {data['dir']} ({len(data['files'])} files, DFM {'pass' if data['dfm']['pass'] else 'fail'})")


# ----------------------------------------------------------------- photos


@command(photos_app, "add")
def photos_add(case_id: str = typer.Argument(...), photo: list[str] = typer.Argument(..., help="view=path (repeat).")) -> None:
    data = api.case_add_photos(case_id, {k: str(v) for k, v in parse_kv(photo, "photo").items()})
    emit("photos", data, next_actions=[f"designaround model {case_id} --agent --max-credits 3000", f"designaround photo-viewpack {case_id} --office CN"])


@command(photos_app, "check")
def photos_check(case_id: str = typer.Argument(...)) -> None:
    """Coverage and quality of the case photos."""

    photos = api.case_photos(case_id)
    emit("photos", {"photos": photos, "check": api.photos_mod_check(photos)})


@command(photos_app, "verify")
def photos_verify(case_id: str = typer.Argument(...), version: str = typer.Argument(...), min_score: float = typer.Option(0.72)) -> None:
    """Reconstruction gate: does the model's silhouette match the photos?"""

    photos = api.case_photos(case_id)
    if not photos:
        raise CliExit(EXIT_INVALID, "no_photos", "No photos on the case.")
    data = api.reconstruction_gate(case_id, version, photos, min_score=min_score)
    if not data["pass"]:
        raise CliExit(EXIT_NEEDS_HUMAN, "gate_failed", "The model does not match the photos well enough.", data=data, next_actions=[f"designaround cad tweak {case_id} {version} --set name=value", f"designaround cad sheet {case_id} {version}"])
    emit("reconstruction_gate", data)


# --------------------------------------------------------------- evidence


@command(evidence_app, "export")
def evidence_export(case_id: str = typer.Argument(...), out: Path | None = typer.Option(None)) -> None:
    """Zip of all case files + the hash-chained event log."""

    emit("evidence_export", api.evidence_export(case_id, str(out) if out else None))


@command(evidence_app, "anchor")
def evidence_anchor(case_id: str = typer.Argument(...)) -> None:
    """Send the current head hash to the server for a signed timestamp receipt (no case content leaves the machine)."""

    head = api.evidence_head(case_id)
    if not head["chain_ok"]:
        raise CliExit(EXIT_ERROR, "chain_broken", "Local evidence chain is broken; not anchoring.", data=head)
    if not head["seq"]:
        raise CliExit(EXIT_INVALID, "no_events", "Case has no events yet.")
    body = Server().post("/v1/evidence/anchor", {"case_id": case_id, "seq": head["seq"], "head_hash": head["head_hash"]})
    data = {**head, **body["data"]}
    open_case(case_id).event("evidence.anchored", {"seq": head["seq"], "head_hash": head["head_hash"], "server_ts": data.get("server_ts"), "receipt": data.get("receipt")})
    emit("anchor", data)


@command(evidence_app, "verify")
def evidence_verify(case_id: str = typer.Argument(...), offline: bool = typer.Option(False, help="Skip the server anchor check.")) -> None:
    """Verify the local chain and compare it with server anchors."""

    case = open_case(case_id)
    events = case.log.read()
    ok, bad = case.log.verify()
    data: dict = {"case": case_id, "events": len(events), "chain_ok": ok, "first_bad_index": bad}
    if not offline and config.api_key():
        anchors = Server().get("/v1/evidence/anchors", params={"case_id": case_id})["data"]
        checks = []
        for a in anchors:
            local = events[a["seq"] - 1]["hash"] if 0 < a["seq"] <= len(events) else None
            checks.append({"seq": a["seq"], "server_ts": a["server_ts"], "match": local == a["head_hash"]})
        data["anchors"] = checks
        data["anchors_ok"] = all(c["match"] for c in checks)
    if not ok or data.get("anchors_ok") is False:
        raise CliExit(EXIT_ERROR, "evidence_mismatch", "Evidence does not verify.", data=data)
    emit("evidence_verify", data, markdown=f"Chain OK · {len(events)} events" + (f" · {len(data['anchors'])} anchors match" if data.get("anchors") else ""))


# ------------------------------------------------------------------ gates


GATE_TYPES = ["filing_submission", "design_review", "fto_conclusion", "platform_appeal", "tro_response", "representation"]


@command(gate_app, "create")
def gate_create(case_id: str = typer.Argument(...), type_: str = typer.Option(..., "--type", help=" | ".join(GATE_TYPES)), reviewer_email: str | None = typer.Option(None), summary_file: Path | None = typer.Option(None, help="JSON summary for the reviewer (default: built from the case).")) -> None:
    """Ask a registered practitioner to approve or return a step."""

    if type_ not in GATE_TYPES:
        raise CliExit(EXIT_INVALID, "bad_gate_type", f"type must be one of {', '.join(GATE_TYPES)}")
    if summary_file:
        summary = json.loads(summary_file.read_text(encoding="utf-8"))
    else:
        info = api.case_show(case_id)
        summary = {"case": info.get("name"), "article": info.get("article"), "jurisdictions": info.get("jurisdictions"), "versions": [v for v in info["versions"] if v.get("kind") == "original" or v.get("qualified")], "prior_art": info["prior_art"][:20], "evidence_head": api.evidence_head(case_id)}
    body = Server().post("/v1/gates", {"case_id": case_id, "type": type_, "summary": summary, "reviewer_email": reviewer_email})
    data = body["data"]
    open_case(case_id).event("gate.created", {"gate_id": data["id"], "type": type_})
    emit("gate", data, markdown=f"Gate **{data['id']}** ({type_}) created. Send this link to the practitioner: {data['review_url']}", next_actions=body.get("next_actions"))


@command(gate_app, "status")
def gate_status(gate_id: str = typer.Argument(...)) -> None:
    data = Server().get(f"/v1/gates/{gate_id}")["data"]
    if data.get("status") == "pending":
        emit("gate", data, next_actions=[f"designaround gate status {gate_id}  # waiting for the practitioner"])
        return
    emit("gate", data)


# ---------------------------------------------------------------- billing


CREDIT_ALIASES = {"5k": "credits_5k", "5000": "credits_5k", "21k": "credits_21k", "21000": "credits_21k", "55k": "credits_55k", "55000": "credits_55k", "240k": "credits_240k", "240000": "credits_240k"}


@command(billing_app, "plans")
def billing_plans() -> None:
    """Plans, credit packs and action prices (USD)."""

    try:
        data = Server(api_key="").get("/v1/catalog", auth=False, timeout=10)["data"]
    except CliExit:
        data = catalog.public_catalog()
    rows = [{"plan": k, "monthly_usd": v["monthly_usd"], "annual_usd": v["annual_usd"], "credits_per_month": v["monthly_credits"] if k != "free" else "starter credits once"} for k, v in data["plans"].items() if k in ("free", "pro", "team")]
    packs = [{"sku": k, "credits": v["credits"], "usd": v["usd"]} for k, v in data["credit_packs"].items()]
    emit("catalog", data, markdown=md_table(rows, ["plan", "monthly_usd", "annual_usd", "credits_per_month"]) + "\n\n" + md_table(packs, ["sku", "credits", "usd"]))


@command(billing_app, "checkout")
def billing_checkout(
    plan: str | None = typer.Option(None, help="pro | team"),
    annual: bool = typer.Option(False, help="Annual billing (two months free)."),
    credits: str | None = typer.Option(None, help="Credit pack: 5k | 21k | 55k | 240k"),
    package: str | None = typer.Option(None, hidden=True, help="Managed package SKU (only where the deployment sells them)."),
    open_browser: bool = typer.Option(False, "--open", help="Open the checkout page (interactive use only)."),
) -> None:
    """Create a Creem checkout. Prints the URL; a person completes payment in the browser."""

    chosen = [x for x in (plan, credits, package) if x]
    if len(chosen) != 1:
        raise CliExit(EXIT_INVALID, "choose_one", "Pass exactly one of --plan or --credits.")
    note = None
    if plan:
        if plan not in ("pro", "team"):
            raise CliExit(EXIT_INVALID, "bad_plan", "Self-serve plans are pro and team; contact sales for enterprise.")
        sku = f"plan:{plan}:{'annual' if annual else 'monthly'}"
    elif credits:
        sku = CREDIT_ALIASES.get(credits.lower().removeprefix("credits_"), credits)
        if sku not in catalog.CREDIT_PACKS:
            raise CliExit(EXIT_INVALID, "bad_pack", f"Unknown credit pack {credits}", data={"packs": sorted(catalog.CREDIT_PACKS)})
    else:
        sku = package
        if sku not in catalog.MANAGED_PACKAGES:
            raise CliExit(EXIT_INVALID, "bad_package", f"Unknown package {package}", data={"packages": sorted(catalog.MANAGED_PACKAGES)})
        pkg = catalog.MANAGED_PACKAGES[sku]
        note = f"This checkout covers the platform fee (${pkg['platform_usd']}). The practitioner fee (about ${pkg['practitioner_usd_est']}) and official fees are paid directly to the practitioner / office, not through DesignAround."
    body = Server().post("/v1/billing/checkout", {"sku": sku})
    data = {**body["data"], **({"note": note} if note else {})}
    if open_browser and not config.agent_mode() and data.get("checkout_url"):
        import webbrowser

        webbrowser.open(data["checkout_url"])
    emit("checkout", data, markdown=f"Checkout for **{sku}** (${data.get('amount_usd')}): {data.get('checkout_url')}" + (f"\n\n{note}" if note else ""), next_actions=["A person opens checkout_url and pays; agents cannot pay.", f"designaround billing order {data.get('order_id')}"])


@command(billing_app, "portal")
def billing_portal(open_browser: bool = typer.Option(False, "--open", help="Open the portal in the browser (interactive use only).")) -> None:
    """Manage or cancel your subscription, update the card, download receipts (Creem customer portal)."""

    body = Server().post("/v1/billing/portal", {})
    url = body["data"]["url"]
    if open_browser and not config.agent_mode():
        import webbrowser

        webbrowser.open(url)
    emit("billing_portal", {"url": url}, markdown=f"Billing portal: {url}", next_actions=body.get("next_actions"))


@command(billing_app, "order")
def billing_order(order_id: str = typer.Argument(...)) -> None:
    data = Server().get(f"/v1/orders/{order_id}")["data"]
    if data.get("status") == "paid":
        config.cache_entitlements(Server().get("/v1/me")["data"])
    emit("order", data)


# ------------------------------------------------------------------ skill


@command(skill_app, "targets")
def skill_targets() -> None:
    rows = [{"target": k, "user_path": f"~/{v}", "project_path": f"./{v}"} for k, v in bundles.SKILL_TARGETS.items()]
    emit("skill_targets", rows, markdown=md_table(rows, ["target", "user_path", "project_path"]))


@command(skill_app, "install")
def skill_install(target: str = typer.Argument(..., help="claude-code | cursor | codex | gemini-cli | opencode | openclaw | kiro | all"), project: bool = typer.Option(False, "--project", help="Install into the current project instead of the user home."), overwrite: bool = typer.Option(False)) -> None:
    """Copy the DesignAround Skill into an agent host."""

    targets = list(bundles.SKILL_TARGETS) if target == "all" else [target]
    rows = [bundles.install_skill(t, project=project, overwrite=overwrite) for t in targets]
    emit("skill_install", rows if target == "all" else rows[0], markdown="\n".join(f"Installed for **{r['target']}** → {r['path']}" for r in rows))


@command(skill_app, "path")
def skill_path() -> None:
    emit("skill_path", {"path": str(bundles.skill_dir())})


# ------------------------------------------------------------------ agent


def _assistant_env(mode: str, max_credits: int) -> dict:
    if mode == "host":
        raise CliExit(EXIT_NEEDS_HUMAN, "host_mode", "The built-in assistant needs a model: switch to platform or byok mode. In host mode your agent host (Claude Code, Codex, Kiro, …) is the assistant.", next_actions=["designaround config mode platform", "designaround config byok --provider <id> --model <model>"])
    env = runner.child_env(mode)
    env["DA_MAX_CREDITS"] = str(int(max_credits))
    if mode == "platform":
        env["DESIGNAROUND_BASE_URL"] = config.server_url()
    if os.environ.get("DESIGNAROUND_NO_KEYRING"):
        env["DESIGNAROUND_NO_KEYRING"] = "1"
    return env


@command(app, "ui")
def ui_cmd(
    port: int = typer.Option(0, help="Port on 127.0.0.1 (0 = any free port)."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Print the link instead of opening the browser."),
    max_credits: int = typer.Option(3000, help="Model credit cap for the assistant's own conversation (tools have their own caps)."),
    auto_approve: int = typer.Option(0, help="Paid steps up to this many credits run without asking (0 = always ask)."),
) -> None:
    """Open the chat workbench: talk to the DesignAround Assistant (embedded, isolated Pi agent)."""

    import subprocess
    import webbrowser

    mode = config.model_mode()
    env = _assistant_env(mode, max_credits)
    env["DA_UI_AUTO_APPROVE"] = str(max(0, auto_approve))
    info, path = runner.script("ui-server.ts")
    # The workbench exits when this process goes away (its stdin pipe closes), so a
    # killed desktop shell or terminal never leaves an orphaned server behind.
    env["DA_UI_WATCH_STDIN"] = "1"
    proc = subprocess.Popen([info["node"], path, "--port", str(port)], cwd=info["agents_dir"], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None, text=True, creationflags=runner.console_flags())
    assert proc.stdout is not None
    if os.environ.get("DA_UI_WATCH_STDIN") == "1":
        import threading

        def _watch_parent() -> None:  # the desktop shell holds our stdin; EOF means it is gone
            try:
                while sys.stdin.read(4096):
                    pass
            except (OSError, ValueError):
                pass
            proc.terminate()

        threading.Thread(target=_watch_parent, daemon=True).start()
    first = proc.stdout.readline()
    try:
        ready = json.loads(first)
    except ValueError:
        proc.terminate()
        raise CliExit(EXIT_ERROR, "ui_failed", f"Workbench did not start: {first[:500]}") from None
    if ready.get("type") != "ui_ready":
        proc.wait(timeout=10)
        raise CliExit(proc.returncode or EXIT_ERROR, ready.get("code") or "ui_failed", ready.get("message") or "Workbench did not start.", next_actions=["designaround auth status", "designaround agent check"])
    url = ready["url"]
    if STATE["json"]:
        emit("ui", {"url": url, "pid": proc.pid, "mode": mode, "models": ready.get("models"), "pi_agent_dir": ready.get("pi_agent_dir")})
    else:
        sys.stderr.write(f"DesignAround workbench: {url}\n(mode {mode} · assistant {ready.get('models', {}).get('assistant')} · Ctrl+C to stop)\n")
    if not no_browser and not STATE["json"]:
        webbrowser.open(url)
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait(timeout=10)


@command(app, "chat")
def chat_cmd(
    message: str | None = typer.Option(None, "--message", "-m", help="Send one message and exit (paid steps are declined unless --yes)."),
    session: str | None = typer.Option(None, help="Resume a chat session by id (prefix)."),
    resume: bool = typer.Option(False, "--continue", "-c", help="Resume the most recent chat."),
    yes: bool = typer.Option(False, "--yes", help="Approve paid steps without asking (they still respect their own caps)."),
    max_credits: int = typer.Option(3000, help="Model credit cap for the conversation."),
) -> None:
    """Chat with the DesignAround Assistant in the terminal."""

    import subprocess

    mode = config.model_mode()
    env = _assistant_env(mode, max_credits)
    if yes:
        env["DA_CHAT_AUTO_APPROVE"] = "1"
    info, path = runner.script("chat-cli.ts")
    args = [info["node"], path]
    if message is not None:
        args += ["--message", message]
    if session:
        args += ["--session", session]
    if resume:
        args.append("--continue")
    try:
        code = subprocess.call(args, cwd=info["agents_dir"], env=env)
    except KeyboardInterrupt:
        code = 130
    if code:
        raise typer.Exit(code)


@command(agent_app, "setup")
def agent_setup() -> None:
    """Install the pinned agent runtime into ~/.designaround/agent-runtime (never touches ~/.pi)."""

    data = bundles.setup_agents()
    emit("agent_setup", {**data, **runner.check_runtime()})


@command(agent_app, "check")
def agent_check() -> None:
    """Node version, runtime files and Pi isolation."""

    info = runner.check_runtime()
    import subprocess

    env = {k: v for k, v in os.environ.items() if not k.startswith("PI_")}
    env["DESIGNAROUND_HOME"] = str(config.home())
    proc = subprocess.run([info["node"], info["main"], "isolation-check"], cwd=info["agents_dir"], env=env, capture_output=True, text=True, check=False, timeout=60, creationflags=runner.NO_WINDOW)
    line = next((ln for ln in proc.stdout.splitlines() if ln.strip().startswith("{")), "{}")
    iso = json.loads(line)
    expected = str(Path(config.home()) / "pi-agent")
    isolated = iso.get("pi_agent_dir") == expected and (iso.get("env") or {}).get("PI_CODING_AGENT_DIR") == expected and (iso.get("env") or {}).get("PI_OFFLINE") == "1"
    data = {**info, "isolation": iso, "isolated": isolated, "model_mode": config.model_mode()}
    if not isolated:
        raise CliExit(EXIT_ERROR, "not_isolated", "The agent runtime is not isolated from the local Pi configuration.", data=data)
    emit("agent_check", data, markdown=f"Node {info['node_version']} · runtime {info['agents_dir']} · Pi config dir {iso.get('pi_agent_dir')} (isolated)")


# ------------------------------------------------------------------ entry


GLOBAL_FLAGS = ("--json", "--human", "--quiet", "-q")


def normalize_argv(argv: list[str]) -> list[str]:
    """Allow global flags anywhere (`da case list --json`), as agents often write them last."""

    if "--" in argv:
        cut = argv.index("--")
        head, tail = argv[:cut], argv[cut:]
    else:
        head, tail = argv, []
    flags = [a for a in head if a in GLOBAL_FLAGS]
    rest = [a for a in head if a not in GLOBAL_FLAGS]
    return flags + rest + tail


def run() -> None:
    args = normalize_argv(sys.argv[1:])
    try:
        app(args=args, prog_name=Path(sys.argv[0]).name or "designaround")
    except SystemExit as exc:
        raise SystemExit(exc.code) from None


if __name__ == "__main__":
    run()
