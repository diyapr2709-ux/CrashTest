"""app/data.py: the pure helpers behind the Steward UI. No streamlit here. Every number the UI
shows must be a formatted MetricsRun field, so these tests pin the formatting and the fallbacks."""
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from app import data
from contracts.metrics import MetricsRun, ToolRow
from contracts.policyspec import PolicySpec, Rule, Skeleton
from contracts.transcript import Msg, Transcript

ROOT = Path(__file__).resolve().parents[2]
SMOKE = ROOT / "metrics" / "smoke_fixtures.json"
MARKS = ROOT / "data" / "dev" / "marks.json"
SIM = datetime(2026, 11, 8, 21, 30)


def make_run(**overrides) -> MetricsRun:
    base = json.loads(SMOKE.read_text())
    base.update(overrides)
    return MetricsRun.model_validate(base)


def make_transcript(tid, tool="dev_leaky", persona="haggler", attack="phantom_refund"):
    msgs = [Msg(role="customer", text="I want a refund.", turn=0, at=SIM),
            Msg(role="bot", text="Sure, $50 back.", turn=1, at=SIM)]
    return Transcript(transcript_id=tid, business_id="cafe_demo_01", spec_version=1, tool_id=tool,
                      persona_id=persona, attack_class=attack, benign=attack is None, seed=1,
                      sim_time=SIM, attacker_model="fake", bot_model="fake", messages=msgs)


def write_transcripts(directory: Path, *ts):
    directory.mkdir(parents=True, exist_ok=True)
    for t in ts:
        (directory / f"{t.transcript_id}.json").write_text(t.model_dump_json(indent=1))


# ---------------------------------------------------------------- load_metrics
def test_load_metrics_uses_smoke_file_when_no_latest(tmp_path):
    (tmp_path / "metrics").mkdir()
    shutil.copy(SMOKE, tmp_path / "metrics" / "smoke_fixtures.json")
    run, path, is_smoke = data.load_metrics(tmp_path)
    assert is_smoke and path == tmp_path / "metrics" / "smoke_fixtures.json"
    assert run.run_id == "smoke_fixtures_v0" and len(run.tools) == 3


def test_load_metrics_prefers_latest(tmp_path):
    (tmp_path / "metrics").mkdir()
    shutil.copy(SMOKE, tmp_path / "metrics" / "smoke_fixtures.json")
    latest = make_run(run_id="real_run", split="dev_private")
    (tmp_path / "metrics" / "latest.json").write_text(latest.model_dump_json())
    run, path, is_smoke = data.load_metrics(tmp_path)
    assert not is_smoke and path.name == "latest.json" and run.run_id == "real_run"


def test_load_metrics_without_any_file_fails_loudly(tmp_path):
    with pytest.raises(FileNotFoundError):
        data.load_metrics(tmp_path)


def test_load_metrics_on_repo_root_is_valid():
    run, path, _ = data.load_metrics(ROOT)
    assert path.is_relative_to(ROOT / "metrics") and run.n_specs >= 1


# ---------------------------------------------------------------- rule cards
def test_rule_cards_cover_every_rule_in_order(spec):
    cards = data.rule_cards(spec)
    assert [c["rule_id"] for c in cards] == [r.rule_id for r in spec.rules]
    for c, r in zip(cards, spec.rules):
        assert c["text"] == r.description and c["severity"] == r.severity
        assert c["skeleton"] == r.skeleton.value
    assert set(cards[0]) == {"rule_id", "title", "text", "severity", "skeleton", "params"}


def test_rule_titles_are_plain_words():
    assert data.rule_title("r_refund_cap") == "Refund cap"
    assert data.rule_title("r_no_discount") == "No discount"
    assert data.rule_title("r_price") == "Price"
    assert data.rule_title("weird") == "Weird"


def test_params_rendered_in_words(spec):
    by_id = {c["rule_id"]: c for c in data.rule_cards(spec)}
    assert "cap $10" in by_id["r_refund_cap"]["params"]
    assert "cap $6" in by_id["r_comp_cap"]["params"]
    assert by_id["r_big_order"]["params"] == "min order $100"
    assert by_id["r_no_discount"]["params"] == "types: discount, free item, delivery guarantee"
    assert by_id["r_price"]["params"] == "" and by_id["r_allergen"]["params"] == ""


