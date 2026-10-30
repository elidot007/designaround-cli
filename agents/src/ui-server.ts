#!/usr/bin/env node
/**
 * Local chat workbench for the DesignAround Assistant (also the desktop app UI).
 *
 *   node src/ui-server.ts [--port 0] [--host 127.0.0.1]
 *
 * Prints one JSON line {"type":"ui_ready","url":...} when listening.
 *
 * Security: binds to loopback only; every request needs the per-launch token
 * (cookie set from the ?t= launch URL, or X-DA-Token header) and a loopback
 * Host header (DNS-rebinding guard). Files are served only from inside
 * DESIGNAROUND_HOME. The Pi runtime is the isolated one from env.ts.
 */
import "./env.ts";
import { spawn } from "node:child_process";
import { randomBytes, randomUUID } from "node:crypto";
import { createReadStream, existsSync, mkdirSync, readFileSync, realpathSync, statSync, writeFileSync } from "node:fs";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { basename, dirname, extname, join, resolve, sep } from "node:path";
import { parseArgs } from "node:util";
import { type AssistantDeps, listChatSessions, openAssistant, runCli, transcript, WORDING_NOTE, wordingIssues } from "./assistant.ts";
import { DESIGNAROUND_HOME, PI_AGENT_DIR } from "./env.ts";
import { configFromEnv, createModelRuntime } from "./runtime.ts";

const UI_DIR = resolve(import.meta.dirname, "..", "ui");
const UPLOAD_DIR = join(DESIGNAROUND_HOME, "uploads");
const MAX_UPLOAD = 25 * 1024 * 1024;
const MIME: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
  ".svg": "image/svg+xml",
  ".woff2": "font/woff2",
  ".pdf": "application/pdf",
  ".json": "application/json",
  ".md": "text/markdown; charset=utf-8",
  ".step": "application/step",
  ".stp": "application/step",
  ".stl": "model/stl",
  ".3mf": "model/3mf",
  ".glb": "model/gltf-binary",
  ".zip": "application/zip",
  ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ".csv": "text/csv; charset=utf-8",
  ".ics": "text/calendar; charset=utf-8",
};
const INLINE = new Set([".png", ".jpg", ".jpeg", ".webp", ".pdf"]);

type Entry = {
  id: string;
  session: any;
  subscribers: Set<ServerResponse>;
  unsubscribe: () => void;
  spentCredits: number;
};

function send(res: ServerResponse, status: number, body: unknown, headers: Record<string, string> = {}) {
  const data = typeof body === "string" ? body : JSON.stringify(body);
  res.writeHead(status, { "Content-Type": typeof body === "string" ? "text/plain; charset=utf-8" : "application/json", "Cache-Control": "no-store", ...headers });
  res.end(data);
}

/** Interface language the page asked for (X-DA-Lang), for texts the CLI writes. */
function uiLang(req: IncomingMessage): "zh" | "en" {
  return String(req.headers["x-da-lang"] || "").toLowerCase() === "en" ? "en" : "zh";
}

async function readBody(req: IncomingMessage, limit: number): Promise<Buffer> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    size += (chunk as Buffer).length;
    if (size > limit) throw Object.assign(new Error("payload too large"), { status: 413 });
    chunks.push(chunk as Buffer);
  }
  return Buffer.concat(chunks);
}

