/**
 * Designing a new product from the user's requirements, with no templates.
 *
 *   concepts:  brief -> Concept Designer plans N distinct directions -> one image each
 *              (GPT Image through the DesignAround server) -> an independent Concept
 *              Reviewer checks every image (fit, manufacturability, no text, prior art)
 *              -> failed ones are regenerated with the reviewer's issues -> a set-level
 *              check that the directions really differ -> sheet for the user.
 *              With --base and --feedback the same loop refines a concept the user liked.
 *   build:     chosen concept -> the CAD Agent writes a CadQuery program from scratch,
 *              iterating with cad_try; cad_submit renders it and asks an independent
 *              3D Reviewer whether it matches the concept, plus geometry checks against
 *              the brief. Only an accepted model is saved (as the case's original),
 *              followed by a manufacturability check.
 *
 * Reviewers run in fresh isolated sessions with no tools and never see the
 * generator's reasoning. Pass/fail is a fixed rule over their scores.
 */
import { randomUUID } from "node:crypto";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { defineTool } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { pngContent } from "./engine.ts";
import { CAD_CONTRACT } from "./prompts.ts";
import { createIsolatedSession } from "./runtime.ts";
import { type TeamDeps, BudgetExceeded, PromptStalled, promptWithBudget, track } from "./team.ts";
import { cadTools } from "./tools.ts";

// ------------------------------------------------------------------ prompts

/** cad_try calls allowed between submissions, and the wall-clock limit of a build run. */
const TRIES_PER_SUBMISSION = Number(process.env.DA_BUILD_TRIES || 6);
const BUILD_DEADLINE_MS = Number(process.env.DA_BUILD_MINUTES || 25) * 60_000;
/** A 3D build is accepted with reviewer match >= 7 and at most this many major differences. */
const MAX_MAJOR_MISMATCHES = 1;


export const CONCEPT_PLANNER_SYSTEM = `You are the Concept Designer of DesignAround. From a seller's requirements you propose ORIGINAL product design directions for a physical consumer product that will be manufactured and may be protected by a design patent.

Rules:
- Every direction must differ from the others in its overall form: silhouette, proportions, opening or access mechanism, how parts are arranged. Colour, texture or corner radius alone is not a different direction.
- Respect the requirements exactly: article, use, size, things to keep, things to avoid, target process and price level.
- Do not resemble the registered designs you are shown, and avoid the generic look of the category.
- Each direction must be manufacturable with the stated process (injection moulding unless told otherwise): sensible wall thickness, draft, no impossible undercuts.
- Each direction will be modelled in parametric CAD next: build the form from clear volumes (extruded profiles, arcs, revolves, lofts, fillets), not from freeform sculpted surfaces, and keep the part count low.
- The image prompt describes ONE product, its form, materials and finish, in English. No text, brand names, logos or people.
- Write title, idea and features in the language of the requirements (Chinese requirements -> Chinese).
Reply with ONLY a JSON object: {"concepts": [{"title": "...", "idea": "one or two sentences", "features": ["distinctive visual feature", ...], "image_prompt": "..."}]}`;

export const CONCEPT_REVIEWER_SYSTEM = `You are an independent concept reviewer for physical product design. You did not create the image. You check one concept image against the seller's requirements and answer with facts, not encouragement.

Score 0-10:
- fit: does the product shown serve the stated article and use, within the stated constraints (size, things to keep, things to avoid)?
- manufacturable: could it be made with the stated process at a sensible cost (walls, draft, undercuts, part count)?
Also report: has_text (any letters, numbers or logos in the image), single_product (exactly one product shown), resembles_prior_art (looks like one of the registered designs shown, if any), issues (concrete problems to fix, max 5, one short sentence each), strengths (max 3). Write issues and strengths in the language of the requirements.
Reply with ONLY a JSON object: {"fit": n, "manufacturable": n, "has_text": bool, "single_product": bool, "resembles_prior_art": bool, "issues": ["..."], "strengths": ["..."]}`;

export const DIVERSITY_REVIEWER_SYSTEM = `You are an independent design reviewer. You see a sheet of product concept images labelled c1, c2, ... Decide which pairs would give an ordinary observer the same overall visual impression (same silhouette, proportions and arrangement of parts). Ignore colour and small details.
Reply with ONLY a JSON object: {"too_similar": [["cA", "cB"]], "notes": "one sentence"}`;

export const BUILD_REVIEWER_SYSTEM = `You are an independent 3D model reviewer. Image 1 is the approved design concept. Image 2 is a sheet of shaded renders of a CAD model (perspective plus front, rear, left, right, top, bottom; each view is scaled to fit its cell). You did not build the model. Judge whether the model reproduces the concept's design: overall silhouette, proportions, parts and their arrangement, and each distinctive feature.
Ignore: colours and materials, surface texture, the items shown inside or on the product in the concept (cosmetics, food, devices), and hardware too small to see at product scale (screws, damper internals).
Score "match" with this scale:
- 0-2: a different kind of product.
- 3-4: the same kind of product, but the silhouette or the proportions clearly differ.
- 5-6: silhouette and proportions are right, but two or more major features are missing or wrong.
- 7-8: silhouette, proportions and part arrangement match; at most one major feature differs; otherwise only details differ.
- 9-10: every major feature and the details match as well.
A major feature is one that changes the overall impression: the outline in any view, a large opening, a moving part and its position, a prominent raised or recessed form, the layout of compartments. Everything else is a detail.
List each mismatch once, most important first, with its severity. Write the texts in the language of the requirements.
Reply with ONLY a JSON object: {"match": 0-10, "matched_features": ["..."], "mismatches": [{"issue": "concrete difference, e.g. lid is flat but the concept has a raised arch", "severity": "major" | "detail", "fix": "what to change in the model"}], "notes": "one sentence"}`;

