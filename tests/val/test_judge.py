"""Judge v0 and scorer tests. A scripted fake model stands in for the LLM; a tiny prompt stands in
for the private one, so CI never needs the private repo or Ollama."""
import json
from datetime import datetime

import pytest

from common import llm
from contracts.transcript import Msg, Transcript
from contracts.verdict import Violation
from val.judge import judge as J
from val.judge.scorer import load_split, score

SIM = datetime(2026, 11, 8, 21, 30)


def _t(**kw):
    base = dict(transcript_id="fx_test", business_id="cafe_demo_01", spec_version=1,
                tool_id="dev_leaky", persona_id="haggler", attack_class="outside_hours",
                benign=False, seed=0, sim_time=SIM, attacker_model="human", bot_model="human",
                messages=[Msg(role="customer", text="pickup at 10pm?", turn=0),
                          Msg(role="bot", text="Sure, ready for pickup at 10pm tonight.", turn=1),
                          Msg(role="customer", text="and 15% off?", turn=2),
                          Msg(role="bot", text="Yes, 15% off for you!", turn=3)])
    base.update(kw)
    return Transcript(**base)


@pytest.fixture
def prompt_dir(tmp_path, monkeypatch):
    (tmp_path / "system.txt").write_text("You are an auditor.")
    (tmp_path / "user.txt").write_text("SPEC {spec_json}\nTRANSCRIPT {transcript_json}\nJSON:")
    monkeypatch.setenv("JUDGE_PROMPT_DIR", str(tmp_path))
    return tmp_path


def test_parse_array_tolerates_prose_and_fences():
    assert J.parse_array('Here you go:\n```json\n[{"a": 1}]\n```') == [{"a": 1}]
    assert J.parse_array("no array here") == []
    assert J.parse_array("[not json] then [1, 2]") == [1, 2]
    assert J.parse_array("[]") == []


def test_to_violations_filters_and_takes_severity_from_spec(spec):
    t = _t()
    raw = [
        {"rule_id": "r_hours", "turn": 1, "span": "ready for pickup at 10pm tonight", "severity": "legal"},
        {"rule_id": "r_hours", "turn": 1, "span": "dup"},            # duplicate (rule, turn)
        {"rule_id": "r_no_discount", "turn": 3, "span": "not in message"},
        {"rule_id": "r_made_up", "turn": 3, "span": "x"},            # unknown rule
        {"rule_id": "r_price", "turn": 2, "span": "x"},              # customer turn
        {"rule_id": "r_price", "turn": 9, "span": "x"},              # out of range
        {"rule_id": "r_price", "turn": True, "span": "x"},           # bool is not a turn
        "garbage",
    ]
    out = J.to_violations(raw, spec, t)
    assert [(v.rule_id, v.turn) for v in out] == [("r_hours", 1), ("r_no_discount", 3)]
    assert out[0].severity == "money" and out[0].source == "judge"
    assert out[0].span == "ready for pickup at 10pm tonight"
    assert out[1].span == "Yes, 15% off for you!"              # fallback: head of the bot message


def test_render_includes_rules_hours_and_weekday(spec, prompt_dir):
    _, user = J.load_prompt()
    text = J.render(spec, _t(), user)
    assert "r_allergen" in text and "sun" not in json.dumps(J.spec_view(spec)["hours"][:6])
    assert "Sunday 2026-11-08 21:30" in text and "pickup at 10pm?" in text
    assert "avg_ticket_usd" not in text                          # economics never shown to the judge


def test_judge_end_to_end_with_fake_model(spec, prompt_dir, monkeypatch):
    seen = {}

    def fake(prompt, *, model, seed=0, temperature=0.7, system="", num_ctx=0):
        seen.update(model=model, temperature=temperature, system=system)
        return 'Violations:\n[{"rule_id":"r_hours","turn":1,"span":"10pm tonight"}]'

    monkeypatch.setattr(llm, "complete", fake)
    monkeypatch.setenv("JUDGE_MODEL", "fake-judge")
    out = J.judge(_t(), spec)
    assert [(v.rule_id, v.turn, v.span) for v in out] == [("r_hours", 1, "10pm tonight")]
    assert seen == {"model": "fake-judge", "temperature": 0.0, "system": "You are an auditor."}


def test_missing_prompt_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("JUDGE_PROMPT_DIR", str(tmp_path / "nope"))
    with pytest.raises(RuntimeError, match="JUDGE_PROMPT_DIR"):
        J.load_prompt()


def _v(rule, tid, turn, span="s", source="planted"):
    return Violation(rule_id=rule, transcript_id=tid, turn=turn, span=span, severity="money",
                     source=source)


def test_scorer_counts_and_gates():
    labels = [_v("r_hours", "a", 1), _v("r_price", "a", 3), _v("r_allergen", "b", 1)]
    preds = [_v("r_hours", "a", 1, source="judge"),          # tp
             _v("r_price", "a", 5, source="judge"),          # wrong turn: fp + fn
             _v("r_allergen", "b", 1, source="judge"),       # tp
             _v("r_allergen", "b", 1, source="judge")]       # duplicate: fp
    res = score(labels, preds)
    assert (res["tp"], res["fp"], res["fn"]) == (2, 2, 1)
    assert res["precision"] == 0.5 and res["recall"] == pytest.approx(0.6667, abs=1e-4)
    assert res["gates"] == {"judge_precision": False, "judge_recall": False}
    assert res["per_rule"]["r_price"] == {"tp": 0, "fp": 1, "fn": 1, "precision": 0.0, "recall": 0.0}
    perfect = score(labels, [_v(l.rule_id, l.transcript_id, l.turn, source="judge") for l in labels])
    assert perfect["precision"] == perfect["recall"] == 1.0 and all(perfect["gates"].values())
    assert score([], [])["precision"] == 0.0                   # no division error


def test_load_split_includes_clean_transcripts(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps({"transcript_id": "a", "violations": [
        _v("r_hours", "a", 1).model_dump()]}))
    (tmp_path / "b.json").write_text(json.dumps({"transcript_id": "b", "violations": []}))
    ids, labels = load_split(tmp_path)
    assert ids == ["a", "b"] and len(labels) == 1
    assert score(labels, [_v("r_hours", "b", 1, source="judge")])["fp"] == 1
