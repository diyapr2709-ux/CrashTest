"""gen.demo.curate: the curated demo layer and its provenance manifest, built from hand-made
transcripts, marks, specs, a fake cache/forge tree and a fake metrics dir on tmp dirs. Never
touches the real cache/demo; no model is called."""
import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from gen.demo import curate as cur
from gen.forge.report import source_label

ROOT = Path(__file__).resolve().parents[2]
TOP_KEYS = ["created_at", "git_sha", "specs", "transcripts", "marks", "forge", "metrics"]
TRANSCRIPT_KEYS = {"count", "by_tool", "attack", "benign", "attacker_models", "bot_models", "seeds"}
SPEC_KEYS = {"path", "version", "schema_version", "n_rules"}
FORGE_KEYS = {"source", "skeleton", "seed", "prompt_version", "iterations", "passed"}
METRICS_KEYS = {"latest", "run_id", "git_sha", "split", "files"}
SHA_FILES = {"0123456789abcdef0123456789abcdef.json": b'{"text": "cached a"}',
             "fedcba9876543210fedcba9876543210.json": b'{"text": "cached b"}'}


def _transcript(tid, tool, benign, seed=1, bot="bot:2b", business_id="cafe_demo_01"):
    return {"transcript_id": tid, "business_id": business_id, "spec_version": 1, "tool_id": tool,
            "persona_id": "regular", "attack_class": None if benign else "invented_discount",
            "benign": benign, "seed": seed, "sim_time": "2026-11-08T09:00:00",
            "attacker_model": "att:1b", "bot_model": bot, "git_sha": "abc1234",
            "messages": [{"role": "customer", "text": "any deals?", "turn": 0,
                          "at": "2026-11-08T09:00:00"},
                         {"role": "bot", "text": "no deals, sorry", "turn": 1,
                          "at": "2026-11-08T09:00:45"}]}


def _forge_run(d, skeleton, seed, passed, iterations, prompt_version=None):
    run_dir = d / skeleton / f"seed_{seed}"
    run_dir.mkdir(parents=True)
    res = {"skeleton": skeleton, "seed": seed, "model": "fake", "iterations": iterations,
           "passed": passed, "code": "x = 1\n", "tests": "# tests", "problems": []}
    if prompt_version is not None:
        res["prompt_version"] = prompt_version
    (run_dir / "result.json").write_text(json.dumps(res))


@pytest.fixture
def tree(tmp_path):
    src = tmp_path / "transcripts"
    src.mkdir()
    (src / "t_a1.json").write_text(json.dumps(_transcript("t_a1", "tool_a", False), indent=1))
    (src / "t_a2.json").write_text(json.dumps(_transcript("t_a2", "tool_a", True, seed=2)))
    # file name differs from the id: the demo layer names it by transcript_id
    (src / "src_b1.json").write_text(json.dumps(_transcript("t_b1", "tool_b", False, bot="bot:9b")))
    bad = _transcript("t_bad", "tool_a", True)
    bad["attack_class"] = "invented_discount"            # benign with an attack class: invalid
    (src / "t_bad.json").write_text(json.dumps(bad))

    marks = tmp_path / "marks.json"
    marks.write_text(json.dumps([
        {"transcript_id": "t_a1", "turn": 1, "rule_id": "r_price", "span": "no deals"},
        {"transcript_id": "t_a1", "turn": 1, "rule_id": "r_no_discount", "span": "sorry"},
        {"transcript_id": "t_b1", "turn": 1, "rule_id": "r_price", "span": "no deals"},
        {"transcript_id": "t_gone", "turn": 1, "rule_id": "r_price", "span": "x"},  # not curated
        {"note": "not a mark"},
    ]))

    specs = tmp_path / "specs"
    specs.mkdir()
    shutil.copy(ROOT / "data/specs/cafe_demo.json", specs / "cafe_demo.json")
    (specs / "compiled_broken.json").write_text(json.dumps({"business_id": "nope"}))

    forge_a, forge_b = tmp_path / "cache" / "forge", tmp_path / "cache" / "forge_prompt_v1"
    _forge_run(forge_a, "hours_bound", 1, False, 3, prompt_version=4)
    _forge_run(forge_b, "forbidden_commitment", 2, True, 2)            # v1 results lack the key
    (forge_a / "batch_summary.csv").write_text("skeleton,seed\n")      # ignored

    metrics = tmp_path / "metrics"
    metrics.mkdir()
    shutil.copy(ROOT / "metrics/smoke_fixtures.json", metrics / "latest.json")

    out = tmp_path / "out"
    out.mkdir()
    for name, data in SHA_FILES.items():
        (out / name).write_bytes(data)
    return {"transcripts": src, "marks": marks, "specs": specs, "forge_dirs": [forge_a, forge_b],
            "metrics": metrics, "out": out}


