# Case constraints

Saved with `designaround --json case set <case> --constraints constraints.yaml`. A copy to start from is in `assets/constraints.template.yaml`.

| key | type | used by | meaning |
|---|---|---|---|
| `locked_params` | list of parameter names | originality gate, designer | must not change (e.g. `length` to keep the packaging size) |
| `target_features` | list of feature group names | originality gate | a candidate must change at least one of these groups (see `cad params` → `features`) |
| `interfaces` | list | DFM gate | functional interfaces that must stay where they are |
| `interfaces[].param` | parameter name | | value must be identical to the original |
| `interfaces[].part` + `tolerance_mm` | part name, mm | | part bounding box may move at most this much |
| `keep` | list of strings | designer (free text) | brand features to keep, e.g. "logo panel on the front" |
| `avoid` | list of strings | designer (free text) | e.g. "no visible screws" |
| `processes` | list | user reference | `injection`, `print3d` |
| `notes` | string | designer | anything else |

Ask the user before locking or unlocking parameters. Too many locked parameters is the usual reason `around` finds no qualified candidate; report that rather than guessing.
