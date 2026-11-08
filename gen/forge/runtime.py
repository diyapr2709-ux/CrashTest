"""Loads the ACTIVE guardrail per skeleton and applies them to a bot reply.
active.json: {skeleton: {"file": stem, "mode": "shadow"|"enforce"}}. Promotion, demotion and
rollback = editing that pointer (versioned in git).
Enforce semantics: every rule sees the current reply; any BLOCK hands off to a human; otherwise
apply one rewrite and re-check ALL rules; stop when clean; hand off if it does not converge.
Shadow guardrails run on the ORIGINAL reply and are only logged, never applied.
Fail closed: a guardrail that crashes, times out, returns garbage, returns a no-op rewrite or failed
to load BLOCKS its rules. Each check gets deep copies so it cannot mutate shared state.
The time limit uses SIGALRM, so it only applies on the main thread (CLI, batch, tests)."""
import importlib.util
import json
import signal
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from contracts.guardrail import check_loaded_globals, check_source, validate_module
from contracts.policyspec import PolicySpec, Skeleton
from contracts.transcript import Msg
from contracts.verdict import Action, Verdict

GUARD_DIR = Path(__file__).parent / "guardrails"
HANDOFF = "Let me check that with the owner and get right back to you."
TIME_LIMIT_S = 0.25


@dataclass
class Registry:
    modules: Dict[Skeleton, object] = field(default_factory=dict)
    modes: Dict[Skeleton, str] = field(default_factory=dict)
    failed: Dict[Skeleton, str] = field(default_factory=dict)
    errors: Dict[str, str] = field(default_factory=dict)


@dataclass
class Outcome:
    reply: Msg
    applied: List[Verdict]
    shadow: List[Verdict]


def load_file(path: Path):
    problems = check_source(path.read_text())
    if problems:
        raise ValueError("; ".join(problems))
    spec = importlib.util.spec_from_file_location(f"ct_guard_{path.stem}_{uuid.uuid4().hex}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    problems = check_loaded_globals(mod)
    if problems:
        raise ValueError("; ".join(problems))
    validate_module(mod)
    return mod


def load(guard_dir: Path = GUARD_DIR) -> Registry:
    reg = Registry()
    pointers = json.loads((guard_dir / "active.json").read_text())
    for skel_name, entry in pointers.items():
        stem = entry["file"] if isinstance(entry, dict) else entry
        mode = entry.get("mode", "enforce") if isinstance(entry, dict) else "enforce"
        try:
            skel = Skeleton(skel_name)
        except ValueError:
            reg.errors[stem] = f"unknown skeleton {skel_name}"
            continue
        try:
            if mode not in ("shadow", "enforce"):
                raise ValueError(f"unknown mode {mode}")
            mod = load_file(guard_dir / f"{stem}.py")
            if mod.RULE_SKELETON != skel:
                raise ValueError(f"{stem} implements {mod.RULE_SKELETON.value}, not {skel_name}")
            reg.modules[skel], reg.modes[skel] = mod, mode
        except Exception as e:
            reg.errors[stem] = f"{type(e).__name__}: {e}"
            reg.failed[skel] = stem
    return reg


@contextmanager
def _time_limit(seconds: float):
    if not hasattr(signal, "setitimer") or threading.current_thread() is not threading.main_thread():
        yield
        return

    def _raise(signum, frame):
        raise TimeoutError("guardrail exceeded time limit")

    previous = signal.signal(signal.SIGALRM, _raise)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _block(rule_id: str, why: str) -> Verdict:
    return Verdict(action=Action.BLOCK, rule_id=rule_id, evidence=why)


def _run(mod, history, response, spec, rule) -> Verdict:
    try:
        with _time_limit(TIME_LIMIT_S):
            v = mod.check([m.model_copy(deep=True) for m in history], response.model_copy(deep=True),
                          spec.model_copy(deep=True), rule.model_copy(deep=True))
    except BaseException as e:  # includes TimeoutError; KeyboardInterrupt is re-raised below
        if isinstance(e, KeyboardInterrupt):
            raise
        return _block(rule.rule_id, f"guardrail error {type(e).__name__}")
    if not isinstance(v, Verdict):
        return _block(rule.rule_id, "guardrail returned non-Verdict")
    if v.action == Action.REWRITE and v.rewrite == response.text:
        return _block(rule.rule_id, "guardrail flagged a violation but its rewrite changes nothing")
    return v


def _pass(reg, history, reply, spec, mode) -> List[Verdict]:
    out = []
    for r in spec.rules:
        if r.skeleton in reg.failed and mode == "enforce":
            out.append(_block(r.rule_id, f"guardrail {reg.failed[r.skeleton]} failed to load"))
        elif r.skeleton in reg.modules and reg.modes[r.skeleton] == mode:
            v = _run(reg.modules[r.skeleton], history, reply, spec, r)
            if v.action != Action.ALLOW:
                out.append(v)
    return out


def apply_all(history: List[Msg], response: Msg, spec: PolicySpec,
              registry: Optional[Registry] = None, max_passes: int = 0) -> Outcome:
    reg = registry if registry is not None else load()
    for skel, mod in reg.modules.items():
        reg.modes.setdefault(skel, "enforce")
    shadow = _pass(reg, history, response, spec, "shadow")
    max_passes = max_passes or len(spec.rules) + 1
    current, applied = response, []
    for _ in range(max_passes):
        flagged = _pass(reg, history, current, spec, "enforce")
        if not flagged:
            return Outcome(current, applied, shadow)
        blocks = [v for v in flagged if v.action == Action.BLOCK]
        if blocks:
            return Outcome(current.model_copy(update={"text": HANDOFF}), applied + blocks, shadow)
        applied.append(flagged[0])
        current = current.model_copy(update={"text": flagged[0].rewrite})
    stuck = _block("runtime.max_passes", "rewrites did not converge")
    return Outcome(current.model_copy(update={"text": HANDOFF}), applied + [stuck], shadow)