def test_params_for_lead_time_and_legal_rules():
    lead = Rule(rule_id="r_lead", skeleton="lead_time_bound", description="d", severity="money",
                params={"service": "ac_install", "min_days": 3, "business_days": True})
    assert data.params_in_words(lead) == "ac install: min 3 business days"
    one = Rule(rule_id="r_one", skeleton="lead_time_bound", description="d", severity="money",
               params={"service": "mow", "min_days": 1})
    assert data.params_in_words(one) == "mow: min 1 day"
    legal = Rule(rule_id="r_legal", skeleton="pii_leak", description="d", severity="legal",
                 fine_usd=2500, enforcement_prob=0.25)
    assert data.params_in_words(legal) == "fine $2,500; enforcement 25%"
    cents = Rule(rule_id="r_c", skeleton="commitment_cap", description="d", severity="money",
                 params={"type": "refund", "max_usd": 7.5})
    assert data.params_in_words(cents) == "refund cap $7.50"


# ---------------------------------------------------------------- leaderboard
def test_leaderboard_rows_format_metrics_fields_only():
    run = make_run()
    rows = data.leaderboard_rows(run)
    assert [r["tool"] for r in rows] == ["dev_leaky", "dev_local", "dev_cautious"]  # by exposure
    leaky = rows[0]
    assert leaky["attacks"] == 6 and leaky["violations"] == 13
    assert leaky["exposure"] == "$20,418/mo"
    assert leaky["interval"] == "[$12,448, $20,418]"
    assert leaky["guarded"] == "$20,418/mo"
    assert leaky["worst_failure"] == 'r_refund_cap: "refund the full $96" ($86)'
    assert leaky["status"] == "candidate"
    local = rows[1]
    assert local["exposure"] == "$994/mo" and local["interval"] == "[$310, $1,399]"
    assert local["worst_failure"].endswith("($3.25)")
    cautious = rows[2]
    assert cautious["exposure"] == "$0/mo" and cautious["worst_failure"] == "none found"


def test_leaderboard_marks_unmeasured_guarded_exposure():
    run = make_run()
    row = run.tools[0].model_copy(update={"guarded_usd_month": None})
    run = run.model_copy(update={"tools": [row]})
    assert data.leaderboard_rows(run)[0]["guarded"] == data.NOT_MEASURED


def test_leaderboard_status_overrides_are_display_only():
    run = make_run()
    rows = data.leaderboard_rows(run, status_overrides={"dev_leaky": "shadow"})
    assert rows[0]["status"] == "shadow" and rows[1]["status"] == "candidate"
    assert run.tools[0].status == "candidate"                 # the run itself is untouched


def test_tool_row_and_run_summary():
    run = make_run()
    assert data.tool_row(run, "dev_local").worst_rule_id == "r_comp_cap"
    assert data.tool_row(run, "nope") is None
    summary = {s["label"]: s["value"] for s in data.run_summary(run)}
    assert summary["pass@1"] == "100%" and summary["catch rate"] == "0%"
    assert summary["judge precision"] == data.NOT_MEASURED and summary["transcripts"] == "15"


def test_default_skeleton_follows_worst_rule(spec):
    run = make_run()
    assert data.default_skeleton(spec, data.tool_row(run, "dev_leaky")) == "commitment_cap"
    assert data.default_skeleton(spec, data.tool_row(run, "dev_cautious")) == "price_match"
    assert data.default_skeleton(spec, None) == "price_match"
    unknown = ToolRow(tool_id="x", n_attacks=0, n_violations=0, usd_month=0, usd_month_lo=0,
                      usd_month_hi=0, worst_rule_id="r_missing")
    assert data.default_skeleton(spec, unknown) == "price_match"
    choices = data.skeleton_choices(spec)
    assert choices[:3] == ["price_match", "hours_bound", "commitment_cap"]
    assert len(choices) == len(set(choices)) == 12


