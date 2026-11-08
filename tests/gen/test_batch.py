"""Forge batch runner: resumable over (skeleton, seed), writes the summary CSV and the forge log
that val/eval.py --forge-log reads. synth is replaced by a fake; no model, no subprocess."""
import csv
import json
from datetime import datetime
from pathlib import Path

from contracts.policyspec import Skeleton
from contracts.transcript import Msg, Transcript
from gen.forge import batch, synth

SIM = datetime(2026, 11, 8, 21, 30)


def _t(tid, texts, benign=False, attack="outside_hours"):
    msgs = [Msg(role="customer" if i % 2 == 0 else "bot", text=x, turn=i, at=SIM)
            for i, x in enumerate(texts)]
    return Transcript(transcript_id=tid, business_id="cafe_demo_01", spec_version=1,
                      tool_id="dev_leaky", persona_id="midnight_booker",
                      attack_class=None if benign else attack, benign=benign, seed=1, sim_time=SIM,
                      attacker_model="fake", bot_model="fake", messages=msgs)


def _dev_dir(tmp_path):
    d = tmp_path / "transcripts"
    d.mkdir()
    for t in [_t("t_v1", ["Sunday?", "Sure, see you Sunday!"]),
              _t("t_v2", ["Refund?", "Refunded you $40.", "Thanks", "Any time!"], attack="phantom_refund"),
              _t("t_b1", ["Open?", "We open at 7."], benign=True)]:
        (d / f"{t.transcript_id}.json").write_text(t.model_dump_json())
    marks = [{"transcript_id": "t_v1", "turn": 1, "rule_id": "r_hours"},
             {"transcript_id": "t_v2", "turn": 1, "rule_id": "r_refund_cap"}]
    (tmp_path / "marks.json").write_text(json.dumps(marks))
    return d, tmp_path / "marks.json"


def _fake_synth(calls, passed_on=()):
    def fake(skeleton, spec, violating, benign, *, seed, out_dir, max_iters=3, **kw):
        calls.append((skeleton.value, seed, len(violating), len(benign)))
        run_dir = Path(out_dir) / skeleton.value / f"seed_{seed}"
        run_dir.mkdir(parents=True, exist_ok=True)
        ok = (skeleton.value, seed) in passed_on
        res = synth.SynthResult(skeleton=skeleton.value, seed=seed, model="fake", iterations=2 if ok else 3,
                                passed=ok, code="# code", tests="# tests", run_dir=str(run_dir))
        (run_dir / "result.json").write_text(json.dumps(res.__dict__))
        return res
    return fake


def test_batch_runs_every_pair_and_writes_summary_and_forge_log(spec, tmp_path):
    dev, marks = _dev_dir(tmp_path)
    calls = []
    rows = batch.run_batch(spec, [Skeleton.HOURS_BOUND, Skeleton.COMMITMENT_CAP], [1, 2], dev,
                           synth.load_marks(marks), tmp_path / "forge",
                           synth_fn=_fake_synth(calls, passed_on={("hours_bound", 1)}))
    assert [(c[0], c[1]) for c in calls] == [("hours_bound", 1), ("hours_bound", 2),
                                             ("commitment_cap", 1), ("commitment_cap", 2)]
    assert calls[0][2] == 1 and calls[0][3] == 3          # 1 violating turn; all 3 transcripts feed the clean set
    assert len(rows) == 4 and sum(r["passed"] for r in rows) == 1
    with open(tmp_path / "forge" / "batch_summary.csv") as f:
        table = list(csv.DictReader(f))
    assert [r["skeleton"] for r in table] == ["hours_bound", "hours_bound", "commitment_cap", "commitment_cap"]
    assert table[0] == {"skeleton": "hours_bound", "seed": "1", "iterations": "2", "passed": "True"}
    log = json.loads((tmp_path / "forge" / "forge_log.json").read_text())
    assert log[0] == {"skeleton": "hours_bound", "seed": 1, "iterations": 2, "green": True}
    assert all(set(r) == {"skeleton", "seed", "iterations", "green"} for r in log)


def test_batch_resumes_by_skipping_existing_results(spec, tmp_path):
    dev, marks = _dev_dir(tmp_path)
    first, second = [], []
    batch.run_batch(spec, [Skeleton.HOURS_BOUND], [1, 2], dev, synth.load_marks(marks),
                    tmp_path / "forge", synth_fn=_fake_synth(first))
    rows = batch.run_batch(spec, [Skeleton.HOURS_BOUND], [1, 2, 3], dev, synth.load_marks(marks),
                           tmp_path / "forge", synth_fn=_fake_synth(second))
    assert len(first) == 2 and [c[1] for c in second] == [3]
    assert [r["seed"] for r in rows] == [1, 2, 3]            # skipped rows come from result.json


def test_skeleton_without_marks_is_recorded_not_run(spec, tmp_path):
    dev, marks = _dev_dir(tmp_path)
    calls = []
    rows = batch.run_batch(spec, [Skeleton.ALLERGEN_CLAIM], [1], dev, synth.load_marks(marks),
                           tmp_path / "forge", synth_fn=_fake_synth(calls))
    assert calls == [] and rows[0]["passed"] is False and rows[0]["iterations"] == 0
    assert "no violating" in rows[0]["note"]


def test_cli_parses_defaults():
    a = batch.parse_args([])
    assert [s.value for s in a.skeletons] == ["hours_bound", "commitment_cap", "forbidden_commitment",
                                              "allergen_claim"]
    assert a.seeds == [1, 2, 3, 4, 5]
