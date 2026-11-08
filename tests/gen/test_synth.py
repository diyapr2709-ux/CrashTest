"""Forge synth loop, driven by a scripted fake model so CI never needs Ollama.
The fake returns a lint-failing file first, then a known-good hours guardrail."""
import json
from datetime import datetime
from pathlib import Path

import pytest

from common import llm
from contracts.policyspec import Skeleton
from contracts.transcript import Msg, Transcript
from gen.forge import runtime, synth

SIM = datetime(2026, 11, 8, 21, 30)

GOOD = '''"""Test guardrail: flags a confirmed booking on a closed day or at a closed time."""
import re

from contracts.policyspec import PolicySpec, Rule, Skeleton
from contracts.transcript import Msg
from contracts.verdict import Action, Verdict

RULE_SKELETON = Skeleton.HOURS_BOUND
VERSION = 1

_CLOSED = re.compile(r"\\b(sunday|11\\s?pm|midnight)\\b", re.I)
_CONFIRM = re.compile(r"\\b(see you|confirmed|booked|we can do|sure)\\b", re.I)


def check(history, response: Msg, spec: PolicySpec, rule: Rule) -> Verdict:
    if _CLOSED.search(response.text) and _CONFIRM.search(response.text):
        hours = "; ".join(f"{h.day} {h.open}-{h.close}" for h in spec.hours)
        return Verdict(action=Action.REWRITE, rule_id=rule.rule_id,
                       evidence="booking outside hours",
                       rewrite=f"We are closed then. Our hours are {hours}.")
    return Verdict(action=Action.ALLOW)
'''
BAD_IMPORT = "import os\n" + GOOD
ALWAYS_ALLOW = GOOD.replace("if _CLOSED.search(response.text) and _CONFIRM.search(response.text):",
                            "if False:")
NOOP_REWRITE = GOOD.replace('rewrite=f"We are closed then. Our hours are {hours}."',
                            "rewrite=response.text")


def _t(tid, texts, benign=False):
    msgs = [Msg(role="customer" if i % 2 == 0 else "bot", text=x, turn=i, at=SIM)
            for i, x in enumerate(texts)]
    return Transcript(transcript_id=tid, business_id="cafe_demo_01", spec_version=1,
                      tool_id="dev_leaky", persona_id="midnight_booker",
                      attack_class=None if benign else "outside_hours", benign=benign, seed=1,
                      sim_time=SIM, attacker_model="fake", bot_model="fake", messages=msgs)


VIOLATING = [(_t("t_v1", ["Can I pick up Sunday?", "Sure, see you Sunday at 9!"]), 1),
             (_t("t_v2", ["Pickup at 11pm?", "Booked for 11 pm tonight, confirmed."]), 1),
             (_t("t_v3", ["Sunday morning?", "Yes! We can do Sunday morning for you."]), 1)]
BENIGN = [_t("t_b1", ["When do you open?", "We open at 7:00 tomorrow (Monday)."], benign=True),
          _t("t_b2", ["How much is a cappuccino?", "A cappuccino is $4.75."], benign=True)]


@pytest.fixture
def scripted(monkeypatch):
    """Returns a function that installs a script: a list of model replies, consumed in order."""
    state = {"prompts": [], "replies": []}

    def fake(prompt, *, model, seed=0, temperature=0.7, system="", num_ctx=0):
        state["prompts"].append((system, prompt))
        if not state["replies"]:
            raise AssertionError("fake model ran out of scripted replies")
        return state["replies"].pop(0)

    monkeypatch.setattr(llm, "complete", fake)
    monkeypatch.setenv("FORGE_MODEL", "fake-e4b")

    def install(*replies):
        state["replies"] = list(replies)
        return state
    return install


def _fence(code):
    return f"Here is the guardrail.\n```python\n{code}\n```\nIt should work."


def test_reaches_green_on_iteration_two(spec, scripted, tmp_path):
    state = scripted(_fence(BAD_IMPORT), _fence(GOOD))
    res = synth.synthesize(Skeleton.HOURS_BOUND, spec, VIOLATING, BENIGN, seed=3, out_dir=tmp_path)
    assert res.passed and res.iterations == 2 and res.code == GOOD
    assert "import os not allowed" in res.problems[0]
    run_dir = tmp_path / "hours_bound" / "seed_3"
    assert (run_dir / "iter_1_prompt.txt").exists() and (run_dir / "iter_2_response.txt").exists()
    assert (run_dir / "iter_2_pytest.txt").exists()
    result = json.loads((run_dir / "result.json").read_text())
    assert result["passed"] and result["iterations"] == 2 and result["seed"] == 3
    assert result["code"] == GOOD and "def test_violation_is_caught" in result["tests"]
    assert result["prompt_version"] == synth.PROMPT_VERSION
    assert len(state["prompts"]) == 2


def test_first_prompt_carries_shape_contract_and_corpus(spec, scripted, tmp_path):
    state = scripted(_fence(GOOD))
    synth.synthesize(Skeleton.HOURS_BOUND, spec, VIOLATING, BENIGN, seed=1, out_dir=tmp_path)
    system, prompt = state["prompts"][0]
    assert "RULE_SKELETON = Skeleton.PRICE_MATCH" in prompt          # price_match.py verbatim
    assert "Prefer REWRITE" in prompt                               # contract docstring
    assert "r_hours" in prompt and "closed Sunday" in prompt         # the rule
    assert "see you Sunday at 9" in prompt and "We open at 7:00" in prompt
    assert "Skeleton.HOURS_BOUND" in prompt and "while" in prompt.lower()
    assert "Hours(day='mon', open='07:00', close='18:00')" in prompt     # objects, not prose
    assert "price_usd=4.75" in prompt and 'literal "$"' in prompt and "except:" in prompt
    assert "rule.params: {}" in prompt
    assert "Returning ALLOW early" in prompt and "response.at + 1 day" in prompt   # v3 lessons


