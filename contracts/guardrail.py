"""Guardrail contract. FROZEN.
A guardrail file in gen/forge/guardrails/ handles one Skeleton and exports:
    RULE_SKELETON: Skeleton
    VERSION: int (>= 1)
    def check(history: list[Msg], response: Msg, spec: PolicySpec, rule: Rule) -> Verdict
`history` is every message before `response` (the last item is the customer's latest turn).
Pure Python only: imports limited to ALLOWED_IMPORTS, no I/O, no model calls, under 50 ms.
Prefer REWRITE (a corrected reply) over BLOCK."""
import ast
from types import ModuleType
from typing import List

from contracts.policyspec import Skeleton

ALLOWED_IMPORTS = {"re", "math", "datetime", "decimal", "typing", "dataclasses", "zoneinfo",
                   "contracts"}
BANNED_NAMES = {"open", "eval", "exec", "compile", "input", "breakpoint", "globals", "locals",
                "vars", "getattr", "setattr", "delattr", "memoryview", "help"}
DANGEROUS_ATTRS = {"sys", "os", "subprocess", "inspect", "importlib", "builtins", "io", "pathlib",
                   "shutil", "socket", "ctypes", "pickle", "marshal", "gc", "threading",
                   "multiprocessing", "signal", "urllib", "http", "tempfile", "glob"}


def check_source(src: str) -> List[str]:
    """Static lint run on every guardrail before import. Not an OS sandbox: it stops accidental
    I/O by a generated guardrail. Hangs are handled by the runtime's time limit."""
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [f"syntax error: {e}"]
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in ALLOWED_IMPORTS:
                    problems.append(f"import {a.name} not allowed")
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                problems.append("relative imports not allowed")
            elif (node.module or "").split(".")[0] not in ALLOWED_IMPORTS:
                problems.append(f"from {node.module} import not allowed")
            for a in node.names:
                if a.name in DANGEROUS_ATTRS or a.name == "*":
                    problems.append(f"importing {a.name} not allowed")
        elif isinstance(node, ast.Name) and (node.id in BANNED_NAMES or "__" in node.id):
            problems.append(f"name {node.id} not allowed")
        elif isinstance(node, ast.Attribute) and (node.attr in DANGEROUS_ATTRS
                                                  or node.attr.startswith("_")):
            problems.append(f"attribute .{node.attr} not allowed")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "__" in node.value:
            problems.append("string constants containing '__' not allowed")
        elif isinstance(node, ast.While):
            problems.append("while loops not allowed (use bounded for loops)")
        elif isinstance(node, ast.ExceptHandler):
            caught = getattr(node.type, "id", None)
            if node.type is None or caught in {"BaseException", "TimeoutError"}:
                problems.append("bare/BaseException/TimeoutError except not allowed")
    return problems


def check_loaded_globals(mod: ModuleType) -> List[str]:
    """After import: every module object a guardrail holds must be allowlisted."""
    return [f"holds module {v.__name__}" for v in vars(mod).values()
            if isinstance(v, ModuleType) and v.__name__.split(".")[0] not in ALLOWED_IMPORTS]


def validate_module(mod: ModuleType) -> Skeleton:
    skel = getattr(mod, "RULE_SKELETON", None)
    if not isinstance(skel, Skeleton):
        raise TypeError(f"{mod.__name__}: RULE_SKELETON must be a Skeleton")
    version = getattr(mod, "VERSION", None)
    if not isinstance(version, int) or version < 1:
        raise TypeError(f"{mod.__name__}: VERSION must be an int >= 1")
    fn = getattr(mod, "check", None)
    if not callable(fn) or fn.__code__.co_argcount != 4:
        raise TypeError(f"{mod.__name__}: needs check(history, response, spec, rule)")
    return skel
