---
name: designaround
description: Design-patent risk screening, design-around, patent drawings and filing packages for physical products (US, CN and EU). Use when the user sells or develops a physical product and asks whether its appearance infringes a design patent, wants a design changed to avoid a registered design, needs patent line drawings or a design-patent filing package, received an infringement complaint / TRO, or wants a factory prototype pack. Drives the `designaround` CLI.
license: Apache-2.0
compatibility: Needs the `designaround` CLI (`uv tool install designaround --python 3.12`, or `python3.12 -m pip install designaround`) on PATH. Python 3.11 or 3.12. Agent runs need Node.js 22.19+. Network is only used for screening, hosted scoring, platform models and billing.
metadata:
  version: "0.1.3"
  cli: "designaround >= 0.1.0"
---

# DesignAround

DesignAround checks a product's appearance against registered designs, changes the design until the risk is low, and produces the drawings and documents a patent practitioner needs. Heavy work (CAD, views, manufacturability, documents) runs locally and is free. Screening, hosted scoring and platform models cost credits (1 credit = $0.01).

## Rules you must follow

1. **Never present results as legal advice.** Say "risk assessment" and "a registered practitioner must confirm". Do not tell the user a product "does not infringe".
2. **Always pass `--max-credits`** on commands that spend credits (`screen`, `rescreen`, `batch screen`, `observe`, `model --agent`, `design concepts`, `design build`, `around`, `monitor add`). Start small. Never raise a cap the user set without asking.
3. **Never pay.** `billing checkout` returns a URL; the user opens it and pays. Agents cannot complete payment.
4. **Never put keys on the command line.** Use `designaround onboard`, `auth login` (reads stdin or prompts) or `config byok --key-stdin`.
5. **The user decides.** Show candidates side by side and let the user choose. Record each choice with `designaround case decide <case> "<what was chosen and why>" --person "<name>"`. Only natural persons are inventors.
6. **Do not judge your own designs.** In host mode the observer score must come from `designaround observe` (an isolated model that never sees your reasoning), not from you.
7. Exit code `2` means a person must act. Read `data.message` and `next_actions`, then ask the user.

See [references/safety.md](references/safety.md) for the full list.

## Setup (once)

```bash
designaround --json auth status            # key? mode?
designaround --json onboard                # trial key (free plan), stored in the OS keychain
designaround --json config mode host       # you are the designer (recommended inside an agent host)
designaround --json agent check            # only for platform/byok mode: Node + isolated agent runtime
```

Model modes (`config mode`):

| mode | who designs | who scores | LLM cost |
|---|---|---|---|
| `host` | you, via `cad run` / `cad tweak` | hosted observer, 120 credits per call | none for design |
| `platform` | embedded agent team via our metered models | embedded isolated observer | metered credits |
| `byok` | embedded agent team with the user's own key | embedded isolated observer | the user's provider bill |

Inside Claude Code, Codex, Cursor, Gemini CLI, OpenCode, OpenClaw or Kiro, use **host** mode unless the user asks otherwise.

People without an agent host can talk to DesignAround's own assistant: `designaround ui` (chat workbench in the browser or the desktop app) or `designaround chat` (terminal). It runs on the embedded, isolated Pi runtime in platform or byok mode.

## Output contract

Always call the CLI with `--json`. Every command prints one envelope:

```json
{"schema_version": 1, "kind": "evaluation", "ok": true, "data": {...}, "next_actions": ["designaround ..."], "cost": {"estimated": 0, "charged": 0}}
```

Exit codes: `0` ok · `2` needs a person · `3` budget or plan · `4` auth · `5` invalid input · `6` error. Details: [references/output-format.md](references/output-format.md).

## Workflows

Pick the path from what the user has. Full steps: [references/workflow.md](references/workflow.md).

There are no product templates. Every model is built for this product: imported (`--cad`), reconstructed from photos, or designed from the user's requirements.

**0. New product from an idea (design it)**

```bash
designaround --json case create "<product>" --article "<article>" -j US -j CN
designaround --json case set <case> --brief-json '{"use": "...", "size": "240x160x110mm", "users": "...", "style": "...", "process": "injection moulding", "must_have": ["..."], "avoid": ["..."]}'
designaround --json design concepts <case> --count 4 --max-credits 680     # platform mode: 2D concepts, each independently reviewed
designaround --json design sheet <case>                                     # image path: show it to the user
designaround --json design concepts <case> --base c2 --feedback "<the user's words>" --max-credits 440
designaround --json design choose <case> c2 --person "<name>"               # only after the user chose
designaround --json design build <case> --max-credits 3000                  # CAD Agent builds it; independent 3D review + size check
designaround --json design accept <case> v3 --person "<name>"              # only if the user accepts a draft the reviewer rejected
```

If `design build` exits 2 (`needs_input`), it saved the closest model as a `draft` version (`data.draft`) with the reviewer's remaining `mismatches`. Show the user the draft's render sheet and those differences, then let them choose: build again, simplify the concept (`design concepts --base`), or accept the draft themselves (`design accept`). Never accept it for them.

Ask only for the requirements that are missing, in one message. Let the user pick and revise as often as they want. In host mode you draw nothing yourself: describe directions in words, then write the CadQuery program from scratch (`cad run <case> model.py --try`, then `--kind original`).

**A. "Does my product infringe?" (screening)**

