"""gen.forge.report: per-run rows and per-(source dir, prompt_version, skeleton) aggregates from
fabricated result.json / corpus.json files on tmp dirs. No model, no real cache."""
import csv
import json

import pytest

from gen.forge import report

ROW_KEYS = {"source", "skeleton", "seed", "prompt_version", "iterations", "passed", "n_violating",
            "n_benign", "code_lines"}
AGG_KEYS = {"source", "prompt_version", "skeleton", "seeds_run", "seeds_green", "pass_rate",
            "mean_iters_to_green"}


def _run(d, skeleton, seed, passed, iterations, prompt_version=2, code="a = 1\nb = 2\nc = 3\n",
         corpus=(3, 2)):
    run_dir = d / skeleton / f"seed_{seed}"
    run_dir.mkdir(parents=True)
    res = {"skeleton": skeleton, "seed": seed, "model": "fake", "iterations": iterations,
           "passed": passed, "code": code, "tests": "# tests", "problems": [],
           "code_path": str(run_dir / f"{skeleton}_v{seed}.py"), "run_dir": "/elsewhere/" + skeleton}
    if prompt_version is not None:
        res["prompt_version"] = prompt_version
    (run_dir / "result.json").write_text(json.dumps(res))
    if corpus is not None:
        (run_dir / "corpus.json").write_text(json.dumps(
            {"spec": {}, "violating": [{}] * corpus[0], "benign": [{}] * corpus[1]}))
    return run_dir


@pytest.fixture
def dirs(tmp_path):
    a, b = tmp_path / "forge", tmp_path / "forge_prompt_v1"
    _run(a, "hours_bound", 1, False, 3)
    _run(a, "hours_bound", 2, True, 2, corpus=(4, 1))
    _run(a, "forbidden_commitment", 1, True, 1, corpus=None)          # no corpus.json
    _run(b, "hours_bound", 1, True, 3, prompt_version=None)            # v1 results lack the key
    (a / "batch_summary.csv").write_text("skeleton,seed\n")            # ignored
    (a / "forge_log.json").write_text("[]")                            # ignored
    return [a, b]


def _one(rows, **where):
    found = [r for r in rows if all(r[k] == v for k, v in where.items())]
    assert len(found) == 1, (where, found)
    return found[0]


def test_collect_rows(dirs):
    rows = report.collect(dirs)
    assert len(rows) == 4 and all(set(r) == ROW_KEYS for r in rows)
    a, b = report.source_label(dirs[0]), report.source_label(dirs[1])
    r = _one(rows, source=a, skeleton="hours_bound", seed=1)
    assert (r["prompt_version"], r["iterations"], r["passed"]) == (2, 3, False)
    assert (r["n_violating"], r["n_benign"], r["code_lines"]) == (3, 2, 3)
    assert _one(rows, source=a, skeleton="hours_bound", seed=2)["n_violating"] == 4
    fc = _one(rows, source=a, skeleton="forbidden_commitment")
    assert fc["n_violating"] is None and fc["n_benign"] is None and fc["passed"] is True
    v1 = _one(rows, source=b)
    assert v1["prompt_version"] == 1 and v1["skeleton"] == "hours_bound" and v1["seed"] == 1


def test_collect_skips_unreadable_result_and_empty_dirs(tmp_path):
    d = tmp_path / "forge"
    (d / "hours_bound" / "seed_1").mkdir(parents=True)
    (d / "hours_bound" / "seed_1" / "result.json").write_text("{not json")
    _run(d, "hours_bound", 2, True, 1)
    assert [r["seed"] for r in report.collect([d])] == [2]
    assert report.collect([tmp_path / "missing"]) == [] and report.collect([]) == []


def test_aggregate(dirs):
    aggs = report.aggregate(report.collect(dirs))
    assert len(aggs) == 3 and all(set(a) == AGG_KEYS for a in aggs)
    a, b = report.source_label(dirs[0]), report.source_label(dirs[1])
    hb = _one(aggs, source=a, skeleton="hours_bound")
    assert (hb["prompt_version"], hb["seeds_run"], hb["seeds_green"]) == (2, 2, 1)
    assert hb["pass_rate"] == 0.5 and hb["mean_iters_to_green"] == 2.0
    fc = _one(aggs, source=a, skeleton="forbidden_commitment")
    assert (fc["seeds_run"], fc["seeds_green"], fc["pass_rate"], fc["mean_iters_to_green"]) == (1, 1, 1.0, 1.0)
    v1 = _one(aggs, source=b)
    assert (v1["prompt_version"], v1["seeds_green"], v1["mean_iters_to_green"]) == (1, 1, 3.0)


def test_aggregate_with_no_green_seed_has_no_mean(tmp_path):
    d = tmp_path / "forge"
    _run(d, "hours_bound", 1, False, 3)
    _run(d, "hours_bound", 2, False, 3)
    (agg,) = report.aggregate(report.collect([d]))
    assert agg["seeds_run"] == 2 and agg["seeds_green"] == 0 and agg["pass_rate"] == 0.0
    assert agg["mean_iters_to_green"] is None


def test_aggregate_separates_prompt_versions_in_one_dir(tmp_path):
    d = tmp_path / "forge"
    _run(d, "hours_bound", 1, True, 1, prompt_version=None)
    _run(d, "hours_bound", 2, True, 2, prompt_version=2)
    aggs = report.aggregate(report.collect([d]))
    assert [(a["prompt_version"], a["seeds_run"]) for a in aggs] == [(1, 1), (2, 1)]


def test_cli_prints_markdown_and_writes_md_and_csv(dirs, tmp_path, capsys):
    md, csv_path = tmp_path / "out.md", tmp_path / "out.csv"
    rc = report.main(["--dirs", str(dirs[0]), str(dirs[1]), "--md", str(md), "--csv", str(csv_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "| skeleton |" in out and "| hours_bound |" in out and "forbidden_commitment" in out
    assert "seeds_green" in out and "mean_iters_to_green" in out
    assert md.read_text() == out
    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 4 and set(rows[0]) == ROW_KEYS
    assert {r["prompt_version"] for r in rows} == {"1", "2"}
    assert [r["n_violating"] for r in rows if r["skeleton"] == "forbidden_commitment"] == [""]


def test_cli_on_empty_dir_says_so(tmp_path, capsys):
    assert report.main(["--dirs", str(tmp_path)]) == 0
    assert "No Forge runs" in capsys.readouterr().out


def test_default_dirs_are_the_existing_cache_forge_dirs():
    dirs = report.default_dirs()
    assert all(d.is_dir() and d.name.startswith("forge") and d.parent == report.ROOT / "cache"
               for d in dirs)
