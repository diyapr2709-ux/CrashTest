"""gen.forge.promote: the human promotion step (copy the green candidate + edit active.json), checked.
Every test works on a tmp COPY of gen/forge/guardrails; the real directory must be byte-identical
before and after this module runs."""
import json
import shutil

import pytest

from contracts.policyspec import Skeleton
from gen.forge import promote, runtime

REAL_ACTIVE = runtime.GUARD_DIR / "active.json"
REAL_BEFORE = REAL_ACTIVE.read_bytes()
PM = {"file": "price_match", "mode": "enforce"}

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


@pytest.fixture(autouse=True, scope="module")
def real_guardrails_untouched():
    before = {p.name: p.read_bytes() for p in runtime.GUARD_DIR.iterdir() if p.is_file()}
    yield
    after = {p.name: p.read_bytes() for p in runtime.GUARD_DIR.iterdir() if p.is_file()}
    assert after == before, "a promote test touched the real gen/forge/guardrails"


@pytest.fixture
def guard_dir(tmp_path):
    d = tmp_path / "guardrails"
    d.mkdir()
    shutil.copy(runtime.GUARD_DIR / "price_match.py", d / "price_match.py")
    (d / "active.json").write_text(json.dumps({"price_match": PM}))
    return d


def _result(forge_dir, skeleton, seed, passed, code=GOOD, iterations=1):
    run_dir = forge_dir / skeleton / f"seed_{seed}"
    run_dir.mkdir(parents=True)
    code_path = run_dir / f"{skeleton}_v{seed}.py"
    code_path.write_text(code)
    res = {"skeleton": skeleton, "seed": seed, "model": "fake", "iterations": iterations,
           "passed": passed, "code": code, "tests": "# tests", "problems": ["" if passed else "red"],
           "code_path": str(code_path), "run_dir": str(run_dir), "prompt_version": 2}
    (run_dir / "result.json").write_text(json.dumps(res))
    return run_dir


@pytest.fixture
def forge_dir(tmp_path):
    d = tmp_path / "forge"
    _result(d, "hours_bound", 3, True)
    _result(d, "hours_bound", 4, False, iterations=3)
    return d


def _active(guard_dir):
    return json.loads((guard_dir / "active.json").read_text())


def _snapshot(guard_dir):
    return {p.name: p.read_bytes() for p in guard_dir.iterdir()}


# ---------------------------------------------------------------- promote()
def test_green_result_is_copied_and_pointed_to_in_shadow(forge_dir, guard_dir):
    info = promote.promote("hours_bound", 3, forge_dir=forge_dir, guard_dir=guard_dir)
    assert (guard_dir / "hours_bound_v3.py").read_text() == GOOD
    active = _active(guard_dir)
    assert list(active) == ["price_match", "hours_bound"] and active["price_match"] == PM
    assert active["hours_bound"] == {"file": "hours_bound_v3", "mode": "shadow"}
    assert info["before"] is None and info["after"] == {"file": "hours_bound_v3", "mode": "shadow"}
    assert (guard_dir / "active.json").read_text().count("\n") > 2        # pretty-printed
    reg = runtime.load(guard_dir)
    assert set(reg.modules) == {Skeleton.PRICE_MATCH, Skeleton.HOURS_BOUND} and not reg.errors
    assert reg.modes == {Skeleton.PRICE_MATCH: "enforce", Skeleton.HOURS_BOUND: "shadow"}


def test_enforce_only_when_asked(forge_dir, guard_dir):
    promote.promote("hours_bound", 3, mode="enforce", forge_dir=forge_dir, guard_dir=guard_dir)
    assert _active(guard_dir)["hours_bound"]["mode"] == "enforce"
    with pytest.raises(promote.PromoteError, match="mode"):
        promote.promote("hours_bound", 3, mode="loud", forge_dir=forge_dir, guard_dir=guard_dir, yes=True)


def test_existing_pointer_is_replaced_in_place_keeping_key_order(forge_dir, guard_dir):
    (guard_dir / "active.json").write_text(json.dumps(
        {"hours_bound": {"file": "hours_bound_v1", "mode": "enforce"}, "price_match": PM}))
    info = promote.promote("hours_bound", 3, forge_dir=forge_dir, guard_dir=guard_dir)
    assert info["before"] == {"file": "hours_bound_v1", "mode": "enforce"}
    active = _active(guard_dir)
    assert list(active) == ["hours_bound", "price_match"]
    assert active["hours_bound"] == {"file": "hours_bound_v3", "mode": "shadow"}


def test_red_result_is_refused(forge_dir, guard_dir):
    before = _snapshot(guard_dir)
    with pytest.raises(promote.PromoteError, match="RED"):
        promote.promote("hours_bound", 4, forge_dir=forge_dir, guard_dir=guard_dir)
    assert _snapshot(guard_dir) == before


def test_missing_result_is_refused(forge_dir, guard_dir):
    with pytest.raises(promote.PromoteError, match="no Forge result"):
        promote.promote("hours_bound", 9, forge_dir=forge_dir, guard_dir=guard_dir)
    with pytest.raises(promote.PromoteError, match="no Forge result"):
        promote.promote("allergen_claim", 3, forge_dir=forge_dir, guard_dir=guard_dir)