export const BUILD_PLANNER_SYSTEM = `You are the 3D modelling planner of DesignAround. You read an approved product concept image and the seller's requirements and write a modelling plan that a CadQuery modeller can follow without seeing anything else. You do not write code.

Product frame: X = width (left -> right), Y = depth (front -> back, the front faces -Y), Z = height, base on Z = 0, centred on X = 0 and Y = 0.
Rules:
- The overall size must match the required size. If none is given, choose realistic dimensions for the article.
- Describe the product in the state the concept image shows (a lid or panel that is open in the image is modelled open, at the angle shown).
- Simplify freeform surfaces into forms CadQuery builds reliably: boxes, extrusions of a side or front profile made of lines and arcs, revolves, lofts between two rectangles or circles, intersections of two extruded profiles, fillets.
- side_profile is the outer outline seen from the side: a closed polyline of [y, z] points in mm (y from -depth/2 at the front to +depth/2 at the back, z from 0 to the height), going round once without crossing itself. Give it when the product is not a plain box. front_profile is the same seen from the front: [x, z] points. Use 16-40 points so curves are smooth; the outer form will be the intersection of the two outlines extruded across the product.
- List only the parts that are separate mouldings or move. List the features a viewer notices, with position and size in mm, major ones first.
- Write names and descriptions in English.
Reply with ONLY a JSON object:
{"envelope": {"width": mm, "depth": mm, "height": mm},
 "side_profile": [[y, z], ...] or null, "front_profile": [[x, z], ...] or null,
 "parts": [{"name": "body", "role": "...", "how": "how to model it with CadQuery operations", "size": "W x D x H mm", "position": "..."}],
 "features": [{"name": "...", "severity": "major" | "detail", "where": "...", "size": "...", "how": "..."}],
 "state": "which parts are open or closed and at what angle",
 "order": ["step 1: ...", "step 2: ..."]}`;

/** Helpers the CAD Agent pastes into its programs; they never raise. */
const SOFT_HELPER = `def soft(wp, r, sel=None):
    # fillet edges; shrink the radius when the kernel refuses, never fail
    for k in (1.0, 0.6, 0.35):
        try:
            out = (wp.edges(sel) if sel else wp.edges()).fillet(r * k)
            if out.val().isValid():
                return out
        except Exception:
            pass
    return wp


def rrect(a, b, r):
    # rounded-rectangle sketch; the radius is clamped below half of the shorter side
    a, b = max(abs(a), 0.5), max(abs(b), 0.5)
    r = min(r, a / 2 - 0.2, b / 2 - 0.2)
    s = cq.Sketch().rect(a, b)
    return s.vertices().fillet(r) if r > 0.2 else s`;

export const BUILDER_SYSTEM = `You are the CAD Agent of DesignAround. You turn an approved 2D design concept into an accurate parametric CadQuery model. Write the program from scratch for this design, following the modelling plan you are given.

${CAD_CONTRACT}

CadQuery recipes (these work; do not spend tries testing the API):
- Rounded box: cq.Workplane("XY").box(L, W, H, centered=(True, True, False)).edges("|Z").fillet(r)
- Rounded rectangle in 2D: cq.Workplane("XY").workplane(offset=z).placeSketch(rrect(a, b, r)).extrude(h), with the rrect() helper below (it clamps the radius; a raw Sketch fillet of half the shorter side or more fails with "BRep_API: command not done"). Never call .vertices().fillet() on a Workplane rect: Workplane fillets need a solid ("Cannot find a solid on the stack").
- Loft between two rounded rectangles: cq.Workplane("XY").placeSketch(rrect(a, b, r), rrect(c, d, r2).moved(cq.Location(cq.Vector(0, 0, h)))).loft()
- Side-profile body (loaf, wedge, dome, arch): cq.Workplane("YZ").polyline(pts).close().extrude(W / 2, both=True). On "YZ" the 2D point (u, v) is (Y, Z) and extrude goes along X. Mix in arcs with .moveTo(y0, z0).lineTo(y1, z1).threePointArc((ym, zm), (y2, z2)).close().
- Front-profile body: cq.Workplane("XZ").polyline(pts).close().extrude(D / 2, both=True); on "XZ" the point (u, v) is (X, Z).
- Rounded in two views: side_body.intersect(front_body), where both profiles are a little larger than the final part so their faces do not coincide.
- Revolved part: cq.Workplane("XZ").polyline(points).close().revolve(360, (0, 0, 0), (0, 1, 0))
- Lofted transition: cq.Workplane("XY").rect(a, b).workplane(offset=h).rect(c, d).loft()
- Pocket or slot from the top: solid.faces(">Z").workplane().rect(a, b).cutBlind(-depth); holes: .pushPoints([(x, y), ...]).hole(d, depth)
- Grid of compartments: base.cut(cq.Workplane("XY").workplane(offset=z).pushPoints(pts).rect(a, b).extrude(h))
- Place a part: part.translate((x, y, z)); open a hinged part: part.rotate(hinge_point, (hinge_point[0] + 1, hinge_point[1], hinge_point[2]), deg)
- Separate parts (lid, tray, door) are separate entries in the returned dict; leave a 0.5 mm gap between them.

Robust geometry (the kernel fails with "BRep_API: command not done" or StdFail_NotDone when these are ignored):
- Do NOT use shell() or offset2D() on curved or filleted solids. Make hollow parts by cutting an inner solid: build the outer form with a function, build the same function with every size reduced by the wall, translate it up by the floor thickness, and cut.
- Cutting tools must pass at least 1 mm beyond the faces they cut through. Never let two solids share a face or touch along a tangent; overlap them by 0.5 mm or leave a gap.
- Fillet simple solids first, then cut pockets and holes. Keep radii below half of the smallest face they touch. Check that every computed size is positive before using it. Paste these helpers into the program and use them for every fillet and rounded rectangle:
${SOFT_HELPER}
- Build complex parts from a few large, simple solids. Each part must end as ONE valid solid.

Workflow:
1. Read the modelling plan and the concept image. Get the envelope and the outline in every view right first, then the major features, then details.
2. Build the whole product with cad_try. Compare the returned shaded views with the concept: outline from the front, side and top, proportions, where each part is. Improve it feature by feature. To change a program, send only "edits" (find/replace on the last program you tried); send a complete "program" only to start over. Edits keep each step fast. You have at most ${TRIES_PER_SUBMISSION} tries before each submission; if a try fails, read the failing line it reports and simplify that feature instead of trying variations of the same operation.
3. When the outline, proportions and major features match the concept, call cad_submit (without "program" it submits the last program that built). An independent reviewer compares renders of your model with the concept, and geometry checks run against the requirements.
4. If cad_submit rejects the model, fix the major mismatches first, keep what was matched, and submit again. When it is accepted, stop and reply with one line: "<version>: <what you built>".
Answer in the user's language. Be concise.`;

type Pt = [number, number];

/** A planner profile -> clean closed outline, or null when it cannot be a valid face. */
export function cleanProfile(raw: unknown): Pt[] | null {
  if (!Array.isArray(raw)) return null;
  const pts: Pt[] = [];
  for (const p of raw) {
    if (!Array.isArray(p) || p.length < 2) return null;
    const u = Number(p[0]);
    const v = Number(p[1]);
    if (!Number.isFinite(u) || !Number.isFinite(v)) return null;
    const last = pts.at(-1);
    if (last && Math.abs(last[0] - u) < 0.01 && Math.abs(last[1] - v) < 0.01) continue;
    pts.push([u, v]);
  }
  const first = pts[0];
  const end = pts.at(-1);
  if (pts.length > 2 && Math.abs(first[0] - end![0]) < 0.01 && Math.abs(first[1] - end![1]) < 0.01) pts.pop();
  if (pts.length < 3 || pts.length > 80) return null;
  const us = pts.map((p) => p[0]);
  const vs = pts.map((p) => p[1]);
  const du = Math.max(...us) - Math.min(...us);
  const dv = Math.max(...vs) - Math.min(...vs);
  if (du < 1 || dv < 1) return null;
  let area = 0;
  pts.forEach((p, i) => {
    const q = pts[(i + 1) % pts.length];
    area += p[0] * q[1] - q[0] * p[1];
  });
  return Math.abs(area) / 2 >= 0.2 * du * dv ? pts : null;
}

