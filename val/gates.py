"""Metric gate: exits non-zero if metrics/latest.json misses a pre-registered threshold."""
import json
import sys
from pathlib import Path

from contracts.metrics import MetricsRun, gate_failures, unmeasured


def main(path: str = "metrics/latest.json") -> int:
    p = Path(path)
    if not p.exists():
        print(f"gates: {path} not found yet, skipping")
        return 0
    run = MetricsRun.model_validate(json.loads(p.read_text()))
    failed = gate_failures(run)
    print(f"gates: {run.run_id} split={run.split} sha={run.git_sha}")
    for name in unmeasured(run):
        print(f"  not measured: {name}")
    for f in failed:
        print(f"  FAIL {f}")
    print("gates: PASS" if not failed else f"gates: {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
