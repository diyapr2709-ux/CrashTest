"""Harness tests run against a scripted fake model, so CI never needs Ollama."""
import json
from datetime import datetime
from pathlib import Path

import pytest

from common import llm
from contracts.policyspec import PolicySpec, Vertical
from contracts.transcript import Transcript
from gen.harness import run
from gen.harness.bots import facts, load_archetypes, system_prompt
from gen.swarm.library import ATTACKS, BENIGN, PERSONAS, attacks_for

SIM = datetime(2026, 11, 8, 21, 30)


@pytest.fixture
def fake_llm(monkeypatch):
    calls = []

    def fake(prompt, *, model, seed=0, temperature=0.7, system="", num_ctx=0):
        calls.append((model, seed, system[:20]))
        who = "customer" if "You are The" in system else "bot"
        return f"{who} says ({model}, seed {seed}): {prompt.count('You:')}"

    monkeypatch.setattr(llm, "complete", fake)
    monkeypatch.setenv("ATTACKER_MODEL", "fake-e4b")
    monkeypatch.setenv("BOT_MODEL", "fake-e4b")
    return calls


def test_library_is_consistent(spec):
    for vertical, attacks in ATTACKS.items():
        ids = [a.attack_id for a in attacks]
        assert len(ids) == len(set(ids))
        for a in attacks:
            assert len(a.persona_ids) >= 3 and set(a.persona_ids) <= set(PERSONAS)
            assert a.target in {r.skeleton for r in spec.rules} or vertical != Vertical.CAFE
    for b in BENIGN[Vertical.CAFE]:
        assert b.persona_id in PERSONAS
    assert len(attacks_for(Vertical.CAFE)) == 5


def test_archetypes_load_and_render(spec):
    archs = load_archetypes()
    assert {a.tool_id for a in archs} == {"dev_leaky", "dev_cautious", "dev_local"}
    for a in archs:
        text = system_prompt(a, spec, seed=1)
        assert "cappuccino" in text and "Reply in 1-3 sentences" in text


def test_stale_grounding_perturbs_and_is_seeded(spec):
    exact = facts(spec, "spec", 1)
    assert "$4.75" in exact and "Other days closed" in exact
    stale1, stale2 = facts(spec, "stale", 1), facts(spec, "stale", 1)
    assert stale1 == stale2 and "Other days closed" not in stale1
    assert facts(spec, "none", 1).startswith("You are a cafe")


def test_conversation_is_valid_and_deterministic(spec, fake_llm):
    arch = load_archetypes()[0]
    atk = attacks_for(Vertical.CAFE)[0]
    t1 = run.run_conversation(spec, arch, "haggler", seed=7, sim_time=SIM, attack=atk)
    t2 = run.run_conversation(spec, arch, "haggler", seed=7, sim_time=SIM, attack=atk)
    assert t1 == t2 and len(t1.messages) == 6
    assert [m.role for m in t1.messages] == ["customer", "bot"] * 3
    assert t1.messages[0].text.startswith("customer says") and t1.messages[1].text.startswith("bot says")
    assert t1.messages[5].at == SIM + 5 * run.TURN_GAP
    assert t1.attack_class == "phantom_refund" and not t1.benign
    assert Transcript.model_validate_json(t1.model_dump_json()) == t1


def test_seed_and_persona_change_the_id(spec, fake_llm):
    arch = load_archetypes()[0]
    atk = attacks_for(Vertical.CAFE)[0]
    a = run.run_conversation(spec, arch, "haggler", seed=1, sim_time=SIM, attack=atk)
    b = run.run_conversation(spec, arch, "haggler", seed=2, sim_time=SIM, attack=atk)
    c = run.run_conversation(spec, arch, "regular", seed=1, sim_time=SIM, attack=atk)
    assert len({a.transcript_id, b.transcript_id, c.transcript_id}) == 3


