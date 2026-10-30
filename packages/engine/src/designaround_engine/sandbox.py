"""Static policy for CAD programs written by people or by the CAD Agent.

A CAD program is ordinary Python that uses CadQuery. Because programs can come
from a language model, they are validated before execution and then executed in
a separate process (see ``cad_worker``) with a short timeout, a stripped
environment and a restricted set of builtins.

This is defence in depth, not a perfect sandbox. Server-side execution of
untrusted programs must additionally run inside a container sandbox
(gVisor/nsjail/firecracker); see 开发文档/技术方案.md §10.
"""

from __future__ import annotations

import ast

from .errors import ProgramRejected

ALLOWED_IMPORTS = frozenset({"cadquery", "math"})

FORBIDDEN_NAMES = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "open",
        "__import__",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
        "input",
        "breakpoint",
        "exit",
        "quit",
        "help",
        "memoryview",
        "classmethod",
        "staticmethod",
        "super",
        "type",
        "object",
    }
)

# Methods on CadQuery objects that touch the filesystem or the network.
FORBIDDEN_ATTRIBUTES = frozenset(
    {
        "exporters",
        "importers",
        "export",
        "exportStl",
        "exportStep",
        "exportBrep",
        "importStep",
        "importBrep",
        "importStl",
        "occ_impl",
        "system",
        "popen",
    }
)

SAFE_BUILTINS = {
    name: __builtins__[name] if isinstance(__builtins__, dict) else getattr(__builtins__, name)
    for name in (
        "abs",
        "all",
        "any",
        "bool",
        "dict",
        "divmod",
        "enumerate",
        "filter",
        "float",
        "int",
        "isinstance",
        "len",
        "list",
        "map",
        "max",
        "min",
        "pow",
        "range",
        "reversed",
        "round",
        "set",
        "sorted",
        "str",
        "sum",
        "tuple",
        "zip",
        "ValueError",
        "Exception",
        "print",
    )
}

MAX_SOURCE_CHARS = 60_000


def validate_program(source: str) -> ast.Module:
    """Parse and validate a CAD program. Raises ProgramRejected on violations."""

    if not isinstance(source, str) or not source.strip():
        raise ProgramRejected("Program is empty.")
    if len(source) > MAX_SOURCE_CHARS:
        raise ProgramRejected(f"Program is longer than {MAX_SOURCE_CHARS} characters.")
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        raise ProgramRejected(f"Syntax error on line {exc.lineno}: {exc.msg}") from exc

    violations: list[str] = []
    has_build = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in ALLOWED_IMPORTS:
                    violations.append(f"line {node.lineno}: import of '{alias.name}' is not allowed")
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")[0]
            if module not in ALLOWED_IMPORTS or node.level:
                violations.append(f"line {node.lineno}: import from '{node.module}' is not allowed")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            violations.append(f"line {node.lineno}: use of '{node.id}' is not allowed")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__"):
                violations.append(f"line {node.lineno}: dunder attribute '{node.attr}' is not allowed")
            elif node.attr in FORBIDDEN_ATTRIBUTES:
                violations.append(f"line {node.lineno}: attribute '{node.attr}' is not allowed")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            violations.append(f"line {node.lineno}: global/nonlocal is not allowed")
        elif isinstance(node, (ast.AsyncFunctionDef, ast.Await, ast.AsyncFor, ast.AsyncWith)):
            violations.append(f"line {node.lineno}: async code is not allowed")
        elif isinstance(node, ast.FunctionDef) and node.name == "build":
            has_build = True
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "__" in node.value:
            violations.append(f"line {node.lineno}: string constants containing '__' are not allowed")

    if not has_build:
        violations.append("program must define a function build(p)")
    if violations:
        raise ProgramRejected("Program rejected by sandbox policy.", details={"violations": violations[:20]})
    return tree


def restricted_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
    if level or name.split(".")[0] not in ALLOWED_IMPORTS:
        raise ImportError(f"import of '{name}' is not allowed")
    return __import__(name, globals, locals, fromlist, level)


def safe_globals() -> dict:
    builtins = dict(SAFE_BUILTINS)
    builtins["__import__"] = restricted_import
    return {"__builtins__": builtins, "__name__": "designaround_program"}
