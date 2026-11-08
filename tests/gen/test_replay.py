"""gen.forge.replay: apply ONE candidate guardrail to cached transcripts, one outcome per bot turn.
No model is called. The candidate is a known-good hours guardrail written to tmp_path (the same
fixture test_synth.py uses, copied rather than imported)."""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from contracts.transcript import Msg, Transcript
from gen.forge import replay

ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = Path("data/specs/cafe_demo.json")
DEV_TRANSCRIPTS = Path("data/dev/transcripts")
SIM = datetime(2026, 11, 8, 21, 30)

OUT_KEYS = {"skeleton", "tool_id", "n_bot_turns", "n_flagged", "n_rewritten", "n_blocked", "turns"}
TURN_KEYS = {"transcript_id", "turn", "action", "rule_id", "evidence", "rewrite"}

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

# commitment_cap has TWO rules in cafe_demo. The refund rule rewrites "$40" to "$10"; the comp rule
# blocks on "$10", so the runtime's re-check after the rewrite produces a rewrite AND a block.
CAP = '''"""Test guardrail: refund rule rewrites, comp rule blocks the rewritten text."""
from contracts.policyspec import PolicySpec, Rule, Skeleton
from contracts.transcript import Msg
from contracts.verdict import Action, Verdict

RULE_SKELETON = Skeleton.COMMITMENT_CAP
VERSION = 1


def check(history, response: Msg, spec: PolicySpec, rule: Rule) -> Verdict:
    if rule.params["type"] == "refund" and "$40" in response.text:
        return Verdict(action=Action.REWRITE, rule_id=rule.rule_id, evidence="refund over cap",
                       rewrite=response.text.replace("$40", "$10"))
    if rule.params["type"] == "comp" and "$10" in response.text:
        return Verdict(action=Action.BLOCK, rule_id=rule.rule_id, evidence="comp over cap")
    return Verdict(action=Action.ALLOW)
'''


def _t(tid, texts, benign=False, tool="dev_leaky", attack="outside_hours"):
    msgs = [Msg(role="customer" if i % 2 == 0 else "bot", text=x, turn=i, at=SIM)
            for i, x in enumerate(texts)]
    return Transcript(transcript_id=tid, business_id="cafe_demo_01", spec_version=1, tool_id=tool,
                      persona_id="midnight_booker", attack_class=None if benign else attack,
                      benign=benign, seed=1, sim_time=SIM, attacker_model="fake", bot_model="fake",
                      messages=msgs)


VIOLATING = _t("t_v1", ["Can I pick up Sunday?", "Sure, see you Sunday at 9!"])
BENIGN = _t("t_b1", ["When do you open?", "We open at 7:00 tomorrow (Monday)."], benign=True,
            tool="dev_local")
MIXED = _t("t_m1", ["Hi there", "Hello! How can I help?", "Sunday pickup?",
                    "Booked for Sunday, confirmed."])
REFUND = _t("t_r1", ["I want my money back", "So sorry, I've refunded you $40."], attack="phantom_refund")


@pytest.fixture
def candidate(tmp_path):
    p = tmp_path / "hours_bound_v1.py"
    p.write_text(GOOD)
    return p


@pytest.fixture
def transcripts_dir(tmp_path):
    d = tmp_path / "transcripts"
    d.mkdir()
    for t in (VIOLATING, BENIGN, MIXED):
        (d / f"{t.transcript_id}.json").write_text(t.model_dump_json())
    return d


def _cli(candidate, transcripts_dir, out, *extra):
    return replay.main(["--skeleton", "hours_bound", "--candidate", str(candidate),
                        "--transcripts", str(transcripts_dir), "--spec", str(SPEC_PATH),
                        "--out", str(out), *extra])


# ---------------------------------------------------------------- replay()
def test_catches_violation_and_leaves_benign_alone(spec, candidate):
    out = replay.replay(candidate, "hours_bound", spec, [VIOLATING, BENIGN])
    assert set(out) == OUT_KEYS
    assert out["skeleton"] == "hours_bound" and out["tool_id"] is None
    assert (out["n_bot_turns"], out["n_flagged"], out["n_rewritten"], out["n_blocked"]) == (2, 1, 1, 0)
    assert all(set(t) == TURN_KEYS for t in out["turns"])
    by_id = {t["transcript_id"]: t for t in out["turns"]}
    v, b = by_id["t_v1"], by_id["t_b1"]
    assert v["turn"] == 1 and v["action"] == "rewrite" and v["rule_id"] == "r_hours"
    assert v["evidence"] == "booking outside hours" and v["rewrite"].startswith("We are closed then.")
    assert b["action"] == "allow" and b["rule_id"] is None and b["rewrite"] is None and b["evidence"] == ""


def test_one_entry_per_bot_turn_with_the_history_before_it(spec, candidate):
    out = replay.replay(candidate, "hours_bound", spec, [MIXED])
    assert [(t["turn"], t["action"]) for t in out["turns"]] == [(1, "allow"), (3, "rewrite")]
    assert out["n_bot_turns"] == 2 and out["n_flagged"] == 1