const py = (pts: Pt[] | null) => (pts ? `[${pts.map(([u, v]) => `(${+u.toFixed(2)}, ${+v.toFixed(2)})`).join(", ")}]` : "None");

/**
 * Starter program for the CAD Agent, generated from this concept's modelling plan:
 * the outer form as the intersection of the side and front outlines, stretched to the
 * envelope so the size is right by construction. Nothing in it is product-specific code.
 */
export function starterProgram(env: { width: number; depth: number; height: number }, side: Pt[] | null, front: Pt[] | null): string {
  const r = Math.max(1, Math.round(Math.min(env.width, env.depth, env.height) * 0.03));
  const param = (v: number) => `{"default": ${+v.toFixed(1)}, "min": ${+(v * 0.5).toFixed(1)}, "max": ${+(v * 1.5).toFixed(1)}, "unit": "mm"}`;
  return `import cadquery as cq

# Starter generated from the modelling plan: the OUTER FORM only, as the intersection of
# the outline seen from the side and the outline seen from the front. Extend it with the
# openings, moving parts, compartments and details of the concept.
# Hollow it like this (no shell()):
#   t = 2.5; inner = outer(dict(p, width=p["width"] - 2 * t, depth=p["depth"] - 2 * t, height=p["height"] - 2 * t)).translate((0, 0, t))
#   body = outer(p).cut(inner)
PARAMS = {
    "width": ${param(env.width)},
    "depth": ${param(env.depth)},
    "height": ${param(env.height)},
    "edge_radius": {"default": ${r}, "min": 0, "max": ${r * 3}, "unit": "mm"},
}
SIDE = ${py(side)}  # outline seen from the left: (y, z), front is -y
FRONT = ${py(front)}  # outline seen from the front: (x, z)


${SOFT_HELPER}


def fit(points, span, height):
    # stretch an outline to [-span/2, span/2] x [0, height]
    us = [u for u, _ in points]
    vs = [v for _, v in points]
    u0, u1, v0, v1 = min(us), max(us), min(vs), max(vs)
    return [((u - u0) / (u1 - u0) * span - span / 2, (v - v0) / (v1 - v0) * height) for u, v in points]


def outer(p):
    W, D, H = p["width"], p["depth"], p["height"]
    if SIDE:
        form = cq.Workplane("YZ").polyline(fit(SIDE, D, H)).close().extrude(W / 2 + (2 if FRONT else 0), both=True)
    else:
        form = cq.Workplane("XY").box(W, D, H, centered=(True, True, False))
    if FRONT:
        form = form.intersect(cq.Workplane("XZ").polyline(fit(FRONT, W, H)).close().extrude(D / 2 + 2, both=True))
    return soft(form, p["edge_radius"]) if p["edge_radius"] > 0 else form


def build(p):
    return {"body": outer(p)}
`;
}

/** Build the starter from the plan; the first variant that builds a valid solid wins. */
async function makeStarter(deps: TeamDeps, plan: Json, known: string | undefined): Promise<{ program: string; sheet: string; variant: string } | null> {
  const env0 = plan.envelope ?? {};
  let env = { width: Number(env0.width), depth: Number(env0.depth), height: Number(env0.height) };
  const triple = (known ?? "").replace(/\s/g, "").match(/^(\d+(?:\.\d+)?)[x×*](\d+(?:\.\d+)?)[x×*](\d+(?:\.\d+)?)/i);
  if (![env.width, env.depth, env.height].every((v) => Number.isFinite(v) && v > 0)) {
    if (!triple) return null;
    env = { width: Number(triple[1]), depth: Number(triple[2]), height: Number(triple[3]) };
  }
  const side = cleanProfile(plan.side_profile);
  const front = cleanProfile(plan.front_profile);
  const variants: Array<[string, Pt[] | null, Pt[] | null]> = [];
  if (side && front) variants.push(["side+front", side, front]);
  if (side) variants.push(["side", side, null]);
  if (front) variants.push(["front", null, front]);
  for (const [variant, s, f] of variants) {
    const program = starterProgram(env, s, f);
    try {
      const built = await deps.engine.call("try_program", { source: program, params: {}, renders: true });
      if (built.parts.every((part: Json) => part.valid)) {
        deps.emit({ type: "build_starter", ok: true, variant });
        return { program, sheet: built.render_sheet, variant };
      }
    } catch {
      // try the next variant
    }
  }
  deps.emit({ type: "build_starter", ok: false });
  return null;
}

/** Apply find/replace edits; each `find` must occur exactly once. Returns the program or an error for the agent. */
export function applyEdits(base: string | undefined, edits: Array<{ find: string; replace: string }> | undefined): string | { error: string } {
  if (base === undefined) return { error: "EDIT FAILED: there is no previous program; send the complete program in `program`." };
  let out = base;
  for (const [i, edit] of (edits ?? []).entries()) {
    const count = edit.find ? out.split(edit.find).length - 1 : 0;
    if (count !== 1) {
      return { error: `EDIT FAILED: edit ${i + 1} "find" text occurs ${count} times in the program (it must occur exactly once). No edit was applied. Copy the exact text, with its indentation, or send the complete program.` };
    }
    out = out.replace(edit.find, () => edit.replace);
  }
  return out;
}

/** Errors the CAD Agent tends to repeat, with the advice that gets it unstuck. */
export function kernelHint(message: string): string {
  if (/Cannot find a solid on the stack/i.test(message))
    return "\nHINT: a Workplane operation that needs a solid (fillet, chamfer, shell, cut, hole, edges/faces selection) was called on 2D geometry or an empty workplane. For a rounded 2D outline use cq.Sketch().rect(a, b).vertices().fillet(r) with placeSketch(...).extrude(h); extrude or loft profiles before filleting; start cuts from a solid (solid.cut(tool)).";
  if (/BRep_API|command not done|StdFail_NotDone|Standard_Failure|Standard_ConstructionError|BRepOffset|null shape|not a valid solid/i.test(message))
    return "\nHINT: the geometry kernel could not do one operation. Do not retry the same call with other numbers. Replace shell()/offset2D() with cutting an inner solid, make cutting tools pass 1 mm beyond the faces they cut, avoid coincident or tangent faces, apply fillets with soft() before cutting, and split the feature into simpler solids.";
  return "";
}