def test_existing_destination_is_refused_without_yes(forge_dir, guard_dir):
    (guard_dir / "hours_bound_v3.py").write_text("# old\n")
    with pytest.raises(promote.PromoteError, match="--yes"):
        promote.promote("hours_bound", 3, forge_dir=forge_dir, guard_dir=guard_dir)
    assert (guard_dir / "hours_bound_v3.py").read_text() == "# old\n" and _active(guard_dir) == {"price_match": PM}
    promote.promote("hours_bound", 3, forge_dir=forge_dir, guard_dir=guard_dir, yes=True)
    assert (guard_dir / "hours_bound_v3.py").read_text() == GOOD


def test_dry_run_writes_nothing(forge_dir, guard_dir, capsys):
    before = _snapshot(guard_dir)
    info = promote.promote("hours_bound", 3, forge_dir=forge_dir, guard_dir=guard_dir, dry_run=True)
    assert _snapshot(guard_dir) == before and info["dry_run"]
    assert info["after"] == {"file": "hours_bound_v3", "mode": "shadow"}
    out = capsys.readouterr().out
    assert "dry run" in out and "hours_bound_v3" in out


def test_lint_failure_is_refused_before_anything_is_written(tmp_path, guard_dir):
    forge = tmp_path / "forge_bad"
    _result(forge, "hours_bound", 5, True, code="import os\n" + GOOD)
    before = _snapshot(guard_dir)
    with pytest.raises(promote.PromoteError, match="import os not allowed"):
        promote.promote("hours_bound", 5, forge_dir=forge, guard_dir=guard_dir)
    assert _snapshot(guard_dir) == before


def test_code_that_does_not_load_is_refused(tmp_path, guard_dir):
    forge = tmp_path / "forge_bad"
    _result(forge, "hours_bound", 6, True, code=GOOD.replace("VERSION = 1", "VERSION = 0"))
    with pytest.raises(promote.PromoteError, match="VERSION"):
        promote.promote("hours_bound", 6, forge_dir=forge, guard_dir=guard_dir)
    assert not (guard_dir / "hours_bound_v6.py").exists()


def test_code_declaring_another_skeleton_is_refused(tmp_path, guard_dir):
    forge = tmp_path / "forge_bad"
    _result(forge, "price_match", 7, True, code=GOOD)          # GOOD declares HOURS_BOUND
    with pytest.raises(promote.PromoteError, match="hours_bound, not price_match"):
        promote.promote("price_match", 7, forge_dir=forge, guard_dir=guard_dir)
    assert not (guard_dir / "price_match_v7.py").exists() and _active(guard_dir) == {"price_match": PM}


# ---------------------------------------------------------------- demote()
def test_demote_removes_the_pointer_and_keeps_the_file(forge_dir, guard_dir):
    promote.promote("hours_bound", 3, forge_dir=forge_dir, guard_dir=guard_dir)
    info = promote.demote("hours_bound", guard_dir=guard_dir)
    assert _active(guard_dir) == {"price_match": PM} and (guard_dir / "hours_bound_v3.py").exists()
    assert info["before"] == {"file": "hours_bound_v3", "mode": "shadow"} and info["after"] is None
    assert list(runtime.load(guard_dir).modules) == [Skeleton.PRICE_MATCH]


def test_demote_without_a_pointer_is_refused(guard_dir):
    with pytest.raises(promote.PromoteError, match="no pointer"):
        promote.demote("hours_bound", guard_dir=guard_dir)


def test_demote_dry_run_writes_nothing(forge_dir, guard_dir):
    promote.promote("hours_bound", 3, forge_dir=forge_dir, guard_dir=guard_dir)
    before = _snapshot(guard_dir)
    promote.demote("hours_bound", guard_dir=guard_dir, dry_run=True)
    assert _snapshot(guard_dir) == before


# ---------------------------------------------------------------- CLI
def test_cli_promote_then_demote(forge_dir, guard_dir, capsys):
    common = ["--skeleton", "hours_bound", "--forge-dir", str(forge_dir), "--guard-dir", str(guard_dir)]
    assert promote.main(common + ["--seed", "3"]) == 0
    out = capsys.readouterr().out
    assert "(none)" in out and "hours_bound_v3" in out and "shadow" in out
    assert _active(guard_dir)["hours_bound"] == {"file": "hours_bound_v3", "mode": "shadow"}

    assert promote.main(common + ["--seed", "4"]) == 1
    assert "RED" in capsys.readouterr().err

    assert promote.main(common + ["--seed", "3"]) == 1                     # exists, no --yes
    assert "--yes" in capsys.readouterr().err
    assert promote.main(common + ["--seed", "3", "--mode", "enforce", "--yes"]) == 0
    assert _active(guard_dir)["hours_bound"]["mode"] == "enforce"

    assert promote.main(["--skeleton", "hours_bound", "--demote", "--guard-dir", str(guard_dir)]) == 0
    assert _active(guard_dir) == {"price_match": PM} and (guard_dir / "hours_bound_v3.py").exists()


def test_cli_needs_seed_unless_demoting(guard_dir):
    with pytest.raises(SystemExit):
        promote.main(["--skeleton", "hours_bound", "--guard-dir", str(guard_dir)])


def test_default_guard_dir_is_the_runtime_dir_and_default_mode_is_shadow():
    assert promote.DEFAULT_GUARD_DIR == runtime.GUARD_DIR and promote.DEFAULT_MODE == "shadow"


def test_real_active_json_is_byte_identical():
    assert REAL_ACTIVE.read_bytes() == REAL_BEFORE