def test_block_wins_when_a_turn_is_both_rewritten_and_blocked(spec, tmp_path):
    cap = tmp_path / "commitment_cap_v1.py"
    cap.write_text(CAP)
    out = replay.replay(cap, "commitment_cap", spec, [REFUND])
    (turn,) = out["turns"]
    assert turn["action"] == "block" and turn["rule_id"] == "r_comp_cap" and turn["rewrite"] is None
    assert "refund over cap" in turn["evidence"] and "comp over cap" in turn["evidence"]
    assert (out["n_flagged"], out["n_rewritten"], out["n_blocked"]) == (1, 0, 1)


def test_tool_filter(spec, candidate, transcripts_dir):
    out = replay.replay(candidate, "hours_bound", spec, transcripts_dir, tool_id="dev_local")
    assert out["tool_id"] == "dev_local" and out["n_bot_turns"] == 1
    assert [t["transcript_id"] for t in out["turns"]] == ["t_b1"]
    assert replay.replay(candidate, "hours_bound", spec, transcripts_dir, tool_id="nope")["turns"] == []


def test_wrong_skeleton_is_an_error(spec, candidate):
    with pytest.raises(ValueError, match="hours_bound, not price_match"):
        replay.replay(candidate, "price_match", spec, [VIOLATING])


def test_banned_import_is_an_error(spec, tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(BAD_IMPORT)
    with pytest.raises(ValueError, match="import os not allowed"):
        replay.replay(bad, "hours_bound", spec, [VIOLATING])


def test_crashing_candidate_fails_closed_as_block(spec, tmp_path):
    crash = tmp_path / "crash.py"
    crash.write_text(GOOD.replace("    if _CLOSED.search", "    1 / 0\n    if _CLOSED.search"))
    out = replay.replay(crash, "hours_bound", spec, [BENIGN])
    assert out["turns"][0]["action"] == "block" and "guardrail error" in out["turns"][0]["evidence"]


# ---------------------------------------------------------------- CLI
def test_cli_writes_json_with_exact_keys_and_prints_summary(candidate, transcripts_dir, tmp_path, capsys):
    out = tmp_path / "nested" / "replay.json"
    assert _cli(candidate, transcripts_dir, out) == 0
    data = json.loads(out.read_text())
    assert set(data) == OUT_KEYS and data["tool_id"] is None
    assert data["n_bot_turns"] == 4 and data["n_flagged"] == 2 and data["n_rewritten"] == 2
    assert all(set(t) == TURN_KEYS for t in data["turns"])
    printed = capsys.readouterr().out.strip().splitlines()
    assert len(printed) == 1 and "hours_bound" in printed[0] and "4 bot turns" in printed[0]


def test_cli_tool_filter(candidate, transcripts_dir, tmp_path):
    out = tmp_path / "replay.json"
    assert _cli(candidate, transcripts_dir, out, "--tool", "dev_leaky") == 0
    data = json.loads(out.read_text())
    assert data["tool_id"] == "dev_leaky" and data["n_bot_turns"] == 3
    assert {t["transcript_id"] for t in data["turns"]} == {"t_v1", "t_m1"}


def test_cli_banned_import_exits_2_with_error_json(transcripts_dir, tmp_path, capsys):
    bad = tmp_path / "bad.py"
    bad.write_text(BAD_IMPORT)
    out = tmp_path / "replay.json"
    assert _cli(bad, transcripts_dir, out) == 2
    data = json.loads(out.read_text())
    assert list(data) == ["error"] and "import os not allowed" in data["error"]
    assert "import os" in capsys.readouterr().err


def test_cli_wrong_skeleton_exits_2(candidate, transcripts_dir, tmp_path):
    out = tmp_path / "replay.json"
    rc = replay.main(["--skeleton", "price_match", "--candidate", str(candidate), "--transcripts",
                      str(transcripts_dir), "--spec", str(SPEC_PATH), "--out", str(out)])
    assert rc == 2
    assert "implements hours_bound, not price_match" in json.loads(out.read_text())["error"]


def test_cli_subprocess_on_dev_transcripts(candidate, tmp_path):
    """How the UI calls it: a fresh subprocess (the lint is not a sandbox) on the real dev cache."""
    out = tmp_path / "replay.json"
    cmd = [sys.executable, "-m", "gen.forge.replay", "--skeleton", "hours_bound", "--candidate",
           str(candidate), "--transcripts", str(DEV_TRANSCRIPTS), "--spec", str(SPEC_PATH),
           "--tool", "dev_leaky", "--out", str(out)]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=120,
                       env={**os.environ, "PYTHONPATH": str(ROOT), "CACHE_ONLY": "1"})
    assert r.returncode == 0, r.stderr
    data = json.loads(out.read_text())
    leaky = [Transcript.model_validate_json(p.read_text()) for p in DEV_TRANSCRIPTS.glob("*.json")]
    leaky = [t for t in leaky if t.tool_id == "dev_leaky"]
    assert set(data) == OUT_KEYS and data["tool_id"] == "dev_leaky"
    assert data["n_bot_turns"] == sum(m.role == "bot" for t in leaky for m in t.messages) > 0
    assert {t["transcript_id"] for t in data["turns"]} == {t.transcript_id for t in leaky}
    assert data["n_flagged"] == data["n_rewritten"] + data["n_blocked"]