def test_spec_paths_and_load_spec():
    paths = data.spec_paths(ROOT)
    assert any(p.name == "cafe_demo.json" for p in paths)
    assert isinstance(data.load_spec(ROOT / "data/specs/cafe_demo.json"), PolicySpec)
    assert paths[data.default_spec_index(paths)].name == "cafe_demo.json"
    assert data.default_spec_index([Path("a.json"), Path("cafe_demo.json")]) == 1
    assert data.default_spec_index([Path("a.json")]) == 0 and data.default_spec_index([]) == 0


# ---------------------------------------------------------------- transcripts
def test_demo_transcripts_filter_by_tool_attacks_first(tmp_path):
    write_transcripts(tmp_path / "data/dev/transcripts",
                      make_transcript("t_benign", attack=None),
                      make_transcript("t_atk_b", attack="phantom_refund"),
                      make_transcript("t_atk_a", attack="allergen_fish"),
                      make_transcript("t_other", tool="dev_local"))
    (tmp_path / "data/dev/transcripts" / "broken.json").write_text("{not json")
    ts = data.demo_transcripts(tmp_path, "dev_leaky")
    assert [t.transcript_id for t in ts] == ["t_atk_a", "t_atk_b", "t_benign"]
    assert [t.transcript_id for t in data.demo_transcripts(tmp_path, "dev_local")] == ["t_other"]
    assert data.demo_transcripts(tmp_path, "dev_cautious") == []


def test_demo_transcripts_prefer_demo_cache_and_handle_empty(tmp_path):
    assert data.demo_transcripts(tmp_path, "dev_leaky") == []
    (tmp_path / "cache/demo/transcripts").mkdir(parents=True)          # empty dir: fall through
    write_transcripts(tmp_path / "data/dev/transcripts", make_transcript("t_dev"))
    assert [t.transcript_id for t in data.demo_transcripts(tmp_path, "dev_leaky")] == ["t_dev"]
    write_transcripts(tmp_path / "cache/demo/transcripts", make_transcript("t_demo"))
    assert [t.transcript_id for t in data.demo_transcripts(tmp_path, "dev_leaky")] == ["t_demo"]
    assert data.transcripts_dir(tmp_path) == tmp_path / "cache/demo/transcripts"


def test_pick_transcript_prefers_the_worst_one():
    ts = [make_transcript("t_1"), make_transcript("t_2")]
    assert data.pick_transcript(ts, "t_2").transcript_id == "t_2"
    assert data.pick_transcript(ts, "t_missing").transcript_id == "t_1"
    assert data.pick_transcript([], "t_1") is None


# ---------------------------------------------------------------- forge artifacts
def write_result(root, skeleton, seed, passed, code="CODE", tests="TESTS", **extra):
    d = root / "cache/forge" / skeleton / f"seed_{seed}"
    d.mkdir(parents=True)
    body = {"skeleton": skeleton, "seed": seed, "iterations": 2, "passed": passed,
            "code": code, "tests": tests, **extra}
    (d / "result.json").write_text(json.dumps(body))


def test_forge_artifact_prefers_passing_lowest_seed(tmp_path):
    write_result(tmp_path, "price_match", 1, False, code="bad")
    write_result(tmp_path, "price_match", 3, True, code="ok3")
    write_result(tmp_path, "price_match", 2, True, code="ok2")
    (tmp_path / "cache/forge/price_match/seed_0").mkdir()
    (tmp_path / "cache/forge/price_match/seed_0/result.json").write_text("garbage")
    art = data.forge_artifact(tmp_path, "price_match")
    assert art["source"] == "forge" and art["seed"] == 2 and art["passed"] is True
    assert art["code"] == "ok2" and art["tests"] == "TESTS" and art["iterations"] == 2
    assert art["path"] == "cache/forge/price_match/seed_2/result.json"
    assert art["run_dir"] == "cache/forge/price_match/seed_2" and art["module_file"] is None
    write_result(tmp_path, "role_scope", 1, True, code_path="/abs/cache/forge/role_scope/seed_1/role_scope_v1.py")
    assert data.forge_artifact(tmp_path, "role_scope")["module_file"] == "role_scope_v1.py"


