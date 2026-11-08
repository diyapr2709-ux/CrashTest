"""Replay ONE candidate guardrail over cached transcripts and report the outcome of every bot turn.
    python -m gen.forge.replay --skeleton hours_bound \\
        --candidate cache/forge/hours_bound/seed_1/hours_bound_v1.py \\
        --transcripts data/dev/transcripts --spec data/specs/cafe_demo.json \\
        [--tool dev_leaky] --out cache/forge/hours_bound/seed_1/replay.json
The UI's "re-run the swarm" step calls this in a fresh subprocess (the source lint is not a
sandbox). Run it on the main thread: the runtime's SIGALRM time limit only works there.
The candidate goes through runtime.load_file (lint + import + validate), must declare
RULE_SKELETON == --skeleton, and is the ONLY guardrail in the registry, in enforce mode. Every bot
turn is checked with the history before it, exactly as the runtime would at serving time; a turn
that is both rewritten and then blocked is reported as "block". No model is called.
Exit 0 on success. On any failure before the replay (candidate does not load, wrong skeleton,
unreadable spec or transcripts) --out holds {"error": "..."} and the exit code is 2."""
import argparse
import json
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Union

from contracts.policyspec import PolicySpec, Skeleton
from contracts.transcript import Msg, Transcript
from contracts.verdict import Action, Verdict
from gen.forge import runtime

TranscriptSource = Union[str, Path, Iterable[Transcript]]


def load_candidate(path: Path, skeleton: Skeleton):
    """Lint, import and validate the candidate; it must implement `skeleton`."""
    path = Path(path)
    mod = runtime.load_file(path)
    if mod.RULE_SKELETON != skeleton:
        raise ValueError(f"{path.name} implements {mod.RULE_SKELETON.value}, not {skeleton.value}")
    return mod


def load_transcripts(src: TranscriptSource, tool_id: Optional[str] = None) -> List[Transcript]:
    """A directory of *.json transcripts or an iterable of Transcript objects, optionally filtered."""
    if isinstance(src, (str, Path)):
        d = Path(src)
        if not d.is_dir():
            raise FileNotFoundError(f"transcripts directory not found: {d}")
        src = [Transcript.model_validate_json(p.read_text()) for p in sorted(d.glob("*.json"))]
    return [t for t in src if tool_id is None or t.tool_id == tool_id]


def _turn_entry(transcript_id: str, msg: Msg, applied: List[Verdict], final_text: str) -> dict:
    blocks = [v for v in applied if v.action == Action.BLOCK]
    rewrites = [v for v in applied if v.action == Action.REWRITE]
    lead = blocks[0] if blocks else rewrites[0] if rewrites else None
    action = "block" if blocks else "rewrite" if rewrites else "allow"
    return {"transcript_id": transcript_id, "turn": msg.turn, "action": action,
            "rule_id": lead.rule_id if lead else None,
            "evidence": "; ".join(v.evidence for v in applied if v.evidence),
            "rewrite": final_text if action == "rewrite" else None}


def replay(candidate_path: Union[str, Path], skeleton: Union[str, Skeleton], spec: PolicySpec,
           transcripts: TranscriptSource, tool_id: Optional[str] = None) -> dict:
    skeleton = Skeleton(skeleton)
    if not any(r.skeleton == skeleton for r in spec.rules):
        raise ValueError(f"spec {spec.business_id} has no rule with skeleton {skeleton.value}")
    mod = load_candidate(Path(candidate_path), skeleton)
    reg = runtime.Registry(modules={skeleton: mod}, modes={skeleton: "enforce"})
    turns = []
    for t in load_transcripts(transcripts, tool_id):
        for i, msg in enumerate(t.messages):
            if msg.role != "bot":
                continue
            out = runtime.apply_all(t.messages[:i], msg, spec, registry=reg)
            turns.append(_turn_entry(t.transcript_id, msg, out.applied, out.reply.text))
    n_rewritten = sum(x["action"] == "rewrite" for x in turns)
    n_blocked = sum(x["action"] == "block" for x in turns)
    return {"skeleton": skeleton.value, "tool_id": tool_id, "n_bot_turns": len(turns),
            "n_flagged": n_rewritten + n_blocked, "n_rewritten": n_rewritten,
            "n_blocked": n_blocked, "turns": turns}


def summary(result: dict) -> str:
    tool = f" on tool {result['tool_id']}" if result["tool_id"] else ""
    return (f"{result['skeleton']}{tool}: {result['n_bot_turns']} bot turns, "
            f"{result['n_flagged']} flagged ({result['n_rewritten']} rewritten, "
            f"{result['n_blocked']} blocked)")


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Replay one candidate guardrail over cached transcripts")
    ap.add_argument("--skeleton", required=True, choices=[s.value for s in Skeleton])
    ap.add_argument("--candidate", required=True, type=Path, help="candidate guardrail .py")
    ap.add_argument("--transcripts", required=True, type=Path, help="directory of transcript JSON")
    ap.add_argument("--spec", required=True, type=Path, help="PolicySpec JSON")
    ap.add_argument("--tool", default=None, help="only transcripts with this tool_id")
    ap.add_argument("--out", required=True, type=Path, help="where the result JSON goes")
    a = ap.parse_args(argv)
    try:
        spec = PolicySpec.model_validate_json(a.spec.read_text())
        result = replay(a.candidate, a.skeleton, spec, a.transcripts, tool_id=a.tool)
    except Exception as e:  # noqa: BLE001 - every pre-replay failure is reported the same way
        _write(a.out, {"error": f"{type(e).__name__}: {e}"})
        print(f"error: {e}", file=sys.stderr)
        return 2
    _write(a.out, result)
    print(f"{summary(result)} -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