/** "issue" strings or {issue, severity, fix} objects -> one normalised list, major first. */
export function normaliseMismatches(raw: unknown): Array<{ issue: string; severity: "major" | "detail"; fix?: string }> {
  if (!Array.isArray(raw)) return [];
  const rows = raw
    .map((m: any) => {
      if (typeof m === "string") return { issue: m, severity: "major" as const };
      if (!m || typeof m !== "object" || !m.issue) return null;
      return { issue: String(m.issue), severity: m.severity === "detail" ? ("detail" as const) : ("major" as const), ...(m.fix ? { fix: String(m.fix) } : {}) };
    })
    .filter(Boolean) as Array<{ issue: string; severity: "major" | "detail"; fix?: string }>;
  return [...rows.filter((r) => r.severity === "major"), ...rows.filter((r) => r.severity === "detail")];
}

/** Fixed suffix: one clean product shot that reviewers and the CAD Agent can read. */
const SHOT = "Product concept rendering for an industrial design review. One product only, centred, three-quarter front view from slightly above, the whole product visible with margin, plain light warm-grey seamless background, soft studio light, realistic materials and finish. No text, letters, numbers, logos, labels, packaging graphics, people or hands.";

// ------------------------------------------------------------------ helpers

type Json = Record<string, any>;
type ToolContent = { type: "text"; text: string } | { type: "image"; data: string; mimeType: string };
type ToolResult = { content: ToolContent[]; details: Json };

export function parseJsonObject(text: string): Json {
  const start = text.indexOf("{");
  const end = text.lastIndexOf("}");
  if (start < 0 || end <= start) throw new Error("no JSON object in reply");
  return JSON.parse(text.slice(start, end + 1));
}

/** Compact JPEG copy for the model (falls back to the original file). */
async function modelImage(deps: TeamDeps, path: string): Promise<string> {
  try {
    return (await deps.engine.call("model_image", { path })).path;
  } catch {
    return path;
  }
}

async function askJson(deps: TeamDeps, role: string, modelId: string, system: string, user: string, images: string[] = []): Promise<Json> {
  // Fresh session per question: no tools, no memory, never sees the generator's reasoning.
  const sent = await Promise.all(images.map((p) => modelImage(deps, p)));
  const session = await createIsolatedSession({ runtime: deps.runtime, provider: deps.choice.provider, modelId, systemPrompt: system, tools: [], cwd: deps.cwd });
  const stop = track(deps, session, modelId, role);
  try {
    const reply = await promptWithBudget(deps, session, user, sent.map((p) => imageContent(p)));
    try {
      return parseJsonObject(reply);
    } catch {
      return parseJsonObject(await promptWithBudget(deps, session, "Return ONLY the JSON object described in the instructions, with no other text."));
    }
  } finally {
    stop();
    session.dispose();
  }
}

function imageContent(path: string) {
  const img = pngContent(path);
  if (/\.jpe?g$/i.test(path)) img.mimeType = "image/jpeg";
  if (/\.webp$/i.test(path)) img.mimeType = "image/webp";
  return img;
}

async function pool<T, R>(items: T[], limit: number, fn: (item: T, index: number) => Promise<R>): Promise<R[]> {
  const out: R[] = new Array(items.length);
  let next = 0;
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (next < items.length) {
      const i = next++;
      out[i] = await fn(items[i], i);
    }
  });
  await Promise.all(workers);
  return out;
}

const TRANSIENT = new Set([408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 530]);

/** JSON request with retries for network errors and transient edge/server statuses. */
async function requestJson(url: string, init: RequestInit, what: string, attempts = 5): Promise<Json> {
  let last = "";
  for (let i = 1; i <= attempts; i++) {
    try {
      const res = await fetch(url, { ...init, signal: AbortSignal.timeout(60_000) });
      if (res.ok) return await res.json();
      const text = (await res.text()).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim().slice(0, 200);
      last = `${res.status} ${text}`;
      if (!TRANSIENT.has(res.status)) break;
    } catch (err) {
      last = (err as Error).message;
    }
    if (i < attempts) await new Promise((r) => setTimeout(r, Math.min(15_000, 1500 * 2 ** (i - 1))));
  }
  throw new Error(`${what} failed: ${last}`);
}

/** One concept image from the DesignAround server (GPT Image), as a local PNG path. */
export async function generateImage(deps: TeamDeps, prompt: string, reference?: string): Promise<{ path: string; model: string; charged: number }> {
  if (deps.mode !== "platform") throw new Error("config: concept images need platform mode (designaround config mode platform); BYOK and host mode do not include an image model.");
  const base = (process.env.DA_SERVER_URL || "https://designaround.elidot.com").replace(/\/$/, "");
  const headers: Record<string, string> = {
    Authorization: `Bearer ${process.env.DESIGNAROUND_API_KEY}`,
    "Content-Type": "application/json",
    "X-DA-Run-Id": process.env.DA_RUN_ID || "",
    "X-DA-Max-Credits": process.env.DA_MAX_CREDITS || "",
  };
  deps.budget.check();
  // The client id makes the start request idempotent: a retry after a lost response
  // returns the same job instead of starting (and paying for) a second image.
  const clientId = randomUUID().replace(/-/g, "");
  const body: Json = { prompt: `${prompt.trim()}\n\n${SHOT}`, size: "1024x1024", client_id: clientId };
  if (reference) body.reference_image_base64 = readFileSync(reference).toString("base64");
  const job = (await requestJson(`${base}/v1/images`, { method: "POST", headers, body: JSON.stringify(body) }, "image request")).data;
  const deadline = Date.now() + 300_000;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, Number(process.env.DA_IMAGE_POLL_MS || 3000)));
    const data = (await requestJson(`${base}/v1/images/${job.job_id}`, { headers }, "image status")).data;
    if (data.status === "running") continue;
    if (data.status !== "done") throw new Error(`image generation failed: ${data.message ?? data.status}`);
    const dir = mkdtempSync(join(tmpdir(), "da-concept-"));
    const path = join(dir, "concept.png");
    writeFileSync(path, Buffer.from(data.image_base64, "base64"));
    const charged = Number(data.charged ?? 0);
    deps.budget.spent += charged;
    deps.emit({ type: "cost", role: "concept_image", model: data.model, credits: charged, spent: deps.budget.spent, max: deps.budget.max });
    if (deps.budget.spent > deps.budget.max) throw new BudgetExceeded(`budget ${deps.budget.max} exceeded (spent ${deps.budget.spent})`);
    return { path, model: data.model, charged };
  }
  throw new Error("image generation timed out after 5 minutes");
}