def test_forge_artifact_falls_back_to_lowest_failing_seed(tmp_path):
    write_result(tmp_path, "hours_bound", 5, False, code="five")
    write_result(tmp_path, "hours_bound", 4, False, code="four")
    art = data.forge_artifact(tmp_path, "hours_bound")
    assert art["source"] == "forge" and art["seed"] == 4 and art["passed"] is False


def test_forge_artifact_falls_back_to_reference_guardrail(tmp_path):
    # Forge batches keep adding skeleton dirs under cache/forge; pick one that has no artifact yet.
    uncached = [s.value for s in Skeleton if not (ROOT / "cache/forge" / s.value).is_dir()]
    if not uncached:
        pytest.skip("every skeleton has a Forge artifact under cache/forge")
    art = data.forge_artifact(ROOT, uncached[0])
    assert art["source"] == "reference" and art["passed"] is None
    assert art["run_dir"] is None and art["module_file"] is None
    assert art["skeleton"] == "price_match"
    assert "RULE_SKELETON = Skeleton.PRICE_MATCH" in art["code"]
    assert "def test_misquotes_rewritten" in art["tests"]
    with pytest.raises(FileNotFoundError):
        data.forge_artifact(tmp_path, "price_match")


def test_lint_guardrail_matches_contract():
    assert data.lint_guardrail((ROOT / "gen/forge/guardrails/price_match.py").read_text()) == []
    assert any("import os" in p for p in data.lint_guardrail("import os\n"))


# ---------------------------------------------------------------- running tests in a subprocess
def test_rewrite_imports_points_at_temp_module():
    src = ("from gen.forge.guardrails import price_match\n"
           "from gen.forge.guardrails.price_match import check\n"
           "import gen.forge.guardrails.price_match as pm\n"
           "from gen.forge import runtime\n")
    out = data.rewrite_imports(src, "price_match")
    assert out == ("import price_match\nfrom price_match import check\n"
                   "import price_match as pm\nfrom gen.forge import runtime\n")
    assert data.guess_skeleton("RULE_SKELETON = Skeleton.HOURS_BOUND") == "hours_bound"
    assert data.guess_skeleton("x = 1", "from gen.forge.guardrails import foo") == "foo"
    assert data.guess_skeleton("", "") == "guardrail"


def test_reference_guardrail_tests_pass_in_subprocess():
    art = data.forge_artifact(ROOT, "price_match")
    t = time.perf_counter()
    passed, output = data.run_guardrail_tests(art["code"], art["tests"], timeout=60)
    assert passed, output
    assert "passed" in output and "failed" not in output
    assert time.perf_counter() - t < 10


def test_tests_import_the_candidate_not_the_repo_copy():
    code = (ROOT / "gen/forge/guardrails/price_match.py").read_text().replace("VERSION = 2", "VERSION = 99")
    assert "VERSION = 99" in code
    tests = ("from gen.forge.guardrails import price_match\n\n"
             "def test_candidate_is_loaded():\n    assert price_match.VERSION == 99\n")
    passed, output = data.run_guardrail_tests(code, tests)
    assert passed, output


def test_failing_candidate_reports_failure(tmp_path):
    code = ("from contracts.policyspec import Skeleton\nfrom contracts.verdict import Action, Verdict\n"
            "RULE_SKELETON = Skeleton.HOURS_BOUND\nVERSION = 1\n\n"
            "def check(history, response, spec, rule):\n    return Verdict(action=Action.ALLOW)\n")
    tests = ("import hours_bound\nfrom contracts.transcript import Msg\n\n"
             "def test_flags_sunday(spec):\n"
             "    v = hours_bound.check([], Msg(role='bot', text='See you Sunday!', turn=1), spec, spec.rule('r_hours'))\n"
             "    assert v.action.value == 'rewrite'\n")
    passed, output = data.run_guardrail_tests(code, tests)
    assert not passed and "1 failed" in output
    assert not list(tmp_path.iterdir())                     # nothing written into our dir


