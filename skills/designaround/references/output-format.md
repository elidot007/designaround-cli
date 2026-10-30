# Output format

`--json` (or any non-terminal stdout) prints exactly one JSON object on stdout. Progress lines of agent runs go to stderr.

```json
{
  "schema_version": 1,
  "kind": "evaluation",
  "ok": true,
  "data": {},
  "next_actions": ["designaround ..."],
  "cost": {"estimated": 0, "charged": 0}
}
```

On failure `ok` is `false` and `data` is `{"message": "...", "details": {...}}`; `kind` is the error code.

## Exit codes

| code | meaning | what to do |
|---|---|---|
| 0 | ok | continue |
| 2 | needs a person | ask the user (photos do not match, host mode instructions, practitioner gate) |
| 3 | budget or plan | `budget_required` / `budget_too_low`: pass a higher `--max-credits` only if the user agrees; `insufficient_credits`, `plan_required`: tell the user and give the `billing checkout` URL |
| 4 | auth | `designaround onboard` or `auth login` |
| 5 | invalid input | fix the arguments, the CAD program or the case data |
| 6 | error | network or internal error; retry once, then report |

## Common error kinds

`auth_required`, `auth_failed`, `budget_required`, `budget_too_low`, `budget_exceeded`, `insufficient_credits`, `plan_required`, `not_found`, `invalid_input`, `program_rejected`, `build_failed`, `host_mode`, `gate_failed`, `network_error`, `server_error`, `agents_missing`, `node_too_old`.

## Costs

`cost.estimated` is the cap or the list price; `cost.charged` is what the server charged. Fixed prices (credits): quick screen 50 (5 free per month), deep screen 350, hosted observer 120. Platform model calls are metered at upstream cost × 1.25. Local commands cost 0.
