"""Judge v0 (VAL lane): a model from a different family than the attacker and bot reads one
Transcript plus the PolicySpec and returns Violations with source="judge".

The prompt text is PRIVATE (crashtest-val-private/judge/v0/{system,user}.txt) and is loaded from
JUDGE_PROMPT_DIR, so this code can live in the shared repo while GEN never sees the prompt. All
model calls go through common.llm.complete (cached, replayable, CACHE_ONLY-aware)."""
import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from common import llm
from contracts.policyspec import PolicySpec
from contracts.transcript import Transcript
from contracts.verdict import Violation

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROMPT_DIR = ROOT.parent / "crashtest-val-private" / "judge" / "v0"
PROMPT_VERSION = "v0"


def prompt_dir() -> Path:
    return Path(os.environ.get("JUDGE_PROMPT_DIR") or DEFAULT_PROMPT_DIR)


def load_prompt(directory: Optional[Path] = None) -> Tuple[str, str]:
    """Returns (system, user_template). user_template holds {spec_json} and {transcript_json}."""
    d = directory or prompt_dir()
    try:
        return (d / "system.txt").read_text(), (d / "user.txt").read_text()
    except FileNotFoundError:
        raise RuntimeError(f"judge prompt not found in {d}; set JUDGE_PROMPT_DIR to the private "
                           "repo's judge/v0 directory") from None


def spec_view(spec: PolicySpec) -> Dict:
    """The compact, rule-centred view of the spec the judge reads. No economics, no tools."""
    return {
        "business_id": spec.business_id, "vertical": spec.vertical.value,
        "timezone": spec.timezone,
        "hours": [f"{h.day} {h.open}-{h.close}" for h in spec.hours] + ["other days: closed"],
        "service_area_zips": spec.service_area_zips,
        "catalog": [{"name": c.name, "price_usd": c.price_usd, "unit": c.unit,
                     "allergens": c.allergens, "available": c.available} for c in spec.catalog],
        "warranties": [w.model_dump() for w in spec.warranties],
        "rules": [{"rule_id": r.rule_id, "skeleton": r.skeleton.value, "description": r.description,
                   "severity": r.severity, "params": r.params} for r in spec.rules],
    }


def transcript_view(t: Transcript) -> Dict:
    return {
        "transcript_id": t.transcript_id,
        "conversation_time": t.sim_time.strftime("%A %Y-%m-%d %H:%M (local)"),
        "messages": [{"turn": m.turn, "role": m.role, "text": m.text} for m in t.messages],
    }


def render(spec: PolicySpec, t: Transcript, user_template: str) -> str:
    # str.replace, not str.format: spec text may contain braces.
    return (user_template
            .replace("{spec_json}", json.dumps(spec_view(spec), indent=1, ensure_ascii=False))
            .replace("{transcript_json}", json.dumps(transcript_view(t), indent=1, ensure_ascii=False)))


def parse_array(text: str) -> List:
    """First JSON array in the model output, tolerant of prose and code fences."""
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "[":
            try:
                value, _ = dec.raw_decode(text, i)
            except json.JSONDecodeError:
                continue
            if isinstance(value, list):
                return value
    return []


def to_violations(raw: List, spec: PolicySpec, t: Transcript) -> List[Violation]:
    """Keep only well-formed items: a known rule, a bot turn, one entry per (rule, turn).
    Severity always comes from the spec, never from the model."""
    rules = {r.rule_id: r for r in spec.rules}
    bot_turns = {m.turn: m for m in t.messages if m.role == "bot"}
    out: List[Violation] = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        rid, turn = item.get("rule_id"), item.get("turn")
        if rid not in rules or isinstance(turn, bool) or not isinstance(turn, int) \
                or turn not in bot_turns or (rid, turn) in seen:
            continue
        seen.add((rid, turn))
        msg = bot_turns[turn].text
        span = str(item.get("span") or "").strip()
        if not span or span not in msg:
            span = msg[:120]        # span is diagnostic; the (rule, turn) pair is what is scored
        out.append(Violation(rule_id=rid, transcript_id=t.transcript_id, turn=turn, span=span,
                             severity=rules[rid].severity, source="judge"))
    return sorted(out, key=lambda v: (v.turn, v.rule_id))


def judge(t: Transcript, spec: PolicySpec, *, model: Optional[str] = None, seed: int = 0,
          prompt: Optional[Tuple[str, str]] = None) -> List[Violation]:
    model = model or llm.model_for("judge")
    system, user_template = prompt or load_prompt()
    text = llm.complete(render(spec, t, user_template), model=model, seed=seed, temperature=0.0,
                        system=system)
    return to_violations(parse_array(text), spec, t)