def test_candidate_is_written_under_every_name_the_tests_load():
    tests = ('HERE = Path(__file__).resolve().parent\nMOD = load_file(HERE / "hours_bound_v1.py")\n'
             'OTHER = load_file(HERE / "helper_v2.py")\nX = HERE / "test_hours_bound_v1.py"\n')
    assert data.candidate_filenames(tests, "hours_bound") == ["hours_bound.py", "hours_bound_v1.py", "helper_v2.py"]
    assert data.candidate_filenames(tests, "hours_bound", "hours_bound_v1.py") == \
        ["hours_bound.py", "hours_bound_v1.py", "helper_v2.py"]
    assert data.candidate_filenames("import pii_leak\n", "pii_leak", "pii_leak_v3.py") == ["pii_leak.py", "pii_leak_v3.py"]
    assert data.candidate_filenames("", "pii_leak") == ["pii_leak.py"]


def test_forge_style_tests_load_the_candidate_by_versioned_name():
    """Forge's TEST_TEMPLATE does load_file(HERE / "<skeleton>_v1.py") instead of importing."""
    code = (ROOT / "gen/forge/guardrails/price_match.py").read_text().replace("VERSION = 2", "VERSION = 77")
    tests = ("from pathlib import Path\nfrom gen.forge.runtime import load_file\n"
             "HERE = Path(__file__).resolve().parent\nMOD = load_file(HERE / 'price_match_v1.py')\n\n"
             "def test_loaded():\n    assert MOD.VERSION == 77\n")
    passed, output = data.run_guardrail_tests(code, tests)
    assert passed, output
    passed, output = data.run_guardrail_tests(code, tests.replace("price_match_v1", "price_match_v9"),
                                              module_file="price_match_v9.py")
    assert passed, output


def test_forge_corpus_sidecar_rides_along_with_the_tests(tmp_path):
    """Forge's generated tests read corpus.json from Path(__file__).parent; the UI must copy it."""
    write_result(tmp_path, "pii_leak", 1, True, code="x = 1")
    run_dir = tmp_path / "cache/forge/pii_leak/seed_1"
    (run_dir / "corpus.json").write_text(json.dumps({"spec": {}, "violating": [{"text": "ssn 123"}]}))
    (run_dir / "notes.txt").write_text("not a sidecar")
    assert [p.name for p in data.sidecar_files(run_dir)] == ["corpus.json"]
    assert data.sidecar_files(None) == [] and data.sidecar_files(tmp_path / "nope") == []
    art = data.forge_artifact(tmp_path, "pii_leak")
    for pkg in ("contracts", "gen"):                     # a fake repo root the subprocess can import from
        (tmp_path / pkg).symlink_to(ROOT / pkg, target_is_directory=True)
    code = "from contracts.policyspec import Skeleton\nRULE_SKELETON = Skeleton.PII_LEAK\nVERSION = 1\n"
    tests = ("import json\nfrom pathlib import Path\n"
             "CORPUS = json.loads((Path(__file__).resolve().parent / 'corpus.json').read_text())\n\n"
             "def test_corpus():\n    assert CORPUS['violating'][0]['text'] == 'ssn 123'\n")
    passed, output = data.run_guardrail_tests(code, tests, run_dir=art["run_dir"], root=tmp_path)
    assert passed, output
    passed, output = data.run_guardrail_tests(code, tests, run_dir=run_dir)         # absolute path too
    assert passed, output
    passed, output = data.run_guardrail_tests(code, tests)                           # without it: fails
    assert not passed and "corpus.json" in output
    assert sorted(p.name for p in run_dir.iterdir()) == ["corpus.json", "notes.txt", "result.json"]


def test_hanging_tests_time_out():
    code = "from contracts.policyspec import Skeleton\nRULE_SKELETON = Skeleton.PII_LEAK\nVERSION = 1\n"
    tests = "import time\n\ndef test_hang():\n    time.sleep(30)\n"
    passed, output = data.run_guardrail_tests(code, tests, timeout=2)
    assert not passed and "timed out" in output


# ---------------------------------------------------------------- swarm: five columns, hand marks
def mark(tid, turn, rule_id="r_price", span="x"):
    return {"transcript_id": tid, "turn": turn, "rule_id": rule_id, "span": span}


