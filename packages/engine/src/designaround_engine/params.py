"""Parameter schemas for parametric CAD programs.

Programs declare a module-level ``PARAMS`` dict::

    PARAMS = {
        "length": {"default": 320.0, "min": 120.0, "max": 600.0, "unit": "mm", "desc": "Outer length"},
        "handle_style": {"default": "arched", "choices": ["arched", "recessed", "none"]},
        "rib_count": {"default": 0, "min": 0, "max": 8, "type": "int"},
    }

Parameters are what users drag in the parameter panel (FR-GEN-12) and what the
design-around explorer varies (FR-GEN-03).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any

from .errors import InvalidInput


@dataclass
class ParamSpec:
    name: str
    default: Any
    min: float | None = None
    max: float | None = None
    choices: list[Any] | None = None
    unit: str | None = None
    desc: str | None = None
    kind: str = "float"  # float | int | choice | bool

    def to_dict(self) -> dict:
        data = {"name": self.name, "default": self.default, "kind": self.kind}
        for key in ("min", "max", "choices", "unit", "desc"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data


@dataclass
class ResolvedParams:
    values: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


def extract_param_schema(source: str) -> dict[str, ParamSpec]:
    """Read the PARAMS literal from program source without executing it."""

    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "PARAMS" for t in node.targets
        ):
            try:
                raw = ast.literal_eval(node.value)
            except ValueError as exc:
                raise InvalidInput("PARAMS must be a literal dict.") from exc
            return parse_schema(raw)
    return {}


def parse_schema(raw: dict) -> dict[str, ParamSpec]:
    if not isinstance(raw, dict):
        raise InvalidInput("PARAMS must be a dict.")
    specs: dict[str, ParamSpec] = {}
    for name, spec in raw.items():
        if not isinstance(spec, dict) or "default" not in spec:
            raise InvalidInput(f"PARAMS['{name}'] must be a dict with a default.")
        default = spec["default"]
        if "choices" in spec:
            kind = "choice"
        elif isinstance(default, bool):
            kind = "bool"
        elif spec.get("type") == "int" or (isinstance(default, int) and not isinstance(default, bool)):
            kind = "int"
        else:
            kind = "float"
        specs[name] = ParamSpec(
            name=name,
            default=default,
            min=spec.get("min"),
            max=spec.get("max"),
            choices=list(spec["choices"]) if "choices" in spec else None,
            unit=spec.get("unit"),
            desc=spec.get("desc"),
            kind=kind,
        )
    return specs


def coerce(spec: ParamSpec, value: Any) -> Any:
    if spec.kind == "choice":
        if value not in spec.choices:
            # Accept string forms of numeric choices from the CLI.
            for choice in spec.choices:
                if str(choice) == str(value):
                    return choice
            raise InvalidInput(f"'{spec.name}' must be one of {spec.choices}, got {value!r}.")
        return value
    if spec.kind == "bool":
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidInput(f"'{spec.name}' must be a number, got {value!r}.") from exc
    return int(round(number)) if spec.kind == "int" else number


def resolve(schema: dict[str, ParamSpec], overrides: dict[str, Any] | None) -> ResolvedParams:
    overrides = dict(overrides or {})
    unknown = sorted(set(overrides) - set(schema))
    if unknown:
        raise InvalidInput(f"Unknown parameters: {', '.join(unknown)}", details={"known": sorted(schema)})
    values: dict[str, Any] = {}
    warnings: list[str] = []
    for name, spec in schema.items():
        value = coerce(spec, overrides[name]) if name in overrides else spec.default
        if spec.kind in {"float", "int"}:
            low = spec.min if spec.min is not None else value
            high = spec.max if spec.max is not None else value
            clamped = min(max(value, low), high)
            if clamped != value:
                warnings.append(f"{name}={value} clamped to {clamped} (range {spec.min}..{spec.max})")
                value = int(clamped) if spec.kind == "int" else float(clamped)
        values[name] = value
    return ResolvedParams(values=values, warnings=warnings)


def parse_assignments(items: list[str]) -> dict[str, str]:
    """Parse ``["length=320", "handle_style=recessed"]`` from the CLI."""

    result: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise InvalidInput(f"Expected name=value, got {item!r}.")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise InvalidInput(f"Empty parameter name in {item!r}.")
        result[key] = value.strip()
    return result