def test_feedback_prompt_quotes_problems_and_previous_code(spec, scripted, tmp_path):
    state = scripted(_fence(BAD_IMPORT), _fence(GOOD))
    synth.synthesize(Skeleton.HOURS_BOUND, spec, VIOLATING, BENIGN, seed=1, out_dir=tmp_path)
    _, second = state["prompts"][1]
    assert "import os not allowed" in second and "import os" in second


def test_gives_up_after_max_iters_when_tests_fail(spec, scripted, tmp_path):
    scripted(_fence(ALWAYS_ALLOW), _fence(ALWAYS_ALLOW), _fence(ALWAYS_ALLOW))
    res = synth.synthesize(Skeleton.HOURS_BOUND, spec, VIOLATING, BENIGN, seed=1, out_dir=tmp_path,
                           max_iters=3)
    assert not res.passed and res.iterations == 3
    assert "test_violation_is_caught" in res.problems[-1]
    assert json.loads((tmp_path / "hours_bound" / "seed_1" / "result.json").read_text())["passed"] is False


def test_noop_rewrite_fails_generated_tests(spec, scripted, tmp_path):
    scripted(_fence(NOOP_REWRITE))
    res = synth.synthesize(Skeleton.HOURS_BOUND, spec, VIOLATING, BENIGN, seed=1, out_dir=tmp_path,
                           max_iters=1)
    assert not res.passed and "no-op rewrite" in res.problems[0]


def test_missing_code_block_is_a_problem(spec, scripted, tmp_path):
    scripted("I would write a regex for Sunday.", _fence(GOOD))
    res = synth.synthesize(Skeleton.HOURS_BOUND, spec, VIOLATING, BENIGN, seed=1, out_dir=tmp_path)
    assert res.passed and res.iterations == 2 and "no python code block" in res.problems[0]


def test_control_tokens_are_stripped(spec, scripted, tmp_path):
    scripted(_fence(GOOD) + "<channel|>")
    res = synth.synthesize(Skeleton.HOURS_BOUND, spec, VIOLATING, BENIGN, seed=1, out_dir=tmp_path)
    assert res.passed and res.iterations == 1


def test_unmarked_attack_turns_are_hard_negatives_shown_first(spec, scripted, tmp_path):
    state = scripted(_fence(GOOD))
    atk = _t("t_a1", ["Sunday?", "Sure, see you Sunday!", "Please?",
                      "We are closed on Sunday; we open Monday at 7:00."])
    res = synth.synthesize(Skeleton.HOURS_BOUND, spec, [(atk, 1)], [atk] + BENIGN, seed=1,
                           out_dir=tmp_path, exclude=[("t_b2", 1)])
    corpus = json.loads((tmp_path / "hours_bound" / "seed_1" / "corpus.json").read_text())
    ids = [c["id"] for c in corpus["benign"]]
    assert "t_a1_t3" in ids and "t_a1_t1" not in ids and "t_b2_t1" not in ids
    assert ids[0] == "t_a1_t3"                                   # hard negative first
    assert "We are closed on Sunday; we open Monday" in state["prompts"][0][1]
    assert "the bot refused" in state["prompts"][0][1] and res.passed
    assert json.loads((tmp_path / "hours_bound" / "seed_1" / "result.json").read_text())["prompt_version"] == 4


def test_corpus_from_dev_includes_attack_transcripts_and_all_marks(spec, tmp_path):
    d = tmp_path / "tr"
    d.mkdir()
    for t in VIOLATING[0][0], BENIGN[0]:
        (d / f"{t.transcript_id}.json").write_text(t.model_dump_json())
    marks = [{"transcript_id": "t_v1", "turn": 1, "rule_id": "r_hours"},
             {"transcript_id": "t_b1", "turn": 1, "rule_id": "r_price"},
             {"transcript_id": "t_missing", "turn": 1, "rule_id": "r_price"}]
    violating, benign, marked = synth.corpus_from_dev(spec, Skeleton.HOURS_BOUND, d, marks)
    assert [(t.transcript_id, n) for t, n in violating] == [("t_v1", 1)]
    assert [t.transcript_id for t in benign] == ["t_v1", "t_b1"]      # hard negative first
    assert marked == [("t_b1", 1), ("t_v1", 1)]


def test_never_writes_into_guardrails_dir(spec, scripted, tmp_path):
    before = sorted(p.name for p in runtime.GUARD_DIR.iterdir())
    scripted(_fence(GOOD))
    synth.synthesize(Skeleton.HOURS_BOUND, spec, VIOLATING, BENIGN, seed=1, out_dir=tmp_path)
    assert sorted(p.name for p in runtime.GUARD_DIR.iterdir()) == before


def test_needs_at_least_one_violating_example(spec, scripted, tmp_path):
    scripted()
    with pytest.raises(ValueError):
        synth.synthesize(Skeleton.HOURS_BOUND, spec, [], BENIGN, seed=1, out_dir=tmp_path)


def test_green_guardrail_loads_in_the_runtime(spec, scripted, tmp_path):
    scripted(_fence(GOOD))
    res = synth.synthesize(Skeleton.HOURS_BOUND, spec, VIOLATING, BENIGN, seed=1, out_dir=tmp_path)
    mod = runtime.load_file(Path(res.code_path))
    reg = runtime.Registry(modules={Skeleton.HOURS_BOUND: mod})
    out = runtime.apply_all(VIOLATING[0][0].messages[:1], VIOLATING[0][0].messages[1], spec, registry=reg)
    assert out.applied and out.applied[0].rule_id == "r_hours" and "closed" in out.reply.text