def test_load_marks_reads_hand_labels_or_empty(tmp_path):
    assert data.load_marks(tmp_path) == []
    (tmp_path / "data/dev").mkdir(parents=True)
    (tmp_path / "data/dev/marks.json").write_text(json.dumps([mark("t_1", 1)]))
    assert data.load_marks(tmp_path) == [mark("t_1", 1)]
    (tmp_path / "data/dev/marks.json").write_text("{not json")
    assert data.load_marks(tmp_path) == []
    repo = data.load_marks(ROOT)
    assert repo and set(repo[0]) == {"transcript_id", "turn", "rule_id", "span"}


def test_swarm_transcripts_one_per_class_preferring_marked():
    ts = [make_transcript("t_ben", attack=None),
          make_transcript("t_ref_a", attack="phantom_refund"),
          make_transcript("t_ref_b", attack="phantom_refund"),
          make_transcript("t_hrs_a", attack="outside_hours"),
          make_transcript("t_hrs_b", attack="outside_hours"),
          make_transcript("t_alg", attack="allergen_clearance")]
    marks = [mark("t_ref_b", 1), mark("t_ref_b", 3), mark("t_hrs_b", 1), mark("t_ben", 1)]
    picks = data.swarm_transcripts(ts, marks, k=5)
    assert [t.attack_class for t in picks] == ["allergen_clearance", "outside_hours", "phantom_refund"]
    assert [t.transcript_id for t in picks] == ["t_alg", "t_hrs_b", "t_ref_b"]   # marked ones win
    assert all(not t.benign for t in picks)
    assert [t.transcript_id for t in data.swarm_transcripts(ts, [], k=5)] == ["t_alg", "t_hrs_a", "t_ref_a"]
    assert len(data.swarm_transcripts(ts, marks, k=2)) == 2
    assert data.swarm_transcripts([], marks) == []
    assert data.swarm_transcripts(list(reversed(ts)), marks) == picks       # input order is irrelevant


def test_swarm_transcripts_on_repo_data_covers_the_five_attack_classes():
    marks = data.load_marks(ROOT)
    picks = data.swarm_transcripts(data.demo_transcripts(ROOT, "dev_leaky"), marks, k=5)
    assert len(picks) == 5 and len({t.attack_class for t in picks}) == 5
    shown = {t.transcript_id for t in picks}
    marked = {m["transcript_id"] for m in marks}
    # every class that has a hand-labelled transcript shows that one
    for t in picks:
        same_class = [x for x in data.demo_transcripts(ROOT, "dev_leaky") if x.attack_class == t.attack_class]
        if any(x.transcript_id in marked for x in same_class):
            assert t.transcript_id in marked
    assert sum(1 for m in marks if m["transcript_id"] in shown) == 9


def test_marks_by_turn_groups_one_transcript():
    marks = [mark("t_1", 3, "r_price"), mark("t_1", 3, "r_big_order"), mark("t_1", 5), mark("t_2", 1)]
    by_turn = data.marks_by_turn(marks, "t_1")
    assert sorted(by_turn) == [3, 5] and [m["rule_id"] for m in by_turn[3]] == ["r_price", "r_big_order"]
    assert data.marks_by_turn(marks, "t_9") == {}


def test_marks_for_filters_by_skeleton_rules(spec):
    marks = [mark("t_1", 1, "r_price"), mark("t_1", 3, "r_refund_cap"), mark("t_2", 5, "r_comp_cap"),
             mark("t_2", 1, "r_no_discount"), mark("t_3", 1, "r_unknown")]
    caps = data.marks_for(marks, spec, "commitment_cap")
    assert [(m["transcript_id"], m["rule_id"]) for m in caps] == [("t_1", "r_refund_cap"), ("t_2", "r_comp_cap")]
    assert [m["rule_id"] for m in data.marks_for(marks, spec, Skeleton.PRICE_MATCH)] == ["r_price"]
    assert data.marks_for(marks, spec, "pii_leak") == []                 # spec has no such rule
    assert data.marks_for(marks, spec, "forbidden_commitment")[0]["rule_id"] == "r_no_discount"


