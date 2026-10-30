#!/usr/bin/env node
/**
 * Terminal chat with the DesignAround Assistant (same agent, tools and session
 * store as the workbench UI).
 *
 *   node src/chat-cli.ts [--session <id>] [--continue] [--message "..."]
 *
 * Paid steps ask "allow? [y/N]" on the terminal. /exit quits, /abort stops the
 * current step, /sessions lists conversations.
 */
import "./env.ts";
import { randomUUID } from "node:crypto";
import { createInterface } from "node:readline/promises";
import { parseArgs } from "node:util";
import { listChatSessions, openAssistant, WORDING_NOTE, wordingIssues } from "./assistant.ts";
import { PI_AGENT_DIR } from "./env.ts";
import { configFromEnv, createModelRuntime } from "./runtime.ts";

const dim = (s: string) => (process.stdout.isTTY ? `\x1b[2m${s}\x1b[0m` : s);
const cyan = (s: string) => (process.stdout.isTTY ? `\x1b[36m${s}\x1b[0m` : s);
const yellow = (s: string) => (process.stdout.isTTY ? `\x1b[33m${s}\x1b[0m` : s);

async function main() {
  const { values } = parseArgs({ options: { session: { type: "string" }, continue: { type: "boolean" }, message: { type: "string" } } });
  const cfg = configFromEnv();
  const { runtime, choice } = await createModelRuntime(cfg);
  process.env.DA_RUN_ID = process.env.DA_RUN_ID || `chat-${randomUUID()}`;
  process.env.DA_MAX_CREDITS = process.env.DA_MAX_CREDITS || "3000";
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  const oneShot = values.message !== undefined;

  let sessionPath: string | undefined;
  if (values.session || values.continue) {
    const sessions = await listChatSessions();
    const found = values.session ? sessions.find((s) => s.id.startsWith(values.session!)) : sessions.sort((a, b) => +new Date(b.modified) - +new Date(a.modified))[0];
    if (values.session && !found) throw new Error(`no chat session ${values.session}`);
    sessionPath = found?.path;
  }

  const session = await openAssistant({
    runtime,
    choice,
    sessionPath,
    deps: {
      python: process.env.DA_PYTHON || "python3",
      mode: cfg.mode,
      env: {},
      host: {
        async confirm(request) {
          if (oneShot && process.env.DA_CHAT_AUTO_APPROVE !== "1") {
            process.stdout.write(yellow(`\n[declined automatically in one-shot mode] ${request.title} (${request.credits} credits)\n`));
            return false;
          }
          if (process.env.DA_CHAT_AUTO_APPROVE === "1") return true;
          const answer = await rl.question(yellow(`\n${request.title} — ${request.detail}\nAllow? [y/N] `));
          return /^y(es)?$/i.test(answer.trim());
        },
        progress(_id, line) {
          process.stdout.write(dim(`    ${line}\n`));
        },
      },
    },
  });

  session.subscribe((ev: any) => {
    if (ev.type === "message_update" && ev.assistantMessageEvent?.type === "text_delta") process.stdout.write(ev.assistantMessageEvent.delta);
    else if (ev.type === "tool_execution_start") process.stdout.write(cyan(`\n  ▸ ${ev.toolName} `) + dim(JSON.stringify(ev.args).slice(0, 160)) + "\n");
    else if (ev.type === "tool_execution_end") {
      const d = ev.result?.details ?? {};
      process.stdout.write(dim(`  ${d.ok === false || ev.isError ? "✗" : "✓"} ${ev.toolName}${d.images?.length ? `  images: ${d.images.join(", ")}` : ""}\n`));
    } else if (ev.type === "message_end" && ev.message?.role === "assistant") {
      if (ev.message.stopReason === "error") process.stdout.write(yellow(`\n[model error] ${ev.message.errorMessage}\n`));
      else {
        const text = (ev.message.content ?? []).filter((c: any) => c.type === "text").map((c: any) => c.text).join("");
        process.stdout.write(wordingIssues(text).length ? yellow(`\n${/[\u4e00-\u9fff]/.test(text) ? WORDING_NOTE.zh : WORDING_NOTE.en}\n`) : "\n");
      }
    }
  });

  if (!oneShot) {
    process.stdout.write(dim(`DesignAround Assistant · ${cfg.mode} · ${choice.designer} · Pi config ${PI_AGENT_DIR} (isolated) · session ${session.sessionId.slice(0, 8)}\n`));
    process.stdout.write(dim("Type your question. /sessions, /abort, /exit.\n\n"));
  }

  if (oneShot) {
    await session.prompt(values.message!);
    rl.close();
    session.dispose();
    return 0;
  }
  for (;;) {
    const line = (await rl.question("> ")).trim();
    if (!line) continue;
    if (line === "/exit" || line === "/quit") break;
    if (line === "/abort") {
      await session.abort();
      continue;
    }
    if (line === "/sessions") {
      for (const s of await listChatSessions()) process.stdout.write(`${s.id.slice(0, 8)}  ${new Date(s.modified).toISOString().slice(0, 16)}  ${s.name ?? s.first}\n`);
      continue;
    }
    if (!session.messages.length) session.setSessionName(line.slice(0, 60));
    await session.prompt(line);
  }
  rl.close();
  session.dispose();
  return 0;
}

main().then(
  (code) => process.exit(code),
  (err) => {
    process.stderr.write(`error: ${err?.message ?? err}\n`);
    process.exit(/DESIGNAROUND_API_KEY|BYOK/.test(String(err?.message)) ? 4 : 6);
  },
);
