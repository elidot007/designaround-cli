import json

import pytest

from designaround_engine import cad
from designaround_engine.errors import BuildFailed, InvalidInput, ProgramRejected
from designaround_engine.params import extract_param_schema, resolve
from designaround_engine.sandbox import validate_program

GOOD = """
import cadquery as cq
PARAMS = {"w": {"default": 40.0, "min": 10.0, "max": 100.0}}
def build(p):
    return cq.Workplane("XY").box(p["w"], 20, 10)
"""


@pytest.mark.parametrize(
    "source,needle",
    [
        ("import os\ndef build(p):\n    return 1", "import of 'os'"),
        ("import cadquery as cq\ndef build(p):\n    return open('/etc/passwd')", "'open'"),
        ("import cadquery as cq\ndef build(p):\n    return cq.__class__", "dunder"),
        ("import cadquery as cq\ndef build(p):\n    cq.exporters.export(None, 'x')", "exporters"),
        ("import cadquery as cq\nx = 1", "build(p)"),
        ("from subprocess import run\ndef build(p):\n    return 1", "subprocess"),
        ("import cadquery as cq\ndef build(p):\n    return getattr(cq, 'Workplane')", "getattr"),
    ],
)
def test_sandbox_rejects(source, needle):
    with pytest.raises(ProgramRejected) as info:
        validate_program(source)
    assert needle in json.dumps(info.value.details) or needle in info.value.message


def test_param_schema_and_clamp():
    schema = extract_param_schema(GOOD)
    resolved = resolve(schema, {"w": 500})
    assert resolved.values["w"] == 100.0
    assert resolved.warnings
    with pytest.raises(InvalidInput):
        resolve(schema, {"nope": 1})


def test_build_exports_and_hashes(tmp_path):
    manifest = cad.build_program(GOOD, {"w": 50}, tmp_path / "v")
    assert manifest["bbox"]["xlen"] == pytest.approx(50, abs=1e-6)
    for name in ("model.step", "model.stl", "model.glb", "program.py", "params.json"):
        assert name in manifest["files"]
        assert len(manifest["files"][name]["sha256"]) == 64


def test_build_failure_reports_line(tmp_path):
    bad = GOOD.replace('box(p["w"], 20, 10)', 'box(p["w"], 20, 10).faces(">Z").fillet(50)')
    with pytest.raises(BuildFailed) as info:
        cad.build_program(bad, {}, tmp_path / "v")
    assert info.value.details.get("line") is not None


def test_rebuild_with_overrides(tmp_path):
    cad.build_program(GOOD, {}, tmp_path / "a")
    manifest = cad.rebuild_with(tmp_path / "a", {"w": 70}, tmp_path / "b")
    assert manifest["params"]["w"] == 70.0