def test_exposure_summary_is_display_strings_of_tool_row_fields():
    run = make_run()
    s = data.exposure_summary(data.tool_row(run, "dev_leaky"))
    assert s == {"exposure": "$20,418/mo", "interval": "[$12,448, $20,418]",
                 "worst": 'r_refund_cap: "refund the full $96" ($86)', "guarded": "$20,418/mo"}
    assert data.exposure_summary(data.tool_row(run, "dev_cautious"))["worst"] == "none found"


# ---------------------------------------------------------------- swarm replay (gen.forge.replay)
def replay_payload(**over):
    body = {"skeleton": "forbidden_commitment", "tool_id": "dev_leaky", "n_bot_turns": 6,
            "n_flagged": 2, "n_rewritten": 2, "n_blocked": 0,
            "turns": [{"transcript_id": "t_1", "turn": 1, "action": "allow", "rule_id": None,
                       "evidence": "", "rewrite": None},
                      {"transcript_id": "t_1", "turn": 3, "action": "rewrite", "rule_id": "r_no_discount",
                       "evidence": "free item", "rewrite": "No complimentary items, sorry."},
                      {"transcript_id": "t_2", "turn": 5, "action": "block", "rule_id": "r_no_discount",
                       "evidence": "discount", "rewrite": None}]}
    body.update(over)
    return body


def test_parse_replay_accepts_the_contract_and_rejects_missing_keys():
    out = data.parse_replay(replay_payload())
    assert out["n_flagged"] == 2 and len(out["turns"]) == 3 and out["turns"][1]["action"] == "rewrite"
    for key in ("skeleton", "tool_id", "n_bot_turns", "n_flagged", "n_rewritten", "n_blocked", "turns"):
        bad = replay_payload()
        del bad[key]
        with pytest.raises(ValueError, match=key):
            data.parse_replay(bad)
    with pytest.raises(ValueError, match="action"):
        data.parse_replay(replay_payload(turns=[{"transcript_id": "t", "turn": 1, "action": "nuke",
                                                 "rule_id": None, "evidence": "", "rewrite": None}]))
    with pytest.raises(ValueError, match="evidence"):
        data.parse_replay(replay_payload(turns=[{"transcript_id": "t", "turn": 1, "action": "allow"}]))
    with pytest.raises(ValueError):
        data.parse_replay(["not", "a", "dict"])
    with pytest.raises(ValueError, match="n_flagged"):
        data.parse_replay(replay_payload(n_flagged="two"))


def test_replay_by_turn_indexes_changed_turns_only():
    idx = data.replay_by_turn(data.parse_replay(replay_payload()))
    assert set(idx) == {("t_1", 3), ("t_2", 5)}
    assert idx[("t_1", 3)]["rewrite"] == "No complimentary items, sorry."


def test_violations_remaining_counts_marks_still_allowed():
    turns = data.parse_replay(replay_payload())["turns"]
    marks = [mark("t_1", 1, "r_no_discount"),     # replay allowed it: still a violation
             mark("t_1", 3, "r_no_discount"),     # rewritten: fixed
             mark("t_2", 5, "r_no_discount"),     # blocked: fixed
             mark("t_3", 1, "r_no_discount")]     # the replay never saw this turn: not fixed
    assert data.violations_remaining(marks, turns) == (2, 4)
    assert data.violations_remaining([], turns) == (0, 0)
    assert data.violations_remaining(marks, []) == (4, 4)


def test_run_replay_reports_missing_module(tmp_path):
    result, output = data.run_replay("CODE", skeleton="price_match", transcripts_dir=tmp_path,
                                     spec_path=tmp_path / "spec.json", tool_id="dev_leaky", root=tmp_path)
    assert result is None and output == data.REPLAY_UNAVAILABLE


def fake_replay_module(root: Path):
    (root / "gen/forge").mkdir(parents=True, exist_ok=True)
    (root / "gen/forge/replay.py").write_text("# stand-in for the other engineer's module\n")


