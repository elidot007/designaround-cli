"""Build CAD programs into versioned model directories.

A *version directory* is the single source of truth for one design version::

    <version>/
      program.py        CadQuery program (PARAMS + build(p))
      params.json       resolved parameter values
      manifest.json     parts, claimed flags, bbox, volume, hashes, provenance
      model.step        combined B-rep
      parts/*.step      one B-rep per named part
      model.stl / model.glb / model.3mf   exported meshes

Everything downstream (views, renders, DFM, viewpacks) reads from here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .errors import BuildFailed, ProgramRejected
from .evidence import sha256_file
from .params import extract_param_schema, resolve
from .sandbox import validate_program

DEFAULT_TIMEOUT_S = 120


def ensure_kernel() -> None:
    """Load CadQuery/OCP in this process before any timed CAD worker starts.

    The first load after installation can take minutes (macOS verifies OCP's
    large native libraries once). That must not count against the build
    timeout; later loads take about a second.
    """

    if "cadquery" in sys.modules:
        return
    from .workspace import home

    marker = home() / ".cad-kernel-ready"
    if not marker.exists():
        sys.stderr.write("Loading the CAD kernel (first run after install can take a few minutes)…\n")
        sys.stderr.flush()
    import cadquery  # noqa: F401

    marker.touch()


def _worker_env() -> dict[str, str]:
    keep = {"PATH", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "PYTHONNOUSERSITE", "PYTHONUTF8"}
    # Windows has no passwd database: Path.home() and per-user cache dirs need these.
    keep |= {"USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "WINDIR", "COMSPEC", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE"}
    env = {k: v for k, v in os.environ.items() if k in keep}
    env["PYTHONHASHSEED"] = "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["DA_CAD_CPU_SECONDS"] = os.environ.get("DA_CAD_CPU_SECONDS", str(DEFAULT_TIMEOUT_S))
    env["DA_CAD_MEMORY_MB"] = os.environ.get("DA_CAD_MEMORY_MB", "4096")
    # The worker must import the same installed engine package.
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    return env


def build_program(
    source: str,
    overrides: dict[str, Any] | None,
    outdir: str | os.PathLike,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    provenance: dict | None = None,
) -> dict:
    """Validate, execute and export one program. Returns the manifest dict."""

    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    validate_program(source)
    schema = extract_param_schema(source)
    resolved = resolve(schema, overrides)

    (outdir / "program.py").write_text(source, encoding="utf-8")
    (outdir / "params.json").write_text(json.dumps(resolved.values, indent=2, ensure_ascii=False), encoding="utf-8")

    payload = json.dumps({"source": source, "params": resolved.values, "outdir": str(outdir.resolve())})
    ensure_kernel()
    from .geometry import export_meshes, load_version_parts

    started = time.perf_counter()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "designaround_engine.cad_worker"],
            input=payload,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(outdir),
            env=_worker_env(),
            check=False,
            # Windows: no console window for the worker when the desktop app runs us.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise BuildFailed(f"CAD build timed out after {timeout:.0f}s.") from exc
    elapsed = time.perf_counter() - started

    try:
        result = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise BuildFailed(
            "CAD worker returned invalid output.",
            details={"stderr": (proc.stderr or "")[-2000:], "returncode": proc.returncode},
        ) from exc
    if not result.get("ok"):
        error = result.get("error") or {}
        if error.get("code") == "program_rejected":
            raise ProgramRejected(error.get("message", "rejected"), details=error.get("details"))
        raise BuildFailed(error.get("message", "build failed"), details=error.get("details") or {})

    parts = load_version_parts(outdir, part_rows=result["parts"])
    meshes = export_meshes(parts, outdir)

    files = {"program.py": "program.py", "params.json": "params.json", "model.step": "model.step", **meshes}
    for row in result["parts"]:
        files[row["file"]] = row["file"]
    manifest = {
        "schema_version": 1,
        "engine_version": __version__,
        "representation": "brep",
        "params": resolved.values,
        "param_schema": {name: spec.to_dict() for name, spec in schema.items()},
        "warnings": resolved.warnings,
        "parts": result["parts"],
        "bbox": result["bbox"],
        "volume_mm3": result["volume_mm3"],
        "build_seconds": round(elapsed, 3),
        "files": {name: {"path": rel, "sha256": sha256_file(outdir / rel)} for name, rel in files.items()},
        "provenance": provenance or {},
    }
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


def rebuild_with(version_dir: str | os.PathLike, overrides: dict[str, Any], outdir: str | os.PathLike, **kw) -> dict:
    """Rebuild an existing version with changed parameters (parameter tweak)."""

    version_dir = Path(version_dir)
    source = (version_dir / "program.py").read_text(encoding="utf-8")
    base = json.loads((version_dir / "params.json").read_text(encoding="utf-8"))
    base.update(overrides)
    return build_program(source, base, outdir, **kw)


def read_manifest(version_dir: str | os.PathLike) -> dict:
    path = Path(version_dir) / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))