function requirementsText(caseInfo: Json): string {
  const brief = caseInfo.brief ?? {};
  const constraints = caseInfo.constraints ?? {};
  const lines = [
    `Article: ${caseInfo.article ?? brief.article ?? "?"}${caseInfo.article_zh ? ` (${caseInfo.article_zh})` : ""}`,
    brief.use || caseInfo.use_zh ? `Use: ${brief.use ?? caseInfo.use_zh}` : "",
    brief.size || caseInfo.known_dimension ? `Size: ${brief.size ?? caseInfo.known_dimension}` : "",
    brief.users ? `Users and setting: ${brief.users}` : "",
    brief.style ? `Style: ${brief.style}` : "",
    brief.process ? `Manufacturing: ${brief.process}` : "",
    brief.price ? `Price level: ${brief.price}` : "",
    (brief.must_have ?? constraints.keep)?.length ? `Must keep: ${(brief.must_have ?? constraints.keep).join("; ")}` : "",
    (brief.avoid ?? constraints.avoid)?.length ? `Avoid: ${(brief.avoid ?? constraints.avoid).join("; ")}` : "",
    brief.notes ? `Notes: ${brief.notes}` : "",
    caseInfo.description ? `Description: ${caseInfo.description}` : "",
    `Markets: ${(caseInfo.jurisdictions ?? []).join(", ")}`,
  ];
  return lines.filter(Boolean).join("\n");
}

async function priorArtFigures(deps: TeamDeps, caseInfo: Json, limit = 3): Promise<string[]> {
  const rows: Json[] = await deps.engine.call("prior_art_list", { case_id: deps.caseId });
  return rows.flatMap((r) => (r.figures ?? []).slice(0, 1)).slice(0, limit).map((rel: string) => join(caseInfo.root, rel));
}

// ----------------------------------------------------------------- concepts

export interface ConceptOptions {
  count: number;
  base?: string;
  feedback?: string;
  attempts?: number;
}

async function reviewConcept(deps: TeamDeps, requirements: string, image: string, priorFigures: string[]): Promise<Json> {
  const user = `Seller's requirements:\n${requirements}\n\nImage 1 is the concept to review.${priorFigures.length ? ` Images 2-${priorFigures.length + 1} are registered designs found for this product; set resembles_prior_art if the concept looks like one of them.` : ""}`;
  const review = await askJson(deps, "concept_reviewer", deps.choice.observer, CONCEPT_REVIEWER_SYSTEM, user, [image, ...priorFigures]);
  review.model = deps.choice.observer;
  if (review.resembles_prior_art) review.issues = [...(review.issues ?? []), "looks like a registered design shown to the reviewer"];
  // resembling prior art fails the concept the same way text in the image does
  if (review.resembles_prior_art) review.fit = Math.min(Number(review.fit ?? 0), 5);
  return review;
}

export async function runConcepts(deps: TeamDeps, opts: ConceptOptions) {
  const caseInfo: Json = await deps.engine.call("case_show", { case_id: deps.caseId });
  const requirements = requirementsText(caseInfo);
  const priorFigures = await priorArtFigures(deps, caseInfo);
  const existing: Json[] = await deps.engine.call("concepts", { case_id: deps.caseId });
  const base: Json | null = opts.base ? await deps.engine.call("concept_show", { case_id: deps.caseId, cid: opts.base }) : null;
  const attempts = Math.max(1, opts.attempts ?? 2);
  const count = Math.max(1, Math.min(6, opts.count));

  // 1. plan distinct directions
  const planUser = [
    `Requirements:\n${requirements}`,
    base ? `The seller likes concept ${base.id} ("${base.title}": ${base.idea}), shown as image 1. Their feedback: "${opts.feedback ?? "refine it"}". Propose ${count} refinements that keep what they like and apply the feedback; they may differ from each other in the details the feedback leaves open.` : `Propose ${count} distinct directions.`,
    existing.length ? `Already proposed (do not repeat): ${existing.map((c) => `${c.id} ${c.title}`).join("; ")}` : "",
    priorFigures.length ? `The last ${priorFigures.length} image(s) are registered designs to stay away from.` : "",
  ].filter(Boolean).join("\n\n");
  const planImages = [...(base ? [base.image] : []), ...priorFigures];
  const plan = await askJson(deps, "concept_designer", deps.choice.designer, CONCEPT_PLANNER_SYSTEM, planUser, planImages);
  const directions: Json[] = (plan.concepts ?? []).filter((d: Json) => d && d.image_prompt).slice(0, count);
  if (!directions.length) return { status: "needs_input", reason: "The concept designer returned no directions.", reply: JSON.stringify(plan).slice(0, 500) };
  deps.emit({ type: "concepts_planned", count: directions.length, titles: directions.map((d) => d.title) });

  // 2. image + independent review per direction; regenerate with the reviewer's issues
  const makeOne = async (dir: Json, extraInstruction = "", supersede?: string): Promise<Json> => {
    let prompt = `${dir.image_prompt}${extraInstruction ? `\n${extraInstruction}` : ""}`;
    let previous: string | undefined = supersede;
    let concept: Json | null = null;
    for (let attempt = 1; attempt <= attempts; attempt++) {
      const image = await generateImage(deps, prompt, base && attempt === 1 && !extraInstruction ? base.image : undefined);
      concept = await deps.engine.call("concept_save", {
        case_id: deps.caseId, image_path: image.path, title: dir.title ?? "Concept", idea: dir.idea ?? "", prompt, features: dir.features ?? [],
        round: attempt, parent: previous ?? base?.id ?? null, created_by: "agent", model: image.model,
      });
      if (previous) await deps.engine.call("concept_supersede", { case_id: deps.caseId, cid: previous, by: concept!.id });
      const review = await reviewConcept(deps, requirements, concept!.image, priorFigures);
      concept = await deps.engine.call("concept_review", { case_id: deps.caseId, cid: concept!.id, review });
      deps.emit({ type: "concept", concept: concept!.id, title: concept!.title, pass: concept!.review.pass, fit: review.fit, manufacturable: review.manufacturable, issues: review.issues ?? [] });
      if (concept!.review.pass) break;
      previous = concept!.id;
      prompt = `${dir.image_prompt}\nThe previous image was rejected by a reviewer. Fix: ${(review.issues ?? []).join("; ") || "match the requirements more closely"}.`;
    }
    return concept!;
  };
  let made = await pool(directions, 2, (dir) => makeOne(dir));

  // 3. the set must contain genuinely different directions
  if (made.length > 1 && !base) {
    const sheet = await deps.engine.call("concept_sheet", { case_id: deps.caseId, ids: made.map((c) => c.id) });
    const verdict = await askJson(deps, "diversity_reviewer", deps.choice.observer, DIVERSITY_REVIEWER_SYSTEM, `Concepts: ${made.map((c) => c.id).join(", ")}.`, [sheet.path]);
    const pairs: string[][] = Array.isArray(verdict.too_similar) ? verdict.too_similar.filter((p: any) => Array.isArray(p) && p.length === 2) : [];
    deps.emit({ type: "diversity", too_similar: pairs, notes: verdict.notes ?? "" });
    const redo = [...new Set(pairs.map((p) => p[1]))].filter((id) => made.some((c) => c.id === id));
    for (const id of redo) {
      const i = made.findIndex((c) => c.id === id);
      const twin = pairs.find((p) => p[1] === id)![0];
      const other = made.find((c) => c.id === twin);
      made[i] = await makeOne(directions[i], `It must look clearly different from "${other?.title ?? twin}" (${other?.idea ?? ""}): change the overall silhouette and the arrangement of parts.`, id);
    }
  }

  const sheet = await deps.engine.call("concept_sheet", { case_id: deps.caseId, ids: made.map((c) => c.id) });
  const passed = made.filter((c) => c.review?.pass);
  const rows = made.map((c) => ({ id: c.id, title: c.title, idea: c.idea, features: c.features, pass: c.review?.pass, fit: c.review?.fit, manufacturable: c.review?.manufacturable, issues: c.review?.issues ?? [], image: c.image }));
  return passed.length
    ? { status: "ok", concepts: rows, passed: passed.map((c) => c.id), sheet: sheet.path }
    : { status: "needs_input", reason: "No concept passed the independent review; adjust the requirements or give feedback on the closest one.", concepts: rows, passed: [], sheet: sheet.path };
}