def _snapshot(d: Path) -> dict:
    return {str(p.relative_to(d)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(d.rglob("*")) if p.is_file()}


def _no_time(m: dict) -> dict:
    return {k: v for k, v in m.items() if k != "created_at"}


def test_manifest_keys_exactly_as_specified(tree):
    m = cur.curate(**tree)
    assert list(m) == TOP_KEYS
    assert set(m["transcripts"]) == TRANSCRIPT_KEYS
    assert set(m["marks"]) == {"count", "by_rule"}
    assert set(m["metrics"]) == METRICS_KEYS
    assert all(set(s) == SPEC_KEYS for s in m["specs"].values())
    assert m["forge"] and all(set(r) == FORGE_KEYS for r in m["forge"])
    assert isinstance(m["git_sha"], str) and m["git_sha"]
    created = datetime.fromisoformat(m["created_at"])
    assert created.utcoffset() == timedelta(0)
    on_disk = json.loads((tree["out"] / "manifest.json").read_text())
    assert on_disk == m and list(on_disk) == TOP_KEYS


def test_counts_and_provenance(tree):
    m = cur.curate(**tree)
    assert m["transcripts"] == {"count": 3, "by_tool": {"tool_a": 2, "tool_b": 1}, "attack": 2,
                                "benign": 1, "attacker_models": ["att:1b"],
                                "bot_models": ["bot:2b", "bot:9b"], "seeds": [1, 2]}
    assert m["marks"] == {"count": 3, "by_rule": {"r_no_discount": 1, "r_price": 2}}
    spec = json.loads((tree["specs"] / "cafe_demo.json").read_text())
    assert m["specs"] == {spec["business_id"]: {
        "path": source_label(tree["specs"] / "cafe_demo.json"), "version": spec["version"],
        "schema_version": spec["schema_version"], "n_rules": len(spec["rules"])}}
    a, b = (source_label(d) for d in tree["forge_dirs"])
    assert sorted(m["forge"], key=lambda r: r["source"]) == [
        {"source": a, "skeleton": "hours_bound", "seed": 1, "prompt_version": 4,
         "iterations": 3, "passed": False},
        {"source": b, "skeleton": "forbidden_commitment", "seed": 2, "prompt_version": 1,
         "iterations": 2, "passed": True}]
    smoke = json.loads((ROOT / "metrics/smoke_fixtures.json").read_text())
    latest = source_label(tree["metrics"] / "latest.json")
    assert m["metrics"] == {"latest": latest, "run_id": smoke["run_id"],
                            "git_sha": smoke["git_sha"], "split": smoke["split"],
                            "files": [latest]}


def test_layer_contents_match_the_manifest(tree):
    cur.curate(**tree)
    out = tree["out"]
    names = sorted(p.name for p in (out / "transcripts").iterdir())
    assert names == ["t_a1.json", "t_a2.json", "t_b1.json"]
    assert (out / "transcripts/t_b1.json").read_bytes() == (tree["transcripts"] / "src_b1.json").read_bytes()
    assert (out / "transcripts/t_a1.json").read_bytes() == (tree["transcripts"] / "t_a1.json").read_bytes()
    marks = json.loads((out / "marks.json").read_text())
    assert [(k["transcript_id"], k["rule_id"]) for k in marks] == [
        ("t_a1", "r_price"), ("t_a1", "r_no_discount"), ("t_b1", "r_price")]
    assert not any(p.name.startswith("t_") for p in out.iterdir())   # nothing forge/transcript at root


def test_tools_filter(tree):
    m = cur.curate(**tree, tools=["tool_b"])
    assert m["transcripts"]["count"] == 1 and m["transcripts"]["by_tool"] == {"tool_b": 1}
    assert (m["transcripts"]["attack"], m["transcripts"]["benign"]) == (1, 0)
    assert m["marks"] == {"count": 1, "by_rule": {"r_price": 1}}
    assert sorted(p.name for p in (tree["out"] / "transcripts").iterdir()) == ["t_b1.json"]


def test_rerun_with_filter_prunes_stale_transcripts(tree, capsys):
    cur.curate(**tree)
    capsys.readouterr()
    cur.curate(**tree, tools=["tool_a"])
    assert "removed 1 stale" in capsys.readouterr().out
    assert sorted(p.name for p in (tree["out"] / "transcripts").iterdir()) == ["t_a1.json", "t_a2.json"]


def test_invalid_transcript_skipped_with_warning(tree, capsys):
    m = cur.curate(**tree)
    err = capsys.readouterr().err
    assert "t_bad.json" in err and "not a valid Transcript" in err
    assert "compiled_broken.json" in err                          # invalid spec skipped, named
    assert not (tree["out"] / "transcripts/t_bad.json").exists()
    assert m["transcripts"]["count"] == 3


def test_dry_run_writes_nothing(tree, capsys):
    before = {k: _snapshot(tree[k]) for k in ("out", "transcripts", "specs", "metrics")}
    m = cur.curate(**tree, dry_run=True)
    after = {k: _snapshot(tree[k]) for k in ("out", "transcripts", "specs", "metrics")}
    assert before == after
    assert sorted(p.name for p in tree["out"].iterdir()) == sorted(SHA_FILES)
    out = capsys.readouterr().out
    printed, last = out.rsplit("\n", 2)[0], out.strip().splitlines()[-1]
    assert json.loads(printed) == m
    assert last.startswith("dry run")


def test_sha_cache_files_at_root_untouched(tree):
    out = tree["out"]
    before = {n: (out / n).stat().st_mtime_ns for n in SHA_FILES}
    cur.curate(**tree)
    cur.curate(**tree, tools=["tool_a"])
    for name, data in SHA_FILES.items():
        assert (out / name).read_bytes() == data
        assert (out / name).stat().st_mtime_ns == before[name]
    root_files = sorted(p.name for p in out.iterdir() if p.is_file())
    assert root_files == sorted([*SHA_FILES, "manifest.json", "marks.json"])
    assert sorted(p.name for p in out.iterdir() if p.is_dir()) == ["transcripts"]


def test_idempotent_apart_from_created_at(tree):
    first = cur.curate(**tree)
    snap = _snapshot(tree["out"])
    second = cur.curate(**tree)
    again = _snapshot(tree["out"])
    assert _no_time(first) == _no_time(second)
    assert {k: v for k, v in snap.items() if k != "manifest.json"} == \
           {k: v for k, v in again.items() if k != "manifest.json"}   # bytes and mtimes
    assert _no_time(json.loads(again["manifest.json"][0])) == _no_time(first)


def test_build_manifest_reads_only(tree):
    before = _snapshot(tree["out"])
    m = cur.build_manifest(transcripts=tree["transcripts"], marks=tree["marks"],
                           forge_dirs=tree["forge_dirs"], specs=tree["specs"],
                           metrics=tree["metrics"], tools=["tool_a"])
    assert list(m) == TOP_KEYS and m["transcripts"]["count"] == 2
    assert _snapshot(tree["out"]) == before


def test_missing_latest_and_marks_stay_null_not_invented(tree):
    (tree["metrics"] / "latest.json").unlink()
    (tree["metrics"] / "smoke_fixtures.json").write_text("{}")
    tree["marks"].unlink()
    m = cur.curate(**tree)
    assert m["metrics"] == {"latest": None, "run_id": None, "git_sha": None, "split": None,
                            "files": [source_label(tree["metrics"] / "smoke_fixtures.json")]}
    assert m["marks"] == {"count": 0, "by_rule": {}}
    assert json.loads((tree["out"] / "marks.json").read_text()) == []


def test_refuses_when_source_is_the_destination(tree):
    with pytest.raises(ValueError):
        cur.curate(**dict(tree, out=tree["transcripts"].parent))


def test_unsafe_transcript_id_is_never_written_outside_transcripts(tree):
    (tree["transcripts"] / "evil.json").write_text(
        json.dumps(_transcript("../escaped", "tool_a", False)))
    m = cur.curate(**tree)
    assert m["transcripts"]["count"] == 3
    assert not (tree["out"] / "escaped.json").exists()


def test_cli(tree, capsys):
    args = ["--transcripts", str(tree["transcripts"]), "--marks", str(tree["marks"]),
            "--forge-dirs", *map(str, tree["forge_dirs"]), "--specs", str(tree["specs"]),
            "--metrics", str(tree["metrics"]), "--out", str(tree["out"]),
            "--tools", "tool_a", "tool_b"]
    assert cur.main(args + ["--dry-run"]) == 0
    assert not (tree["out"] / "manifest.json").exists()
    capsys.readouterr()
    assert cur.main(args) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith("curated 3 transcripts (2 attack, 1 benign")
    assert json.loads((tree["out"] / "manifest.json").read_text())["transcripts"]["count"] == 3
    assert cur.main(args + ["--out", str(tree["transcripts"].parent)]) == 2   # last --out wins
