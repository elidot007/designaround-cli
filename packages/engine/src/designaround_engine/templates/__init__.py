"""Parametric starting points for the CAD Agent.

Templates are ordinary CAD programs (``PARAMS`` + ``build(p)``) with an extra
``TEMPLATE`` literal describing the article, Locarno class and which
parameters control which visual feature. The CAD Agent can start from a
template and edit it, or write a program from scratch.
"""

from __future__ import annotations

import ast
from pathlib import Path

from ..errors import NotFound
from ..params import extract_param_schema

_DIR = Path(__file__).parent


def _template_meta(source: str) -> dict:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "TEMPLATE" for t in node.targets):
            return ast.literal_eval(node.value)
    return {}


def list_templates() -> list[dict]:
    rows = []
    for path in sorted(_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        source = path.read_text(encoding="utf-8")
        meta = _template_meta(source)
        meta["params"] = {name: spec.to_dict() for name, spec in extract_param_schema(source).items()}
        rows.append(meta)
    return rows


def get_template_source(template_id: str) -> str:
    path = _DIR / f"{template_id}.py"
    if not path.exists() or template_id.startswith("_"):
        raise NotFound(f"Unknown template '{template_id}'", details={"known": [t["id"] for t in list_templates()]})
    return path.read_text(encoding="utf-8")


def get_template_meta(template_id: str) -> dict:
    return _template_meta(get_template_source(template_id))


def program_meta(source: str) -> dict:
    """TEMPLATE metadata of any program (empty dict if the program has none)."""

    try:
        return _template_meta(source)
    except (SyntaxError, ValueError):
        return {}
