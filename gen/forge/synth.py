"""Forge: the developer agent. Writes a guardrail for ONE skeleton from a failure corpus.
    synthesize(skeleton, spec, violating=[(transcript, turn), ...], benign=[transcript, ...])
Loop, at most `max_iters` times: prompt FORGE_MODEL (through common.llm, fixed seed) with the
contract docstring, price_match.py as the reference shape, the rule's typed params and sample
replies; extract the code block; run the static lint; write the candidate and generated tests to a
fresh temp dir; run pytest in a fresh subprocess (the lint is not a sandbox); feed problems back.
Every prompt, response and pytest output is logged under <out_dir>/<skeleton>/seed_<seed>/.
Nothing is ever written into gen/forge/guardrails/: a human promotes by copying the file and
editing active.json."""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import contracts.guardrail as guardrail_contract
from common import llm
from contracts.guardrail import ALLOWED_IMPORTS, check_source
from contracts.policyspec import PolicySpec, Skeleton
from contracts.transcript import Transcript
from gen.swarm.library import attacks_for

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "gen" / "forge" / "guardrails" / "price_match.py"
OUT_DIR = ROOT / "cache" / "forge"
TEST_TIMEOUT_S = 60
PROMPT_VERSION = 4          # bump when base_prompt changes; recorded in every result.json
PROMPT_VIOLATING = 3
PROMPT_BENIGN = 3           # hard negatives (correct refusals from the same attack) come first
_FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S)
_CONTROL = re.compile(r"<\|?channel\|?>|<\|?(?:start|end)_of_turn\|?>|<\|im_(?:start|end)\|>")

SYSTEM = ("You are a careful Python developer writing a guardrail module for a small business's "
          "customer-facing chatbot. Reply with exactly one ```python code block containing the "
          "complete file and nothing else outside it.")


@dataclass
class Case:
    id: str
    history: List[dict]
    response: dict


@dataclass
class SynthResult:
    skeleton: str
    seed: int
    model: str
    iterations: int
    passed: bool
    code: str
    tests: str
    problems: List[str] = field(default_factory=list)
    code_path: str = ""
    run_dir: str = ""
    prompt_version: int = PROMPT_VERSION


# ---------------------------------------------------------------- corpus
def make_case(t: Transcript, turn: int) -> Case:
    idx = next(i for i, m in enumerate(t.messages) if m.turn == turn)
    if t.messages[idx].role != "bot":
        raise ValueError(f"{t.transcript_id} turn {turn} is not a bot turn")
    return Case(id=f"{t.transcript_id}_t{turn}",
                history=[m.model_dump(mode="json") for m in t.messages[:idx]],
                response=t.messages[idx].model_dump(mode="json"))


def benign_cases(transcripts: Sequence[Transcript], exclude: Sequence[Tuple[str, int]] = ()) -> List[Case]:
    skip = set(exclude)
    return [make_case(t, m.turn) for t in transcripts for m in t.messages
            if m.role == "bot" and (t.transcript_id, m.turn) not in skip]


# ---------------------------------------------------------------- prompt
def _rule_text(spec: PolicySpec, skeleton: Skeleton) -> str:
    rules = [r for r in spec.rules if r.skeleton == skeleton]
    if not rules:
        raise ValueError(f"spec has no rule with skeleton {skeleton.value}")
    lines = []
    for r in rules:
        params = r.typed_params().model_dump()
        lines.append(f"- rule_id={r.rule_id!r} severity={r.severity} params={params} "
                     f"description={r.description!r}")
    return "\n".join(lines)


def _inputs_text(spec: PolicySpec, skeleton: Skeleton, violating: List[Case]) -> str:
    """What check() receives, shown as the Python objects they are (prompt v2: v1 showed prose
    facts and the model regex-parsed them instead of reading spec.hours / spec.catalog)."""
    hours = ", ".join(f"Hours(day={h.day!r}, open={h.open!r}, close={h.close!r})" for h in spec.hours)
    items = ", ".join(f"CatalogItem(name={c.name!r}, price_usd={c.price_usd}, unit={c.unit!r}, "
                      f"allergens={c.allergens})" for c in spec.catalog)
    rule = next(r for r in spec.rules if r.skeleton == skeleton)
    at = violating[0].response.get("at") or "2026-11-08T21:33:00"
    return f"""- history: list[Msg]; each Msg has .role ("customer" | "bot"), .text (str), .turn (int), .at (datetime)
- response: the bot Msg to check; response.text is the reply, response.at its local wall-clock
  datetime, e.g. datetime.fromisoformat({at!r}); use response.at.weekday() (0=mon) or
  response.at.strftime("%a").lower()[:3] and response.at.time()
- spec.timezone: {spec.timezone!r}
- spec.hours: [{hours}]  (unlisted days are closed; "HH:MM" strings)
- spec.catalog: [{items}]
- rule.rule_id: {rule.rule_id!r}; rule.params: {rule.typed_params().model_dump()}"""


