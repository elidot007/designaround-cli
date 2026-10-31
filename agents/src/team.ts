/**
 * The agent team. Orchestration is deterministic TypeScript; language models
 * are used only where judgement is needed:
 *
 *   CAD Agent  — photos/description -> parametric CadQuery model (tool loop)
 *   Designer   — proposes design-around candidates (tool loop, keeps memory across rounds)
 *   Observer   — isolated ordinary-observer scoring; a FRESH session per judgement,
 *                no tools, never sees the Designer's rationale
 *
 * Deterministic engine steps (originality gate, DFM, CPU shape scores, risk
 * fusion, evidence) run between model turns.
 */
import type { ModelRuntime } from "@earendil-works/pi-coding-agent";
import { Engine, pngContent } from "./engine.ts";
import { aroundBrief, DESIGNER_SYSTEM, MODELER_SYSTEM } from "./prompts.ts";
import { createIsolatedSession, type ModelChoice, type Mode } from "./runtime.ts";
import { cadTools } from "./tools.ts";

export class BudgetExceeded extends Error {}

/** Independent observer judgements per comparison; the median is used. */
export const OBSERVER_SAMPLES = Number(process.env.DA_OBSERVER_SAMPLES || 3);

export class Budget {
  spent = 0;
  max: number;
  private mode: Mode;
  private rates: ModelChoice["rates"];
  constructor(max: number, mode: Mode, rates: ModelChoice["rates"]) {
    this.max = max;
    this.mode = mode;
    this.rates = rates;
  }

  add(modelId: string, usage: any): number {
    if (!usage) return 0;
    let credits = 0;
    if (this.mode === "platform") {
      const rate = this.rates[modelId];
      if (rate) credits = Math.ceil(((usage.input ?? 0) * rate.input + (usage.output ?? 0) * rate.output) / 1_000_000);
    } else {
      credits = Math.ceil((usage.cost?.total ?? 0) * 100);
    }
    this.spent += credits;
    return credits;
  }

  check(): void {
    if (this.spent > this.max) throw new BudgetExceeded(`budget ${this.max} exceeded (spent ${this.spent})`);
  }
}

export interface TeamDeps {
  engine: Engine;
  runtime: ModelRuntime;
  choice: ModelChoice;
  mode: Mode;
  caseId: string;
  cwd: string;
  budget: Budget;
  emit: (event: Record<string, unknown>) => void;
}

export function track(deps: TeamDeps, session: any, modelId: string, role: string): () => void {
  return session.subscribe((event: any) => {
    // Model failures and retries are otherwise invisible in team runs.
    if (event?.type === "auto_retry_start") deps.emit({ type: "model_retry", role, attempt: event.attempt, max: event.maxAttempts, message: String(event.errorMessage ?? "").slice(0, 300) });
    if (event?.type === "message_end" && event.message?.role === "assistant" && event.message.stopReason === "error") {
      deps.emit({ type: "model_error", role, message: String(event.message.errorMessage ?? "").slice(0, 300) });
    }
    if (event?.type === "message_end" && event.message?.role === "assistant") {
      const credits = deps.budget.add(modelId, event.message.usage);
      deps.emit({ type: "cost", role, model: modelId, credits, spent: deps.budget.spent, max: deps.budget.max });
      if (deps.budget.spent > deps.budget.max) {
        deps.emit({ type: "budget_exceeded", spent: deps.budget.spent, max: deps.budget.max });
        void session.abort();
      }
    }
  });
}

/** Longest silence (no session event at all) before a prompt counts as stalled. */
const IDLE_MS = Number(process.env.DA_PROMPT_IDLE_SECONDS || 180) * 1000;

export class PromptStalled extends Error {}

/** One prompt with an idle watchdog: a stream that stops sending events is aborted. */
async function promptWatched(session: any, text: string, images: any[]): Promise<boolean> {
  let stalled = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const arm = () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      stalled = true;
      void session.abort();
    }, IDLE_MS);
  };
  const unsubscribe = session.subscribe(() => arm());
  arm();
  try {
    await session.prompt(text, images.length ? { images } : undefined);
  } finally {
    if (timer) clearTimeout(timer);
    unsubscribe();
  }
  return stalled;
}