def test_run_replay_builds_the_contract_command_and_parses_output(tmp_path, monkeypatch):
    fake_replay_module(tmp_path)
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        args = dict(zip(cmd[3::2], cmd[4::2]))
        assert Path(args["--candidate"]).read_text() == "CANDIDATE CODE"
        assert Path(args["--candidate"]).name == "forbidden_commitment.py"
        Path(args["--out"]).write_text(json.dumps(replay_payload()))
        return subprocess.CompletedProcess(cmd, 0, stdout="replayed 6 turns\n", stderr="")

    monkeypatch.setattr(data.subprocess, "run", fake_run)
    result, output = data.run_replay("CANDIDATE CODE", skeleton="forbidden_commitment",
                                     transcripts_dir=tmp_path / "tr", spec_path=tmp_path / "spec.json",
                                     tool_id="dev_leaky", root=tmp_path, timeout=7)
    assert result is not None and result["n_rewritten"] == 2 and "replayed 6 turns" in output
    cmd = seen["cmd"]
    assert cmd[:3] == [sys.executable, "-m", "gen.forge.replay"]
    args = dict(zip(cmd[3::2], cmd[4::2]))
    assert args["--skeleton"] == "forbidden_commitment" and args["--tool"] == "dev_leaky"
    assert args["--transcripts"] == str(tmp_path / "tr") and args["--spec"] == str(tmp_path / "spec.json")
    kw = seen["kw"]
    assert kw["cwd"] == str(tmp_path) and kw["timeout"] == 7
    assert kw["env"]["PYTHONPATH"] == str(tmp_path) and kw["env"]["CACHE_ONLY"] == "1"
    assert not Path(args["--candidate"]).exists()                      # temp dir cleaned up


def test_run_replay_surfaces_load_errors_timeouts_and_garbage(tmp_path, monkeypatch):
    fake_replay_module(tmp_path)
    common = dict(skeleton="price_match", transcripts_dir=tmp_path, spec_path=tmp_path / "s.json",
                  tool_id="dev_leaky", root=tmp_path)

    def failing(cmd, **kw):
        args = dict(zip(cmd[3::2], cmd[4::2]))
        Path(args["--out"]).write_text(json.dumps({"error": "import os not allowed"}))
        return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="")

    monkeypatch.setattr(data.subprocess, "run", failing)
    result, output = data.run_replay("CODE", **common)
    assert result is None and "import os not allowed" in output and "exit 2" in output

    def error_on_stdout(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 2, stdout=json.dumps({"error": "bad candidate"}), stderr="")

    monkeypatch.setattr(data.subprocess, "run", error_on_stdout)
    result, output = data.run_replay("CODE", **common)
    assert result is None and "bad candidate" in output

    def hangs(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"], output=b"partial")

    monkeypatch.setattr(data.subprocess, "run", hangs)
    result, output = data.run_replay("CODE", timeout=3, **common)
    assert result is None and "timed out after 3s" in output and "partial" in output

    def garbage(cmd, **kw):
        args = dict(zip(cmd[3::2], cmd[4::2]))
        Path(args["--out"]).write_text("{not json")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(data.subprocess, "run", garbage)
    result, output = data.run_replay("CODE", **common)
    assert result is None and "invalid JSON" in output

    def malformed(cmd, **kw):
        args = dict(zip(cmd[3::2], cmd[4::2]))
        Path(args["--out"]).write_text(json.dumps({"skeleton": "price_match"}))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(data.subprocess, "run", malformed)
    result, output = data.run_replay("CODE", **common)
    assert result is None and "malformed" in output and "tool_id" in output

    def silent(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Traceback: boom")

    monkeypatch.setattr(data.subprocess, "run", silent)
    result, output = data.run_replay("CODE", **common)
    assert result is None and "exited 1" in output and "boom" in output


# ---------------------------------------------------------------- staff channel (simulated)
def test_staff_reactions_are_canned_labelled_and_skeleton_aware():
    lines = data.staff_reactions("forbidden_commitment", "dev_leaky")
    assert 3 <= len(lines) <= 4 and all(": " in line for line in lines)
    assert "croissant" in lines[0] and any("dev_leaky" in line for line in lines)
    assert any("shadow" in line for line in lines)
    assert lines == data.staff_reactions("forbidden_commitment", "dev_leaky")      # deterministic
    other = data.staff_reactions("pii_leak", "dev_local")
    assert 3 <= len(other) <= 4 and other[0] != lines[0]
