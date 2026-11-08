"""The attack library must stay consistent with the demo specs for every vertical it covers,
and the cafe entries (used by the demo cache) must not drift."""
import json
from pathlib import Path

import pytest

from contracts.policyspec import PolicySpec, Skeleton, Vertical
from gen.swarm.library import ATTACKS, BENIGN, PERSONAS, attacks_for, benign_for

SPECS = {
    Vertical.CAFE: Path("data/specs/cafe_demo.json"),
    Vertical.HVAC: Path("data/specs/hvac_demo.json"),
    Vertical.LANDSCAPING: Path("data/specs/landscaping_demo.json"),
}
OWNER_DOCS = Path("data/specs/owner_docs")
NEW_VERTICALS = [Vertical.HVAC, Vertical.LANDSCAPING]

CAFE_ATTACK_IDS = ["phantom_refund", "allergen_clearance", "outside_hours", "catering_misquote",
                   "invented_discount"]
CAFE_BENIGN_IDS = ["ask_hours", "ask_price", "ask_allergens", "small_catering", "ask_parking"]
ORIGINAL_PERSONAS = {"haggler", "allergy_parent", "midnight_booker", "refund_scammer", "caterer",
                     "regular"}


def load(vertical: Vertical) -> PolicySpec:
    return PolicySpec.model_validate(json.loads(SPECS[vertical].read_text()))


# ---- the new specs


@pytest.mark.parametrize("vertical", NEW_VERTICALS)
def test_new_spec_validates_and_names_its_vertical(vertical):
    spec = load(vertical)
    assert spec.vertical == vertical
    assert spec.business_id == f"{vertical.value}_demo_01"
    assert spec.timezone == "America/New_York"
    assert "sun" not in {h.day for h in spec.hours}
    assert spec.service_area_zips and len(spec.service_area_zips) >= 8
    assert all(z.startswith("207") and len(z) == 5 for z in spec.service_area_zips)
    assert spec.warranties and all(w.item_ids for w in spec.warranties)


@pytest.mark.parametrize("vertical", NEW_VERTICALS)
def test_new_spec_has_one_legal_rule_with_fine_and_prob(vertical):
    legal = [r for r in load(vertical).rules if r.severity == "legal"]
    assert len(legal) == 1
    assert legal[0].fine_usd and legal[0].fine_usd > 0
    assert legal[0].enforcement_prob and 0 < legal[0].enforcement_prob < 1


def test_hvac_spec_shape():
    spec = load(Vertical.HVAC)
    assert spec.after_hours_surcharge_usd == 150
    assert spec.economics.trip_hours == 1.5
    assert spec.rule("r_lead").typed_params().model_dump() == {
        "service": "ac install", "min_days": 3, "business_days": True}
    assert spec.rule("r_discount_cap").typed_params().max_usd == 50
    assert spec.rule("r_big_job").typed_params().min_order_usd == 1000
    assert {w.scope: w.months for w in spec.warranties} == {"parts": 12, "labor": 3}
    assert {w.day: (w.open, w.close) for w in spec.hours}["sat"] == ("09:00", "13:00")
    assert spec.rule("r_refrigerant").skeleton == Skeleton.ROLE_SCOPE


def test_landscaping_spec_shape():
    spec = load(Vertical.LANDSCAPING)
    lead = spec.rule("r_lead").typed_params()
    assert (lead.service, lead.min_days) == ("sod install", 5)
    assert spec.rule("r_big_job").typed_params().min_order_usd == 1500
    assert set(spec.rule("r_no_discount").typed_params().types) == {
        "discount", "free_service", "weather_guarantee"}
    assert {w.scope: w.months for w in spec.warranties} == {"plant": 12, "workmanship": 6}
    assert all((h.open, h.close) == ("07:00", "18:00") for h in spec.hours)
    assert Skeleton.AVAILABILITY_CLAIM in {r.skeleton for r in spec.rules}
    assert spec.rule("r_locate").skeleton == Skeleton.ROLE_SCOPE


# ---- the library against every spec


def test_every_vertical_in_library_has_a_spec():
    assert set(ATTACKS) == set(BENIGN) == set(SPECS)


@pytest.mark.parametrize("vertical", list(SPECS))
def test_attacks_target_skeletons_in_that_spec(vertical):
    skeletons = {r.skeleton for r in load(vertical).rules}
    attacks = attacks_for(vertical)
    assert len(attacks) == 5
    ids = [a.attack_id for a in attacks]
    assert len(ids) == len(set(ids))
    for a in attacks:
        assert a.target in skeletons, f"{vertical.value}:{a.attack_id} targets {a.target}"
        assert len(a.persona_ids) >= 3
        assert len(set(a.persona_ids)) == len(a.persona_ids)
        assert set(a.persona_ids) <= set(PERSONAS)
        assert a.goal and a.opener_hint


@pytest.mark.parametrize("vertical", list(SPECS))
def test_benign_scenarios_are_well_formed(vertical):
    benign = benign_for(vertical)
    assert len(benign) == 5
    ids = [b.scenario_id for b in benign]
    assert len(ids) == len(set(ids))
    for b in benign:
        assert b.persona_id in PERSONAS and b.goal


@pytest.mark.parametrize("vertical", NEW_VERTICALS)
def test_new_vertical_attacks_hit_the_legal_rule(vertical):
    """Each home-services vertical attacks its legal rule, so the pricer sees a fine in the demo."""
    spec = load(vertical)
    legal = {r.skeleton for r in spec.rules if r.severity == "legal"}
    assert legal & {a.target for a in attacks_for(vertical)}


def test_personas_are_well_formed():
    for pid, p in PERSONAS.items():
        assert p.persona_id == pid
        assert p.name.startswith("The ") and p.style and p.pressure
    assert {"out_of_area_homeowner", "warranty_lawyer", "emergency_caller", "bargain_landlord",
            "weekend_planner"} <= set(PERSONAS)


# ---- owner docs


@pytest.mark.parametrize("vertical", NEW_VERTICALS)
def test_owner_docs_exist_short_and_consistent(vertical):
    doc = OWNER_DOCS / f"{vertical.value}_demo.txt"
    assert doc.exists()
    text = doc.read_text()
    assert len(text.splitlines()) < 60
    spec = load(vertical)
    for z in spec.service_area_zips:
        assert z in text
    for item in spec.catalog:
        whole = f"${item.price_usd:,.0f}" if item.price_usd >= 1000 else f"${item.price_usd:.0f}"
        assert whole in text, f"{item.name} price {whole} missing from {doc}"


# ---- the cafe must not drift (the demo cache was generated from these ids)


def test_cafe_entries_unchanged():
    assert [a.attack_id for a in attacks_for(Vertical.CAFE)] == CAFE_ATTACK_IDS
    assert [a.target for a in attacks_for(Vertical.CAFE)] == [
        Skeleton.COMMITMENT_CAP, Skeleton.ALLERGEN_CLAIM, Skeleton.HOURS_BOUND,
        Skeleton.PRICE_MATCH, Skeleton.FORBIDDEN_COMMITMENT]
    assert [b.scenario_id for b in benign_for(Vertical.CAFE)] == CAFE_BENIGN_IDS
    assert ORIGINAL_PERSONAS <= set(PERSONAS)
    assert PERSONAS["haggler"].pressure == "claims a competitor is cheaper and threatens to leave"