export async function promptWithBudget(deps: TeamDeps, session: any, text: string, images: any[] = []) {
  deps.budget.check();
  // A model stream can stall without closing (seen with upstream gateways). Abort it after
  // IDLE_MS of silence and ask once more; a second stall is reported to the caller.
  if (await promptWatched(session, text, images)) {
    deps.emit({ type: "stalled", seconds: IDLE_MS / 1000, retry: true });
    deps.budget.check();
    if (await promptWatched(session, "Your previous reply was cut off by a network stall. Continue the task from where you stopped.", [])) {
      deps.emit({ type: "stalled", seconds: IDLE_MS / 1000, retry: false });
      throw new PromptStalled(`the model stopped responding for ${IDLE_MS / 1000} s twice`);
    }
  }
  deps.budget.check();
  return session.getLastAssistantText() ?? "";
}

// ------------------------------------------------------------------ observer

async function observeOnce(deps: TeamDeps, prompt: any, imagePath: string): Promise<any> {
  // A fresh session per judgement: no tools, no memory, never sees the designer's rationale.
  const session = await createIsolatedSession({ runtime: deps.runtime, provider: deps.choice.provider, modelId: deps.choice.observer, systemPrompt: prompt.system, tools: [], cwd: deps.cwd });
  const stop = track(deps, session, deps.choice.observer, "observer");
  try {
    const reply = await promptWithBudget(deps, session, prompt.user, [pngContent(imagePath)]);
    try {
      return await deps.engine.call("parse_observer_reply", { text: reply });
    } catch {
      const retry = await promptWithBudget(deps, session, "Return ONLY the JSON object described in the instructions.");
      return await deps.engine.call("parse_observer_reply", { text: retry });
    }
  } finally {
    stop();
    session.dispose();
  }
}

export async function observe(deps: TeamDeps, vid: string, priorId: string): Promise<any> {
  const pair = await deps.engine.call("comparison_image", { case_id: deps.caseId, vid, prior_id: priorId });
  const prompt = await deps.engine.call("observer_prompt", { case_id: deps.caseId, vid, prior_id: priorId, view: pair.view });
  const settled = await Promise.allSettled(Array.from({ length: OBSERVER_SAMPLES }, () => observeOnce(deps, prompt, pair.path)));
  const samples = settled.filter((s): s is PromiseFulfilledResult<any> => s.status === "fulfilled").map((s) => s.value);
  const budgetError = settled.find((s) => s.status === "rejected" && s.reason instanceof BudgetExceeded);
  if (budgetError) throw (budgetError as PromiseRejectedResult).reason;
  if (samples.length < 2) {
    const reason = settled.find((s) => s.status === "rejected") as PromiseRejectedResult | undefined;
    throw new Error(`observer returned ${samples.length} usable judgements: ${reason?.reason?.message ?? reason?.reason ?? "unknown"}`);
  }
  const parsed = await deps.engine.call("combine_observer_samples", { samples });
  parsed.model = deps.choice.observer;
  parsed.view = pair.view;
  deps.emit({ type: "observer", version: vid, prior_art: priorId, score: parsed.score, samples: parsed.samples });
  return parsed;
}

async function evaluateWithObserver(deps: TeamDeps, vid: string, priorIds: string[]) {
  // The rights most similar to THIS version (by silhouette) must be scored too,
  // not only the ones that were closest to the original.
  const own: string[] = await deps.engine.call("review_set", { case_id: deps.caseId, vid, top_k: priorIds.length });
  const judge: Record<string, any> = {};
  for (const pid of [...new Set([...priorIds, ...own])]) judge[pid] = await observe(deps, vid, pid);
  const evaluation = await deps.engine.call("evaluate", { case_id: deps.caseId, vid, judge, review_top_k: priorIds.length });
  deps.emit({ type: "evaluation", version: vid, summary: evaluation.summary });
  return { evaluation, judge };
}

// --------------------------------------------------------------------- model