COMMON_MISTAKES = """- Parsing hours or prices out of a prose string: read the spec.hours / spec.catalog objects above.
- A price regex without a literal "$": "50 units" must never be read as a price. Use
  r"\\$\\s?(\\d[\\d,]*)(\\.\\d{1,2})?" and convert the match yourself.
- Passing a list to re.search: history is a list of Msg objects; search m.text.
- `except:` or `except BaseException:` (the lint rejects them): catch ValueError etc., or avoid try/except.
- Flagging a benign reply that merely mentions the topic ("we cannot guarantee", "we are closed on
  Sunday", "refunds up to $10" are fine); the benign tests count every false intervention.
- A REWRITE whose text equals response.text (counted as a failure): change the offending words.
- Returning ALLOW early because one sentence is fine: a reply can state a true fact AND a violation
  ("we are closed on Sunday" ... "we are open until 8:00 PM tonight", said on a Sunday). Check every
  claim; never short-circuit on a reassuring phrase.
- Relative time words resolve against response.at, not against "now": "today"/"tonight"/"right now"
  = response.at's date, "tomorrow" = response.at + 1 day, a weekday name = that day. Judge each claim
  against the hours of THAT day; a true statement about another day ("tomorrow, Tuesday, we open at
  07:00") is fine.
- Trace your regexes by hand against every VIOLATING reply below before answering."""


def _case_text(c: Case) -> str:
    prev = next((m["text"] for m in reversed(c.history) if m["role"] == "customer"), "")
    return f"  customer: {prev}\n  bot: {c.response['text']}"


def base_prompt(skeleton: Skeleton, spec: PolicySpec, violating: List[Case], benign: List[Case]) -> str:
    bad = "\n".join(_case_text(c) for c in violating[:PROMPT_VIOLATING])
    good = "\n".join(_case_text(c) for c in benign[:PROMPT_BENIGN]) or "  (none)"
    return f"""# Task
Write gen/forge/guardrails/{skeleton.value}.py: a guardrail for the skeleton `{skeleton.value}`.
It must export RULE_SKELETON = Skeleton.{skeleton.name}, VERSION = 1 and
check(history, response, spec, rule) -> Verdict.

# Contract (from contracts/guardrail.py)
{guardrail_contract.__doc__}

Allowed imports: {", ".join(sorted(ALLOWED_IMPORTS))}. No while loops, no bare except, no names or
strings containing "__", no I/O, no datetime.now(): read the time from response.at (a datetime).
check() must return in under 50 ms. Prefer REWRITE with a corrected, polite reply over BLOCK.
A REWRITE must change the text; `rewrite == response.text` is treated as a failure.
Return Verdict(action=Action.ALLOW) when the reply is fine.

# Reference implementation: gen/forge/guardrails/price_match.py (copy its shape and style)
```python
{REFERENCE.read_text()}```

# The rule(s) this guardrail enforces
{_rule_text(spec, skeleton)}

# What check() receives (real values from this spec; use them as Python objects)
{_inputs_text(spec, skeleton, violating)}

# Common mistakes that fail the tests
{COMMON_MISTAKES}

# Bot replies that VIOLATE the rule (check must return REWRITE or BLOCK)
{bad}

# Bot replies that are FINE (check must return ALLOW). The first ones come from the SAME attack:
# the bot refused or stated the policy correctly. Never flag a refusal or a correct statement.
{good}

Write the complete file now in one ```python block."""


def feedback_prompt(base: str, code: str, problems: str) -> str:
    return (f"{base}\n\n# Your previous attempt\n```python\n{code}\n```\n\n# Problems found\n"
            f"{problems}\n\nFix every problem and return the complete corrected file in one "
            f"```python block.")


# ---------------------------------------------------------------- extraction + tests
def extract_code(response: str) -> Optional[str]:
    blocks = _FENCE.findall(_CONTROL.sub("", response))
    return max(blocks, key=len).strip("\n") + "\n" if blocks else None


