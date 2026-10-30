#!/usr/bin/env node
/**
 * Agent runner. Invoked by the `designaround` CLI; prints NDJSON events.
 *
 *   node src/main.ts around --case <id> --max-credits 6000 [--rounds 3] [--per-round 3] [--target 2]
 *   node src/main.ts model    --case <id> --max-credits 3000 [--known "length=320mm"]       (from photos)
 *   node src/main.ts concepts --case <id> --max-credits 600 [--count 4] [--base c2 --feedback "..."]
 *   node src/main.ts build    --case <id> --max-credits 3000 [--concept c2] [--known "240x160x90mm"] [--rounds 3]
 *   node src/main.ts observe --case <id> --version v3 --prior USD1000001S --max-credits 500
 *   node src/main.ts isolation-check
 *
 * Exit codes: 0 ok | 2 needs human input | 3 budget | 4 auth/config | 5 bad args | 6 error
 */
import "./env.ts";
import { randomUUID } from "node:crypto";
import { parseArgs } from "node:util";
import { DESIGNAROUND_HOME, PI_AGENT_DIR } from "./env.ts";
import { Engine } from "./engine.ts";
import { configFromEnv, createModelRuntime } from "./runtime.ts";
import { runBuild, runConcepts } from "./design.ts";
import { Budget, BudgetExceeded, observe, runAround, runModel } from "./team.ts";

function emit(event: Record<string, unknown>) {
  process.stdout.write(JSON.stringify({ ts: new Date().toISOString(), ...event }) + "\n");
}

async function main(): Promise<number> {
  const { positionals, values } = parseArgs({
    allowPositionals: true,
    options: {
      case: { type: "string" },
      version: { type: "string" },
      prior: { type: "string" },
      "max-credits": { type: "string" },
      rounds: { type: "string" },
      "per-round": { type: "string" },
      target: { type: "string" },
      top: { type: "string" },
      process: { type: "string" },
      known: { type: "string" },
      description: { type: "string" },
      count: { type: "string" },
      base: { type: "string" },
      feedback: { type: "string" },
      concept: { type: "string" },
    },
  });
  const task = positionals[0];
  if (task === "isolation-check") {
    emit({ type: "isolation", designaround_home: DESIGNAROUND_HOME, pi_agent_dir: PI_AGENT_DIR, env: { PI_CODING_AGENT_DIR: process.env.PI_CODING_AGENT_DIR, PI_OFFLINE: process.env.PI_OFFLINE, PI_TELEMETRY: process.env.PI_TELEMETRY } });
    return 0;
  }
  if (!task || !["around", "model", "observe", "concepts", "build"].includes(task)) {
    emit({ type: "error", code: "bad_args", message: "task must be around | model | observe | concepts | build | isolation-check" });
    return 5;
  }
  if (!values.case) {
    emit({ type: "error", code: "bad_args", message: "--case is required" });
    return 5;
  }
  const maxCredits = Number(values["max-credits"]);
  if (!Number.isFinite(maxCredits) || maxCredits <= 0) {
    emit({ type: "error", code: "budget_required", message: "--max-credits is required for agent runs" });
    return 3;
  }

  let cfg;
  try {
    cfg = configFromEnv();
  } catch (err) {
    emit({ type: "error", code: "config", message: (err as Error).message });
    return 4;
  }
  const runId = process.env.DA_RUN_ID || `run-${randomUUID()}`;
  process.env.DA_RUN_ID = runId;
  process.env.DA_MAX_CREDITS = String(Math.floor(maxCredits));
  // Team tasks are tool loops that must act, not deliberate: ask the platform proxy to
  // switch off long reasoning (the chat assistant keeps it). Override with DA_THINKING=on.
  process.env.DA_THINKING = process.env.DA_THINKING || "off";

  const engine = new Engine();
  try {
    const { runtime, choice } = await createModelRuntime(cfg);
    const budget = new Budget(maxCredits, cfg.mode, choice.rates);
    const deps = { engine, runtime, choice, mode: cfg.mode, caseId: values.case, cwd: DESIGNAROUND_HOME, budget, emit };
    emit({ type: "run_start", run_id: runId, task, case: values.case, mode: cfg.mode, models: { designer: choice.designer, observer: choice.observer }, max_credits: maxCredits });
    let result: any;
    if (task === "around") {
      result = await runAround(deps, {
        rounds: Number(values.rounds ?? 3),
        perRound: Number(values["per-round"] ?? 3),
        target: Number(values.target ?? 2),
        topPriorArt: Number(values.top ?? 3),
        process: values.process ?? "injection",
      });
    } else if (task === "model") {
      result = await runModel(deps, { knownDimension: values.known, description: values.description });
    } else if (task === "concepts") {
      result = await runConcepts(deps, { count: Number(values.count ?? 4), base: values.base, feedback: values.feedback });
    } else if (task === "build") {
      result = await runBuild(deps, { concept: values.concept, knownDimension: values.known, rounds: values.rounds ? Number(values.rounds) : undefined, process: values.process });
    } else {
      if (!values.version || !values.prior) {
        emit({ type: "error", code: "bad_args", message: "observe needs --version and --prior" });
        return 5;
      }
      result = { status: "ok", observer: await observe(deps, values.version, values.prior) };
    }
    emit({ type: "result", run_id: runId, spent: budget.spent, ...result });
    return result.status === "needs_input" || result.status === "gate_failed" ? 2 : 0;
  } catch (err) {
    if (err instanceof BudgetExceeded) {
      emit({ type: "error", code: "budget_exceeded", message: err.message });
      return 3;
    }
    const message = (err as Error).message ?? String(err);
    const code = /402/.test(message) ? 3 : /401|403|^config:/.test(message) ? 4 : 6;
    emit({ type: "error", code: code === 3 ? "insufficient_credits" : code === 4 ? "auth" : "internal", message });
    return code;
  } finally {
    engine.close();
  }
}

main().then(
  (code) => process.exit(code),
  (err) => {
    emit({ type: "error", code: "internal", message: String(err?.stack ?? err) });
    process.exit(6);
  },
);
