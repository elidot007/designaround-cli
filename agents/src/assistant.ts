/**
 * The DesignAround Assistant: a conversational agent on the embedded, isolated
 * Pi runtime. The user talks to it in the chat UI (`designaround ui`, also the
 * desktop app) or in the terminal (`designaround chat`).
 *
 * Every tool runs the `designaround` CLI (`python -m designaround_cli --json`),
 * so the assistant uses exactly the same code paths, budgets, plan checks and
 * evidence log as a person or a host agent using the CLI. Tools that spend
 * credits ask the human first through `confirm()` (UI dialog / terminal y/n);
 * the model cannot skip that gate.
 */
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { isAbsolute, join } from "node:path";
import { defineTool, SessionManager, type ToolDefinition } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { DESIGNAROUND_HOME } from "./env.ts";
import { pngContent } from "./engine.ts";
import { CHAT_SESSION_DIR, createIsolatedSession, type ModelChoice } from "./runtime.ts";

export const ASSISTANT_SYSTEM = `You are the DesignAround Assistant. You help sellers, designers and factories with design-patent (industrial design) risk for physical products: screening registered designs, designing around them, patent drawings, filing packages, prototypes and evidence. You work through tools that run the local DesignAround engine and the DesignAround server.

How you work:
- Answer in the user's language. Be concise and concrete. Use short Markdown (lists, small tables).
- Start from what the user has: a product idea, photos, a CAD file, a known patent, a complaint. Create a case first (case_create) and reuse its id.
- There are no product templates. Every model is designed for this user's requirements.
- New product from an idea (the user wants something designed):
  1. Requirements: find out what you need in one short message, only what is missing: what the product is and does, size, who uses it and where, style, must-have features, things to avoid, process and price level, markets. Store them with case_set (brief).
  2. design_concepts: 2D concept images of distinct directions. Each image is checked by an independent reviewer; failed ones are regenerated automatically. Show the sheet, say which passed and why, and ask the user to pick one or say what to change.
  3. If the user wants changes, design_revise with their words, as often as needed, until they are happy. Never pick for them.
  4. concept_choose records their choice (with their name). Then design_build: the CAD Agent builds that concept in 3D from scratch and an independent 3D reviewer plus size checks must accept it. Show the render sheet and ask the user to confirm before you continue.
     If the build is not accepted, it returns a draft version with the reviewer's remaining differences. Show the draft's render sheet and those differences honestly, then let the user choose: accept the draft themselves (design_accept, with their name), build again, or simplify the concept with design_revise. Never accept a draft for them.
  5. Then screen -> prior_art_rank -> observe (top 1-3) -> evaluate. If the risk is high, go back to design_revise with the observer's findings, or run design_around.
- Existing product (photos or CAD of something the user already has): photos_add + model_from_photos, or model_import_cad, then screen and the steps above.
- Finished design: originality + dfm -> viewpack -> filing_pack -> a registered practitioner reviews. Once the user has filed, record it with filing_record (and later official notices with filing_notice); deadlines then shows the calendar.
- Complaint or lawsuit (a platform IP complaint, a TRO / Schedule A case): defense_intake with the notice the user uploaded -> observe the user's product version against the asserted design and the closest earlier designs (ask first, paid) -> defense_pack -> gate_create (platform_appeal or tro_response) for counsel. For lawsuits, tell the user at once that the answer is due 21 days after service and that they need US litigation counsel.
- After each stage (concepts, 3D build, screening, evaluation, design-around, filing), call decision_card and give the user its verdict, the main reasons and the next step with its cost and time. Offer risk_report when the user wants something to share. To send files to a factory or a practitioner, use share_create (a read-only link that expires) instead of telling the user to e-mail files.
- After any tool that creates a version or image, look at the image before you describe it.
- When a tool result lists next_actions, use them to decide the next step, but ask the user before long or paid steps.
- Every check is a separate step with its own result. If one fails, say what failed and propose the fix; do not skip ahead.

Rules you must follow:
1. Never present results as legal advice. Say "risk assessment" and "a registered practitioner must confirm". Never say a design is "safe" or "does not infringe".
2. Never rate similarity yourself. Similarity scores come only from the observe tool (an isolated observer that does not see your reasoning).
3. The user decides. Show candidates (cad_compare, images) and let the user choose; record the user's choice with case_decide, naming the person.
4. Paid steps (screen, observe, model_from_photos, design_around) cost credits; the user is asked to confirm each one. If the user declines, do not retry it; propose a free alternative.
5. Never try to pay. billing_checkout returns a link the user opens.
6. Do not copy a competitor's design as the user's "original" for filing. Inventors are natural persons.
7. Keep locked parameters and the user's constraints unless the user agrees to change them.`;

/**
 * Wording guard (T1-52): phrases that read as a legal conclusion or a guarantee.
 * The system prompt forbids them; this deterministic check catches what slips through
 * and the interfaces show a correction under the reply.
 */
