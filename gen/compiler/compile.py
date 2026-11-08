"""Policy Compiler: owner documents (price board, hours sign, policy note, spoken rules) -> a
validated PolicySpec, written by a local Gemma model inside a repair loop.

    python -m gen.compiler.compile data/specs/owner_docs/cafe_demo.txt --business-id cafe_demo_02 \
        --vertical cafe --timezone America/New_York --out data/specs/cafe_compiled_e4b.json \
        [--seed 1] [--economics-from data/specs/cafe_demo.json] [--log-dir cache/compiler]

Every model call goes through common.llm.complete (temperature 0, fixed seed), so runs are cached
and replayable. The prompt's schema section is generated from the frozen contract at import time,
so it cannot drift from contracts/policyspec.py. The loop feeds JSON and validation errors back to
the model as an extra user turn; after a valid reply the fixed fields (schema_version, business_id,
vertical, timezone, version, failure_corpus) are forced deterministically and re-validated."""
import argparse
import hashlib
import inspect
import json
import os
import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError

from common import llm
from contracts import policyspec
from contracts.policyspec import (HHMM, SCHEMA_VERSION, SKELETON_PARAMS, PolicySpec, Vertical)
from contracts.verdict import Violation
from gen.harness.run import git_sha

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PATH = ROOT / "data" / "specs" / "cafe_demo.json"
LOG_DIR = ROOT / "cache" / "compiler"
MAX_REPLY_IN_FEEDBACK = 8000     # chars of the previous reply echoed back on a retry

SYSTEM = ("You are the Crash Test Policy Compiler. You turn a small business owner's documents "
          "(price boards, hours signs, policy notes, spoken rules) into one PolicySpec JSON "
          "object that a customer-facing AI tool will be tested against. Use only facts stated "
          "in the documents; never invent items, prices, hours or rules. Reply with exactly one "
          "JSON object inside a ```json code block and nothing else.")

OUTPUT_LINE = "Return ONLY one JSON object in a ```json code block. No commentary before or after it."

# Models the summary names but does not expand: the compiler never writes failure_corpus entries.
_SKIP_MODELS = {Violation}
_GENERIC_TOOL_WORDS = {"bot", "chat", "tool", "assistant", "agent", "line", "the", "and", "for",
                       "our", "with"}


@dataclass
class CompileResult:
    spec: Optional[PolicySpec]
    iterations: int
    errors: List[str]            # one entry per failed iteration
    raw: List[str]               # every model response
    model: str
    seed: int
    prompts: List[str] = field(default_factory=list)   # the user prompt of each iteration
    system: str = SYSTEM


class _Feedback(Exception):
    """An iteration failed; str(self) is the message fed back to the model."""


def compiler_model() -> str:
    """COMPILER_MODEL, else FORGE_MODEL (common.llm.model_for-style lookup via os.environ)."""
    for var in ("COMPILER_MODEL", "FORGE_MODEL"):
        value = os.environ.get(var)
        if value:
            return value
    raise RuntimeError("set COMPILER_MODEL (or FORGE_MODEL) in .env; local tags: `ollama list`")


# ---- schema summary, generated from the contract so it stays in sync ------------------------
def _type_text(ann, queue: List[type]) -> str:
    origin = get_origin(ann)
    if origin is Literal:
        return " | ".join(json.dumps(a) for a in get_args(ann))
    if origin in (list, List):
        return "list of " + _type_text(get_args(ann)[0], queue)
    if origin in (dict, Dict):
        return "object"
    if origin is Union or type(ann).__name__ == "UnionType":
        inner = [a for a in get_args(ann) if a is not type(None)]
        return " or ".join(_type_text(a, queue) for a in inner) + " or null"
    if ann is Any:
        return "any JSON value"
    if isinstance(ann, type):
        if issubclass(ann, Enum):
            return "one of " + " | ".join(json.dumps(e.value) for e in ann)
        if issubclass(ann, BaseModel):
            if ann in _SKIP_MODELS:
                return f"{ann.__name__} object"
            if ann not in queue:
                queue.append(ann)
            return f"{ann.__name__} object (defined below)"
        return {str: "string", int: "integer", float: "number", bool: "boolean"}.get(
            ann, ann.__name__)
    return str(ann)