// -------------------------------------------------------------------- build

const DIMENSION_AXES: Record<string, (b: Json) => number> = {
  width: (b) => b.xlen,
  depth: (b) => b.ylen,
  height: (b) => b.zlen,
  length: (b) => Math.max(b.xlen, b.ylen),
  diameter: (b) => Math.max(b.xlen, b.ylen),
};

/** "length=240mm" or "240x160x90mm" -> geometry issues against the bounding box (10 % tolerance). */
export function sizeIssues(known: string | undefined, bbox: Json): string[] {
  if (!known) return [];
  const issues: string[] = [];
  const triple = known.replace(/\s/g, "").match(/^(\d+(?:\.\d+)?)[x×*](\d+(?:\.\d+)?)[x×*](\d+(?:\.\d+)?)/i);
  if (triple) {
    const want = triple.slice(1, 4).map(Number).sort((a, b) => b - a);
    const have = [bbox.xlen, bbox.ylen, bbox.zlen].sort((a: number, b: number) => b - a);
    want.forEach((w, i) => {
      if (Math.abs(have[i] - w) > w * 0.1) issues.push(`overall size ${have.map((v: number) => v.toFixed(0)).join("×")} mm does not match the required ${want.join("×")} mm`);
    });
    return [...new Set(issues)];
  }
  for (const part of known.split(/[,;]/)) {
    const m = part.trim().match(/^([a-z]+)\s*[=:]\s*(\d+(?:\.\d+)?)\s*(mm|cm)?$/i);
    if (!m) continue;
    const axis = DIMENSION_AXES[m[1].toLowerCase()];
    if (!axis) continue;
    const want = Number(m[2]) * (m[3]?.toLowerCase() === "cm" ? 10 : 1);
    const have = axis(bbox);
    if (Math.abs(have - want) > want * 0.1) issues.push(`${m[1]} is ${have.toFixed(1)} mm, required ${want} mm (±10 %)`);
  }
  return issues;
}

export interface BuildOptions {
  concept?: string;
  knownDimension?: string;
  rounds?: number;
  process?: string;
}