def test_benign_customers_get_no_attack_lever(spec):
    attack = run.customer_system(spec, "midnight_booker", "book late", "ask late", attacking=True)
    benign = run.customer_system(spec, "midnight_booker", "ask about parking", "", attacking=False)
    assert "Lever:" in attack and "offers to pay extra" in attack
    assert "Lever:" not in benign and "pay extra" not in benign
    assert "no agenda" in benign and "You are The Midnight Booker" in benign
    assert "ask about parking" in benign


def test_must_pass_exactly_one_scenario(spec, fake_llm):
    arch = load_archetypes()[0]
    with pytest.raises(ValueError):
        run.run_conversation(spec, arch, "haggler", seed=1, sim_time=SIM)


def test_dev_set_counts_and_files(spec, fake_llm, tmp_path):
    made = run.generate_dev_set(spec, load_archetypes(), tmp_path, seed=1, personas_per_attack=1)
    assert len(made) == 3 * (5 + 5)
    assert sum(t.benign for t in made) == 15
    files = list(tmp_path.glob("t_*.json"))
    assert len(files) == 30
    loaded = Transcript.model_validate_json(files[0].read_text())
    assert loaded.business_id == spec.business_id and loaded.spec_version == spec.version


def test_attack_classes_without_a_matching_rule_are_skipped(spec_dict, fake_llm, tmp_path):
    spec_dict["rules"] = [r for r in spec_dict["rules"] if r["skeleton"] != "hours_bound"]
    spec = PolicySpec.model_validate(spec_dict)
    assert [a.attack_id for a in run.applicable_attacks(spec)] == \
        ["phantom_refund", "allergen_clearance", "catering_misquote", "invented_discount"]
    made = run.generate_dev_set(spec, load_archetypes()[:1], tmp_path, seed=1)
    assert len(made) == 4 + 5 and "outside_hours" not in {t.attack_class for t in made}


def test_vertical_noun_keeps_cafe_prompts_identical_and_fixes_articles(spec):
    assert "a customer messaging a cafe's chat assistant" in run.customer_system(spec, "regular", "g", "h")
    assert run.public_facts(spec).startswith("The shop is a cafe. Hours: mon 07:00-18:00")
    hvac = PolicySpec.model_validate_json(Path("data/specs/hvac_demo.json").read_text())
    assert "messaging an HVAC company's chat assistant" in run.customer_system(hvac, "regular", "g", "h")
    assert run.public_facts(hvac).startswith("The shop is an HVAC company.")


def test_tools_filter_selects_archetypes():
    archs = load_archetypes()
    assert run.filter_archetypes(archs, None) == archs
    assert [a.tool_id for a in run.filter_archetypes(archs, ["dev_leaky"])] == ["dev_leaky"]
    with pytest.raises(SystemExit):
        run.filter_archetypes(archs, ["stub_leaky"])


def test_main_tools_flag_generates_only_that_bot(spec, fake_llm, tmp_path):
    run.main(["data/specs/cafe_demo.json", "--out", str(tmp_path), "--tools", "dev_leaky",
              "--personas", "2"])
    made = [Transcript.model_validate_json(p.read_text()) for p in tmp_path.glob("t_*.json")]
    assert {t.tool_id for t in made} == {"dev_leaky"} and len(made) == 5 * 2 + 5


def test_cache_only_blocks_live_generation(spec, monkeypatch, tmp_path):
    monkeypatch.setenv("CACHE_ONLY", "1")
    monkeypatch.setenv("CT_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("CT_DEMO_DIR", str(tmp_path / "demo"))
    monkeypatch.setenv("ATTACKER_MODEL", "x")
    monkeypatch.setenv("BOT_MODEL", "x")
    with pytest.raises(llm.CacheMiss):
        run.run_conversation(spec, load_archetypes()[0], "haggler", seed=1, sim_time=SIM,
                             attack=attacks_for(Vertical.CAFE)[0])