```bash
designaround --json case create "<product>" --article "<article, e.g. storage box>" -j US
designaround --json model <case> --cad file.step        # STEP/IGES/BREP, or a mesh/scan: STL/OBJ/GLB/PLY/3MF (repaired; add --units cm|m|inch, --up y if needed); or photos + --agent
designaround --json screen <case> --max-credits 50      # registry search + local ranking; prior art is added to the case
designaround --json observe <case> v1 <prior-id> --max-credits 120   # top 1-3 prior art
designaround --json evaluate <case> v1
```

Report the risk level, the closest registered designs, what the observer found similar, and the blind spots from `screen` (unpublished applications cannot be found). `case card <case>` gives the verdict, reasons and next steps with cost and time; `report <case> --lang en` writes a risk report PDF.

**B. Design-around (host mode: you run the loop)**

1. `cad params <case> v1` lists parameters, feature groups and locked parameters. Ask the user what must not change (size, interfaces, brand features) and save it: `case set <case> --constraints constraints.yaml` (template: [assets/constraints.template.yaml](assets/constraints.template.yaml), schema: [references/constraints.md](references/constraints.md)).
2. Look at `cad pair <case> v1 <prior-id>` (image path) to see what makes them look alike.
3. Make 2-3 candidates that change the **features an ordinary observer notices first** (overall silhouette, handle, lid, distinctive surfaces), not just corner radii: `cad tweak <case> v1 --set <param>=<value> --rationale "..."` for parameters the program exposes, or write a new program and `cad run <case> program.py --parent v1 --kind candidate`. For a new design you can also go back to `design concepts --base <concept> --feedback "<observer findings>"`.
4. For each candidate: `originality` → `dfm` → `observe` (each top prior art) → `evaluate`. Discard candidates that fail a gate.
5. Stop when 2 candidates are `qualified`, or after 3 rounds; then report where it got stuck.
6. Show the user `cad compare <case> v1 <vN>` and the contact sheets; record their choice with `case decide`.

In platform/byok mode the embedded team does steps 2-5: `designaround --json around <case> --depth standard --max-credits 5900` (quick: 1 round, deep: 6 rounds). A standard or deep run with no qualified candidate refunds 50% of its credits automatically; report `data.guarantee` (refund and stuck points) to the user. Long runs: add the global `--detach`, then `run status <run_id>`.

**C. Filing package** (Pro plan; free plan gives watermarked view packs only). Offices: `US`, `CN`, `EU`.

```bash
designaround --json viewpack <case> <vN> --office US      # line drawings with surface shading + rule checks (EU: numbered JPEG views)
designaround --json case set <case> --applicant "ACME Ltd" --applicant-domicile CN --applicant-type juristic --applicant-address "..." --entity-size small --inventor "Jane Doe;Shenzhen, CN"
designaround --json filing-pack <case> <vN> --office US   # spec DOCX, ADS data, IDS, checklist, human-contribution record
designaround --json gate create <case> --type filing_submission --reviewer-email <practitioner>
designaround --json evidence anchor <case>
```

Foreign-domiciled applicants need a registered US practitioner for USPTO filings from 2026-07-20. For CN, `--similar auto` adds similar designs to one application. Rules: [references/jurisdictions.md](references/jurisdictions.md).

**D. Prototype**: `designaround --json proto <case> <vN> --process injection` (STEP/STL/3MF + factory PDF).

**E. Already disclosed?** `designaround --json disclose <case> --kind listing --date YYYY-MM-DD` then `timeline`. CN has no general grace period for the applicant's own listing; a `block` finding means filing in CN is likely barred.

**F. Filed: deadlines.** After the user files, record it: `filing record <case> -o CN --filed YYYY-MM-DD --number <no>`; official notices: `filing notice <case> -o US --kind office_action|allowance|granted --date YYYY-MM-DD [--due YYYY-MM-DD] [--mailed]`. `deadlines <case>` lists priority, grace, reply, fee and renewal dates with their legal basis and writes an .ics calendar. Only use dates the user gave you; practitioners confirm.

**G. Send files out.** `share create <case> <vN> --to "<recipient>" --purpose factory|agent|other --days 7` uploads watermarked copies and returns a read-only link that expires; `share show <id>` lists who opened it; `share revoke <id>`. Ask the user before sharing: the files leave their machine.

**H. Watch for new designs.** `rescreen <case> --max-credits 50` repeats the last screen and lists new, dropped and changed designs; `monitor add <case> --every 30 --max-credits 50` repeats it periodically and pushes new hits.

**I. Complaint or lawsuit.** `defense intake <case> --platform amazon|temu|walmart|court|... --number <id> --right "US D1,029,631 S" --notice <file>` identifies the right and the channel (design patents do not go through Amazon APEX; a TRO lawsuit has an answer due 21 days after service). Then `observe` the user's version against the asserted design, `defense pack <case> <vN>` (three-way comparison, earlier-design record, design timeline, appeal draft) and `gate create --type platform_appeal|tro_response`. Tell the user to involve an attorney; never predict the outcome.

**Many products:** `batch screen products.csv --max-credits <n>` (resumable). **MCP:** `designaround mcp` serves screen, design_around, run_status, viewpack, compare, case_list and decision_card on stdio.

## Writing CAD programs (host mode)

Programs are plain Python with only `cadquery` and `math` imports, a literal `PARAMS` dict and a `build(p)` function that returns a dict of named parts. Test with `cad run <case> model.py --try` and look at the contact sheet before saving. Contract and example: [references/cadquery.md](references/cadquery.md).

## Helper script

`scripts/da.py` runs the CLI with `--json`, prints a compact envelope and keeps the exit code. Use it when your host shows long output poorly: `python3 scripts/da.py case show <case>`.