const FORBIDDEN_WORDING: Array<[RegExp, string]> = [
  [/不(会)?侵权|没有侵权(风险)?|不构成侵权|不存在侵权/, "不侵权"],
  [/保证(能)?(授权|通过|获批)|一定(能)?(授权|获批|通过审查)|稳过|包过/, "保证授权"],
  [/零风险|绝对安全|百分之百安全|100%\s*安全/, "零风险"],
  [/\b(does(n't| not) infringe|non-infringing|no infringement risk|not infringing)\b/i, "does not infringe"],
  [/\b(guarantee[ds]? (to be )?(granted|approval|allowance)|will definitely be granted)\b/i, "guaranteed grant"],
  [/\b(100% safe|completely safe|zero risk|risk-free)\b/i, "zero risk"],
];

export function wordingIssues(text: string): string[] {
  const found = new Set<string>();
  for (const [re, label] of FORBIDDEN_WORDING) if (re.test(text)) found.add(label);
  return [...found];
}

export const WORDING_NOTE = {
  zh: "更正：上面是风险评估，不是法律结论。是否侵权、能否授权，只能由执业专利代理师或律师判断。",
  en: "Correction: the reply above is a risk assessment, not a legal conclusion. Only a registered practitioner or attorney can say whether something infringes or will be granted.",
};

export interface AssistantHost {
  /** Ask the human to approve a paid step. Resolves true to proceed. */
  confirm(request: { tool: string; title: string; credits: number; detail: string; title_en?: string; detail_en?: string; always?: boolean }): Promise<boolean>;
  /** Progress lines while a tool runs (CLI stderr). */
  progress?(toolCallId: string, line: string): void;
}

export interface AssistantDeps {
  python: string;
  mode: "platform" | "byok";
  host: AssistantHost;
  /** Extra env for CLI subprocesses (API key, server URL, BYOK settings). */
  env: NodeJS.ProcessEnv;
}

interface CliResult {
  code: number;
  envelope: any;
  stderr: string;
}

export function runCli(deps: AssistantDeps, args: string[], opts: { signal?: AbortSignal; onLine?: (line: string) => void } = {}): Promise<CliResult> {
  return new Promise((resolve) => {
    const env: NodeJS.ProcessEnv = { ...process.env, ...deps.env, DESIGNAROUND_HOME, DESIGNAROUND_AGENT: "1", DA_MODEL_MODE: deps.mode, PYTHONUNBUFFERED: "1" };
    for (const key of Object.keys(env)) if (key.startsWith("PI_") && key !== "PI_CODING_AGENT_DIR" && key !== "PI_OFFLINE" && key !== "PI_SKIP_VERSION_CHECK" && key !== "PI_TELEMETRY") delete env[key];
    const proc = spawn(deps.python, ["-m", "designaround_cli", "--json", ...args], { env, stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
    let out = "";
    let err = "";
    let partial = "";
    proc.stdout.on("data", (chunk) => (out += chunk));
    proc.stderr.on("data", (chunk) => {
      const text = String(chunk);
      err += text;
      partial += text;
      const lines = partial.split("\n");
      partial = lines.pop() ?? "";
      for (const line of lines) if (line.trim()) opts.onLine?.(line.trim());
    });
    const onAbort = () => proc.kill("SIGTERM");
    opts.signal?.addEventListener("abort", onAbort, { once: true });
    proc.on("close", (code) => {
      opts.signal?.removeEventListener("abort", onAbort);
      const last = out.trim().split("\n").filter((l) => l.trim().startsWith("{")).pop();
      let envelope: any = null;
      try {
        envelope = last ? JSON.parse(last) : null;
      } catch {
        envelope = null;
      }
      resolve({ code: code ?? 6, envelope: envelope ?? { ok: false, kind: "cli_error", data: { message: (err || out).slice(-1500) } }, stderr: err });
    });
  });
}

// ------------------------------------------------------------------ results

const IMAGE_RE = /\.(png|jpe?g)$/i;

/** Image files a tool produced (contact sheets, comparison sheets, view packs). */
export function collectImages(data: any, limit = 8): string[] {
  const out: string[] = [];
  const push = (p: unknown) => {
    if (typeof p === "string" && isAbsolute(p) && IMAGE_RE.test(p) && existsSync(p) && !out.includes(p)) out.push(p);
  };
  if (!data || typeof data !== "object") return out;
  push(data.contact_sheet?.path ?? data.contact_sheet);
  if (data.kind !== "line_viewpack") push(data.path);
  if (data.dir && Array.isArray(data.figures)) for (const f of data.figures) push(join(data.dir, f.png ?? f.jpg ?? ""));
  if (data.viewpack_dir) push(join(data.viewpack_dir, "figures", "fig1_perspective.png"));
  push(data.sheet);
  push(data.render_sheet);
  if (data.kind === "concept_sheet") push(data.path);
  if (Array.isArray(data)) for (const row of data) push(row?.contact_sheet);
  return out.slice(0, limit);
}

function trim(value: any, depth = 0): any {
  if (typeof value === "string") return value.length > 600 ? value.slice(0, 600) + "…" : value;
  if (Array.isArray(value)) {
    const head = value.slice(0, 15).map((v) => trim(v, depth + 1));
    return value.length > 15 ? [...head, `… ${value.length - 15} more`] : head;
  }
  if (value && typeof value === "object") {
    if (depth > 5) return "{…}";
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value)) {
      if (k === "image_urls" || k === "markdown" || k === "files" && Array.isArray(v) && v.length > 6) continue;
      out[k] = trim(v, depth + 1);
    }
    return out;
  }
  return value;
}

type Content = { type: "text"; text: string } | { type: "image"; data: string; mimeType: string };
type ToolDetails = { images: string[]; kind?: string; ok: boolean; exit_code?: number; cost?: unknown; declined?: boolean };
type ToolOut = { content: Content[]; details: ToolDetails };

function toolResult(res: CliResult, modelImages = 2): ToolOut {
  const env = res.envelope;
  const images = collectImages(env?.data);
  const summary = { ok: env?.ok ?? false, exit_code: res.code, kind: env?.kind, data: trim(env?.data), next_actions: env?.next_actions ?? [], cost: env?.cost };
  let text = JSON.stringify(summary, null, 1);
  if (text.length > 9000) text = text.slice(0, 9000) + "\n… (truncated)";
  if (res.code === 2) text = `NEEDS THE USER (exit 2): ${env?.data?.message ?? ""}\n` + text;
  const content: Content[] = [{ type: "text", text }];
  for (const p of images.slice(0, modelImages)) {
    const img = pngContent(p);
    if (/\.jpe?g$/i.test(p)) img.mimeType = "image/jpeg";
    content.push(img);
  }
  return { content, details: { images, kind: env?.kind, ok: env?.ok ?? false, exit_code: res.code, cost: env?.cost ?? null } };
}

// -------------------------------------------------------------------- tools

type Spec = {
  name: string;
  label: string;
  description: string;
  params: any;
  argv: (p: any) => string[];
  /** Credits to confirm with the user before running (null = free). */
  cost?: (p: any, deps: AssistantDeps) => { credits: number; title: string; detail: string; title_en?: string; detail_en?: string; always?: boolean } | null;
  images?: number;
};

const S = Type.String;
const O = Type.Optional;
const offices = Type.Array(Type.Union([Type.Literal("US"), Type.Literal("CN"), Type.Literal("EU")]));

function flags(pairs: Array<[string, unknown]>): string[] {
  const out: string[] = [];
  for (const [flag, value] of pairs) {
    if (value === undefined || value === null || value === "" || value === false) continue;
    if (value === true) out.push(flag);
    else if (Array.isArray(value)) for (const v of value) out.push(flag, String(v));
    else out.push(flag, String(value));
  }
  return out;
}

function kv(record: Record<string, unknown> | undefined, flag: string): string[] {
  return Object.entries(record ?? {}).flatMap(([k, v]) => [flag, `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`]);
}

function tempFile(name: string, content: string): string {
  const dir = mkdtempSync(join(tmpdir(), "da-assistant-"));
  const path = join(dir, name);
  writeFileSync(path, content, { mode: 0o600 });
  return path;
}

const conceptCap = (count: number) => Math.round(120 * count + 200);

const SPECS: Spec[] = [
  { name: "status", label: "Status", description: "Account, plan, credits, model mode and server. Call once at the start of a conversation.", params: Type.Object({}), argv: () => ["auth", "status"] },
  { name: "case_list", label: "List cases", description: "List the user's cases (products).", params: Type.Object({}), argv: () => ["case", "list"] },
  { name: "case_create", label: "Create case", description: "Create a case for one product. Ask for the product type and target markets if unknown.", params: Type.Object({ name: S(), article: S({ description: "Article of manufacture in English, e.g. 'storage box'." }), article_zh: O(S({ description: "Chinese product name, e.g. 收纳箱 (needed for CN screening/filing)." })), jurisdictions: O(offices) }), argv: (p) => ["case", "create", p.name, ...flags([["--article", p.article], ["--article-zh", p.article_zh], ["-j", p.jurisdictions ?? ["US"]]])] },
  { name: "case_show", label: "Show case", description: "Versions, prior art and evidence status of a case.", params: Type.Object({ case_id: S() }), argv: (p) => ["case", "show", p.case_id] },
  {
    name: "case_set",
    label: "Update case",
    description: "Set case fields: design requirements (brief: use, size, users, style, process, price, must_have, avoid, notes), applicant (name, domicile ISO code, address, type natural|juristic, entity_size), inventors ('Name;Residence'), product names, known dimension, Chinese use/design points, constraints (locked_params, target_features, interfaces, keep, avoid).",
    params: Type.Object({
      case_id: S(),
      article: O(S()),
      article_zh: O(S()),
      applicant: O(S()),
      applicant_domicile: O(S()),
      applicant_address: O(S()),
      applicant_type: O(S()),
      entity_size: O(S()),
      fee_reduction: O(Type.Boolean()),
      inventors: O(Type.Array(S())),
      jurisdictions: O(offices),
      known_dimension: O(S()),
      locarno: O(S({ description: "Locarno class, e.g. 06-04" })),
      use_zh: O(S()),
      design_points_zh: O(Type.Array(S())),
      constraints: O(Type.Record(S(), Type.Any(), { description: "Constraints object; replaces the case constraints." })),
      brief: O(Type.Object({
        use: O(S()), size: O(S({ description: "e.g. 240x160x90mm" })), users: O(S()), style: O(S()), process: O(S()), price: O(S()),
        must_have: O(Type.Array(S())), avoid: O(Type.Array(S())), notes: O(S()),
      }, { description: "Design requirements for a new product (merged into the stored brief)." })),
    }),
    argv: (p) => [
      "case", "set", p.case_id,
      ...flags([["--article", p.article], ["--article-zh", p.article_zh], ["--applicant", p.applicant], ["--applicant-domicile", p.applicant_domicile], ["--applicant-address", p.applicant_address], ["--applicant-type", p.applicant_type], ["--entity-size", p.entity_size], ["--inventor", p.inventors], ["-j", p.jurisdictions], ["--known-dimension", p.known_dimension], ["--locarno", p.locarno], ["--use-zh", p.use_zh], ["--design-point-zh", p.design_points_zh]]),
      ...(p.fee_reduction === undefined ? [] : [p.fee_reduction ? "--fee-reduction" : "--no-fee-reduction"]),
      ...(p.constraints ? ["--constraints", tempFile("constraints.yaml", JSON.stringify(p.constraints))] : []),
      ...(p.brief ? ["--brief-json", JSON.stringify(p.brief)] : []),
    ],
  },
  { name: "case_decide", label: "Record decision", description: "Record a decision THE USER made (choice of design, constraint, tweak), with the person's name. Only call after the user stated the decision.", params: Type.Object({ case_id: S(), decision: S(), person: S() }), argv: (p) => ["case", "decide", p.case_id, p.decision, "--person", p.person] },
  {
    name: "batch_screen",
    label: "Batch screening",
    description: "Screen many products from a CSV the user uploaded (columns: name, article, article_zh, jurisdictions like US;CN, image, cad). Creates one case per row; resumable. Paid per row (quick 50 credits, 5 free a month; deep 350).",
    params: Type.Object({ csv: S({ description: "Absolute path of the uploaded CSV." }), deep: O(Type.Boolean()), rows: Type.Number({ description: "Number of data rows in the CSV (for the cost estimate)." }), max_credits: O(Type.Number()) }),
    argv: (p) => ["batch", "screen", p.csv, ...(p.deep ? ["--deep"] : []), "--max-credits", String(Math.round(p.max_credits ?? (p.deep ? 350 : 50) * Math.max(1, p.rows)))],
    cost: (p) => ({ credits: Math.round(p.max_credits ?? (p.deep ? 350 : 50) * Math.max(1, p.rows)), title: `批量${p.deep ? "深度" : "快速"}检索 ${p.rows} 个产品`, detail: `每个产品 ${p.deep ? 350 : 50} 积分${p.deep ? "" : "（每月前 5 次快速检索免费）"}；中断后再运行会接着做。`, title_en: `${p.deep ? "Deep" : "Quick"} screening of ${p.rows} products`, detail_en: `${p.deep ? 350 : 50} credits per product${p.deep ? "" : " (the first 5 quick screens a month are free)"}; an interrupted run resumes where it stopped.` }),
  },
  { name: "decision_card", label: "Decision card", description: "Free. Where the case stands: one-line verdict (proceed / redesign / abandon / pending), reasons from the evaluations, next steps with credits and time, keep-vs-redesign-vs-abandon costs and the managed option. Use it after each stage and when the user asks what to do next.", params: Type.Object({ case_id: S(), lang: O(S({ description: "zh or en" })) }), argv: (p) => ["case", "card", p.case_id, "--lang", p.lang === "en" ? "en" : "zh"] },
  { name: "risk_report", label: "Risk report PDF", description: "Free. Risk assessment report (PDF, zh or en): conclusion, every assessed prior design, closest comparisons, method, blind spots, disclaimer.", params: Type.Object({ case_id: S(), version: O(S()), lang: O(S({ description: "zh or en" })) }), argv: (p) => ["report", p.case_id, ...(p.version ? [p.version] : []), "--lang", p.lang === "en" ? "en" : "zh"] },
  {
    name: "design_concepts",
    label: "Design concepts",
    description: "New design from the case requirements (set them with case_set brief first): the Concept Designer proposes `count` distinct directions, GPT Image draws each one, and an independent reviewer checks fit, manufacturability, text-free image and prior-art likeness; failed concepts are regenerated. Returns a sheet image. Paid: 25 credits per image plus review tokens.",
    params: Type.Object({ case_id: S(), count: O(Type.Number({ default: 4 })), max_credits: O(Type.Number()) }),
    argv: (p) => ["design", "concepts", p.case_id, "--count", String(Math.round(p.count ?? 4)), "--max-credits", String(Math.round(p.max_credits ?? conceptCap(p.count ?? 4)))],
    cost: (p) => ({ credits: Math.round(p.max_credits ?? conceptCap(p.count ?? 4)), title: `生成 ${Math.round(p.count ?? 4)} 个设计方向（2D 概念图）`, detail: `每张图 25 积分，审核不过会自动重画。上限 ${Math.round(p.max_credits ?? conceptCap(p.count ?? 4))} 积分。`, title_en: `${Math.round(p.count ?? 4)} design concepts (2D)`, detail_en: `25 credits per image; images that fail review are redrawn automatically. Cap ${Math.round(p.max_credits ?? conceptCap(p.count ?? 4))} credits.` }),
    images: 1,
  },
  {
    name: "design_revise",
    label: "Revise a concept",
    description: "Refine a concept the user liked with their feedback (their words). Produces `count` refined concepts, reviewed the same way. Paid.",
    params: Type.Object({ case_id: S(), base: S({ description: "Concept id, e.g. c2" }), feedback: S(), count: O(Type.Number({ default: 2 })), max_credits: O(Type.Number()) }),
    argv: (p) => ["design", "concepts", p.case_id, "--base", p.base, "--feedback", p.feedback, "--count", String(Math.round(p.count ?? 2)), "--max-credits", String(Math.round(p.max_credits ?? conceptCap(p.count ?? 2)))],
    cost: (p) => ({ credits: Math.round(p.max_credits ?? conceptCap(p.count ?? 2)), title: `按你的意见修改 ${p.base}`, detail: `生成 ${Math.round(p.count ?? 2)} 个修改版，每张 25 积分。上限 ${Math.round(p.max_credits ?? conceptCap(p.count ?? 2))} 积分。`, title_en: `Revise ${p.base} with your feedback`, detail_en: `${Math.round(p.count ?? 2)} revisions, 25 credits each. Cap ${Math.round(p.max_credits ?? conceptCap(p.count ?? 2))} credits.` }),
    images: 1,
  },
  { name: "concepts", label: "Concepts", description: "Concepts of a case with review results and which one is chosen.", params: Type.Object({ case_id: S() }), argv: (p) => ["design", "list", p.case_id] },
  { name: "concept_sheet", label: "Concept sheet", description: "Image grid of concepts (all current ones, or the given ids) to look at or show the user.", params: Type.Object({ case_id: S(), concepts: O(Type.Array(S())) }), argv: (p) => ["design", "sheet", p.case_id, ...(p.concepts ?? [])], images: 1 },
  { name: "design_accept", label: "Accept version", description: "THE USER accepts a version (usually a 3D draft the reviewer did not accept) as the case's original design. Only after the user said so; give their name.", params: Type.Object({ case_id: S(), version: S(), person: S(), note: O(S()) }), argv: (p) => ["design", "accept", p.case_id, p.version, "--person", p.person, ...flags([["--note", p.note]])] },
  { name: "concept_choose", label: "Choose concept", description: "Record THE USER's choice of a concept, with their name. Only after the user chose.", params: Type.Object({ case_id: S(), concept: S(), person: O(S()), note: O(S()) }), argv: (p) => ["design", "choose", p.case_id, p.concept, ...flags([["--person", p.person], ["--note", p.note]])] },
  {
    name: "design_build",
    label: "Build concept in 3D",
    description: "The CAD Agent builds the chosen concept as a parametric 3D model from scratch. An independent reviewer compares renders with the concept, size is checked against the requirements, then manufacturability. Saved as the case's original design when accepted. Paid; takes a few minutes.",
    params: Type.Object({ case_id: S(), concept: O(S()), known_dimension: O(S({ description: "e.g. 240x160x90mm or length=240mm" })), rounds: O(Type.Number({ default: 4 })), process: O(S()), max_credits: O(Type.Number({ default: 3000 })) }),
    argv: (p) => ["design", "build", p.case_id, ...flags([["--concept", p.concept], ["--known", p.known_dimension], ["--rounds", p.rounds], ["--process", p.process]]), "--max-credits", String(Math.round(p.max_credits ?? 3000))],
    cost: (p) => ({ credits: Math.round(p.max_credits ?? 3000), title: `把 ${p.concept ?? "选定的方案"} 建成 3D 模型`, detail: `CAD Agent 从零建模，独立审核对照概念图，最多 ${p.rounds ?? 4} 次提交。上限 ${Math.round(p.max_credits ?? 3000)} 积分，实际通常少得多。`, title_en: `Build ${p.concept ?? "the chosen concept"} in 3D`, detail_en: `The CAD Agent models it from scratch and an independent reviewer compares it with the concept, up to ${p.rounds ?? 4} submissions. Cap ${Math.round(p.max_credits ?? 3000)} credits; usually far less.` }),
    images: 1,
  },
  { name: "model_import_cad", label: "Import CAD or mesh", description: "Free. Import a CAD file (STEP/IGES/BREP) or a mesh (STL/OBJ/GLB/glTF/PLY/3MF, e.g. a scan) by absolute path (e.g. an uploaded file) as the original. Meshes are repaired (holes, duplicates, normals) and simplified when large; the result lists what was fixed and warnings (units, open mesh). units: mm|cm|m|inch when a mesh file does not say; up: z|y (glTF/GLB default y). Imported files have no parameters: changes need the CAD Agent to rebuild a program.", params: Type.Object({ case_id: S(), path: S(), units: O(S()), up: O(S()) }), argv: (p) => ["model", p.case_id, "--cad", p.path, ...flags([["--units", p.units], ["--up", p.up]])] },
  { name: "photos_add", label: "Add photos", description: "Attach labelled product photos (absolute paths). Views: front, rear, left, right, top, bottom, perspective.", params: Type.Object({ case_id: S(), photos: Type.Record(S(), S()) }), argv: (p) => ["photos", "add", p.case_id, ...Object.entries(p.photos as Record<string, string>).map(([k, v]) => `${k}=${v}`)] },
  {
    name: "model_from_photos",
    label: "CAD Agent from photos",
    description: "For an EXISTING product: the CAD Agent builds a parametric model from the case photos. Needs one real dimension (e.g. 'length=320mm'). Paid. For a new design use design_concepts.",
    params: Type.Object({ case_id: S(), known_dimension: S(), description: O(S()), max_credits: O(Type.Number({ default: 1500 })) }),
    argv: (p) => ["model", p.case_id, "--agent", ...flags([["--known", p.known_dimension], ["--description", p.description], ["--max-credits", Math.round(p.max_credits ?? 1500)]])],
    cost: (p) => ({ credits: Math.round(p.max_credits ?? 1500), title: "CAD Agent：根据照片建 3D 模型", detail: `上限 ${Math.round(p.max_credits ?? 1500)} 积分，实际通常少得多。`, title_en: "CAD Agent: 3D model from the photos", detail_en: `Cap ${Math.round(p.max_credits ?? 1500)} credits; usually far less.` }),
  },
  { name: "cad_program", label: "Run CAD program", description: "Build a CadQuery program you wrote (imports: cadquery, math only; literal PARAMS dict; build(p) returns {part: Workplane}). try=true builds without saving. Save candidates with parent and kind 'candidate'.", params: Type.Object({ case_id: S(), program: S(), try: O(Type.Boolean()), parent: O(S()), kind: O(S()), rationale: O(S()), params: O(Type.Record(S(), Type.Any())) }), argv: (p) => ["cad", "run", p.case_id, tempFile("program.py", p.program), ...flags([["--try", p.try], ["--parent", p.parent], ["--kind", p.kind], ["--rationale", p.rationale]]), ...kv(p.params, "--param")] },
  { name: "cad_tweak", label: "Tweak parameters", description: "New version from an existing one with changed parameters.", params: Type.Object({ case_id: S(), version: S(), set: Type.Record(S(), Type.Any()), rationale: O(S()) }), argv: (p) => ["cad", "tweak", p.case_id, p.version, ...kv(p.set, "--set"), ...flags([["--rationale", p.rationale]])] },
  { name: "cad_params", label: "Parameters", description: "Parameters, ranges, feature groups and locked parameters of a version.", params: Type.Object({ case_id: S(), version: S() }), argv: (p) => ["cad", "params", p.case_id, p.version] },
  { name: "cad_versions", label: "Versions", description: "All versions of a case with risk and qualification.", params: Type.Object({ case_id: S() }), argv: (p) => ["cad", "versions", p.case_id] },
  { name: "cad_sheet", label: "View sheet", description: "Seven-view image of a version (lineart or render) to look at or show the user.", params: Type.Object({ case_id: S(), version: S(), kind: O(S()) }), argv: (p) => ["cad", "sheet", p.case_id, p.version, ...flags([["--kind", p.kind]])], images: 1 },
  { name: "cad_compare", label: "Compare versions", description: "Changed parameters, silhouette similarity per view and risk of two versions.", params: Type.Object({ case_id: S(), a: S(), b: S() }), argv: (p) => ["cad", "compare", p.case_id, p.a, p.b] },
  { name: "cad_claim", label: "Claim scope", description: "Mark a part as unclaimed (broken lines) or claimed.", params: Type.Object({ case_id: S(), version: S(), part: S(), unclaimed: Type.Boolean() }), argv: (p) => ["cad", "claim", p.case_id, p.version, p.part, ...flags([["--unclaimed", p.unclaimed]])] },
  {
    name: "screen",
    label: "Screen registries",
    description: "Search registered designs (US via USPTO, CN via Google Patents; ClearComply) and rank them against the original. Paid: quick 50 credits (5 free per month), deep 350.",
    params: Type.Object({ case_id: S(), deep: O(Type.Boolean()), jurisdictions: O(offices) }),
    argv: (p) => ["screen", p.case_id, ...flags([["--deep", p.deep], ["-j", p.jurisdictions], ["--max-credits", p.deep ? 350 : 50]])],
    cost: (p) => ({ credits: p.deep ? 350 : 50, title: p.deep ? "深度注册库检索" : "快速注册库检索", detail: p.deep ? "固定 350 积分（$3.50）。" : "固定 50 积分（$0.50）；每月前 5 次快速检索免费。", title_en: p.deep ? "Deep registry screening" : "Quick registry screening", detail_en: p.deep ? "350 credits ($3.50)." : "50 credits ($0.50); the first 5 quick screens a month are free." }),
  },
  { name: "prior_art_add", label: "Add prior art", description: "Add a known registered design (number, office, figure image paths).", params: Type.Object({ case_id: S(), number: S(), office: O(S()), title: O(S()), figures: O(Type.Array(S())) }), argv: (p) => ["prior-art", "add", p.case_id, ...flags([["--number", p.number], ["--office", p.office], ["--title", p.title], ["-f", p.figures]])] },
  { name: "prior_art_rank", label: "Rank prior art", description: "Free CPU silhouette ranking of the case's prior art against a version.", params: Type.Object({ case_id: S(), version: O(S()) }), argv: (p) => ["prior-art", "rank", p.case_id, ...flags([["--version", p.version]])] },
  { name: "comparison_sheet", label: "Comparison sheet", description: "The multi-view sheet the observer sees (candidate views over prior-art figures).", params: Type.Object({ case_id: S(), version: S(), prior: S() }), argv: (p) => ["cad", "sheet-vs", p.case_id, p.version, p.prior], images: 1 },
  {
    name: "observe",
    label: "Observer score",
    description: "Isolated ordinary-observer score 0-10 of a version against one prior-art right (median of 3 judgements). Paid: default cap 100 credits, usually a few credits.",
    params: Type.Object({ case_id: S(), version: S(), prior: S(), max_credits: O(Type.Number({ default: 100 })) }),
    argv: (p) => ["observe", p.case_id, p.version, p.prior, "--max-credits", String(Math.round(p.max_credits ?? 100))],
    cost: (p) => ({ credits: Math.round(p.max_credits ?? 100), title: `观察者评分：${p.version} 对比 ${p.prior}`, detail: `上限 ${Math.round(p.max_credits ?? 100)} 积分，实际通常只要几个积分。`, title_en: `Observer score: ${p.version} vs ${p.prior}`, detail_en: `Cap ${Math.round(p.max_credits ?? 100)} credits; usually a few credits.` }),
  },
  { name: "evaluate", label: "Evaluate risk", description: "Fuse silhouette and observer scores into a risk level; lists pending observer scores.", params: Type.Object({ case_id: S(), version: S() }), argv: (p) => ["evaluate", p.case_id, p.version] },
  {
    name: "design_around",
    label: "Design-around run",
    description: "The agent team (Designer + isolated Observer) proposes candidates, checks originality and DFM, scores and repeats. Paid; can take 10-60 minutes. depth: quick (1 round, cap 2900) | standard (3 rounds, cap 5900, default) | deep (6 rounds, cap 14900). Result guarantee: a standard or deep run with no qualified candidate refunds 50% of its spend automatically; the result's `guarantee` field has the refund and the stuck points.",
    params: Type.Object({ case_id: S(), max_credits: Type.Number(), depth: O(Type.Union([Type.Literal("quick"), Type.Literal("standard"), Type.Literal("deep")])), rounds: O(Type.Number()), per_round: O(Type.Number()), target: O(Type.Number()), top: O(Type.Number()), process: O(S()) }),
    argv: (p) => ["around", p.case_id, "--max-credits", String(Math.round(p.max_credits)), ...flags([["--depth", p.depth], ["--rounds", p.rounds], ["--per-round", p.per_round], ["--target", p.target], ["--top", p.top], ["--process", p.process]])],
    cost: (p) => {
      const preset = ({ quick: [1, 3], standard: [3, 3], deep: [6, 4] } as Record<string, number[]>)[p.depth ?? "standard"] ?? [3, 3];
      const rounds = p.rounds ?? preset[0];
      const covered = (p.depth ?? "standard") !== "quick" && rounds >= 3;
      return { credits: Math.round(p.max_credits), title: "规避改型（Agent 团队）", detail: `上限 ${Math.round(p.max_credits)} 积分，${rounds} 轮 × 每轮 ${p.per_round ?? preset[1]} 个候选，需要十几分钟到一小时。${covered ? "如果最后没有任何达标方案，自动退回实际花费的 50%。" : "快速模式不在结果保障范围内。"}`, title_en: "Design-around (agent team)", detail_en: `Cap ${Math.round(p.max_credits)} credits, ${rounds} rounds × ${p.per_round ?? preset[1]} candidates, 15 to 60 minutes.${covered ? " If no candidate qualifies, 50% of the spend is refunded automatically." : " Quick runs are not covered by the result guarantee."}` };
    },
  },
  { name: "originality", label: "Originality gate", description: "Checks a candidate changes the features that matter (not just radii/scale).", params: Type.Object({ case_id: S(), version: S() }), argv: (p) => ["originality", p.case_id, p.version] },
  { name: "dfm", label: "Manufacturability", description: "Wall thickness, draft, undercuts, interfaces (process injection|print3d).", params: Type.Object({ case_id: S(), version: S(), process: O(S()) }), argv: (p) => ["dfm", p.case_id, p.version, ...flags([["--process", p.process]])] },
  { name: "viewpack", label: "Patent drawings", description: "Patent line drawings with rule checks for US (with surface shading), CN or EU. Free plan: watermarked.", params: Type.Object({ case_id: S(), version: S(), office: S() }), argv: (p) => ["viewpack", p.case_id, p.version, "--office", p.office], images: 2 },
  { name: "filing_pack", label: "Filing package", description: "Filing package for a practitioner (US spec/ADS/IDS, CN 简要说明/请求书, EU form data) with checklist. Pro plan.", params: Type.Object({ case_id: S(), version: S(), office: S(), similar: O(S()), planned_date: O(S()) }), argv: (p) => ["filing-pack", p.case_id, p.version, "--office", p.office, ...flags([["--similar", p.similar], ["--planned-date", p.planned_date]])] },
  { name: "quote", label: "Quote", description: "Official fees + practitioner estimate + platform fee for offices.", params: Type.Object({ offices, managed: O(Type.Boolean()) }), argv: (p) => ["quote", ...flags([["-o", p.offices], ["--managed", p.managed]])] },
  { name: "disclose", label: "Record disclosure", description: "Record a public disclosure (listing, social, exhibition, sale) and get grace-period findings.", params: Type.Object({ case_id: S(), kind: S(), date: S() }), argv: (p) => ["disclose", p.case_id, "--kind", p.kind, "--date", p.date] },
  { name: "timeline", label: "Disclosure check", description: "Grace-period findings per office for recorded disclosures against a planned filing date.", params: Type.Object({ case_id: S(), planned_date: O(S()) }), argv: (p) => ["timeline", p.case_id, ...flags([["--planned-date", p.planned_date]])] },
  {
    name: "filing_record",
    label: "Record filing",
    description: "Free. Record a filing the user made: office US|CN|EU|JP|HAGUE, filing date YYYY-MM-DD (HAGUE: international registration date), application number, version. Recomputes the deadline calendar. Only with dates the user stated; never guess.",
    params: Type.Object({ case_id: S(), office: S(), filed: S(), number: O(S()), version: O(S()), designate: O(S({ description: "HAGUE only, e.g. US,EU,CN" })) }),
    argv: (p) => ["filing", "record", p.case_id, "--office", String(p.office).toUpperCase(), "--filed", p.filed, ...flags([["--number", p.number], ["--version", p.version], ["--designate", p.designate]])],
  },
  {
    name: "filing_notice",
    label: "Record notice",
    description: "Free. Record an official notice: kind office_action (审查意见/补正/irregularity), allowance (US notice of allowance, CN 办理登记手续通知书) or granted; date on the notice; due = the time limit printed on the notice when the user has it (it wins over the computed one); mailed = CN notice by post.",
    params: Type.Object({ case_id: S(), office: S(), kind: Type.Union([Type.Literal("office_action"), Type.Literal("allowance"), Type.Literal("granted")]), date: S(), due: O(S()), mailed: O(Type.Boolean()), number: O(S()) }),
    argv: (p) => ["filing", "notice", p.case_id, "--office", String(p.office).toUpperCase(), "--kind", p.kind, "--date", p.date, ...flags([["--due", p.due], ["--number", p.number]]), ...(p.mailed ? ["--mailed"] : [])],
  },
  {
    name: "deadlines",
    label: "Deadline calendar",
    description: "Free. Deadline calendar with legal basis: priority (6 months), grace periods, office-action replies, issue/registration/annual fees, EU renewals, Hague milestones; writes an .ics file with reminders 30/14/7/1 days before. Without case_id, use within (days) to list every case's upcoming deadlines. Tell the user practitioners must confirm dates.",
    params: Type.Object({ case_id: O(S()), within: O(Type.Number()) }),
    argv: (p) => ["deadlines", ...(p.case_id ? [p.case_id] : []), ...flags([["--within", p.within && Math.max(1, Math.min(365, Math.round(p.within)))]])],
  },
  { name: "proto", label: "Prototype pack", description: "STEP/STL/3MF + factory PDF for a version.", params: Type.Object({ case_id: S(), version: S(), process: O(S()) }), argv: (p) => ["proto", p.case_id, p.version, ...flags([["--process", p.process]])] },
  {
    name: "defense_intake",
    label: "Record complaint",
    description: "Free. Record a platform complaint or US lawsuit (platform amazon|temu|walmart|shein|tiktok|ebay|aliexpress|court|other): complaint/case number, asserted right (e.g. 'US D1,029,631 S'), the notice file the user uploaded, dates (received; served for lawsuits; respond_by or hearing when the notice states them), affected listing IDs. Determines the right type and the channel (design patents never go through Amazon APEX; Schedule A / TRO is a court matter with an answer due 21 days after service) and adds a US design patent with its USPTO drawings to the case. Tell the user to involve counsel; never promise an outcome.",
    params: Type.Object({ case_id: S(), platform: S(), number: O(S()), right: O(S()), notice: O(S({ description: "absolute path of the uploaded notice" })), kind: O(S()), received: O(S()), served: O(S()), hearing: O(S()), respond_by: O(S()), listings: O(S({ description: "comma-separated ASINs / listing IDs" })), note: O(S()) }),
    argv: (p) => ["defense", "intake", p.case_id, "--platform", p.platform, ...flags([["--number", p.number], ["--right", p.right], ["--notice", p.notice], ["--kind", p.kind], ["--received", p.received], ["--served", p.served], ["--hearing", p.hearing], ["--respond-by", p.respond_by], ["--note", p.note]]), ...String(p.listings ?? "").split(",").map((x: string) => x.trim()).filter(Boolean).flatMap((x: string) => ["--listing", x])],
  },
  { name: "defense_link", label: "Link asserted design", description: "Free. Mark which prior-art record (with figures) is the asserted design of the latest complaint, when it was added by hand (e.g. a CN or EU design).", params: Type.Object({ case_id: S(), prior: S(), matter: O(S()) }), argv: (p) => ["defense", "link", p.case_id, p.prior, ...flags([["--matter", p.matter]])] },
  { name: "defense_list", label: "Complaints", description: "Free. Complaints and lawsuits recorded on a case with their channel.", params: Type.Object({ case_id: S() }), argv: (p) => ["defense", "list", p.case_id] },
  {
    name: "defense_pack",
    label: "Response pack",
    description: "Free. Response pack for counsel: three-way comparison (asserted design, the user's product version, the closest earlier designs), observer findings on record, earlier-design search record, design timeline from the hash chain (independent-design evidence) and a platform appeal draft (PDF + DOCX). Run observe first for missing scores when the user agrees. Not a legal conclusion.",
    params: Type.Object({ case_id: S(), version: S(), matter: O(S()), priors: O(S({ description: "comma-separated prior-art ids" })), seller: O(S()), lang: O(S()) }),
    argv: (p) => ["defense", "pack", p.case_id, p.version, ...flags([["--matter", p.matter], ["--seller", p.seller], ["--lang", p.lang === "en" ? "en" : undefined]]), ...String(p.priors ?? "").split(",").map((x: string) => x.trim()).filter(Boolean).flatMap((x: string) => ["--prior", x])],
    images: 1,
  },
  {
    name: "rescreen",
    label: "Re-run screening",
    description: "Paid like the original screen (quick 50 credits, 5 free a month; deep 350). Re-runs a recorded screen with its original parameters and lists what changed: new designs (with silhouette score, whether they enter the 3 closest, Locarno class match), designs no longer returned, changed status/date/owner. list=true only lists the records (free).",
    params: Type.Object({ case_id: S(), screen_id: O(S()), list: O(Type.Boolean()), max_credits: O(Type.Number()) }),
    argv: (p) => ["rescreen", p.case_id, ...(p.screen_id ? [p.screen_id] : []), ...(p.list ? ["--list"] : ["--max-credits", String(Math.round(p.max_credits ?? 400))])],
    cost: (p) => (p.list ? null : { credits: Math.round(p.max_credits ?? 400), title: "按原条件复跑检索", detail: "和原检索同价（快速 50 积分，每月前 5 次免费；深度 350 积分），完成后列出新增、消失和变化的外观设计。", title_en: "Re-run the screening as it was", detail_en: "Same price as the original (quick 50 credits, 5 free a month; deep 350); lists new, dropped and changed designs." }),
  },
  {
    name: "monitor_add",
    label: "Watch case",
    description: "Watch a case: its latest screen is repeated every `every` days (when the workbench opens, or `designaround monitor run`), new hits are pushed to the user's chat-bot webhook / desktop. max_credits is the most one re-screen may cost (quick 50, deep 350). Ask the user first; the dialog confirms.",
    params: Type.Object({ case_id: S(), every: O(Type.Number()), max_credits: Type.Number() }),
    argv: (p) => ["monitor", "add", p.case_id, "--every", String(Math.round(p.every ?? 30)), "--max-credits", String(Math.round(p.max_credits))],
    cost: (p) => ({ credits: 0, always: true, title: `监控案卷：每 ${Math.round(p.every ?? 30)} 天复查一次`, detail: `每次复查最多 ${Math.round(p.max_credits)} 积分（快速检索每月前 5 次免费），发现新公开的相似外观时推送提醒。随时可以停止。`, title_en: `Watch this case: re-screen every ${Math.round(p.every ?? 30)} days`, detail_en: `At most ${Math.round(p.max_credits)} credits a run (the first 5 quick screens a month are free); you are notified when similar new designs are published. Stop any time.` }),
  },
  { name: "monitor_list", label: "Watched cases", description: "Free. Watched cases with last/next run.", params: Type.Object({}), argv: () => ["monitor", "list"] },
  { name: "monitor_remove", label: "Stop watching", description: "Free. Stop watching a case.", params: Type.Object({ case_id: S() }), argv: (p) => ["monitor", "remove", p.case_id] },
  {
    name: "share_create",
    label: "Share link",
    description: "Free. Read-only link for a factory (purpose factory: prototype pack STEP/STL/3MF + factory PDF), a practitioner (agent: drawings + filing package) or anyone (other: reports, views). Expires after `days` (1-30); images and PDFs are watermarked with the recipient and share id unless watermark=false; every view/download is logged. Uploads the files to the DesignAround server, so the user must agree first (you will get a confirmation dialog). Give the user the url from the result.",
    params: Type.Object({ case_id: S(), version: O(S()), to: S({ description: "recipient, e.g. 东莞某模具厂" }), purpose: O(Type.Union([Type.Literal("factory"), Type.Literal("agent"), Type.Literal("other")])), days: O(Type.Number()), note: O(S()), watermark: O(Type.Boolean()) }),
    argv: (p) => ["share", "create", p.case_id, ...(p.version ? [p.version] : []), "--to", p.to, ...flags([["--purpose", p.purpose], ["--days", p.days], ["--note", p.note]]), ...(p.watermark === false ? ["--no-watermark"] : [])],
    cost: (p) => ({ credits: 0, always: true, title: `生成只读分享链接：给 ${p.to}`, detail: `${({ factory: "打样包（STEP/STL/3MF + 工厂说明 PDF）", agent: "视图和申请材料", other: "报告和视图" } as Record<string, string>)[p.purpose ?? "factory"]}会上传到 DesignAround 服务器，持有链接的人在 ${p.days ?? 7} 天内可以查看和下载${p.watermark === false ? "（不加水印）" : "，图片和 PDF 带水印"}；每次打开都有记录，可以随时撤回。`, title_en: `Read-only share link for ${p.to}`, detail_en: `${({ factory: "The prototype pack (STEP/STL/3MF + factory PDF)", agent: "The drawings and filing package", other: "The reports and views" } as Record<string, string>)[p.purpose ?? "factory"]} will be uploaded to the DesignAround server; anyone with the link can view and download it for ${p.days ?? 7} days${p.watermark === false ? " (no watermark)" : ", with watermarked images and PDFs"}. Every view is logged and you can revoke it.` }),
  },
  { name: "share_list", label: "Share links", description: "Free. The user's share links with status, expiry, views and downloads.", params: Type.Object({}), argv: () => ["share", "list"] },
  { name: "share_show", label: "Share access log", description: "Free. One share link with its access log (when, view or download, truncated address, country).", params: Type.Object({ share_id: S() }), argv: (p) => ["share", "show", p.share_id] },
  { name: "share_revoke", label: "Revoke share", description: "Free. Close a share link now; the server deletes its files. Only when the user asks.", params: Type.Object({ share_id: S() }), argv: (p) => ["share", "revoke", p.share_id] },
  { name: "evidence", label: "Evidence", description: "action: verify | export | anchor (anchor sends only a hash to the server).", params: Type.Object({ case_id: S(), action: S() }), argv: (p) => ["evidence", p.action, p.case_id] },
  { name: "gate_create", label: "Practitioner gate", description: "Create a review link for a registered practitioner (types: filing_submission, design_review, fto_conclusion, platform_appeal, tro_response, representation).", params: Type.Object({ case_id: S(), type: S(), reviewer_email: O(S()) }), argv: (p) => ["gate", "create", p.case_id, "--type", p.type, ...flags([["--reviewer-email", p.reviewer_email]])] },
  { name: "credits", label: "Credits", description: "Credit balance and recent charges.", params: Type.Object({}), argv: () => ["credits"] },
  { name: "billing_checkout", label: "Checkout link", description: "Create a checkout link (plan pro|team, credits 5k|21k|55k|240k). The user pays in the browser.", params: Type.Object({ plan: O(S()), annual: O(Type.Boolean()), credits: O(S()) }), argv: (p) => ["billing", "checkout", ...flags([["--plan", p.plan], ["--annual", p.annual], ["--credits", p.credits]])] },
  { name: "billing_portal", label: "Manage subscription", description: "Link to the billing portal where the user can cancel the subscription, update the card or download receipts.", params: Type.Object({}), argv: () => ["billing", "portal"] },
];

export function assistantTools(deps: AssistantDeps): ToolDefinition<any, any, any>[] {
  return SPECS.map((spec) =>
    defineTool({
      name: spec.name,
      label: spec.label,
      description: spec.description,
      parameters: spec.params,
      async execute(toolCallId, params, signal): Promise<ToolOut> {
        const cost = spec.cost?.(params, deps) ?? null;
        if (cost) {
          const approved = await deps.host.confirm({ tool: spec.name, ...cost });
          if (!approved) {
            return { content: [{ type: "text", text: `The user declined "${cost.title}" (${cost.credits} credits). Do not retry; offer a free alternative or ask what they prefer.` }], details: { declined: true, ok: false, images: [] } };
          }
        }
        let argv: string[];
        try {
          argv = spec.argv(params);
        } catch (err) {
          return { content: [{ type: "text", text: `Invalid arguments: ${(err as Error).message}` }], details: { ok: false, images: [] } };
        }
        const res = await runCli(deps, argv, { signal, onLine: (line) => deps.host.progress?.(toolCallId, line) });
        return toolResult(res, spec.images ?? 2);
      },
    }),
  );
}

export const ASSISTANT_TOOL_NAMES = SPECS.map((s) => s.name);

// ------------------------------------------------------------------ sessions

export async function listChatSessions() {
  return (await SessionManager.list(DESIGNAROUND_HOME, CHAT_SESSION_DIR)).map((s) => ({ id: s.id, path: s.path, name: s.name, created: s.created, modified: s.modified, messages: s.messageCount, first: s.firstMessage.slice(0, 120) }));
}

export async function openAssistant(opts: { runtime: any; choice: ModelChoice; deps: AssistantDeps; sessionPath?: string }) {
  const sessionManager = opts.sessionPath ? SessionManager.open(opts.sessionPath, CHAT_SESSION_DIR, DESIGNAROUND_HOME) : SessionManager.create(DESIGNAROUND_HOME, CHAT_SESSION_DIR);
  return createIsolatedSession({
    runtime: opts.runtime,
    provider: opts.choice.provider,
    modelId: opts.choice.designer,
    systemPrompt: ASSISTANT_SYSTEM,
    tools: assistantTools(opts.deps),
    cwd: DESIGNAROUND_HOME,
    thinking: "low",
    sessionManager,
  });
}

/** Transcript in a UI-friendly shape (images referenced by local path). */
export function transcript(messages: any[]): any[] {
  const out: any[] = [];
  for (const m of messages) {
    if (m.role === "user") {
      const text = typeof m.content === "string" ? m.content : (m.content ?? []).filter((c: any) => c.type === "text").map((c: any) => c.text).join("\n");
      const images = typeof m.content === "string" ? 0 : (m.content ?? []).filter((c: any) => c.type === "image").length;
      // Pi forwards tool-result images as a user message; those carry no text from the user.
      if (text || images) out.push({ role: "user", text, images, ts: m.timestamp });
    } else if (m.role === "assistant") {
      const text = (m.content ?? []).filter((c: any) => c.type === "text").map((c: any) => c.text).join("");
      const tools = (m.content ?? []).filter((c: any) => c.type === "toolCall").map((c: any) => ({ id: c.id, name: c.name, args: c.arguments }));
      out.push({ role: "assistant", text, tools, error: m.stopReason === "error" ? m.errorMessage : undefined, ts: m.timestamp });
    } else if (m.role === "toolResult") {
      const text = (m.content ?? []).filter((c: any) => c.type === "text").map((c: any) => c.text).join("\n");
      out.push({ role: "tool", id: m.toolCallId, name: m.toolName, ok: m.details?.ok ?? !m.isError, images: m.details?.images ?? [], declined: !!m.details?.declined, text: text.slice(0, 1200), ts: m.timestamp });
    }
  }
  return out;
}
