"""Forge batch: synth for each skeleton x seed, resumable, from the dev failure corpus.
    nohup .venv/bin/python -m gen.forge.batch > cache/forge/batch.log 2>&1 &
    tail -f cache/forge/batch.log          # progress
Skips any (skeleton, seed) that already has <out>/<skeleton>/seed_<seed>/result.json. Writes
<out>/batch_summary.csv (skeleton, seed, iterations, passed) and <out>/forge_log.json, the rows
val/eval.py --forge-log reads ({skeleton, seed, iterations, green})."""
import argparse
import csv
import json
import sys
import time
from pathlib import Path
from typing import Callable, List, Sequence

from contracts.policyspec import PolicySpec, Skeleton
from gen.forge import synth

DEFAULT_SKELETONS = [Skeleton.HOURS_BOUND, Skeleton.COMMITMENT_CAP, Skeleton.FORBIDDEN_COMMITMENT,
                     Skeleton.ALLERGEN_CLAIM]
DEFAULT_SEEDS = [1, 2, 3, 4, 5]


def _row(skeleton: str, seed: int, iterations: int, passed: bool, note: str = "") -> dict:
    return {"skeleton": skeleton, "seed": seed, "iterations": iterations, "passed": passed, "note": note}


def write_outputs(rows: List[dict], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "batch_summary.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["skeleton", "seed", "iterations", "passed"], extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    log = [{"skeleton": r["skeleton"], "seed": r["seed"], "iterations": r["iterations"],
            "green": bool(r["passed"])} for r in rows]
    (out_dir / "forge_log.json").write_text(json.dumps(log, indent=1))


def run_batch(spec: PolicySpec, skeletons: Sequence[Skeleton], seeds: Sequence[int],
              transcripts_dir: Path, marks: List[dict], out_dir: Path, *, max_iters: int = 3,
              synth_fn: Callable = synth.synthesize, log=print) -> List[dict]:
    out_dir = Path(out_dir)
    rows: List[dict] = []
    for skel in skeletons:
        violating, benign, marked = synth.corpus_from_dev(spec, skel, transcripts_dir, marks)
        for seed in seeds:
            existing = out_dir / skel.value / f"seed_{seed}" / "result.json"
            if existing.exists():
                r = json.loads(existing.read_text())
                rows.append(_row(skel.value, seed, r["iterations"], r["passed"], "resumed"))
                log(f"skip {skel.value} seed {seed}: result exists (passed={r['passed']})")
            elif not violating:
                rows.append(_row(skel.value, seed, 0, False, "no violating examples in marks"))
                log(f"skip {skel.value} seed {seed}: no violating examples in marks")
            else:
                t0 = time.time()
                log(f"run  {skel.value} seed {seed}: {len(violating)} violating, {len(benign)} benign ...")
                res = synth_fn(skel, spec, violating, benign, seed=seed, out_dir=out_dir,
                               max_iters=max_iters, exclude=marked)
                rows.append(_row(skel.value, seed, res.iterations, res.passed))
                log(f"done {skel.value} seed {seed}: {'GREEN' if res.passed else 'RED'} in "
                    f"{res.iterations} iteration(s), {time.time() - t0:.0f} s")
            write_outputs(rows, out_dir)
    return rows


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Forge batch over skeletons x seeds (resumable)")
    ap.add_argument("--skeletons", nargs="+", type=Skeleton, default=DEFAULT_SKELETONS)
    ap.add_argument("--seeds", nargs="+", type=int, default=DEFAULT_SEEDS)
    ap.add_argument("--spec", type=Path, default=synth.ROOT / "data/specs/cafe_demo.json")
    ap.add_argument("--transcripts", type=Path, default=synth.ROOT / "data/dev/transcripts")
    ap.add_argument("--marks", type=Path, default=synth.ROOT / "data/dev/marks.json")
    ap.add_argument("--out", type=Path, default=synth.OUT_DIR)
    ap.add_argument("--max-iters", type=int, default=3)
    return ap.parse_args(argv)


def main(argv=None) -> int:
    a = parse_args(argv)
    spec = PolicySpec.model_validate_json(a.spec.read_text())

    def log(msg):
        print(time.strftime("%H:%M:%S"), msg, flush=True)

    rows = run_batch(spec, a.skeletons, a.seeds, a.transcripts, synth.load_marks(a.marks), a.out,
                     max_iters=a.max_iters, log=log)
    green = sum(r["passed"] for r in rows)
    log(f"batch done: {green}/{len(rows)} green; summary in {a.out / 'batch_summary.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