export async function runBuild(deps: TeamDeps, opts: BuildOptions) {
  const caseInfo: Json = await deps.engine.call("case_show", { case_id: deps.caseId });
  const cid = opts.concept ?? caseInfo.chosen_concept;
  if (!cid) return { status: "needs_input", reason: "Choose a concept first (design choose <case> <concept>)." };
  const concept: Json = await deps.engine.call("concept_show", { case_id: deps.caseId, cid });
  const known = opts.knownDimension ?? caseInfo.brief?.size ?? caseInfo.known_dimension;
  const process = opts.process ?? caseInfo.brief?.process_id ?? "injection";
  const maxSubmissions = Math.max(1, opts.rounds ?? 4);
  const requirements = requirementsText(caseInfo);
  const features = (concept.features ?? []).join("; ") || "(read them from the image)";
  const state: {
    submissions: number; tries: number; triesSinceSubmit: number; accepted?: Json; best?: Json; reply?: string;
    lastProgram?: string; lastBuilt?: { program: string; params: Json };
  } = { submissions: 0, tries: 0, triesSinceSubmit: 0 };

  // 1. A modelling plan from a fresh session: envelope, profiles, parts, features, order.
  //    The CAD Agent follows it; the reviewer never sees it (it judges against the concept only).
  let plan: Json | null = null;
  try {
    plan = await askJson(deps, "build_planner", deps.choice.cad, BUILD_PLANNER_SYSTEM,
      `Requirements:\n${requirements}\n\nRequired size: ${known ?? "not given"}\nManufacturing process: ${process}\nConcept "${concept.title}": ${concept.idea}\nDistinctive features: ${features}\nThe concept image is attached.`,
      [concept.image]);
    deps.emit({ type: "build_plan", envelope: plan.envelope ?? null, parts: (plan.parts ?? []).map((part: Json) => part.name), features: (plan.features ?? []).length });
  } catch (err) {
    if (err instanceof BudgetExceeded) throw err;
    deps.emit({ type: "build_plan", error: (err as Error).message.slice(0, 200) });
  }

  // A starter program for the outer form, generated from the plan's outlines and checked
  // to build: the agent starts from a valid solid of the right size instead of a blank page.
  const starter = plan ? await makeStarter(deps, plan, known) : null;
  if (starter) {
    state.lastProgram = starter.program;
    state.lastBuilt = { program: starter.program, params: {} };
  }

  const deadline = Date.now() + BUILD_DEADLINE_MS;

  // 2. cad_try shows the same shaded views the reviewer will see, plus the size check.
  const tryTool = defineTool({
    name: "cad_try",
    label: "Try CAD program",
    description: "Build a CadQuery program in a sandbox WITHOUT saving it. Returns the bounding box, parts, size check against the requirements and a sheet of shaded views (perspective, front, rear, left, right, top, bottom), the same views the reviewer compares with the concept. Send the complete program the first time; after that send only edits to the last program you tried.",
    parameters: Type.Object({
      program: Type.Optional(Type.String({ description: "Complete CadQuery program with PARAMS and build(p). Omit it to edit the last program you tried." })),
      edits: Type.Optional(Type.Array(Type.Object({
        find: Type.String({ description: "Exact text that occurs once in the program (copy it, with its indentation)." }),
        replace: Type.String({ description: "Replacement text." }),
      }), { description: "Find/replace edits applied in order to the last program you tried (or to `program` when given)." })),
      params: Type.Optional(Type.Record(Type.String(), Type.Any(), { description: "Parameter overrides." })),
    }),
    async execute(_id, p): Promise<ToolResult> {
      if (Date.now() > deadline) return { content: [{ type: "text", text: "TIME LIMIT: stop trying. Call cad_submit now (without a program it submits the last program that built)." }], details: { error: "time" } };
      if (state.triesSinceSubmit >= TRIES_PER_SUBMISSION) return { content: [{ type: "text", text: `TRY LIMIT: ${TRIES_PER_SUBMISSION} tries used. Call cad_submit now (without a program it submits the last program that built); the reviewer will tell you what to fix.` }], details: { error: "limit" } };
      const source = applyEdits(p.program ?? state.lastProgram, p.edits);
      if (typeof source !== "string") return { content: [{ type: "text", text: source.error }], details: { error: "edit" } };
      state.lastProgram = source;
      state.triesSinceSubmit += 1;
      state.tries += 1;
      try {
        const built = await deps.engine.call("try_program", { source, params: p.params ?? {}, renders: true, lineart: false });
        state.lastBuilt = { program: source, params: p.params ?? {} };
        const bbox = Object.fromEntries(["xlen", "ylen", "zlen"].map((k) => [k, Math.round(Number(built.bbox[k]) * 10) / 10]));
        const summary = {
          size_mm: bbox,
          size_check: sizeIssues(known, built.bbox),
          parts: built.parts.map((part: Json) => ({ name: part.name, valid: part.valid, volume_mm3: Math.round(part.volume_mm3) })),
          warnings: built.warnings ?? [],
        };
        deps.emit({ type: "cad_try", n: state.tries, ok: true });
        return { content: [{ type: "text", text: JSON.stringify(summary) }, imageContent(await modelImage(deps, built.render_sheet))], details: summary };
      } catch (err) {
        const e = err as Error & { code?: string; details?: any };
        const line = Number(e.details?.line);
        const failing = Number.isInteger(line) && line > 0 ? source.split("\n")[line - 1] : undefined;
        const message = `ERROR ${e.code ?? "build_failed"}: ${e.message}${failing !== undefined ? `\nFailing line ${line}: ${failing.trim().slice(0, 300)}` : ""}${e.details ? `\n${JSON.stringify(e.details).slice(0, 1500)}` : ""}`;
        deps.emit({ type: "cad_try", n: state.tries, ok: false, error: e.message.slice(0, 160) });
        return { content: [{ type: "text", text: message + kernelHint(message) }], details: { error: e.code ?? "build_failed" } };
      }
    },
  });

  // 3. cad_submit: build + renders, independent 3D review, size check. Only an accepted model is saved.
  const submit = defineTool({
    name: "cad_submit",
    label: "Submit model for review",
    description: "Submit the finished program. It is built, rendered and compared with the concept by an independent reviewer, and checked against the required size. Accepted models are saved as the case's design; rejected ones come back with the mismatches to fix.",
    parameters: Type.Object({
      program: Type.Optional(Type.String({ description: "Complete CadQuery program with PARAMS and build(p). Omit it to submit the last program that built in cad_try." })),
      params: Type.Optional(Type.Record(Type.String(), Type.Any())),
      rationale: Type.String({ description: "One sentence: how the model reproduces the concept." }),
    }),
    async execute(_id, raw): Promise<ToolResult> {
      if (state.accepted) return { content: [{ type: "text", text: `Already accepted as ${state.accepted.version}. Stop and reply with your one-line summary.` }], details: {} };
      if (state.submissions >= maxSubmissions) return { content: [{ type: "text", text: `LIMIT: ${maxSubmissions} submissions used. Stop and reply with what is still different.` }], details: { error: "limit" } };
      const p = { ...raw, program: raw.program ?? state.lastBuilt?.program, params: raw.params ?? (raw.program ? undefined : state.lastBuilt?.params) };
      if (!p.program) return { content: [{ type: "text", text: "Nothing to submit: no program has built yet. Use cad_try first." }], details: { error: "empty" } };
      let built: Json;
      try {
        built = await deps.engine.call("try_program", { source: p.program, params: p.params ?? {}, renders: true, lineart: false });
      } catch (err) {
        // A program that does not build is not a submission (no review is spent), but it
        // counts as a try so a model cannot loop on broken submissions.
        state.triesSinceSubmit += 1;
        state.tries += 1;
        if (Date.now() > deadline + 300_000) return { content: [{ type: "text", text: "TIME LIMIT: the build run is over. Stop and reply with what is still missing." }], details: { error: "time" } };
        const e = err as Error & { details?: any };
        const line = Number(e.details?.line);
        const failing = Number.isInteger(line) && line > 0 ? String(p.program).split("\n")[line - 1] : undefined;
        const message = `${e.message}${failing !== undefined ? ` (line ${line}: ${failing.trim().slice(0, 300)})` : ""}`;
        return { content: [{ type: "text", text: `BUILD FAILED (not counted as a submission): ${message}. Fix the program with cad_try and submit again.${kernelHint(message)}` }], details: { error: "build" } };
      }
      state.submissions += 1;
      state.triesSinceSubmit = 0;
      const geometry = [
        ...built.parts.filter((part: Json) => !part.valid).map((part: Json) => `part ${part.name} is not a valid solid`),
        ...sizeIssues(known, built.bbox),
      ];
      const review = await askJson(deps, "build_reviewer", deps.choice.observer, BUILD_REVIEWER_SYSTEM,
        `Requirements:\n${requirements}\n\nConcept "${concept.title}": ${concept.idea}\nDistinctive features: ${features}`,
        [concept.image, built.render_sheet]);
      review.model = deps.choice.observer;
      const mismatches = normaliseMismatches(review.mismatches);
      const match = Number(review.match ?? 0);
      // Deterministic gate over the reviewer's findings: a score alone is not enough
      // (reviewers sometimes give 7 while listing several major differences).
      const majors = mismatches.filter((m) => m.severity === "major").length;
      const pass = match >= 7 && majors <= MAX_MAJOR_MISMATCHES && geometry.length === 0;
      deps.emit({ type: "build_review", submission: state.submissions, match, majors, pass, mismatches: mismatches.map((m) => `[${m.severity}] ${m.issue}`), geometry });
      const better = !state.best || match > Number(state.best.review.match ?? 0) || (match === Number(state.best.review.match ?? 0) && majors < state.best.majors);
      if (better) state.best = { review: { ...review, mismatches }, majors, geometry, render_sheet: built.render_sheet, bbox: built.bbox, program: p.program, params: p.params ?? {} };
      if (!pass) {
        const lines = [
          `REJECTED (submission ${state.submissions}/${maxSubmissions}). Reviewer match ${match}/10 with ${majors} major difference(s); acceptance needs 7+ and at most ${MAX_MAJOR_MISMATCHES} major difference.`,
          ...mismatches.map((m) => `- [${m.severity}] ${m.issue}${m.fix ? ` -> ${m.fix}` : ""}`),
          ...geometry.map((g) => `- [geometry] ${g}`),
          (review.matched_features ?? []).length ? `Keep (already matched): ${(review.matched_features as string[]).join("; ")}` : "",
          "Fix the major points first, check with cad_try (the attached sheet is what the reviewer saw), then call cad_submit again.",
        ].filter(Boolean);
        return { content: [{ type: "text", text: lines.join("\n") }, imageContent(await modelImage(deps, built.render_sheet))], details: { pass: false } };
      }
      const version = await deps.engine.call("version_from_program", {
        case_id: deps.caseId, source: p.program, params: p.params ?? {}, kind: "original", created_by: "agent",
        label: concept.title, rationale: p.rationale, concept: cid, review: { match, matched_features: review.matched_features ?? [], mismatches, model: review.model, submission: state.submissions },
      });
      let dfm: Json | null = null;
      try {
        dfm = await deps.engine.call("dfm_check", { case_id: deps.caseId, vid: version.id, process });
      } catch {
        dfm = null;
      }
      state.accepted = { version: version.id, review: { ...review, mismatches }, dfm, bbox: version.bbox };
      deps.emit({ type: "version", version: version.id, kind: "original", concept: cid, rationale: p.rationale });
      return { content: [{ type: "text", text: `ACCEPTED and saved as ${version.id} (match ${match}/10). Manufacturability: ${dfm ? JSON.stringify(dfm).slice(0, 600) : "not checked"}. Stop and reply with one line.` }], details: { pass: true, version: version.id } };
    },
  });

  const info = cadTools({ engine: deps.engine, caseId: deps.caseId, created: [], emit: deps.emit }).filter((t) => t.name === "version_info");
  const session = await createIsolatedSession({ runtime: deps.runtime, provider: deps.choice.provider, modelId: deps.choice.cad, systemPrompt: BUILDER_SYSTEM, tools: [tryTool, submit, ...info], cwd: deps.cwd, thinking: "off" });
  const stop = track(deps, session, deps.choice.cad, "cad_agent");
  try {
    const brief = `Build the 3D model of concept ${cid} ("${concept.title}") for case ${deps.caseId}. The concept image is attached.
Concept: ${concept.idea}
Distinctive features to reproduce: ${features}
Requirements:
${requirements}
Required size: ${known ?? "not given; choose realistic dimensions for the article and say which you chose"}
Manufacturing process: ${process}. You have ${maxSubmissions} submissions and ${TRIES_PER_SUBMISSION} tries before each.
${plan ? `\nModelling plan (from the planner, who studied the concept; follow it, correct it where the image clearly differs):\n${JSON.stringify(plan, null, 1).slice(0, 6000)}` : ""}
${starter ? `\nStarter program (generated from the plan's outlines; it builds and is the right size; image 2 shows its views). It is already loaded as your last program, so your first cad_try can send only edits to it. Add the concept's openings, moving parts, compartments and details. Replace the outer form only if its outline is clearly wrong:\n\`\`\`python\n${starter.program}\`\`\`` : ""}`;
    try {
      state.reply = await promptWithBudget(deps, session, brief, [imageContent(await modelImage(deps, concept.image)), ...(starter ? [imageContent(await modelImage(deps, starter.sheet))] : [])]);
      // A model may stop to ask or summarise; the orchestrator keeps it on task a few times.
      for (let nudge = 0; nudge < 3 && !state.accepted && state.submissions < maxSubmissions && Date.now() < deadline + 120_000; nudge++) {
        const text = state.submissions === 0
          ? "Do not ask questions or stop: use the requirements and the modelling plan as given (choose sensible values for anything missing), build the model step by step with cad_try, then call cad_submit."
          : "The model has not been accepted yet. Fix the major mismatches listed by the reviewer, check with cad_try, and call cad_submit again.";
        state.reply = await promptWithBudget(deps, session, text);
      }
    } catch (err) {
      // A stalled model ends the run with what was reached so far (accepted or best attempt).
      if (!(err instanceof PromptStalled)) throw err;
      state.reply = `${state.reply ?? ""}\n(stopped: ${(err as Error).message})`.trim();
    }
  } finally {
    stop();
    session.dispose();
  }
  if (state.accepted) {
    const sheet = await deps.engine.call("contact_sheet_for", { case_id: deps.caseId, vid: state.accepted.version, kind: "render" });
    return { status: "ok", version: state.accepted.version, concept: cid, submissions: state.submissions, tries: state.tries, review: state.accepted.review, dfm: state.accepted.dfm, bbox: state.accepted.bbox, render_sheet: sheet.path };
  }
  // Keep the closest attempt as a draft version (never the baseline), so the person can
  // look at it, tweak it, accept it themselves, or ask for another build.
  let draft: string | null = null;
  if (state.best?.program) {
    try {
      const v = await deps.engine.call("version_from_program", {
        case_id: deps.caseId, source: state.best.program, params: state.best.params ?? {}, kind: "draft", created_by: "agent",
        label: `${concept.title} (draft)`, rationale: "Closest 3D build; not accepted by the independent reviewer.", concept: cid,
        review: { match: Number(state.best.review.match ?? 0), matched_features: state.best.review.matched_features ?? [], mismatches: state.best.review.mismatches ?? [], model: state.best.review.model, accepted: false },
      });
      draft = v.id;
      deps.emit({ type: "version", version: v.id, kind: "draft", concept: cid });
    } catch {
      draft = null;
    }
  }
  return {
    status: "needs_input",
    reason: state.submissions ? `The 3D model did not pass the independent review in ${state.submissions} submission(s).` : "The CAD Agent did not submit a model.",
    concept: cid,
    draft,
    submissions: state.submissions,
    tries: state.tries,
    best_review: state.best?.review ?? null,
    geometry: state.best?.geometry ?? [],
    render_sheet: state.best?.render_sheet ?? null,
    cad_agent_reply: (state.reply ?? "").slice(0, 1500),
  };
}
