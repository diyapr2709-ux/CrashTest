from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from contracts.guardrail import check_source
from contracts.metrics import BenchItem, MetricsRun, gate_failures
from contracts.policyspec import PolicySpec
from contracts.transcript import Msg, Transcript
from contracts.verdict import Action, Verdict
from gen.forge.runtime import load


@pytest.mark.parametrize("path", sorted(Path("data/specs").glob("*.json")))
def test_every_spec_validates(path):
    PolicySpec.model_validate_json(path.read_text())


def test_spec_roundtrip(spec):
    assert PolicySpec.model_validate_json(spec.model_dump_json()) == spec


@pytest.mark.parametrize("mutate", [
    lambda d: d["rules"].append(dict(d["rules"][0])),                      # duplicate rule id
    lambda d: d.update(surprise=1),                                        # unknown field
    lambda d: d.update(timezone="Mars/Olympus"),                           # bad timezone
    lambda d: d["rules"][2]["params"].pop("max_usd"),                      # untyped params
    lambda d: d["rules"].append({"rule_id": "r_zip", "skeleton": "service_area_bound",
                                 "description": "x", "severity": "money"}),  # no zips
    lambda d: d.update(failure_corpus=[{"rule_id": "nope", "transcript_id": "t", "turn": 1,
                                        "span": "x", "severity": "money", "source": "human"}]),
])
def test_spec_rejects(spec_dict, mutate):
    mutate(spec_dict)
    with pytest.raises(ValidationError):
        PolicySpec.model_validate(spec_dict)


def test_verdict_invariants():
    Verdict(action=Action.ALLOW)
    Verdict(action=Action.REWRITE, rule_id="r", rewrite="fixed")
    for bad in (dict(action="block"), dict(action="rewrite", rule_id="r"),
                dict(action="allow", rewrite="x")):
        with pytest.raises(ValidationError):
            Verdict(**bad)


def _transcript(**kw):
    base = dict(transcript_id="t", business_id="b", spec_version=1, tool_id="x", persona_id="p",
                benign=True, seed=1, sim_time=datetime(2026, 11, 8, 21, 30),
                attacker_model="a", bot_model="b",
                messages=[Msg(role="customer", text="hi", turn=0), Msg(role="bot", text="yo", turn=1)])
    base.update(kw)
    return Transcript(**base)


def test_transcript_invariants():
    _transcript()
    _transcript(benign=False, attack_class="haggler")
    for bad in (dict(attack_class="haggler"), dict(benign=False),
                dict(messages=[Msg(role="bot", text="x", turn=1), Msg(role="customer", text="y", turn=0)])):
        with pytest.raises(ValidationError):
            _transcript(**bad)


def _metrics(**kw):
    base = dict(run_id="r", git_sha="s", split="dev", backend="gemma", n_specs=1, n_transcripts=10,
                catch_rate=0.9, false_block_rate=0.02, false_intervention_rate=0.05, repro_rate=0.9,
                pass_at_1=0.6, iterations_to_green_mean=1.4, conformal_target=0.9,
                conformal_coverage=0.91)
    base.update(kw)
    return MetricsRun(**base)


def test_gates_one_sided_and_unmeasured():
    assert gate_failures(_metrics()) == []
    assert gate_failures(_metrics(conformal_coverage=0.99)) == []          # over-coverage is fine
    assert len(gate_failures(_metrics(false_block_rate=0.2, conformal_coverage=0.7))) == 2
    assert gate_failures(_metrics(judge_precision=None)) == []


def test_bench_item_roundtrip():
    item = BenchItem(item_id="g1", spec_id="cafe_demo_01", spec_version=1, vertical="cafe",
                     transcript_id="t", gold=[{"turn": 1, "rule_id": "r_price", "action": "rewrite"}])
    assert BenchItem.model_validate_json(item.model_dump_json()) == item


@pytest.mark.parametrize("src", [
    "import os", "from subprocess import run", "x = open('f')", "from . import y",
    "y = ().__class__", "eval('1')", "def f(:",
    "from dataclasses import inspect", "o = open", "__builtins__['open']('f')",
    "import typing\ntyping.sys.modules", "import re\nre.os", "while True:\n    pass",
    "try:\n    x = 1\nexcept:\n    pass", "try:\n    x = 1\nexcept BaseException:\n    pass",
    "from typing import *", "import re\nre._compiler", "'{0.__class__}'.format(1)"])
def test_source_check_rejects(src):
    assert check_source(src)


def test_source_check_accepts_reference_guardrail():
    assert check_source(Path("gen/forge/guardrails/price_match.py").read_text()) == []


def test_legal_rules_need_fine_and_probability(spec_dict):
    spec_dict["rules"].append({"rule_id": "r_ftc", "skeleton": "forbidden_commitment",
                               "description": "No incentives for reviews", "severity": "legal",
                               "params": {"types": ["review_incentive"]}})
    with pytest.raises(ValidationError):
        PolicySpec.model_validate(spec_dict)
    spec_dict["rules"][-1].update(fine_usd=51744, enforcement_prob=0.001)
    PolicySpec.model_validate(spec_dict)


def test_holdout_runs_must_measure_every_gate():
    assert gate_failures(_metrics(split="holdout", judge_precision=0.9, judge_recall=0.9)) == []
    assert any("not measured" in f for f in gate_failures(_metrics(split="holdout")))


def test_active_guardrails_load_and_honour_contract(spec):
    reg = load()
    assert reg.errors == {} and reg.failed == {} and reg.modules
    hist = [Msg(role="customer", text="hi", turn=0)]
    benign = Msg(role="bot", text="Hi! We open at 7am.", turn=1)
    for skel, mod in reg.modules.items():
        for rule in (r for r in spec.rules if r.skeleton == skel):
            assert isinstance(mod.check(hist, benign, spec, rule), Verdict)
