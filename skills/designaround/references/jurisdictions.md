# Jurisdiction notes (rule pack 2026.10)

These are the checks the CLI runs. They summarise; they are not legal advice. The practitioner decides.

## United States (USPTO design patent)

- Drawings: perspective + six orthographic views; black-and-white lines, uniform weight, no hidden lines (37 CFR 1.84, 1.152).
- Claimed parts in solid lines, unclaimed parts in broken lines (MPEP 1503.02). Use `cad claim <case> <v> <part> --unclaimed`.
- Curved surfaces get surface shading lines (37 CFR 1.152): thin lines along the direction of least curvature, denser towards the silhouette, only on the side away from the light. `US-DRW-014` reports how many lines were drawn. A practitioner reviews the shading before filing; adding or changing shading after filing can be new matter.
- Sheet margins and figure numbering are checked (`US-DRW-021`, `US-DRW-022`).
- Representation: from **2026-07-20** foreign-domiciled applicants must be represented by a registered US patent practitioner (`REP-US-001`). The filing package always needs a practitioner gate for them.
- Grace period: 12 months after the inventor's own disclosure (`TL-US-*`).
- IDS: prior art found in screening is listed (`ids`, `ids.csv`).
- Inventors are natural persons. The package includes a human-contribution record built from `case decide` entries (USPTO guidance on AI-assisted inventions).
- Fees in quotes use the fee table version shown in `quote`; small and micro entity discounts apply.

## China (CNIPA 外观设计)

- Views: 主视图、后视图、左视图、右视图、俯视图、仰视图、立体图; omitted views need a reason in 简要说明 (`CN-DRW-001`).
- Each figure at most 15 cm × 22 cm (`CN-DRW-002`). Line drawings without shading or dimensions.
- Partial designs (局部外观设计) use broken lines for unclaimed parts and must say so in 简要说明.
- Similar designs: up to 10 similar designs of the same product in one application (`filing-pack --similar auto`).
- Novelty grace period covers only narrow cases (government-recognised exhibitions, academic meetings, disclosure without consent). The applicant's own e-commerce listing is **not** covered: `TL-CN-001` blocks.
- Foreign applicants without residence in China must use a CN patent agency.
- Fee reduction (85 %) for eligible applicants (`quote --fee-reduction`).

## Priority

A design filing gives 6 months of priority for later design filings in Paris Convention countries. Filing CN first and US within 6 months is common.

## European Union (EUIPO registered EU design)

Rules as amended by the EU design reform (phase 1 from 1 May 2025, phase 2 from 1 July 2026):

- Static representation: up to 10 views, JPEG, at least 800 × 800 px, at most 2 MB each for e-filing (`EU-DRW-001…003`). `viewpack --office EU` writes `views/1.1_perspective.jpg`, `1.2_front.jpg`, …
- One type of representation per design (static, dynamic or animated); the design is shown alone; no text or view numbers inside the images (`EU-DRW-006`).
- Visual disclaimers (broken lines, blurring, colour shading) must be applied the same way in every view (`EU-DRW-005`). Use `cad claim … --unclaimed`.
- Every view must show the same design (`EU-DRW-004`). All features for which protection is sought must be visible; omitted views are flagged (`EU-DRW-007`).
- Fees: one application fee from EUR 350 (it replaced the separate registration and publication fees), EUR 125 for each additional design in a multiple application, deferment of publication from EUR 40.
- Applicants without domicile or establishment in the EEA need a representative (`REP-EU-001`). Grace period: 12 months.
- `filing-pack --office EU` writes `euipo_application.json` (indication of product, Locarno class, views, applicant, designers) and a checklist.
