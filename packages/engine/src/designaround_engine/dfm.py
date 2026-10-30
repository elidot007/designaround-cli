"""Design-for-manufacture checks (CPU): watertightness, wall thickness, draft,
undercuts, overhangs, preserved functional interfaces.

Rule thresholds live in ``rules/dfm.yaml`` and are versioned (FR-DFM-02).
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import yaml

from .geometry import combined_mesh, load_version_parts, to_trimesh

RULES_PATH = Path(__file__).parent / "rules" / "dfm.yaml"


def load_rules() -> dict:
    return yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))


def _ray_engine(mesh):
    from trimesh.ray.ray_triangle import RayMeshIntersector

    return RayMeshIntersector(mesh)


def wall_thickness(mesh, samples: int = 400, seed: int = 7) -> dict:
    import trimesh

    if len(mesh.faces) == 0:
        return {"min_mm": None, "p05_mm": None, "median_mm": None, "samples": 0}
    rng = np.random.default_rng(seed)
    points, face_idx = trimesh.sample.sample_surface(mesh, samples, seed=int(rng.integers(1 << 30)))
    normals = mesh.face_normals[face_idx]
    origins = points - normals * 1e-3
    engine = _ray_engine(mesh)
    locations, ray_idx, _tri = engine.intersects_location(origins, -normals, multiple_hits=False)
    if len(ray_idx) == 0:
        return {"min_mm": None, "p05_mm": None, "median_mm": None, "samples": 0}
    distances = np.linalg.norm(locations - origins[ray_idx], axis=1)
    distances = distances[distances > 0.05]
    if len(distances) == 0:
        return {"min_mm": None, "p05_mm": None, "median_mm": None, "samples": 0}
    return {
        "min_mm": round(float(distances.min()), 3),
        "p05_mm": round(float(np.percentile(distances, 5)), 3),
        "median_mm": round(float(np.median(distances)), 3),
        "samples": int(len(distances)),
    }


def draft_analysis(mesh, pull=(0, 0, 1), min_draft_deg: float = 1.0) -> dict:
    pull = np.asarray(pull, dtype=float)
    pull /= np.linalg.norm(pull)
    cos = mesh.face_normals @ pull
    areas = mesh.area_faces
    total = areas.sum() or 1.0
    # Faces almost parallel to the pull direction need draft; exactly 0° walls are "no draft".
    sin_limit = np.sin(np.radians(min_draft_deg))
    near_vertical = np.abs(cos) < sin_limit
    return {
        "insufficient_draft_area_ratio": round(float(areas[near_vertical].sum() / total), 4),
        "min_draft_deg": min_draft_deg,
    }


def undercut_ratio(mesh, pull=(0, 0, 1), samples: int = 400, seed: int = 11) -> float:
    """Fraction of surface trapped for a two-plate mould along ``pull``."""

    import trimesh

    pull = np.asarray(pull, dtype=float)
    pull /= np.linalg.norm(pull)
    points, face_idx = trimesh.sample.sample_surface(mesh, samples, seed=seed)
    normals = mesh.face_normals[face_idx]
    side = np.sign(normals @ pull)
    side[side == 0] = 1
    directions = pull[None, :] * side[:, None]
    origins = points + directions * 1e-2
    engine = _ray_engine(mesh)
    hit = engine.intersects_any(origins, directions)
    return round(float(hit.mean()), 4) if len(hit) else 0.0


def overhang_ratio(mesh, up=(0, 0, 1), max_angle_deg: float = 45.0) -> float:
    up = np.asarray(up, dtype=float)
    up /= np.linalg.norm(up)
    cos = mesh.face_normals @ up
    areas = mesh.area_faces
    zmin = mesh.bounds[0] @ up
    centers = mesh.triangles_center @ up
    on_plate = np.abs(centers - zmin) < 0.5
    downward = cos < -np.cos(np.radians(90 - max_angle_deg))
    mask = downward & ~on_plate
    return round(float(areas[mask].sum() / (areas.sum() or 1.0)), 4)


def check_version(version_dir: str | os.PathLike, process: str = "injection") -> dict:
    rules = load_rules()
    if process not in rules["processes"]:
        raise ValueError(f"Unknown process '{process}'. Known: {', '.join(rules['processes'])}")
    rule = rules["processes"][process]
    parts = load_version_parts(version_dir)
    V, F, _owners = combined_mesh(parts)
    mesh = to_trimesh(V, F)
    checks = []

    def add(rule_id, ok, value, threshold, severity, message):
        checks.append({"rule_id": rule_id, "pass": bool(ok), "value": value, "threshold": threshold, "severity": severity if not ok else "ok", "message": message})

    per_part_watertight = []
    for part in parts:
        pv, pf, _ = combined_mesh([part])
        pm = to_trimesh(pv, pf)
        per_part_watertight.append({"part": part.name, "watertight": bool(pm.is_watertight)})
    all_watertight = all(p["watertight"] for p in per_part_watertight)
    add("DFM-GEN-001", all_watertight, per_part_watertight, True, rule.get("watertight_severity", "error"), "Every part must be a closed solid.")

    thickness = wall_thickness(mesh, samples=rule.get("thickness_samples", 400))
    min_wall = rule["min_wall_mm"]
    tmin = thickness["p05_mm"]
    add("DFM-WALL-001", tmin is None or tmin >= min_wall, thickness, min_wall, "error", f"5th-percentile wall thickness must be >= {min_wall} mm.")
    if rule.get("max_wall_mm") and thickness["median_mm"]:
        add("DFM-WALL-002", thickness["median_mm"] <= rule["max_wall_mm"], thickness["median_mm"], rule["max_wall_mm"], "warning", "Thick sections risk sink marks; consider coring out.")

    if "min_draft_deg" in rule:
        draft = draft_analysis(mesh, pull=rule.get("pull", (0, 0, 1)), min_draft_deg=rule["min_draft_deg"])
        limit = rule.get("max_no_draft_area_ratio", 0.05)
        add("DFM-DRAFT-001", draft["insufficient_draft_area_ratio"] <= limit, draft, limit, "warning", f"Walls parallel to the pull direction need >= {rule['min_draft_deg']}° draft.")
    if "max_undercut_ratio" in rule:
        ratio = undercut_ratio(mesh, pull=rule.get("pull", (0, 0, 1)))
        add("DFM-UNDERCUT-001", ratio <= rule["max_undercut_ratio"], ratio, rule["max_undercut_ratio"], "warning", "Surface trapped for a two-plate mould (needs slides/lifters).")
    if "max_overhang_ratio" in rule:
        ratio = overhang_ratio(mesh, max_angle_deg=rule.get("overhang_angle_deg", 45))
        add("DFM-OVERHANG-001", ratio <= rule["max_overhang_ratio"], ratio, rule["max_overhang_ratio"], "warning", "Downward-facing surfaces need supports.")

    ok = not any(c["severity"] == "error" for c in checks)
    return {
        "process": process,
        "rule_pack_version": rules["version"],
        "pass": ok,
        "checks": checks,
        "volume_mm3": round(float(abs(mesh.volume)), 2) if mesh.is_volume else None,
        "bbox_mm": [round(float(x), 2) for x in mesh.extents],
    }


def check_interfaces(original_dir, candidate_dir, interfaces: list[dict]) -> list[dict]:
    """Functional interfaces that must not move (FR-DFM-05).

    interfaces: [{"part": "body", "what": "bbox"}, {"param": "length"}]
    """

    from .cad import read_manifest

    out = []
    o_manifest, c_manifest = read_manifest(original_dir), read_manifest(candidate_dir)
    o_parts = {p["name"]: p["bbox"] for p in o_manifest["parts"]}
    c_parts = {p["name"]: p["bbox"] for p in c_manifest["parts"]}
    for item in interfaces:
        if "param" in item:
            name = item["param"]
            a = o_manifest["params"].get(name)
            b = c_manifest["params"].get(name)
            out.append({"interface": f"param:{name}", "pass": a == b, "original": a, "candidate": b})
        elif "part" in item:
            name = item["part"]
            tol = float(item.get("tolerance_mm", 1.0))
            a, b = o_parts.get(name), c_parts.get(name)
            if a is None or b is None:
                out.append({"interface": f"part:{name}", "pass": False, "original": a, "candidate": b, "reason": "part missing"})
                continue
            delta = max(abs(a[k] - b[k]) for k in a)
            out.append({"interface": f"part:{name}", "pass": delta <= tol, "max_delta_mm": round(delta, 3), "tolerance_mm": tol})
    return out