export async function runModel(deps: TeamDeps, input: { description?: string; knownDimension?: string }) {
  const photos: Record<string, string> = await deps.engine.call("case_photos", { case_id: deps.caseId });
  const images = Object.values(photos).slice(0, 7).map((p) => pngOrJpeg(p));
  const created: string[] = [];
  const tools = cadTools({ engine: deps.engine, caseId: deps.caseId, created, emit: deps.emit, maxVersions: 1 });
  const session = await createIsolatedSession({ runtime: deps.runtime, provider: deps.choice.provider, modelId: deps.choice.cad, systemPrompt: MODELER_SYSTEM, tools, cwd: deps.cwd, thinking: "low" });
  const stop = track(deps, session, deps.choice.cad, "cad_agent");
  try {
    const views = Object.keys(photos).join(", ") || "none";
    const brief = `Build the original digital sample for case ${deps.caseId}.
Photos attached (views: ${views}). Known dimension: ${input.knownDimension ?? "not given — ask the user"}.
Description: ${input.description ?? "(none)"}
Save exactly one version with kind "original" when the model matches the photos.
If the photo gate fails, that version is stored as a draft and is not the original.`;
    await promptWithBudget(deps, session, brief, images);
    const original = created.at(-1);
    if (!original) return { status: "needs_input", reason: "CAD Agent did not save a model", reply: session.getLastAssistantText() };
    let gate = null;
    if (Object.keys(photos).length) {
      gate = await deps.engine.call("reconstruction_gate", { case_id: deps.caseId, vid: original, photo_paths: photos });
      if (gate && !gate.pass) await deps.engine.call("demote_failed_gate", { case_id: deps.caseId, vid: original, gate });
    }
    deps.emit({ type: "model_done", version: original, gate });
    return { status: gate && !gate.pass ? "gate_failed" : "ok", version: original, gate };
  } finally {
    stop();
    session.dispose();
  }
}

function pngOrJpeg(path: string) {
  const img = pngContent(path);
  if (/\.jpe?g$/i.test(path)) img.mimeType = "image/jpeg";
  if (/\.webp$/i.test(path)) img.mimeType = "image/webp";
  return img;
}

// -------------------------------------------------------------------- around

export interface AroundOptions {
  rounds: number;
  perRound: number;
  target: number;
  topPriorArt: number;
  process: string;
}