TEST_TEMPLATE = '''"""Generated by Forge for {skeleton}. Corpus (spec + cases) in corpus.json next to this file."""
import json
import time
from pathlib import Path

import pytest

from contracts.policyspec import PolicySpec
from contracts.transcript import Msg
from contracts.verdict import Action, Verdict
from gen.forge.runtime import load_file

HERE = Path(__file__).resolve().parent
CORPUS = json.loads((HERE / "corpus.json").read_text())
SPEC = PolicySpec.model_validate(CORPUS["spec"])
MOD = load_file(HERE / "{module_file}")
RULES = [r for r in SPEC.rules if r.skeleton.value == "{skeleton}"]


def _run(case):
    verdicts = []
    for rule in RULES:
        history = [Msg.model_validate(m) for m in case["history"]]
        t0 = time.perf_counter()
        v = MOD.check(history, Msg.model_validate(case["response"]), SPEC, rule)
        assert time.perf_counter() - t0 < 0.2, "check() exceeded the time limit"
        assert isinstance(v, Verdict), "check() must return a Verdict"
        if v.action == Action.REWRITE:
            assert v.rewrite != case["response"]["text"], "no-op rewrite: the text did not change"
        verdicts.append(v)
    return verdicts


@pytest.mark.parametrize("case", CORPUS["violating"], ids=[c["id"] for c in CORPUS["violating"]])
def test_violation_is_caught(case):
    assert any(v.action != Action.ALLOW for v in _run(case)), "allowed: " + case["response"]["text"]


@pytest.mark.parametrize("case", CORPUS["benign"], ids=[c["id"] for c in CORPUS["benign"]])
def test_benign_is_allowed(case):
    assert all(v.action == Action.ALLOW for v in _run(case)), "flagged: " + case["response"]["text"]


def test_module_shape():
    assert MOD.RULE_SKELETON.value == "{skeleton}" and MOD.VERSION >= 1
'''


def generate_tests(skeleton: Skeleton, module_file: str) -> str:
    return TEST_TEMPLATE.format(skeleton=skeleton.value, module_file=module_file)


def run_tests(code: str, tests: str, corpus: dict, module_file: str) -> Tuple[bool, str]:
    """Runs the generated tests against the candidate in a fresh dir and a fresh subprocess."""
    work = Path(tempfile.mkdtemp(prefix="forge_"))
    (work / module_file).write_text(code)
    (work / f"test_{module_file}").write_text(tests)
    (work / "corpus.json").write_text(json.dumps(corpus))
    env = {**os.environ, "PYTHONPATH": str(ROOT), "CACHE_ONLY": "1"}
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(work),
           str(work / f"test_{module_file}")]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, env=env,
                           timeout=TEST_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return False, f"tests timed out after {TEST_TIMEOUT_S} s (infinite loop or hang?)"
    return r.returncode == 0, (r.stdout + r.stderr)[-6000:]


# ---------------------------------------------------------------- the loop
def hard_negative_ids(spec: PolicySpec, skeleton: Skeleton, transcripts: Sequence[Transcript]) -> set:
    """Transcripts that attacked THIS skeleton: their unmarked bot turns are correct refusals, the
    hardest benign cases (a guardrail that flags them has a high false-intervention rate)."""
    attack_ids = {a.attack_id for a in attacks_for(spec.vertical) if a.target == skeleton}
    return {t.transcript_id for t in transcripts if t.attack_class in attack_ids}


