"""Policy Compiler tests run against a scripted fake model, so CI never needs Ollama."""
import copy
import json
from pathlib import Path

import pytest

from common import llm
from contracts.policyspec import (SKELETON_PARAMS, Channel, PolicySpec, Skeleton, Vertical)
from gen.compiler import compile as pc

DOCS = Path("data/specs/owner_docs/cafe_demo.txt")
FIXED = dict(business_id="cafe_demo_02", vertical="cafe", timezone="America/New_York")


def fenced(obj) -> str:
    return "Here is the spec:\n```json\n" + json.dumps(obj, indent=1) + "\n```\nDone."


@pytest.fixture
def scripted(monkeypatch):
    """Returns a factory: scripted(replies) installs a fake model and returns the call log."""
    def install(replies):
        calls = []
        script = list(replies)

        def fake(prompt, *, model, seed=0, temperature=0.7, system="", num_ctx=0):
            calls.append(dict(prompt=prompt, model=model, seed=seed, temperature=temperature,
                              system=system))
            if not script:
                pytest.fail("fake model called more times than scripted")
            return script.pop(0)

        monkeypatch.setattr(llm, "complete", fake)
        monkeypatch.setenv("COMPILER_MODEL", "fake-e4b")
        return calls
    return install


def test_owner_docs_exist_and_are_short():
    lines = DOCS.read_text().splitlines()
    assert 0 < len(lines) < 60
    text = DOCS.read_text().lower()
    for word in ("cappuccino", "cold brew", "almond croissant", "banana bread", "catering"):
        assert word in text


def test_repair_loop_feeds_errors_back(spec_dict, scripted):
    bad_tz = copy.deepcopy(spec_dict)
    bad_tz.update(FIXED)
    bad_tz["timezone"] = "Mars/Olympus"
    good = copy.deepcopy(spec_dict)
    good.update(FIXED)
    calls = scripted(["Sure! The spec is roughly {\"business_id\": \"cafe_demo_02\", and so on",
                      fenced(bad_tz), fenced(good)])
    result = pc.compile(DOCS.read_text(), **FIXED, seed=3)
    assert isinstance(result.spec, PolicySpec)
    assert result.iterations == 3 and len(result.errors) == 2 and len(result.raw) == 3
    assert "not valid JSON" in result.errors[0] and "Mars/Olympus" in result.errors[1]
    assert len(calls) == 3
    assert result.errors[0] in calls[1]["prompt"]
    assert "Mars/Olympus" in calls[2]["prompt"] and "validation error" in calls[2]["prompt"]
    assert all(c["temperature"] == 0.0 and c["seed"] == 3 and c["model"] == "fake-e4b"
               for c in calls)
    assert result.model == "fake-e4b" and result.seed == 3
    assert "Mars/Olympus" not in calls[0]["prompt"]


def test_first_prompt_has_schema_example_fixed_values_and_docs(spec_dict, scripted):
    good = copy.deepcopy(spec_dict)
    good.update(FIXED)
    calls = scripted([fenced(good)])
    docs = DOCS.read_text()
    pc.compile(docs, **FIXED)
    prompt = calls[0]["prompt"]
    assert Path("data/specs/cafe_demo.json").read_text().strip() in prompt
    assert docs.strip() in prompt
    assert '"cafe_demo_02"' in prompt and '"America/New_York"' in prompt and '"1.1"' in prompt
    assert "Return ONLY one JSON object in a ```json code block" in prompt
    assert "price_match" in prompt and calls[0]["system"]


@pytest.mark.parametrize("text, want", [
    ('```json\n{"a": 1}\n```', {"a": 1}),
    ('prose first\n```json\n{"a": [1, 2]}\n```\nand prose after', {"a": [1, 2]}),
    ('no fence here {"a": {"b": "}"}, "c": "{"} trailing text', {"a": {"b": "}"}, "c": "{"}),
    ('```\n{"plain": true}\n```', {"plain": True}),
])
def test_extract_json_fenced_and_unfenced(text, want):
    assert json.loads(pc.extract_json(text)) == want


def test_extract_json_rejects_prose_and_unbalanced():
    with pytest.raises(ValueError):
        pc.extract_json("I could not produce a spec, sorry.")
    with pytest.raises(ValueError):
        pc.extract_json('here: {"a": [1, 2')


def test_fixed_fields_are_forced_after_validation(spec_dict, scripted):
    drift = copy.deepcopy(spec_dict)
    drift.update(business_id="someone_else", vertical="restaurant", timezone="Europe/Paris",
                 version=7)
    drift["failure_corpus"] = [{"rule_id": "r_price", "transcript_id": "t_x", "turn": 1,
                                "span": "x", "severity": "money", "source": "judge"}]
    scripted([fenced(drift)])
    result = pc.compile(DOCS.read_text(), **FIXED)
    s = result.spec
    assert result.iterations == 1 and result.errors == []
    assert s.business_id == "cafe_demo_02" and s.vertical == Vertical.CAFE
    assert s.timezone == "America/New_York" and s.schema_version == "1.1"
    assert s.version == 1 and s.failure_corpus == []


def test_economics_override_lets_model_omit_them(spec_dict, scripted):
    partial = copy.deepcopy(spec_dict)
    partial.update(FIXED)
    econ = partial.pop("economics")
    calls = scripted([fenced(partial)])
    result = pc.compile(DOCS.read_text(), **FIXED, economics=econ)
    assert result.spec is not None and result.iterations == 1
    assert result.spec.economics.model_dump(exclude_none=True) == econ
    assert "economics" in calls[0]["prompt"].lower() and "omit" in calls[0]["prompt"].lower()


