import json
import shutil
import time

import pytest

from contracts.policyspec import PolicySpec, Skeleton
from contracts.transcript import Msg
from contracts.verdict import Action, Verdict
from gen.forge import runtime
from gen.forge.guardrails import price_match

HIST = [Msg(role="customer", text="how much?", turn=0)]


def run(spec, text):
    return price_match.check(HIST, Msg(role="bot", text=text, turn=1), spec, spec.rule("r_price"))


@pytest.mark.parametrize("text,expected", [
    ("A cappuccino is $3.50 and cold brew is $1.00.", "A cappuccino is $4.75 and cold brew is $4.25."),
    ("The cappuccino is $4.75, but for you $2.", "The cappuccino is $4.75, but for you $4.75."),
    ("For $1.00 you get a cappuccino.", "For $4.75 you get a cappuccino."),
    ("2 cappuccinos for $7", "2 cappuccinos for $9.50"),
    ("CAPPUCCINO IS $1", "CAPPUCCINO IS $4.75"),
    ("Cold brew is $4.25. Oh and the cappuccino... the cold brew for you is $1.",
     "Cold brew is $4.25. Oh and the cappuccino... the cold brew for you is $4.25."),
    ("The catering box is $1,250.", "The catering box is $48.00."),
    ("Our $3 cappuccinos are popular.", "Our $4.75 cappuccinos are popular."),
    ("A $1 cappuccino and a $1 cold brew.", "A $4.75 cappuccino and a $4.25 cold brew."),
])
def test_misquotes_rewritten(spec, text, expected):
    v = run(spec, text)
    assert v.action == Action.REWRITE and v.rule_id == "r_price" and v.rewrite == expected


@pytest.mark.parametrize("text", [
    "Banana bread is $3.95 today.",
    "Two cappuccinos come to $9.50.",
    "Cappuccinos are $9.50.",                       # unstated quantity, whole multiple: allowed
    "Sorry about the cappuccino, I refunded you $10.",
    "The cappuccino is popular. We have a $5 card minimum.",
    "The cappuccino is great; the cold brew is $4.25.",
    "The cappuccino is $4.75 plus a $2 delivery fee.",
    "We'll give you $10 back on the cappuccino.",
    "Our coffee is great and the cappuccino is $4.75.",
    "Our hours are 7 to 6.",
])
def test_correct_or_unrelated_prices_allowed(spec, text):
    assert run(spec, text).action == Action.ALLOW


def test_trades_units_and_word_boundaries(spec_dict):
    spec_dict["catalog"] += [{"item_id": "i_cbt", "name": "cold brew tonic", "price_usd": 5.5},
                             {"item_id": "i_tea", "name": "tea", "price_usd": 3.0},
                             {"item_id": "i_ac", "name": "ac tune-up", "price_usd": 1250.0},
                             {"item_id": "i_mul", "name": "mulch", "price_usd": 45.0, "unit": "yard"},
                             {"item_id": "i_lab", "name": "labor", "price_usd": 120.0, "unit": "hour"}]
    spec = PolicySpec.model_validate(spec_dict)
    for ok in ("Cold brew tonic is $5.50.", "Our team says the cold brew is $4.25 today.",
               "An AC tune-up is $1,250.", "3 yards of mulch is $135.", "2 hours of labor is $240.",
               "The AC tune-up is $1,250 plus a $75 after-hours surcharge."):
        assert run(spec, ok).action == Action.ALLOW, ok
    assert run(spec, "3 yards of mulch is $100.").rewrite == "3 yards of mulch is $135.00."


def test_large_catalog_is_fast(spec_dict):
    spec_dict["catalog"] += [{"item_id": f"x{i}", "name": f"special item {i}", "price_usd": 5 + i}
                             for i in range(300)]
    spec = PolicySpec.model_validate(spec_dict)
    text = " ".join(f"Special item {i} is ${5 + i}." for i in range(0, 300, 10)) + " A cappuccino is $1."
    run(spec, text)                                  # warm the compiled-pattern cache
    t = time.perf_counter()
    v = run(spec, text)
    assert v.action == Action.REWRITE and (time.perf_counter() - t) < 0.05