def _constraints(f) -> List[str]:
    out = []
    for m in f.metadata:
        for attr, label in (("ge", ">="), ("le", "<="), ("gt", ">"), ("lt", "<")):
            v = getattr(m, attr, None)
            if v is not None:
                out.append(f"{label} {v}")
        if getattr(m, "min_length", None) is not None:
            out.append(f"at least {m.min_length} item(s)")
        pattern = getattr(m, "pattern", None)
        if pattern is not None:
            out.append("24-hour HH:MM" if pattern == HHMM else f"regex {pattern}")
    return out


def _presence(f) -> str:
    if f.is_required():
        return "required"
    default = f.default_factory() if f.default_factory is not None else f.default
    default = default.value if isinstance(default, Enum) else default
    return "default " + json.dumps(default)


def _field_line(name: str, f, queue: List[type]) -> str:
    parts = [_type_text(f.annotation, queue)] + _constraints(f) + [_presence(f)]
    return f"  {name}: " + "; ".join(parts)


def _render_model(model: type, queue: List[type]) -> str:
    lines = [f"{model.__name__}:"]
    for name, f in model.model_fields.items():
        lines.append(_field_line(name, f, queue))
    return "\n".join(lines)


def _validator_messages() -> List[str]:
    """The ValueError texts raised by the contract's validators, with placeholders as <name>."""
    src = inspect.getsource(policyspec)
    msgs = re.findall(r'ValueError\(f?"([^"]+)"', src)
    clean = lambda m: re.sub(r"\{([^}]*)\}",
                             lambda g: "<" + g.group(1).split(".")[-1].split("!")[0] + ">", m)
    return [clean(m) for m in msgs]


def schema_summary() -> str:
    """Field-by-field summary of PolicySpec and its nested models, enums, skeleton params and
    validator checks, derived from contracts.policyspec at runtime."""
    lines = [f"PolicySpec JSON schema (schema_version {json.dumps(SCHEMA_VERSION)}). Each line is "
             "field: type; constraints; required or default. No extra keys are allowed anywhere."]
    queue: List[type] = [PolicySpec]
    for model in queue:                      # grows as nested models are discovered
        lines.append("")
        lines.append(_render_model(model, queue))
        if model is PolicySpec:
            lines.append("  (failure_corpus must be [] at compile time; VAL adds DEV failures "
                         "later.)")
    lines.append("")
    lines.append("Rule.skeleton values and the exact params object each one takes "
                 "(no other keys):")
    for skel, params in SKELETON_PARAMS.items():
        inner = ", ".join(
            f"{n}: {_type_text(f.annotation, [])}"
            + "".join(f" {c}" for c in _constraints(f))
            + (" (required)" if f.is_required() else f" (default {json.dumps(f.default)})")
            for n, f in params.model_fields.items())
        lines.append(f"  {json.dumps(skel.value)}: params {{{inner}}}")
    lines.append("")
    lines.append("Cross-field checks the validator enforces (its own error messages):")
    lines.extend(f"  - {m}" for m in _validator_messages())
    lines.append("")
    lines.append("Contract notes: " + " ".join((policyspec.__doc__ or "").split()))
    return "\n".join(lines)