def test_without_economics_override_missing_economics_is_an_error(spec_dict, scripted):
    partial = copy.deepcopy(spec_dict)
    partial.update(FIXED)
    partial.pop("economics")
    scripted([fenced(partial)])
    result = pc.compile(DOCS.read_text(), **FIXED, max_iters=1)
    assert result.spec is None and "economics" in result.errors[0]


def test_tools_not_named_in_docs_are_dropped(spec_dict, scripted):
    invented = copy.deepcopy(spec_dict)
    invented.update(FIXED)
    invented["existing_tools"] = [
        {"tool_id": "t_site_bot", "name": "website chat bot", "channel": "web_chat"},
        {"tool_id": "t_phone", "name": "phone concierge", "channel": "voice"}]
    scripted([fenced(invented)])
    result = pc.compile("Our website chat answers customers. Hours 9-5 mon.", **FIXED)
    assert [t.tool_id for t in result.spec.existing_tools] == ["t_site_bot"]
    scripted([fenced(invented)])
    result = pc.compile("Just a price board.", **FIXED)
    assert result.spec.existing_tools == []


def test_null_params_are_repaired(spec_dict, scripted):
    d = copy.deepcopy(spec_dict)
    d.update(FIXED)
    d["rules"][0]["params"] = None
    scripted([fenced(d)])
    result = pc.compile(DOCS.read_text(), **FIXED)
    assert result.spec is not None and result.spec.rules[0].params == {}


def test_max_iters_exhausted_returns_none(scripted):
    scripted(["nope", "still nope", "{\"business_id\": 1}", "never"])
    result = pc.compile(DOCS.read_text(), **FIXED, max_iters=3)
    assert result.spec is None and result.iterations == 3
    assert len(result.errors) == 3 and len(result.raw) == 3
    assert "not valid JSON" in result.errors[0] and "validation error" in result.errors[2]


def test_schema_summary_covers_the_frozen_contract():
    text = pc.schema_summary()
    for s in Skeleton:
        assert s.value in text
    for s, params in SKELETON_PARAMS.items():
        for field in params.model_fields:
            assert field in text, (s, field)
    for name in PolicySpec.model_fields:
        assert name in text
    for enum in (Vertical, Channel):
        for member in enum:
            assert member.value in text
    assert "HH:MM" in text and "unlisted days are closed" in text
    assert "money" in text and "safety" in text and "legal" in text
    assert "schema_version" in text and "1.1" in text


def test_model_lookup_prefers_compiler_then_forge(monkeypatch):
    monkeypatch.delenv("COMPILER_MODEL", raising=False)
    monkeypatch.delenv("FORGE_MODEL", raising=False)
    with pytest.raises(RuntimeError, match="COMPILER_MODEL"):
        pc.compiler_model()
    monkeypatch.setenv("FORGE_MODEL", "forge-x")
    assert pc.compiler_model() == "forge-x"
    monkeypatch.setenv("COMPILER_MODEL", "comp-y")
    assert pc.compiler_model() == "comp-y"


def test_cli_writes_spec_and_log_and_exits_zero(spec_dict, scripted, tmp_path, capsys):
    good = copy.deepcopy(spec_dict)
    good.update(FIXED)
    good.pop("economics")
    scripted(["garbage", fenced(good)])
    out = tmp_path / "out" / "cafe_compiled.json"
    log = tmp_path / "log"
    code = pc.main([str(DOCS), "--business-id", "cafe_demo_02", "--vertical", "cafe",
                    "--timezone", "America/New_York", "--out", str(out), "--seed", "5",
                    "--economics-from", "data/specs/cafe_demo.json", "--log-dir", str(log)])
    assert code == 0
    printed = capsys.readouterr().out
    assert "valid" in printed and "2" in printed
    written = PolicySpec.model_validate_json(out.read_text())
    assert written.business_id == "cafe_demo_02" and written.economics.avg_ticket_usd == 11.5
    run_dir = log / "cafe_demo_02" / "seed_5"
    names = sorted(p.name for p in run_dir.iterdir())
    assert "iter_1_prompt.txt" in names and "iter_2_response.txt" in names
    assert "result.json" in names and "system.txt" in names
    meta = json.loads((run_dir / "result.json").read_text())
    assert meta["model"] == "fake-e4b" and meta["seed"] == 5 and meta["valid"] is True
    assert meta["iterations"] == 2 and "git_sha" in meta and "num_ctx" in meta


def test_cli_exits_one_and_writes_nothing_when_invalid(scripted, tmp_path, capsys):
    scripted(["nope", "nope", "nope"])
    out = tmp_path / "bad.json"
    code = pc.main([str(DOCS), "--business-id", "cafe_demo_02", "--vertical", "cafe",
                    "--timezone", "America/New_York", "--out", str(out),
                    "--log-dir", str(tmp_path / "log")])
    assert code == 1 and not out.exists()
    printed = capsys.readouterr().out
    assert "not valid JSON" in printed
    meta = json.loads((tmp_path / "log" / "cafe_demo_02" / "seed_1" / "result.json").read_text())
    assert meta["valid"] is False and len(meta["errors"]) == 3
