/**
 * Embedded Pi runtime with full isolation from any local Pi installation.
 *
 * Model modes:
 *  - platform: provider "designaround" -> DesignAround server /v1/llm (credits metered server-side,
 *              per-run hard cap via X-DA-Run-Id / X-DA-Max-Credits headers).
 *  - byok:     user's own provider key, kept in memory (setRuntimeApiKey), never written to disk.
 *  Host mode does not use this runtime at all (the host agent drives the CLI).
 */
import { randomUUID } from "node:crypto";
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import {
  createAgentSession,
  createExtensionRuntime,
  ModelRuntime,
  type ResourceLoader,
  SessionManager,
  SettingsManager,
  type ToolDefinition,
} from "@earendil-works/pi-coding-agent";
import { PI_AGENT_DIR } from "./env.ts";

export type Mode = "platform" | "byok";

export interface ModelChoice {
  provider: string;
  designer: string;
  observer: string;
  /** CAD Agent and 3D modelling planner (writes CadQuery); the designer model when the server offers no da-cad. */
  cad: string;
  /** credits per million tokens (platform) — used for local budget tracking. */
  rates: Record<string, { input: number; output: number }>;
}

export interface RuntimeConfig {
  mode: Mode;
  serverUrl?: string;
  apiKey?: string;
  byok?: { provider: string; model: string; observerModel?: string; key: string; baseUrl?: string };
}

export function configFromEnv(): RuntimeConfig {
  const mode = (process.env.DA_MODEL_MODE || "platform") as Mode;
  if (mode === "byok") {
    const key = process.env.DA_BYOK_KEY;
    const provider = process.env.DA_BYOK_PROVIDER;
    const model = process.env.DA_BYOK_MODEL;
    if (!key || !provider || !model) throw new Error("BYOK mode needs DA_BYOK_PROVIDER, DA_BYOK_MODEL and DA_BYOK_KEY");
    return { mode, byok: { provider, model, key, observerModel: process.env.DA_BYOK_OBSERVER_MODEL, baseUrl: process.env.DA_BYOK_BASE_URL } };
  }
  const serverUrl = (process.env.DA_SERVER_URL || "https://designaround.elidot.com").replace(/\/$/, "");
  const apiKey = process.env.DESIGNAROUND_API_KEY;
  if (!apiKey) throw new Error("Platform mode needs DESIGNAROUND_API_KEY (run `designaround auth login`)");
  return { mode, serverUrl, apiKey };
}

async function fetchPlatformModels(cfg: RuntimeConfig) {
  // The workbench cannot start without the model list, so one slow or dropped
  // connection must not fail the whole app: 6 attempts, 20 s each, growing backoff.
  let res: Response | undefined;
  let lastError: unknown;
  // Cloudflare answers 52x/530 for short edge or tunnel blips; ~30 s of retries rides them out.
  for (let attempt = 1; attempt <= 6; attempt++) {
    try {
      res = await fetch(`${cfg.serverUrl}/v1/llm/models`, { headers: { Authorization: `Bearer ${cfg.apiKey}` }, signal: AbortSignal.timeout(20_000) });
      if (res.status < 500) break;
      lastError = new Error(`server answered ${res.status}`);
    } catch (err) {
      lastError = err;
    }
    res = undefined;
    if (attempt < 6) await new Promise((r) => setTimeout(r, attempt * 2000));
  }
  if (!res) throw new Error(`Cannot reach ${cfg.serverUrl} (${(lastError as Error)?.message ?? lastError}). Check the network connection and try again.`);
  if (!res.ok) throw new Error(`model list failed: ${res.status} ${await res.text()}`);
  const body = (await res.json()) as { data: Array<{ id: string; input: string[]; context_window: number; max_tokens: number; credits_per_mtok: { input: number; output: number } }> };
  return body.data;
}