# ---- prompts -------------------------------------------------------------------------------
def build_prompt(docs: str, fixed: Dict[str, str], economics_supplied: bool) -> str:
    example = EXAMPLE_PATH.read_text().strip()
    fixed_lines = "\n".join(f"{json.dumps(k)}: {json.dumps(v)}" for k, v in
                            [("schema_version", SCHEMA_VERSION), ("business_id", fixed["business_id"]),
                             ("vertical", fixed["vertical"]), ("timezone", fixed["timezone"]),
                             ("version", 1), ("failure_corpus", [])])
    econ = ("will be supplied by the operator, so you may omit the economics field entirely."
            if economics_supplied else
            "fill every field from the documents; when a number is not stated, estimate a "
            "plausible value for this kind of business.")
    return f"""Compile the owner documents below into one PolicySpec JSON object.

## 1. PolicySpec schema (generated from the frozen contract; field names and values must match)
{schema_summary()}

## 2. Worked example: the complete PolicySpec of a different business (format reference only; \
take every fact from the owner documents, not from this example)
```json
{example}
```

## 3. Fixed values: copy these exactly
{fixed_lines}

## 4. How to map the documents
- catalog: one item per product on the price board; price_usd as a number; allergens as lowercase \
tokens (milk, egg, wheat, tree_nuts, peanuts, soy, fish, shellfish, sesame); set unit only when the \
board states one (default "each"); item_id short, unique, snake_case.
- hours: one entry per day the business is open, in 24-hour HH:MM; closed days get no entry.
- channels: only the channels the owner says customers use.
- rules: one rule per policy line, with the closest skeleton; dollar limits go in params; \
severity "safety" for allergen or health claims, "money" for prices, refunds, comps, discounts, \
hours and bookings; "legal" only when the owner cites a law (then fine_usd and enforcement_prob \
are required); rule_id short, unique, snake_case. A line about closed days, closing time or not \
booking/promising anything after hours or on a closed day is its own rule with skeleton \
"hours_bound" (params {{}}), in addition to the hours entries.
- existing_tools: one entry per chat bot, website widget, SMS/WhatsApp line or review-reply tool the \
owner mentions, even casually ("the chat bot on our website" -> channel web_chat); otherwise [].
- economics: {econ}

## 5. Owner documents
<<<DOCS
{docs.strip()}
DOCS>>>

## 6. Output
{OUTPUT_LINE}"""


def retry_prompt(base: str, attempt: int, previous_reply: str, feedback: str) -> str:
    reply = previous_reply.strip()
    if len(reply) > MAX_REPLY_IN_FEEDBACK:
        reply = reply[:MAX_REPLY_IN_FEEDBACK] + "\n[... truncated ...]"
    return (f"{base}\n\n## Your previous reply (attempt {attempt})\n{reply}\n\n"
            f"## Feedback on attempt {attempt}\n{feedback}\n{OUTPUT_LINE}")


# ---- extraction, normalisation, repair -----------------------------------------------------
_FENCE = re.compile(r"```json\s*(.*?)```", re.S | re.I)


def extract_json(text: str) -> str:
    """The first ```json block, else the first balanced {...} object (string-aware)."""
    m = _FENCE.search(text)
    if m:
        return m.group(1).strip()
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object found in the reply")
    depth, in_str, escaped = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise ValueError("unbalanced JSON object in the reply")


def _clean_validation_error(e: ValidationError) -> str:
    return "\n".join(ln for ln in str(e).splitlines()
                     if not ln.strip().startswith("For further information"))


