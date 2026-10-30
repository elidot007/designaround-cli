# Workflows in detail

All commands take `--json`. `<case>` is the case id from `case create`; versions are `v1`, `v2`, ….

## 0. Start a case

```bash
designaround --json case create "Stackable storage box" --article "storage box" --article-zh "收纳箱" -j US -j CN
```

Ask the user for: target offices, sales platforms, and whether the product is already on sale (if so, record it with `disclose`, see §6).

## 1. Get the original digital sample

Choose by what the user has:

| user has | command | cost |
|---|---|---|
| an idea, no product yet | `case set <case> --brief-json '{...}'`, `design concepts <case> --count 4 --max-credits 680`, the user picks (`design choose`), `design build <case> --max-credits 3000` (platform mode; a rejected build leaves a `draft` the user may accept with `design accept`); in host mode write the program from scratch | 25 credits per concept image + metered models |
| STEP / IGES / BREP | `model <case> --cad product.step` | free |
| photos (host mode) | `photos add <case> front=f.jpg left=l.jpg top=t.jpg perspective=p.jpg`, then write a CadQuery program (see cadquery.md), `cad run <case> model.py --kind original`, then `photos verify <case> v1` | free |
| photos (platform/byok) | `model <case> --photo front=f.jpg --photo perspective=p.jpg --known length=320mm --agent --max-credits 3000` | metered |

Always set one real dimension (`--known` or `case set --known-dimension`). Photos give shape, not scale.

`photos verify` compares the model's silhouettes with the photos. Exit 2 (`gate_failed`) means the model does not match yet: adjust and re-check, or ask the user for better photos.

## 2. Screen

```bash
designaround --json screen <case> --max-credits 50          # quick: text + image of the original
designaround --json screen <case> --deep --max-credits 350  # more sources, image and text
```

- The first 5 quick screens each month are free on every plan.
- Results are added as prior art with figures and ranked by silhouette (`data.ranked`, `shape_score` 0-1).
- If the user knows a specific patent (complaint, competitor), add it: `prior-art add <case> --number USD1012345S --office US --figure fig1.png --figure fig2.png`.
- Always tell the user the `blind_spots`.

## 3. Score and evaluate

For the top 3 prior art from `prior-art rank` (by silhouette, for the version being evaluated):

```bash
designaround --json observe <case> v1 <prior-id> --max-credits 120
designaround --json evaluate <case> v1
```

The observer sees one multi-view sheet per right (candidate perspective/front/right/top over the prior art's figures; `cad sheet-vs` shows it) and scores 0-10 against fixed anchors; the score is the median of 3 independent judgements. `evaluate` fuses the CPU silhouette score (weight 0.1) and the observer score (weight 0.9) into a risk 0-1. `qualified` means every reviewed right is below 0.40 and none of the top 3 is still waiting for an observer score (`summary.pending_observer`). These weights are uncalibrated; describe the result as an indication.

US designs found in screening get their full drawing sheets from the USPTO public search service, split into figures; the small search thumbnails are replaced.

Risk levels: `high`, `medium`, `low`, `not_seen`. Report the worst prior art and the observer's `similar` / `different` lists.

## 4. Design-around

### Host mode (you design)

```text
for round in 1..3:
    propose 2-3 candidates (cad tweak or cad run --parent v1 --kind candidate)
    for each candidate:
        originality <case> vN      # fail -> drop (only scaled / rounded / recoloured)
        dfm <case> vN              # fail -> fix or drop
        observe <case> vN <prior>  # for each top prior art
        evaluate <case> vN
    stop if 2 candidates are qualified
```

What works: change the elements the observer listed under `similar` and the prior art's `distinctive_features_of_prior_art`: handle form, lid profile, silhouette proportions, surface features (ribs, panels). Changing only corner radii, colour or overall scale fails the originality gate.

Respect `locked_params` and `interfaces` from the case constraints. If no candidate qualifies after 3 rounds, stop and tell the user which constraint blocks progress.

### Platform / BYOK mode (embedded team)

```bash
designaround --json around <case> --max-credits 6000 --rounds 3 --per-round 3 --target 2
```

Progress lines go to stderr; the envelope has `candidates`, `qualified`, `stuck` and `cost.charged`.

### Let the user choose

```bash
designaround --json cad compare <case> v1 v4
designaround --json cad sheet <case> v4 --kind render   # image path to show the user
designaround --json case decide <case> "chose v4: recessed handle, 3 ribs" --person "Jane Doe"
```

## 5. Drawings and filing

```bash
designaround --json viewpack <case> v4 --office US          # free plan: PREVIEW watermark
designaround --json viewpack <case> v4 --office CN
designaround --json viewpack <case> v4 --office EU          # JPEG views 1.1 … 1.7 for EUIPO e-filing
designaround --json cad claim <case> v4 handle --unclaimed  # broken lines for parts not claimed
designaround --json case set <case> --applicant "ACME Ltd" --applicant-domicile CN --applicant-type juristic \
    --applicant-address "..." --entity-size small --fee-reduction --inventor "Jane Doe;Shenzhen, CN" \
    --use-zh "用于收纳物品" --design-point-zh "盖子与把手的形状"
designaround --json filing-pack <case> v4 --office US --planned-date 2026-12-01
designaround --json filing-pack <case> v4 --office CN --similar auto
designaround --json ids <case>
```

Read `checklist.findings` and `checklist.missing` back to the user. `requires_practitioner_gate: true` means a registered practitioner must review before filing:

```bash
designaround --json gate create <case> --type filing_submission --reviewer-email agent@lawfirm.example
designaround --json gate status <gate-id>
```

Cost estimate: `designaround --json quote -o US -o CN` (official fees + platform + practitioner estimate).

## 6. Disclosures and deadlines

```bash
designaround --json disclose <case> --kind listing --date 2026-09-01
designaround --json timeline <case> --planned-date 2026-12-01
```

US and EU give the applicant 12 months after their own disclosure. CN does not cover the applicant's own sales listing: a `TL-CN-001` block means CN filing is likely barred. CN-first filing gives 6 months of priority for US/EU design filings.

## 7. Evidence

Every command appends to a hash-chained event log in the case.

```bash
designaround --json evidence anchor <case>     # signed server timestamp of the head hash (no content uploaded)
designaround --json evidence verify <case>
designaround --json evidence export <case>     # zip for counsel
```

## 8. Prototype

```bash
designaround --json proto <case> v4 --process injection
```
