/** Custom tools exposed to the agent team. They call the local engine only. */
import { defineTool, type ToolDefinition } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { Engine, EngineError, pngContent } from "./engine.ts";

export interface ToolContext {
  engine: Engine;
  caseId: string;
  /** Version ids created through tools during the current turn. */
  created: string[];
  /** Max versions the agent may create before the orchestrator takes over (runaway-loop guard). */
  maxVersions?: number;
  emit: (event: Record<string, unknown>) => void;
}

function text(t: string) {
  return { type: "text" as const, text: t };
}

type Details = Record<string, unknown>;
type Content = { type: "text"; text: string } | { type: "image"; data: string; mimeType: string };
type Result = { content: Content[]; details: Details };

function ok(content: Content[], details: Details = {}): Result {
  return { content, details };
}

function failure(err: unknown): Result {
  if (err instanceof EngineError) {
    return ok([text(`ERROR ${err.code}: ${err.message}\n${JSON.stringify(err.details ?? {}, null, 1).slice(0, 1500)}`)], { error: err.code });
  }
  return ok([text(`ERROR: ${(err as Error).message}`)], { error: "internal" });
}

export function cadTools(ctx: ToolContext): ToolDefinition<any, any, any>[] {
  const tryProgram = defineTool({
    name: "cad_try",
    label: "Try CAD program",
    description: "Build a CadQuery program in a sandbox WITHOUT saving it. Returns bounding box, parts, warnings and a 7-view line-drawing contact sheet image. Use it after every modelling step.",
    parameters: Type.Object({
      program: Type.String({ description: "Complete CadQuery program with PARAMS and build(p)." }),
      params: Type.Optional(Type.Record(Type.String(), Type.Any(), { description: "Parameter overrides." })),
    }),
    async execute(_id, params): Promise<Result> {
      try {
        const result = await ctx.engine.call("try_program", { source: params.program, params: params.params ?? {} });
        const summary = { bbox: result.bbox, parts: result.parts.map((p: any) => ({ name: p.name, valid: p.valid, volume_mm3: p.volume_mm3 })), warnings: result.warnings };
        return ok([text(JSON.stringify(summary)), pngContent(result.contact_sheet)], summary);
      } catch (err) {
        return failure(err);
      }
    },
  });

  const save = defineTool({
    name: "cad_save_version",
    label: "Save CAD version",
    description: "Build a program and save it as a new version of the case (original or design-around candidate). Returns the version id and its contact sheet.",
    parameters: Type.Object({
      program: Type.String(),
      params: Type.Optional(Type.Record(Type.String(), Type.Any())),
      kind: Type.Optional(Type.Union([Type.Literal("original"), Type.Literal("candidate")])),
      parent: Type.Optional(Type.String()),
      label: Type.Optional(Type.String()),
      rationale: Type.String({ description: "One-sentence design rationale (kept in the evidence log)." }),
    }),
    async execute(_id, params): Promise<Result> {
      try {
        if (ctx.maxVersions !== undefined && ctx.created.length >= ctx.maxVersions) {
          return ok([text(`LIMIT: ${ctx.maxVersions} versions already created this round. Stop creating versions and reply with your summary.`)], { error: "limit" });
        }
        const v = await ctx.engine.call("version_from_program", {
          case_id: ctx.caseId,
          source: params.program,
          params: params.params ?? {},
          parent: params.parent ?? null,
          kind: params.kind ?? null,
          created_by: "agent",
          label: params.label ?? null,
          rationale: params.rationale,
        });
        ctx.created.push(v.id);
        ctx.emit({ type: "version", version: v.id, kind: v.kind, parent: v.parent, rationale: params.rationale });
        const sheet = await ctx.engine.call("contact_sheet_for", { case_id: ctx.caseId, vid: v.id });
        return ok([text(`saved ${v.id} (${v.kind}); bbox ${JSON.stringify(v.bbox)}`), pngContent(sheet.path)], { version: v.id });
      } catch (err) {
        return failure(err);
      }
    },
  });

  const tweak = defineTool({
    name: "cad_tweak",
    label: "Tweak parameters",
    description: "Create a new candidate version by changing parameters of an existing version. Returns the new version id and its contact sheet.",
    parameters: Type.Object({
      version: Type.String(),
      set: Type.Record(Type.String(), Type.Any(), { description: "Parameter name -> new value." }),
      label: Type.Optional(Type.String()),
      rationale: Type.String(),
    }),
    async execute(_id, params): Promise<Result> {
      try {
        if (ctx.maxVersions !== undefined && ctx.created.length >= ctx.maxVersions) {
          return ok([text(`LIMIT: ${ctx.maxVersions} versions already created this round. Stop creating versions and reply with your summary.`)], { error: "limit" });
        }
        const v = await ctx.engine.call("tweak", { case_id: ctx.caseId, vid: params.version, overrides: params.set, created_by: "agent", label: params.label ?? null, rationale: params.rationale });
        ctx.created.push(v.id);
        ctx.emit({ type: "version", version: v.id, kind: v.kind, parent: v.parent, rationale: params.rationale, set: params.set });
        const sheet = await ctx.engine.call("contact_sheet_for", { case_id: ctx.caseId, vid: v.id });
        return ok([text(`created ${v.id} from ${params.version}`), pngContent(sheet.path)], { version: v.id });
      } catch (err) {
        return failure(err);
      }
    },
  });

  const info = defineTool({
    name: "version_info",
    label: "Version info",
    description: "Parameters, parameter ranges, feature map and the CAD program of a version.",
    parameters: Type.Object({ version: Type.String(), include_program: Type.Optional(Type.Boolean()) }),
    async execute(_id, params): Promise<Result> {
      try {
        const schema = await ctx.engine.call("params_schema", { case_id: ctx.caseId, vid: params.version });
        let program = "";
        if (params.include_program) {
          const show = await ctx.engine.call("version_show", { case_id: ctx.caseId, vid: params.version });
          const { readFileSync, existsSync } = await import("node:fs");
          const path = `${show.path}/program.py`;
          program = existsSync(path) ? readFileSync(path, "utf8") : "(imported model: no program)";
        }
        return ok([text(JSON.stringify(schema, null, 1) + (program ? `\n\nPROGRAM:\n${program}` : ""))]);
      } catch (err) {
        return failure(err);
      }
    },
  });

  return [tryProgram, save, tweak, info];
}
