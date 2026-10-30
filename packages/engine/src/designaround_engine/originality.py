"""Originality gate (FR-GEN-07): reject cosmetic tweaks presented as a design-around.

A candidate passes when it (a) changes at least one parameter that belongs to
a targeted design point (feature), (b) is not only an edge treatment change,
and (c) visibly changes the silhouette in the main views.
"""

from __future__ import annotations

import os
import re

import numpy as np

from .cad import read_manifest
from .templates import program_meta
from .views import view_masks

EDGE_ONLY = re.compile(r"(fillet|chamfer|edge|round|dome|radius)", re.I)
COLOR_ONLY = re.compile(r"(color|colour|finish|texture_color)", re.I)
MAIN_VIEWS = ["front", "left", "top", "perspective"]
POLICY = {"min_mean_change": 0.03, "min_max_change": 0.06, "policy_version": "orig-2026.10-1"}


def _read_program(version_dir) -> str:
    path = os.path.join(os.fspath(version_dir), "program.py")
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _structure(source: str) -> str:
    """Program without the PARAMS defaults, so a parameter-only change is not a 'program change'."""

    import ast

    if not source:
        return ""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source
    tree.body = [n for n in tree.body if not (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PARAMS" for t in n.targets))]
    return ast.dump(tree)


def changed_params(original: dict, candidate: dict) -> dict:
    keys = set(original) | set(candidate)
    return {k: {"from": original.get(k), "to": candidate.get(k)} for k in sorted(keys) if original.get(k) != candidate.get(k)}


def feature_of(param: str, features: dict[str, list[str]]) -> str | None:
    for feature, params in features.items():
        if param in params:
            return feature
    return None


def check(
    original_dir: str | os.PathLike,
    candidate_dir: str | os.PathLike,
    *,
    target_features: list[str] | None = None,
    locked_params: list[str] | None = None,
) -> dict:
    om, cm = read_manifest(original_dir), read_manifest(candidate_dir)
    original_program = _read_program(original_dir)
    candidate_program = _read_program(candidate_dir)
    meta = program_meta(candidate_program) if candidate_program else {}
    features = meta.get("features", {})
    changes = changed_params(om["params"], cm["params"])
    reasons: list[str] = []

    locked = set(locked_params or [])
    violated = sorted(locked & set(changes))
    if violated:
        reasons.append(f"Locked parameters changed: {', '.join(violated)}")

    program_changed = _structure(original_program) != _structure(candidate_program)

    if not changes and not program_changed:
        reasons.append("No change from the original.")
    non_cosmetic = [p for p in changes if not (EDGE_ONLY.search(p) or COLOR_ONLY.search(p))]
    if changes and not non_cosmetic and not program_changed:
        reasons.append("Only edge-treatment / colour parameters changed (rounded corners, chamfers, colours are not a design-around).")

    touched_features = sorted({f for p in changes if (f := feature_of(p, features))})
    if target_features and not program_changed:
        hit = set(target_features) & set(touched_features)
        if not hit:
            reasons.append(f"No targeted design point was changed (targets: {', '.join(target_features)}).")

    ours, theirs = view_masks(original_dir, keys=MAIN_VIEWS), view_masks(candidate_dir, keys=MAIN_VIEWS)
    per_view = {}
    for key in MAIN_VIEWS:
        a, b = ours.get(key), theirs.get(key)
        if a is None or b is None:
            continue
        inter = np.logical_and(a.mask, b.mask).sum()
        union = np.logical_or(a.mask, b.mask).sum()
        per_view[key] = round(1 - float(inter / union), 4) if union else 0.0
    values = list(per_view.values()) or [0.0]
    mean_change, max_change = float(np.mean(values)), float(np.max(values))
    if mean_change < POLICY["min_mean_change"] and max_change < POLICY["min_max_change"]:
        reasons.append(
            f"Silhouette change is too small (mean {mean_change:.3f} < {POLICY['min_mean_change']}, max {max_change:.3f} < {POLICY['min_max_change']})."
        )
    return {
        "pass": not reasons,
        "reasons": reasons,
        "changed_params": changes,
        "program_changed": program_changed,
        "touched_features": touched_features,
        "silhouette_change": per_view,
        "mean_change": round(mean_change, 4),
        "max_change": round(max_change, 4),
        "policy": POLICY,
    }