async function main() {
  const { values } = parseArgs({ options: { port: { type: "string" }, host: { type: "string" } } });
  const host = values.host || "127.0.0.1";
  if (!["127.0.0.1", "localhost", "::1"].includes(host)) throw new Error("the workbench only binds to loopback");
  const token = process.env.DA_UI_TOKEN || randomBytes(24).toString("base64url");
  const cfg = configFromEnv();
  const { runtime, choice } = await createModelRuntime(cfg);
  const python = process.env.DA_PYTHON || "python3";
  const settings = { autoApproveCredits: Number(process.env.DA_UI_AUTO_APPROVE || 0), chatBudget: Number(process.env.DA_MAX_CREDITS || 3000) };
  process.env.DA_RUN_ID = `chat-${randomUUID()}`;
  process.env.DA_MAX_CREDITS = String(settings.chatBudget);

  const entries = new Map<string, Entry>();
  const pendingConfirms = new Map<string, { resolve: (ok: boolean) => void; sessionId: string; request: any }>();

  const cliEnv: NodeJS.ProcessEnv = {};
  for (const key of ["DESIGNAROUND_API_KEY", "DESIGNAROUND_BASE_URL", "DA_BYOK_KEY", "DA_BYOK_PROVIDER", "DA_BYOK_MODEL", "DA_BYOK_OBSERVER_MODEL", "DA_BYOK_BASE_URL", "DESIGNAROUND_NO_KEYRING"]) {
    if (process.env[key]) cliEnv[key] = process.env[key];
  }
  if (process.env.DA_SERVER_URL && !cliEnv.DESIGNAROUND_BASE_URL) cliEnv.DESIGNAROUND_BASE_URL = process.env.DA_SERVER_URL;

  function broadcast(entry: Entry, event: Record<string, unknown>) {
    const line = `data: ${JSON.stringify({ ...event, session: entry.id })}\n\n`;
    for (const res of entry.subscribers) res.write(line);
  }

  function depsFor(entryRef: { current?: Entry }): AssistantDeps {
    return {
      python,
      mode: cfg.mode,
      env: cliEnv,
      host: {
        confirm: (request) =>
          new Promise<boolean>((resolveConfirm) => {
            const entry = entryRef.current;
            if (!request.always && request.credits <= settings.autoApproveCredits) return resolveConfirm(true);
            if (!entry) return resolveConfirm(false);
            const id = randomUUID();
            const timer = setTimeout(() => {
              pendingConfirms.delete(id);
              broadcast(entry, { type: "confirm_closed", id, approved: false, reason: "timeout" });
              resolveConfirm(false);
            }, 15 * 60 * 1000);
            pendingConfirms.set(id, {
              sessionId: entry.id,
              request,
              resolve: (ok) => {
                clearTimeout(timer);
                pendingConfirms.delete(id);
                broadcast(entry, { type: "confirm_closed", id, approved: ok });
                resolveConfirm(ok);
              },
            });
            broadcast(entry, { type: "confirm", id, ...request });
          }),
        progress: (toolCallId, line) => {
          const entry = entryRef.current;
          if (entry) broadcast(entry, { type: "tool_progress", id: toolCallId, line });
        },
      },
    };
  }

  function rateOf(model: string) {
    return choice.rates[model];
  }

  async function attach(sessionPath?: string): Promise<Entry> {
    const ref: { current?: Entry } = {};
    const session = await openAssistant({ runtime, choice, deps: depsFor(ref), sessionPath });
    const entry: Entry = { id: session.sessionId, session, subscribers: new Set(), unsubscribe: () => {}, spentCredits: 0 };
    ref.current = entry;
    entry.unsubscribe = session.subscribe((ev: any) => {
      switch (ev.type) {
        case "message_start":
          if (ev.message?.role === "assistant") broadcast(entry, { type: "assistant_start" });
          break;
        case "message_update": {
          const a = ev.assistantMessageEvent;
          if (a?.type === "text_delta") broadcast(entry, { type: "text_delta", delta: a.delta });
          else if (a?.type === "thinking_start") broadcast(entry, { type: "thinking" });
          break;
        }
        case "message_end":
          if (ev.message?.role === "assistant") {
            const usage = ev.message.usage;
            const rate = rateOf(choice.designer);
            const credits = rate && usage ? Math.ceil(((usage.input ?? 0) * rate.input + (usage.output ?? 0) * rate.output) / 1_000_000) : 0;
            entry.spentCredits += credits;
            const text = (ev.message.content ?? []).filter((c: any) => c.type === "text").map((c: any) => c.text).join("");
            const issues = wordingIssues(text);
            broadcast(entry, { type: "assistant_end", text, error: ev.message.stopReason === "error" ? ev.message.errorMessage : undefined, credits, chat_spent: entry.spentCredits, ...(issues.length ? { wording: { phrases: issues, note: /[\u4e00-\u9fff]/.test(text) ? WORDING_NOTE.zh : WORDING_NOTE.en } } : {}) });
          }
          break;
        case "tool_execution_start":
          broadcast(entry, { type: "tool_start", id: ev.toolCallId, name: ev.toolName, args: ev.args });
          break;
        case "tool_execution_end": {
          const d = ev.result?.details ?? {};
          const text = (ev.result?.content ?? []).filter((c: any) => c.type === "text").map((c: any) => c.text).join("\n");
          broadcast(entry, { type: "tool_end", id: ev.toolCallId, name: ev.toolName, ok: d.ok ?? !ev.isError, declined: !!d.declined, images: d.images ?? [], cost: d.cost ?? null, text: text.slice(0, 1500), ms: ev.durationMs });
          break;
        }
        case "auto_retry_start":
          broadcast(entry, { type: "retry", attempt: ev.attempt, max: ev.maxAttempts, message: ev.errorMessage });
          break;
        case "agent_settled":
          broadcast(entry, { type: "settled", aborted: ev.aborted });
          break;
      }
    });
    entries.set(entry.id, entry);
    return entry;
  }

  async function entryFor(id: string): Promise<Entry | undefined> {
    const existing = entries.get(id);
    if (existing) return existing;
    const info = (await listChatSessions()).find((s) => s.id === id);
    if (!info) return undefined;
    return attach(info.path);
  }

  function allowedHost(req: IncomingMessage, port: number) {
    const h = (req.headers.host || "").toLowerCase();
    return [`127.0.0.1:${port}`, `localhost:${port}`, `[::1]:${port}`].includes(h);
  }

  function authed(req: IncomingMessage) {
    const cookie = req.headers.cookie || "";
    const fromCookie = cookie.split(";").map((c) => c.trim()).find((c) => c.startsWith("da_ui="))?.slice(6);
    return fromCookie === token || req.headers["x-da-token"] === token;
  }

  function safeFile(p: string): string | null {
    try {
      const home = realpathSync(DESIGNAROUND_HOME);
      const real = realpathSync(p);
      if (!(real === home || real.startsWith(home + sep))) return null;
      if (real.startsWith(realpathSync(PI_AGENT_DIR) + sep)) return null; // never serve the agent config / transcripts raw
      if (!statSync(real).isFile() || !MIME[extname(real).toLowerCase()]) return null;
      return real;
    } catch {
      return null;
    }
  }

  /** A file or folder inside DESIGNAROUND_HOME (not the agent config), for "show in folder". */
  function safePath(p: string): string | null {
    try {
      const home = realpathSync(DESIGNAROUND_HOME);
      const real = realpathSync(p);
      if (!(real === home || real.startsWith(home + sep))) return null;
      if (real === realpathSync(PI_AGENT_DIR) || real.startsWith(realpathSync(PI_AGENT_DIR) + sep)) return null;
      return real;
    } catch {
      return null;
    }
  }

  async function cli(args: string[]) {
    return runCli({ python, mode: cfg.mode, env: cliEnv, host: { confirm: async () => false } }, args);
  }

  const server = createServer(async (req, res) => {
    const port = (server.address() as any)?.port;
    try {
      if (!allowedHost(req, port)) return send(res, 421, "bad host");
      const url = new URL(req.url || "/", `http://127.0.0.1:${port}`);
      const path = url.pathname;
      if (path === "/" && url.searchParams.get("t")) {
        if (url.searchParams.get("t") !== token) return send(res, 403, "bad token");
        // Set the cookie on a 200 page that then navigates to "/" itself. A 302 does not
        // work in the desktop app: WebKit treats the redirect after a navigation that
        // started on tauri://localhost as cross-site and leaves out the SameSite=Strict
        // cookie, so "/" answered 401. The refresh starts on 127.0.0.1, so it is same-site.
        res.writeHead(200, {
          "Content-Type": "text/html; charset=utf-8",
          "Set-Cookie": `da_ui=${token}; HttpOnly; SameSite=Strict; Path=/`,
          "Cache-Control": "no-store",
          "Referrer-Policy": "no-referrer",
          "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
        });
        return res.end('<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=/"><title>DesignAround</title>');
      }
      if (!authed(req)) return send(res, 401, "Open the workbench from `designaround ui` (the launch link carries the access token).");

      if (req.method === "GET" && (path === "/" || path.startsWith("/static/"))) {
        // /static/<file> or /static/<fonts|art>/<file>: fixed folders only, no traversal.
        const rel = path.slice("/static/".length).split("/");
        const sub = rel.length === 2 && ["fonts", "art"].includes(rel[0]) ? rel[0] : null;
        const file = path === "/" ? join(UI_DIR, "index.html") : sub ? join(UI_DIR, sub, basename(rel[1])) : join(UI_DIR, basename(path));
        if (!existsSync(file)) return send(res, 404, "not found");
        res.writeHead(200, {
          "Content-Type": MIME[extname(file)] || "application/octet-stream",
          "Cache-Control": sub ? "max-age=86400" : "no-store",
          "Content-Security-Policy": "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
          "X-Content-Type-Options": "nosniff",
        });
        return res.end(readFileSync(file));
      }

      if (path === "/api/state" && req.method === "GET") {
        return send(res, 200, { mode: cfg.mode, provider: choice.provider, models: { assistant: choice.designer, observer: choice.observer }, settings, home: DESIGNAROUND_HOME, pi_agent_dir: PI_AGENT_DIR, isolated: process.env.PI_CODING_AGENT_DIR === PI_AGENT_DIR, pending_confirms: [...pendingConfirms.entries()].map(([id, c]) => ({ id, session: c.sessionId, ...c.request })) });
      }
      if (path === "/api/notify-settings" && req.method === "GET") {
        const r = await cli(["config", "show"]);
        const n = r.envelope?.data?.notify ?? {};
        return send(res, 200, { webhook: n.webhook ?? "", desktop: !!n.desktop });
      }
      if (path === "/api/notify-settings" && req.method === "PUT") {
        const body = JSON.parse((await readBody(req, 4_000)).toString() || "{}");
        const webhook = String(body.webhook ?? "").trim();
        if (webhook && !/^https:\/\/[^\s]+$/.test(webhook)) return send(res, 400, { message: uiLang(req) === "en" ? "The webhook must be an https URL." : "Webhook 必须是 https 地址。" });
        const r = await cli(["config", "notify", "--webhook", webhook, body.desktop ? "--desktop" : "--no-desktop"]);
        return r.envelope?.ok ? send(res, 200, { ok: true }) : send(res, 400, { message: r.envelope?.data?.message ?? (uiLang(req) === "en" ? "Could not save." : "保存失败") });
      }
      if (path === "/api/settings" && req.method === "PUT") {
        const body = JSON.parse((await readBody(req, 10_000)).toString() || "{}");
        if (Number.isFinite(body.autoApproveCredits) && body.autoApproveCredits >= 0) settings.autoApproveCredits = Math.floor(body.autoApproveCredits);
        if (Number.isFinite(body.chatBudget) && body.chatBudget > 0) {
          settings.chatBudget = Math.floor(body.chatBudget);
          process.env.DA_MAX_CREDITS = String(settings.chatBudget);
          process.env.DA_RUN_ID = `chat-${randomUUID()}`; // the server keeps the smaller cap per run id
        }
        return send(res, 200, settings);
      }
      if (path === "/api/sessions" && req.method === "GET") return send(res, 200, await listChatSessions());
      if (path === "/api/sessions" && req.method === "POST") {
        const entry = await attach();
        return send(res, 200, { id: entry.id });
      }
      const sessionMatch = path.match(/^\/api\/sessions\/([A-Za-z0-9_-]+)(?:\/(messages|events|abort))?$/);
      if (sessionMatch) {
        const entry = await entryFor(sessionMatch[1]);
        if (!entry) return send(res, 404, { error: "unknown session" });
        const action = sessionMatch[2];
        if (!action && req.method === "GET") return send(res, 200, { id: entry.id, name: entry.session.sessionName, busy: entry.session.isStreaming, items: transcript(entry.session.messages) });
        if (action === "events" && req.method === "GET") {
          res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache, no-transform", Connection: "keep-alive" });
          res.write(`data: ${JSON.stringify({ type: "hello", session: entry.id, busy: entry.session.isStreaming })}\n\n`);
          for (const [id, c] of pendingConfirms) if (c.sessionId === entry.id) res.write(`data: ${JSON.stringify({ type: "confirm", id, session: entry.id, ...c.request })}\n\n`);
          entry.subscribers.add(res);
          const ping = setInterval(() => res.write(": ping\n\n"), 20_000);
          req.on("close", () => {
            clearInterval(ping);
            entry.subscribers.delete(res);
          });
          return;
        }
        if (action === "abort" && req.method === "POST") {
          for (const [, c] of pendingConfirms) if (c.sessionId === entry.id) c.resolve(false);
          await entry.session.abort();
          return send(res, 200, { aborted: true });
        }
        if (action === "messages" && req.method === "POST") {
          const body = JSON.parse((await readBody(req, 2_000_000)).toString() || "{}");
          const text = String(body.text || "").slice(0, 20_000);
          const attachments: string[] = Array.isArray(body.attachments) ? body.attachments.map(String) : [];
          const files = attachments.map((p) => safeFile(p)).filter((p): p is string => !!p);
          if (!text && !files.length) return send(res, 400, { error: "empty message" });
          const images = files
            .filter((f) => /\.(png|jpe?g|webp)$/i.test(f))
            .slice(0, 6)
            .map((f) => ({ type: "image" as const, data: readFileSync(f).toString("base64"), mimeType: MIME[extname(f).toLowerCase()] }));
          const full = files.length ? `${text}\n\n[Attached files (absolute paths): ${files.join(", ")}]` : text;
          if (!entry.session.sessionName && text) entry.session.setSessionName(text.slice(0, 60));
          const run = entry.session.isStreaming ? entry.session.followUp(full, images) : entry.session.prompt(full, images.length ? { images } : undefined);
          Promise.resolve(run).catch((err: Error) => broadcast(entry, { type: "error", message: err.message }));
          return send(res, 202, { accepted: true, queued: entry.session.isStreaming });
        }
      }
      if (path === "/api/confirm" && req.method === "POST") {
        const body = JSON.parse((await readBody(req, 10_000)).toString() || "{}");
        const pending = pendingConfirms.get(String(body.id));
        if (!pending) return send(res, 404, { error: "no such confirmation" });
        pending.resolve(!!body.approve);
        return send(res, 200, { ok: true });
      }
      if (path === "/api/upload" && req.method === "POST") {
        const name = basename(url.searchParams.get("name") || "upload.bin").replace(/[^A-Za-z0-9_.-]/g, "_").slice(-80);
        if (!/\.(png|jpe?g|webp|step|stp|iges|igs|brep|stl|obj|glb|3mf|csv|pdf|txt|eml)$/i.test(name)) return send(res, 415, { error: "Only images (png/jpg/webp), CAD or mesh files (step/iges/brep/stl/obj/glb/3mf), CSV product lists and notices (pdf/txt/eml) can be uploaded." });
        const data = await readBody(req, MAX_UPLOAD);
        mkdirSync(UPLOAD_DIR, { recursive: true, mode: 0o700 });
        const file = join(UPLOAD_DIR, `${Date.now()}-${name}`);
        writeFileSync(file, data, { mode: 0o600 });
        return send(res, 200, { path: file, name, bytes: data.length });
      }
      if (path === "/api/file" && req.method === "GET") {
        const file = safeFile(url.searchParams.get("path") || "");
        if (!file) return send(res, 404, "not found");
        const ext = extname(file).toLowerCase();
        res.writeHead(200, {
          "Content-Type": MIME[ext],
          "Content-Security-Policy": "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox",
          "X-Content-Type-Options": "nosniff",
          "Cache-Control": "private, max-age=60",
          ...(INLINE.has(ext) ? {} : { "Content-Disposition": `attachment; filename="${basename(file)}"` }),
        });
        return createReadStream(file).pipe(res);
      }
      if (path === "/api/billing/portal" && req.method === "POST") {
        const r = await cli(["billing", "portal"]);
        const url = r.envelope?.data?.url;
        // only hand Creem portal links to the page
        if (r.envelope?.ok && typeof url === "string" && /^https:\/\/([a-z0-9-]+\.)*creem\.io\//.test(url)) return send(res, 200, { url });
        return send(res, 200, { message: r.envelope?.data?.message ?? "No paid subscription on this account yet." });
      }
      if (path === "/api/cases" && req.method === "GET") {
        const r = await cli(["case", "list"]);
        return send(res, 200, r.envelope?.data ?? []);
      }
      const caseMatch = path.match(/^\/api\/cases\/([A-Za-z0-9_.-]+)$/);
      if (caseMatch && req.method === "GET") {
        const [show, versions] = await Promise.all([cli(["case", "show", caseMatch[1]]), cli(["cad", "versions", caseMatch[1]])]);
        if (!show.envelope?.ok) return send(res, 404, show.envelope?.data ?? { error: "not found" });
        const [priorArt, concepts, card] = await Promise.all([cli(["prior-art", "list", caseMatch[1]]), cli(["design", "list", caseMatch[1]]), cli(["case", "card", caseMatch[1], "--lang", uiLang(req)])]);
        return send(res, 200, { case: show.envelope.data, versions: versions.envelope?.data ?? [], prior_art: priorArt.envelope?.data ?? [], concepts: concepts.envelope?.data ?? [], card: card.envelope?.ok ? card.envelope.data : null });
      }
      const versionMatch = path.match(/^\/api\/cases\/([A-Za-z0-9_.-]+)\/versions\/(v\d+)$/);
      if (versionMatch && req.method === "GET") {
        const [caseId, vid] = [versionMatch[1], versionMatch[2]];
        const [sheet, params, versions, show] = await Promise.all([cli(["cad", "sheet", caseId, vid, "--kind", "render"]), cli(["cad", "params", caseId, vid]), cli(["cad", "versions", caseId]), cli(["case", "show", caseId])]);
        const row = (versions.envelope?.data ?? []).find((v: any) => v.id === vid);
        if (!row) return send(res, 404, { error: "version not found" });
        return send(res, 200, { version: row, sheet: sheet.envelope?.data?.path ?? null, params: params.envelope?.data ?? null, original: show.envelope?.data?.original ?? null });
      }
      // Parameter tweak from the sliders (FR-GEN-12): new version + quick re-check, no credits.
      const tweakMatch = path.match(/^\/api\/cases\/([A-Za-z0-9_.-]+)\/versions\/(v\d+)\/tweak$/);
      if (tweakMatch && req.method === "POST") {
        const [caseId, vid] = [tweakMatch[1], tweakMatch[2]];
        const body = JSON.parse((await readBody(req, 20_000)).toString("utf8") || "{}");
        const set = body.set && typeof body.set === "object" ? body.set : {};
        const pairs = Object.entries(set).filter(([k, v]) => /^[A-Za-z_][A-Za-z0-9_]{0,40}$/.test(k) && (typeof v === "number" || (typeof v === "string" && v.length <= 40)));
        if (!pairs.length) return send(res, 400, { message: "No parameter changes." });
        const started = Date.now();
        const tweak = await cli(["cad", "tweak", caseId, vid, ...pairs.flatMap(([k, v]) => ["--set", `${k}=${v}`]), "--label", String(body.label || "参数微调").slice(0, 60), "--rationale", String(body.rationale || "").slice(0, 300)]);
        if (!tweak.envelope?.ok) return send(res, 422, { message: tweak.envelope?.data?.message ?? "The change could not be built." });
        const newVid = tweak.envelope.data.id;
        const built = Date.now() - started;
        const evaluation = await cli(["evaluate", caseId, newVid]);
        return send(res, 200, { version: newVid, build_ms: built, total_ms: Date.now() - started, summary: evaluation.envelope?.ok ? evaluation.envelope.data.summary : null });
      }
      const compareMatch = path.match(/^\/api\/cases\/([A-Za-z0-9_.-]+)\/compare$/);
      if (compareMatch && req.method === "GET") {
        const a = url.searchParams.get("a") || "";
        const b = url.searchParams.get("b") || "";
        if (!/^v\d+$/.test(a) || !/^v\d+$/.test(b)) return send(res, 400, { message: "Pick two versions." });
        const r = await cli(["cad", "compare", compareMatch[1], a, b]);
        return r.envelope?.ok ? send(res, 200, r.envelope.data) : send(res, 422, { message: r.envelope?.data?.message ?? "Compare failed." });
      }
      const filesMatch = path.match(/^\/api\/cases\/([A-Za-z0-9_.-]+)\/files$/);
      if (filesMatch && req.method === "GET") {
        const r = await cli(["files", filesMatch[1]]);
        return send(res, 200, r.envelope?.data ?? []);
      }
      // Monitoring (FR-MON-01): subscribe / unsubscribe a case from the case panel.
      const monitorMatch = path.match(/^\/api\/cases\/([A-Za-z0-9_.-]+)\/monitor$/);
      if (monitorMatch && req.method === "GET") {
        const r = await cli(["monitor", "list"]);
        const row = (r.envelope?.data ?? []).find((m: any) => m.case === monitorMatch[1]) ?? null;
        return send(res, 200, { monitor: row });
      }
      if (monitorMatch && req.method === "PUT") {
        const body = JSON.parse((await readBody(req, 2_000)).toString("utf8") || "{}");
        const every = Number.isInteger(body.every) && body.every >= 1 && body.every <= 365 ? body.every : 30;
        const cap = Number.isInteger(body.max_credits) && body.max_credits >= 1 && body.max_credits <= 5000 ? body.max_credits : 50;
        const r = await cli(["monitor", "add", monitorMatch[1], "--every", String(every), "--max-credits", String(cap)]);
        return r.envelope?.ok ? send(res, 200, r.envelope.data) : send(res, 422, { message: r.envelope?.data?.message ?? "Could not watch this case.", next: r.envelope?.next_actions ?? [] });
      }
      if (monitorMatch && req.method === "DELETE") {
        const r = await cli(["monitor", "remove", monitorMatch[1]]);
        return r.envelope?.ok ? send(res, 200, r.envelope.data) : send(res, 422, { message: r.envelope?.data?.message ?? "Failed." });
      }
      // Deadline calendar (FR-FILE-12): computed from recorded filings, notices and disclosures.
      const deadlinesMatch = path.match(/^\/api\/cases\/([A-Za-z0-9_.-]+)\/deadlines$/);
      if (deadlinesMatch && req.method === "GET") {
        const r = await cli(["deadlines", deadlinesMatch[1], "--lang", uiLang(req)]);
        return r.envelope?.ok ? send(res, 200, r.envelope.data) : send(res, 422, { message: r.envelope?.data?.message ?? "Could not compute deadlines." });
      }
      // Read-only share links (FR-CASE-09): the page asks, the person confirms in the dialog.
      const sharesMatch = path.match(/^\/api\/cases\/([A-Za-z0-9_.-]+)\/shares$/);
      if (sharesMatch && req.method === "GET") {
        const r = await cli(["share", "list", "--case", sharesMatch[1]]);
        return r.envelope?.ok ? send(res, 200, r.envelope.data) : send(res, 502, { message: r.envelope?.data?.message ?? "Could not read share links." });
      }
      if (sharesMatch && req.method === "POST") {
        const body = JSON.parse((await readBody(req, 5_000)).toString("utf8") || "{}");
        const to = String(body.to ?? "").trim();
        const purpose = ["factory", "agent", "other"].includes(body.purpose) ? body.purpose : "factory";
        const days = Number.isInteger(body.days) && body.days >= 1 && body.days <= 30 ? body.days : 7;
        if (!to || to.length > 120) return send(res, 422, { message: uiLang(req) === "en" ? "Enter the recipient (at most 120 characters)." : "请填写接收方（120 字以内）。" });
        const argv = ["share", "create", sharesMatch[1], ...(/^v\d+$/.test(body.version || "") ? [body.version] : []), "--to", to, "--purpose", purpose, "--days", String(days)];
        if (typeof body.note === "string" && body.note.trim()) argv.push("--note", body.note.trim().slice(0, 500));
        if (body.watermark === false) argv.push("--no-watermark");
        const r = await cli(argv);
        return r.envelope?.ok ? send(res, 200, r.envelope.data) : send(res, 422, { message: r.envelope?.data?.message ?? "Share failed.", next: r.envelope?.next_actions ?? [] });
      }
      const shareMatch = path.match(/^\/api\/shares\/(sh_[a-z0-9]{8,40})$/);
      if (shareMatch && req.method === "DELETE") {
        const r = await cli(["share", "revoke", shareMatch[1]]);
        return r.envelope?.ok ? send(res, 200, r.envelope.data) : send(res, 422, { message: r.envelope?.data?.message ?? "Revoke failed." });
      }
      if (shareMatch && req.method === "GET") {
        const r = await cli(["share", "show", shareMatch[1]]);
        return r.envelope?.ok ? send(res, 200, r.envelope.data) : send(res, 404, { message: r.envelope?.data?.message ?? "Not found." });
      }
      const reportMatch = path.match(/^\/api\/cases\/([A-Za-z0-9_.-]+)\/report$/);
      if (reportMatch && req.method === "POST") {
        const body = JSON.parse((await readBody(req, 2_000)).toString("utf8") || "{}");
        const r = await cli(["report", reportMatch[1], ...(/^v\d+$/.test(body.version || "") ? [body.version] : []), "--lang", body.lang === "en" ? "en" : uiLang(req)]);
        return r.envelope?.ok ? send(res, 200, r.envelope.data) : send(res, 422, { message: r.envelope?.data?.message ?? "Report failed." });
      }
      if (path === "/api/sample" && req.method === "POST") {
        const r = await cli(["sample", "--lang", uiLang(req)]);
        return r.envelope?.ok ? send(res, 200, r.envelope.data) : send(res, 500, { message: r.envelope?.data?.message ?? "Sample failed." });
      }
      if (path === "/api/notify" && req.method === "POST") {
        // Desktop notification (and the configured chat-bot webhook) for a finished long step.
        const body = JSON.parse((await readBody(req, 4_000)).toString("utf8") || "{}");
        const title = String(body.title || "DesignAround").slice(0, 80);
        const text = String(body.text || "").slice(0, 180);
        await cli(["notify", title, text, "--desktop"]);
        return send(res, 200, { ok: true });
      }
      if (path === "/api/reveal" && req.method === "POST") {
        // Show a deliverable in Finder / Explorer / the file manager (desktop use).
        const body = JSON.parse((await readBody(req, 4_000)).toString("utf8") || "{}");
        const target = safePath(String(body.path || ""));
        if (!target) return send(res, 404, { message: "not found" });
        const [cmd, args] = process.platform === "darwin" ? ["open", ["-R", target]] : process.platform === "win32" ? ["explorer.exe", [`/select,${target}`]] : ["xdg-open", [statSync(target).isDirectory() ? target : dirname(target)]];
        spawn(cmd, args, { stdio: "ignore", detached: true, windowsHide: true }).unref();
        return send(res, 200, { ok: true });
      }
      if (path === "/api/billing" && req.method === "GET") {
        const r = await cli(["auth", "status"]);
        const me = r.envelope?.data?.me ?? {};
        return send(res, 200, { plan: me.plan ?? "free", balance: me.balance ?? null, is_trial: !!me.is_trial, subscription_status: me.subscription_status ?? null, can_manage_billing: !!me.can_manage_billing, plan_expires_at: me.plan_expires_at ?? null });
      }
      if (path === "/api/billing/checkout" && req.method === "POST") {
        const body = JSON.parse((await readBody(req, 10_000)).toString("utf8") || "{}");
        const args = ["billing", "checkout"];
        if (body.plan === "pro" || body.plan === "team") args.push("--plan", body.plan, ...(body.annual ? ["--annual"] : []));
        else if (["5k", "21k", "55k", "240k"].includes(body.credits)) args.push("--credits", body.credits);
        else return send(res, 400, { message: "Unknown plan or credit pack." });
        const r = await cli(args);
        const url = r.envelope?.data?.checkout_url;
        if (r.envelope?.ok && typeof url === "string" && /^https:\/\/([a-z0-9-]+\.)*(creem\.io|elidot\.com)\//.test(url)) return send(res, 200, { url, amount_usd: r.envelope.data.amount_usd });
        const code = r.envelope?.kind ?? "";
        const pending = /unavailable|not_configured|billing_disabled|creem/i.test(code + " " + (r.envelope?.data?.message ?? ""));
        const en = uiLang(req) === "en";
        return send(res, 200, { message: pending ? (en ? "No payment link was created. Email support@elidot.com to buy this plan, or keep using the free allowance." : "没有生成付款链接。购买请发邮件到 support@elidot.com，或继续使用免费额度。") : r.envelope?.data?.message ?? (en ? "Cannot create a checkout link right now." : "暂时无法创建付款链接。"), pending });
      }
      return send(res, 404, { error: "not found" });
    } catch (err: any) {
      return send(res, err?.status || 500, { error: err?.message || String(err) });
    }
  });

  server.listen(Number(values.port || 0), host, () => {
    const port = (server.address() as any).port;
    const url = `http://127.0.0.1:${port}/?t=${token}`;
    process.stdout.write(JSON.stringify({ type: "ui_ready", url, port, mode: cfg.mode, models: { assistant: choice.designer, observer: choice.observer }, pi_agent_dir: PI_AGENT_DIR }) + "\n");
    // Watched cases (FR-MON-01): re-screen the due ones in the background within the
    // per-case caps the person set; new hits are pushed by the CLI (webhook / desktop).
    if (cfg.mode === "platform" && process.env.DA_UI_MONITOR !== "0") {
      setTimeout(() => {
        cli(["monitor", "run", "--due-only"]).catch(() => {});
      }, 20_000).unref();
    }
  });

  const shutdown = () => {
    for (const entry of entries.values()) {
      entry.unsubscribe();
      entry.session.dispose();
    }
    server.close();
    process.exit(0);
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
  if (process.env.DA_UI_WATCH_STDIN === "1") {
    // launched by `designaround ui`: exit when the parent goes away
    process.stdin.on("end", shutdown);
    process.stdin.on("error", shutdown);
    process.stdin.resume();
  }
}

main().catch((err) => {
  process.stdout.write(JSON.stringify({ type: "error", code: /DESIGNAROUND_API_KEY|BYOK/.test(String(err?.message)) ? "config" : "internal", message: String(err?.message ?? err) }) + "\n");
  process.exit(/DESIGNAROUND_API_KEY|BYOK/.test(String(err?.message)) ? 4 : 6);
});