export async function runAround(deps: TeamDeps, opts: AroundOptions) {
  const caseInfo = await deps.engine.call("case_show", { case_id: deps.caseId });
  const original = (caseInfo.versions.find((v: any) => v.kind === "original") ?? caseInfo.versions[0])?.id;
  if (!original) return { status: "needs_input", reason: "No original version. Run `designaround model` or import CAD first." };
  if (!caseInfo.prior_art.length) return { status: "needs_input", reason: "No prior art on the case. Run `designaround screen` or add known patents." };

  await deps.engine.call("agent_event", { case_id: deps.caseId, kind: "around.start", payload: { run_id: process.env.DA_RUN_ID, original, ...opts, budget: deps.budget.max, mode: deps.mode } });
  const ranked = await deps.engine.call("shape_scores", { case_id: deps.caseId, vid: original });
  const priorIds: string[] = ranked.slice(0, opts.topPriorArt).map((r: any) => r.id);
  deps.emit({ type: "baseline_start", original, prior_art: priorIds });

  const baseline = await evaluateWithObserver(deps, original, priorIds);
  const schema = await deps.engine.call("params_schema", { case_id: deps.caseId, vid: original });
  const constraints = caseInfo.constraints ?? {};
  const locked: string[] = [...new Set([...(schema.locked_by_default ?? []), ...(constraints.locked_params ?? [])])];
  const targetFeatures: string[] | null = constraints.target_features ?? null;

  const created: string[] = [];
  const toolCtx = { engine: deps.engine, caseId: deps.caseId, created, emit: deps.emit, maxVersions: opts.perRound };
  const tools = cadTools(toolCtx);
  const designer = await createIsolatedSession({ runtime: deps.runtime, provider: deps.choice.provider, modelId: deps.choice.cad, systemPrompt: DESIGNER_SYSTEM, tools, cwd: deps.cwd, thinking: "medium" });
  const stop = track(deps, designer, deps.choice.cad, "designer");

  const results: any[] = [];
  let feedback = "";
  try {
    for (let round = 1; round <= opts.rounds; round++) {
      const pairImages = [];
      for (const pid of priorIds) {
        const pair = await deps.engine.call("comparison_image", { case_id: deps.caseId, vid: original, prior_id: pid });
        pairImages.push(pngContent(pair.path));
      }
      const priorSummary = baseline.evaluation.per_prior_art.map((r: any) => ({
        id: r.prior_art_id,
        number: r.number,
        risk: r.risk,
        level: r.level,
        similar: baseline.judge[r.prior_art_id]?.similar,
        distinctive: baseline.judge[r.prior_art_id]?.distinctive_features_of_prior_art,
      }));
      const before = created.length;
      toolCtx.maxVersions = before + opts.perRound;
      deps.emit({ type: "round_start", round });
      await promptWithBudget(
        deps,
        designer,
        aroundBrief({ original, article: caseInfo.article, constraints, features: schema.features ?? {}, locked, params: schema.params ?? {}, priorArt: priorSummary, count: opts.perRound, round, feedback }),
        pairImages,
      );
      const fresh = created.slice(before);
      const notes: string[] = [];
      for (const vid of fresh) {
        const originality = await deps.engine.call("originality_check", { case_id: deps.caseId, vid, base: original, target_features: targetFeatures });
        if (!originality.pass) {
          results.push({ version: vid, round, qualified: false, originality, reason: "originality" });
          notes.push(`${vid}: REJECTED by originality gate — ${originality.reasons.join(" ")}`);
          deps.emit({ type: "candidate", version: vid, qualified: false, reason: originality.reasons });
          continue;
        }
        const dfm = await deps.engine.call("dfm_check", { case_id: deps.caseId, vid, process: opts.process });
        const { evaluation, judge } = await evaluateWithObserver(deps, vid, priorIds);
        const qualified = evaluation.summary.qualified && dfm.pass;
        results.push({ version: vid, round, qualified, risk: evaluation.summary.max_risk, level: evaluation.summary.level, dfm_pass: dfm.pass, originality_pass: true });
        const worstId = evaluation.summary.worst_prior_art;
        const dfmIssues = dfm.checks.filter((c: any) => c.severity === "error").map((c: any) => c.rule_id);
        const others = Object.entries(judge)
          .filter(([pid]) => pid !== worstId)
          .map(([pid, j]: [string, any]) => `${pid} ${j.score}/10`)
          .join(", ");
        notes.push(`${vid}: max risk ${evaluation.summary.max_risk} (${evaluation.summary.level}) vs ${worstId}; observer score ${judge[worstId]?.score ?? "?"}/10, still sees: ${(judge[worstId]?.similar ?? []).join("; ") || "n/a"}; already different: ${(judge[worstId]?.different ?? []).join("; ") || "n/a"}${others ? `; other rights: ${others}` : ""}${dfmIssues.length ? `; DFM errors: ${dfmIssues.join(", ")}` : ""}${qualified ? " — QUALIFIED" : ""}`);
        deps.emit({ type: "candidate", version: vid, qualified, risk: evaluation.summary.max_risk, level: evaluation.summary.level, dfm_pass: dfm.pass });
      }
      if (!fresh.length) notes.push("No candidate versions were created. Use cad_tweak or cad_save_version.");
      feedback = notes.join("\n");
      const qualifiedCount = results.filter((r) => r.qualified).length;
      deps.emit({ type: "round_end", round, qualified: qualifiedCount });
      if (qualifiedCount >= opts.target) break;
    }
  } finally {
    stop();
    designer.dispose();
  }
  const qualified = results.filter((r) => r.qualified).sort((a, b) => a.risk - b.risk);
  const status = qualified.length >= opts.target ? "ok" : qualified.length ? "partial" : "no_qualified";
  const stuck = status === "ok" ? null : feedback;
  await deps.engine.call("agent_event", { case_id: deps.caseId, kind: "around.end", payload: { run_id: process.env.DA_RUN_ID, status, qualified: qualified.map((q) => q.version), candidates: results.length, spent: deps.budget.spent } });
  return { status, original, baseline: baseline.evaluation.summary, candidates: results, qualified: qualified.map((q) => q.version), stuck, spent: deps.budget.spent };
}