def synthesize(skeleton: Skeleton, spec: PolicySpec, violating: Sequence[Tuple[Transcript, int]],
               benign: Sequence[Transcript], *, seed: int = 1, model: Optional[str] = None,
               max_iters: int = 3, out_dir: Path = OUT_DIR, temperature: float = 0.2,
               exclude: Sequence[Tuple[str, int]] = ()) -> SynthResult:
    """`benign` may include attack transcripts: every bot turn not in `exclude` (all marked
    violations, any rule) and not in `violating` is asserted ALLOW. Hard negatives sort first."""
    if not violating:
        raise ValueError("need at least one violating (transcript, turn) example")
    model = model or llm.model_for("forge")
    bad = [make_case(t, turn) for t, turn in violating]
    skip = set(exclude) | {(t.transcript_id, turn) for t, turn in violating}
    good = benign_cases(benign, exclude=sorted(skip))
    hard = hard_negative_ids(spec, skeleton, benign)
    good.sort(key=lambda c: c.id.rsplit("_t", 1)[0] not in hard)
    corpus = {"spec": spec.model_dump(mode="json"), "violating": [asdict(c) for c in bad],
              "benign": [asdict(c) for c in good]}
    module_file = f"{skeleton.value}_v{seed}.py"
    tests = generate_tests(skeleton, module_file)
    run_dir = Path(out_dir) / skeleton.value / f"seed_{seed}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "corpus.json").write_text(json.dumps(corpus, indent=1))

    base = base_prompt(skeleton, spec, bad, good)
    prompt, code, problems, passed = base, "", [], False
    for i in range(1, max_iters + 1):
        (run_dir / f"iter_{i}_prompt.txt").write_text(prompt)
        response = llm.complete(prompt, model=model, seed=seed * 10 + i, temperature=temperature,
                                system=SYSTEM)
        (run_dir / f"iter_{i}_response.txt").write_text(response)
        candidate = extract_code(response)
        if candidate is None:
            problem = "no python code block in the reply"
        else:
            code = candidate
            lint = check_source(code)
            if lint:
                problem = "static lint: " + "; ".join(lint)
            else:
                passed, output = run_tests(code, tests, corpus, module_file)
                (run_dir / f"iter_{i}_pytest.txt").write_text(output)
                problem = "" if passed else "pytest output:\n" + output
        iterations = i
        if passed:
            problems.append("")
            break
        problems.append(problem)
        prompt = feedback_prompt(base, code or "(no code block was returned)", problem)
    (run_dir / module_file).write_text(code)
    (run_dir / f"test_{module_file}").write_text(tests)
    result = SynthResult(skeleton=skeleton.value, seed=seed, model=model, iterations=iterations,
                         passed=passed, code=code, tests=tests, problems=problems,
                         code_path=str(run_dir / module_file), run_dir=str(run_dir))
    (run_dir / "result.json").write_text(json.dumps(asdict(result), indent=1))
    return result


# ---------------------------------------------------------------- CLI
def load_marks(path: Path) -> List[dict]:
    """data/dev/marks.json: [{"transcript_id", "turn", "rule_id"}] — violating bot turns."""
    return json.loads(path.read_text())


def corpus_from_dev(spec: PolicySpec, skeleton: Skeleton, transcripts_dir: Path, marks: List[dict],
                    max_benign: int = 60):
    """Returns (violating turns for this skeleton, transcripts whose unmarked bot turns are the
    clean set, every marked (transcript_id, turn)). Attack transcripts are included: their unmarked
    turns are correct refusals, which is how VAL counts false interventions too."""
    by_id = {}
    for p in sorted(transcripts_dir.glob("*.json")):
        t = Transcript.model_validate_json(p.read_text())
        by_id[t.transcript_id] = t
    violating = [(by_id[m["transcript_id"]], m["turn"]) for m in marks
                 if m["transcript_id"] in by_id and spec.rule(m["rule_id"]).skeleton == skeleton]
    marked = sorted({(m["transcript_id"], m["turn"]) for m in marks if m["transcript_id"] in by_id})
    hard = hard_negative_ids(spec, skeleton, list(by_id.values()))
    benign = sorted(by_id.values(), key=lambda t: (t.transcript_id not in hard, t.transcript_id))
    return violating, benign[:max_benign], marked


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Forge: write one guardrail from the dev failure corpus")
    ap.add_argument("--skeleton", required=True, choices=[s.value for s in Skeleton])
    ap.add_argument("--spec", type=Path, default=ROOT / "data/specs/cafe_demo.json")
    ap.add_argument("--transcripts", type=Path, default=ROOT / "data/dev/transcripts")
    ap.add_argument("--marks", type=Path, default=ROOT / "data/dev/marks.json")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--max-iters", type=int, default=3)
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    a = ap.parse_args(argv)
    spec = PolicySpec.model_validate_json(a.spec.read_text())
    skeleton = Skeleton(a.skeleton)
    violating, benign, marked = corpus_from_dev(spec, skeleton, a.transcripts, load_marks(a.marks))
    print(f"{skeleton.value}: {len(violating)} violating turns, {len(benign)} transcripts for the clean set")
    res = synthesize(skeleton, spec, violating, benign, seed=a.seed, max_iters=a.max_iters, out_dir=a.out,
                     exclude=marked)
    print(f"{'GREEN' if res.passed else 'RED'} after {res.iterations} iteration(s): {res.code_path}")
    if not res.passed:
        print(res.problems[-1][-2000:])
    return 0 if res.passed else 1


if __name__ == "__main__":
    sys.exit(main())
