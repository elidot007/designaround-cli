"""Subprocess entry point that executes one CAD program.

Input (stdin, JSON): {"source": str, "params": {name: value}, "outdir": str}
Output (stdout, JSON): {"ok": true, "parts": [...], "bbox": {...}, ...}
                    or {"ok": false, "error": {"code": ..., "message": ..., "details": ...}}

Run as ``python -m designaround_engine.cad_worker``. The parent process applies
the timeout and the stripped environment (see cad.build_program).
"""

from __future__ import annotations

import json
import os
import sys
import traceback


def _limit_resources() -> None:
    try:
        import resource

        cpu = int(os.environ.get("DA_CAD_CPU_SECONDS", "120"))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 5))
        mem = int(os.environ.get("DA_CAD_MEMORY_MB", "4096")) * 1024 * 1024
        try:
            resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        except (ValueError, OSError):
            pass  # not enforceable on macOS
    except ImportError:  # Windows
        pass


def _normalise_parts(result, cq, unclaimed: set[str]):
    def to_shape(obj):
        if isinstance(obj, cq.Workplane):
            vals = [v for v in obj.vals() if isinstance(v, cq.Shape)]
            if not vals:
                raise ValueError("Workplane has no solids")
            return vals[0] if len(vals) == 1 else cq.Compound.makeCompound(vals)
        if isinstance(obj, cq.Assembly):
            return obj.toCompound()
        if isinstance(obj, cq.Shape):
            return obj
        raise ValueError(f"build() returned unsupported type {type(obj).__name__}")

    if isinstance(result, dict):
        parts = []
        for name, value in result.items():
            claimed = name not in unclaimed
            if isinstance(value, dict):
                claimed = bool(value.get("claimed", claimed))
                value = value.get("shape")
            if value is None:
                continue
            parts.append((str(name), to_shape(value), claimed))
        if not parts:
            raise ValueError("build() returned an empty dict")
        return parts
    return [("body", to_shape(result), True)]


def main() -> int:
    _limit_resources()
    request = json.loads(sys.stdin.read())
    source = request["source"]
    params = request.get("params") or {}
    outdir = request["outdir"]

    try:
        from designaround_engine.sandbox import safe_globals, validate_program

        validate_program(source)
        import contextlib

        import cadquery as cq

        namespace = safe_globals()
        code = compile(source, "<cad-program>", "exec")
        # print() inside programs goes to stderr so stdout stays valid JSON.
        with contextlib.redirect_stdout(sys.stderr):
            exec(code, namespace)  # noqa: S102 - validated program, isolated process
            build = namespace.get("build")
            if not callable(build):
                raise ValueError("build(p) is not callable")
            unclaimed = set(namespace.get("UNCLAIMED", []) or [])
            result = build(dict(params))
        parts = _normalise_parts(result, cq, unclaimed)

        os.makedirs(os.path.join(outdir, "parts"), exist_ok=True)
        part_rows = []
        shapes = []
        for name, shape, claimed in parts:
            if not shape.isValid():
                shape = shape.fix()
            safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name) or "part"
            path = os.path.join(outdir, "parts", f"{safe}.step")
            cq.exporters.export(cq.Workplane("XY").add(shape), path)
            bb = shape.BoundingBox()
            volume = 0.0
            try:
                volume = float(shape.Volume())
            except Exception:  # noqa: BLE001
                volume = 0.0
            part_rows.append(
                {
                    "name": name,
                    "file": f"parts/{safe}.step",
                    "claimed": claimed,
                    "valid": bool(shape.isValid()),
                    "volume_mm3": round(volume, 3),
                    "bbox": {
                        "xmin": bb.xmin, "ymin": bb.ymin, "zmin": bb.zmin,
                        "xmax": bb.xmax, "ymax": bb.ymax, "zmax": bb.zmax,
                    },
                }
            )
            shapes.append(shape)
        combined = shapes[0] if len(shapes) == 1 else cq.Compound.makeCompound(shapes)
        cq.exporters.export(cq.Workplane("XY").add(combined), os.path.join(outdir, "model.step"))
        bb = combined.BoundingBox()
        out = {
            "ok": True,
            "parts": part_rows,
            "bbox": {
                "xmin": bb.xmin, "ymin": bb.ymin, "zmin": bb.zmin,
                "xmax": bb.xmax, "ymax": bb.ymax, "zmax": bb.zmax,
                "xlen": bb.xlen, "ylen": bb.ylen, "zlen": bb.zlen,
            },
            "volume_mm3": round(sum(p["volume_mm3"] for p in part_rows), 3),
        }
    except Exception as exc:  # noqa: BLE001 - reported to caller
        from designaround_engine.errors import EngineError

        if isinstance(exc, EngineError):
            error = exc.to_dict()
        else:
            tb = traceback.extract_tb(exc.__traceback__)
            program_frames = [f for f in tb if f.filename == "<cad-program>"]
            line = program_frames[-1].lineno if program_frames else None
            error = {
                "code": "build_failed",
                "message": f"{type(exc).__name__}: {exc}",
                "details": {"line": line},
            }
        out = {"ok": False, "error": error}
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