export async function createModelRuntime(cfg: RuntimeConfig): Promise<{ runtime: ModelRuntime; choice: ModelChoice }> {
  const modelsPath = join(PI_AGENT_DIR, "models.json");
  if (cfg.mode === "platform") {
    // Pi resolves "$VAR" headers at request time and fails the request when a variable
    // is unset, so every header variable needs a value. Team runs set DA_THINKING=off
    // before this; the chat assistant keeps the model's default ("on").
    process.env.DA_THINKING ||= "on";
    process.env.DA_RUN_ID ||= `run-${randomUUID()}`;
    process.env.DA_MAX_CREDITS ||= "3000";
    const models = await fetchPlatformModels(cfg);
    const config = {
      providers: {
        designaround: {
          name: "DesignAround",
          baseUrl: `${cfg.serverUrl}/v1/llm`,
          api: "openai-completions",
          apiKey: "$DESIGNAROUND_API_KEY",
          authHeader: true,
          headers: { "X-DA-Run-Id": "$DA_RUN_ID", "X-DA-Max-Credits": "$DA_MAX_CREDITS", "X-DA-Thinking": "$DA_THINKING" },
          compat: { supportsStore: false, supportsDeveloperRole: false, supportsReasoningEffort: false, maxTokensField: "max_tokens" },
          models: models.map((m) => ({
            id: m.id,
            name: m.id,
            input: m.input,
            contextWindow: m.context_window,
            maxTokens: m.max_tokens,
            reasoning: false,
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
          })),
        },
      },
    };
    writeFileSync(modelsPath, JSON.stringify(config, null, 2), { mode: 0o600 });
    const runtime = await ModelRuntime.create({ authPath: join(PI_AGENT_DIR, "auth.json"), modelsPath, modelsStorePath: join(PI_AGENT_DIR, "models-store.json"), allowModelNetwork: false, refreshOnCreate: false });
    const ids = models.map((m) => m.id);
    const pick = (want: string) => (ids.includes(want) ? want : ids[0]);
    const rates: ModelChoice["rates"] = {};
    for (const m of models) rates[m.id] = m.credits_per_mtok;
    // DA_CAD_MODEL lets a run pick another platform model for CAD work (e.g. for comparisons).
    const cadWanted = process.env.DA_CAD_MODEL || "da-cad";
    const cad = ids.includes(cadWanted) ? cadWanted : pick("da-designer");
    return { runtime, choice: { provider: "designaround", designer: pick("da-designer"), observer: pick("da-observer"), cad, rates } };
  }

  const byok = cfg.byok!;
  let provider = byok.provider;
  if (byok.baseUrl) {
    provider = "byok";
    const config = {
      providers: {
        byok: {
          name: "BYOK",
          baseUrl: byok.baseUrl,
          api: "openai-completions",
          apiKey: "$DA_BYOK_KEY",
          authHeader: true,
          compat: { supportsStore: false, supportsDeveloperRole: false },
          models: [byok.model, byok.observerModel].filter(Boolean).map((id) => ({ id, name: id, input: ["text", "image"], reasoning: false, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } })),
        },
      },
    };
    writeFileSync(modelsPath, JSON.stringify(config, null, 2), { mode: 0o600 });
  } else {
    writeFileSync(modelsPath, JSON.stringify({ providers: {} }), { mode: 0o600 });
  }
  const runtime = await ModelRuntime.create({ authPath: join(PI_AGENT_DIR, "auth.json"), modelsPath, modelsStorePath: join(PI_AGENT_DIR, "models-store.json"), allowModelNetwork: false, refreshOnCreate: false });
  if (!byok.baseUrl) await runtime.setRuntimeApiKey(provider, byok.key); // in memory only
  return { runtime, choice: { provider, designer: byok.model, observer: byok.observerModel || byok.model, cad: byok.model, rates: {} } };
}

function isolatedLoader(systemPrompt: string): ResourceLoader {
  return {
    getExtensions: () => ({ extensions: [], errors: [], runtime: createExtensionRuntime() }),
    getSkills: () => ({ skills: [], diagnostics: [] }),
    getPrompts: () => ({ prompts: [], diagnostics: [] }),
    getThemes: () => ({ themes: [], diagnostics: [] }),
    getAgentsFiles: () => ({ agentsFiles: [] }),
    getSystemPrompt: () => systemPrompt,
    getSystemPromptSource: () => undefined,
    getAppendSystemPrompt: () => [],
    getAppendSystemPromptSources: () => [],
    extendResources: () => {},
    reload: async () => {},
  } as ResourceLoader;
}

/** Chat transcripts live inside the isolated Pi dir, never in ~/.pi. */
export const CHAT_SESSION_DIR = join(PI_AGENT_DIR, "chat-sessions");

export async function createIsolatedSession(opts: {
  runtime: ModelRuntime;
  provider: string;
  modelId: string;
  systemPrompt: string;
  tools: ToolDefinition<any, any, any>[];
  cwd: string;
  thinking?: "off" | "low" | "medium" | "high";
  /** Persistent transcript (assistant chat). Default: in-memory (team roles). */
  sessionManager?: SessionManager;
}) {
  const model = opts.runtime.getModel(opts.provider, opts.modelId);
  if (!model) throw new Error(`Model not found: ${opts.provider}/${opts.modelId}`);
  const settingsManager = SettingsManager.inMemory({
    compaction: { enabled: true },
    // Edge blips (Cloudflare 502/530, dropped connections) last seconds to a minute:
    // 5 retries with exponential backoff from 3 s ride them out.
    retry: { enabled: true, maxRetries: 5, baseDelayMs: 3000 },
    enableInstallTelemetry: false,
    enableAnalytics: false,
  } as any);
  const { session } = await createAgentSession({
    cwd: opts.cwd,
    agentDir: PI_AGENT_DIR,
    model,
    thinkingLevel: opts.thinking ?? "off",
    modelRuntime: opts.runtime,
    resourceLoader: isolatedLoader(opts.systemPrompt),
    // Only our tools: no read/bash/edit/write on the user's machine.
    tools: opts.tools.map((t) => t.name),
    customTools: opts.tools,
    sessionManager: opts.sessionManager ?? SessionManager.inMemory(opts.cwd),
    settingsManager,
  });
  return session;
}