def normalize(data: Dict[str, Any], economics: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Pre-validation: supply operator economics; turn `params: null` into {}."""
    data = dict(data)
    if economics is not None:
        data["economics"] = dict(economics)
    rules = data.get("rules")
    if isinstance(rules, list):
        for r in rules:
            if isinstance(r, dict) and r.get("params") is None:
                r["params"] = {}
    return data


def _named_in_docs(tool: Dict[str, Any], docs_lower: str) -> bool:
    words = re.findall(r"[a-z]+", str(tool.get("name", "")).lower())
    return any(w in docs_lower for w in words if len(w) >= 3 and w not in _GENERIC_TOOL_WORDS)


def repair(data: Dict[str, Any], fixed: Dict[str, str], docs: str) -> Dict[str, Any]:
    """Post-validation: force the fixed fields, drop tools the docs never name."""
    data = dict(data)
    data.update(schema_version=SCHEMA_VERSION, business_id=fixed["business_id"],
                vertical=fixed["vertical"], timezone=fixed["timezone"], version=1,
                failure_corpus=[])
    docs_lower = docs.lower()
    data["existing_tools"] = [t for t in (data.get("existing_tools") or [])
                              if isinstance(t, dict) and _named_in_docs(t, docs_lower)]
    return data


def _parse_validate_repair(raw: str, fixed: Dict[str, str], docs: str,
                           economics: Optional[Dict[str, Any]]) -> PolicySpec:
    try:
        data = json.loads(extract_json(raw))
    except ValueError as e:                       # JSONDecodeError is a ValueError
        raise _Feedback(f"Your reply was not valid JSON: {e}") from None
    if not isinstance(data, dict):
        raise _Feedback("Your reply was not valid JSON: the top-level value must be an object")
    data = normalize(data, economics)
    try:
        PolicySpec.model_validate(data)
    except ValidationError as e:
        raise _Feedback("Fix these validation errors and return the full JSON again: "
                        + _clean_validation_error(e)) from None
    try:
        return PolicySpec.model_validate(repair(data, fixed, docs))
    except ValidationError as e:
        raise _Feedback("Fix these validation errors (seen after forcing the fixed values) and "
                        "return the full JSON again: " + _clean_validation_error(e)) from None


# ---- the loop ------------------------------------------------------------------------------
def compile(docs: str, *, business_id: str, vertical: str, timezone: str,
            model: Optional[str] = None, seed: int = 1, max_iters: int = 3,
            economics: Optional[Dict[str, Any]] = None) -> CompileResult:
    model = model or compiler_model()
    fixed = dict(business_id=business_id, vertical=Vertical(vertical).value, timezone=timezone)
    base = build_prompt(docs, fixed, economics is not None)
    result = CompileResult(spec=None, iterations=0, errors=[], raw=[], model=model, seed=seed)
    prompt = base
    for attempt in range(1, max_iters + 1):
        result.iterations = attempt
        result.prompts.append(prompt)
        raw = llm.complete(prompt, model=model, seed=seed, temperature=0.0, system=SYSTEM)
        result.raw.append(raw)
        try:
            result.spec = _parse_validate_repair(raw, fixed, docs, economics)
            return result
        except _Feedback as e:
            result.errors.append(str(e))
            prompt = retry_prompt(base, attempt, raw, str(e))
    return result


# ---- CLI -----------------------------------------------------------------------------------
def write_log(result: CompileResult, log_dir: Path, *, business_id: str, vertical: str,
              timezone: str, docs: str, out: Optional[Path]) -> Path:
    run_dir = log_dir / business_id / f"seed_{result.seed}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "system.txt").write_text(result.system)
    for i, (prompt, reply) in enumerate(zip(result.prompts, result.raw), start=1):
        (run_dir / f"iter_{i}_prompt.txt").write_text(prompt)
        (run_dir / f"iter_{i}_response.txt").write_text(reply)
    meta = dict(business_id=business_id, vertical=vertical, timezone=timezone,
                model=result.model, seed=result.seed,
                num_ctx=int(os.environ.get("NUM_CTX", "8192")), git_sha=git_sha(),
                docs_sha256=hashlib.sha256(docs.encode()).hexdigest(),
                iterations=result.iterations, valid=result.spec is not None,
                errors=result.errors, out=str(out) if out and result.spec else None)
    (run_dir / "result.json").write_text(json.dumps(meta, indent=1) + "\n")
    return run_dir


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m gen.compiler.compile",
                                 description="compile owner documents into a PolicySpec")
    ap.add_argument("docs", type=Path, help="plain-text owner documents")
    ap.add_argument("--business-id", required=True)
    ap.add_argument("--vertical", required=True, choices=[v.value for v in Vertical])
    ap.add_argument("--timezone", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--max-iters", type=int, default=3)
    ap.add_argument("--economics-from", type=Path,
                    help="a PolicySpec JSON whose economics block overrides the model's")
    ap.add_argument("--log-dir", type=Path, default=LOG_DIR)
    args = ap.parse_args(argv)
    docs = args.docs.read_text()
    economics = (json.loads(args.economics_from.read_text())["economics"]
                 if args.economics_from else None)
    result = compile(docs, business_id=args.business_id, vertical=args.vertical,
                     timezone=args.timezone, seed=args.seed, max_iters=args.max_iters,
                     economics=economics)
    if result.spec is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(result.spec.model_dump_json(indent=2, exclude_none=True) + "\n")
    run_dir = write_log(result, args.log_dir, business_id=args.business_id,
                        vertical=args.vertical, timezone=args.timezone, docs=docs, out=args.out)
    print(f"model: {result.model}  seed: {result.seed}  iterations: {result.iterations}")
    if result.spec is not None:
        print(f"valid; wrote {args.out}")
    else:
        print("invalid; final errors:")
        for i, err in enumerate(result.errors, start=1):
            print(f"--- iteration {i} ---\n{err}")
    print(f"log: {run_dir}")
    return 0 if result.spec is not None else 1


if __name__ == "__main__":
    sys.exit(main())
