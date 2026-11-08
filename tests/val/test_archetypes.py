"""VAL publishes the dev-only archetypes; they must load through GEN's harness schema."""
from gen.harness.bots import load_archetypes, system_prompt


def test_dev_archetypes_publish_three_distinct_bots(spec):
    archs = load_archetypes()
    assert {a.tool_id for a in archs} == {"dev_leaky", "dev_cautious", "dev_local"}
    assert len({(a.grounding, a.refusal, a.pressure) for a in archs}) == 3
    for a in archs:
        text = system_prompt(a, spec, seed=1)
        assert "cafe demo 01" in text and "Hours:" in text and "{" not in text