# ---------------------------------------------------------------- runtime
def _registry(*modules, modes=None):
    reg = runtime.Registry(modules={m.RULE_SKELETON: m for m in modules})
    reg.modes.update(modes or {})
    return reg


def _fake(skeleton, fn):
    return type("Fake", (), {"RULE_SKELETON": skeleton, "VERSION": 1, "check": staticmethod(fn)})


REFUND_CAP = _fake(Skeleton.COMMITMENT_CAP, lambda h, r, s, rule: (
    Verdict(action=Action.BLOCK, rule_id=rule.rule_id, evidence="refund > cap")
    if rule.params["type"] == "refund" and "refunded you $40" in r.text else Verdict(action=Action.ALLOW)))


def test_rewrite_cannot_hide_another_violation(spec):
    reply = Msg(role="bot", text="So sorry about the cappuccino, I've refunded you $40.", turn=1)
    out = runtime.apply_all(HIST, reply, spec, registry=_registry(price_match, REFUND_CAP))
    assert out.reply.text == runtime.HANDOFF and "r_refund_cap" in [v.rule_id for v in out.applied]


def test_rewrites_then_rechecks(spec):
    out = runtime.apply_all(HIST, Msg(role="bot", text="Almond croissant: $2.00", turn=1), spec,
                            registry=_registry(price_match))
    assert out.reply.text == "Almond croissant: $4.50" and [v.rule_id for v in out.applied] == ["r_price"]


def test_shadow_mode_logs_but_never_changes_reply(spec):
    out = runtime.apply_all(HIST, Msg(role="bot", text="Almond croissant: $2.00", turn=1), spec,
                            registry=_registry(price_match, modes={Skeleton.PRICE_MATCH: "shadow"}))
    assert out.reply.text == "Almond croissant: $2.00" and out.applied == []
    assert [v.rule_id for v in out.shadow] == ["r_price"]


@pytest.mark.parametrize("fn", [
    lambda h, r, s, rule: 1 / 0,                                                   # crash
    lambda h, r, s, rule: "not a verdict",                                         # garbage
    lambda h, r, s, rule: Verdict(action=Action.REWRITE, rule_id=rule.rule_id, rewrite=r.text),  # no-op
    lambda h, r, s, rule: [None for _ in iter(int, 1)],                            # hang
])
def test_fails_closed(spec, fn):
    out = runtime.apply_all(HIST, Msg(role="bot", text="See you Sunday!", turn=1), spec,
                            registry=_registry(_fake(Skeleton.HOURS_BOUND, fn)))
    assert out.reply.text == runtime.HANDOFF and out.applied[0].action == Action.BLOCK


def test_guardrail_cannot_mutate_shared_state(spec):
    def evil(h, r, s, rule):
        rule.params["max_usd"] = 10**6
        s.catalog.clear()
        h.clear()
        return Verdict(action=Action.ALLOW)
    hist = list(HIST)
    runtime.apply_all(hist, Msg(role="bot", text="ok", turn=1), spec,
                      registry=_registry(_fake(Skeleton.COMMITMENT_CAP, evil)))
    assert spec.rule("r_refund_cap").params["max_usd"] == 10 and spec.catalog and hist


def test_load_failure_fails_closed_and_isolates(tmp_path, spec):
    shutil.copy(runtime.GUARD_DIR / "price_match.py", tmp_path / "price_match.py")
    (tmp_path / "hours_v1.py").write_text("import os\n")
    (tmp_path / "active.json").write_text(json.dumps({
        "price_match": {"file": "price_match", "mode": "enforce"},
        "hours_bound": {"file": "hours_v1", "mode": "enforce"}}))
    reg = runtime.load(tmp_path)
    assert list(reg.modules) == [Skeleton.PRICE_MATCH] and "hours_v1" in reg.errors
    out = runtime.apply_all(HIST, Msg(role="bot", text="Banana bread is $3.95.", turn=1), spec, registry=reg)
    assert out.reply.text == runtime.HANDOFF and out.applied[0].rule_id == "r_hours"
