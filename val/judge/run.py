"""One command: judge every transcript in a label split and print precision / recall.

    .venv/bin/python -m val.judge.run --labels ../crashtest-val-private/labels/test \
        --transcripts ../crashtest-val-private/fixtures/transcripts --out preds_test.json

Needs JUDGE_MODEL (different from ATTACKER_MODEL and BOT_MODEL) and JUDGE_PROMPT_DIR, or cached
outputs when CACHE_ONLY=1. Only the `test` split may be reported publicly."""
import argparse
import json
import sys
from pathlib import Path

from common import llm
from contracts.policyspec import PolicySpec
from contracts.transcript import Transcript
from val.judge.judge import PROMPT_VERSION, judge
from val.judge.scorer import format_line, load_split, score


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", type=Path, default=Path("data/specs/cafe_demo.json"))
    ap.add_argument("--labels", type=Path, required=True, help="label split directory")
    ap.add_argument("--transcripts", type=Path, required=True, help="directory of <transcript_id>.json")
    ap.add_argument("--out", type=Path, default=None, help="write predictions here")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    llm.assert_independent_judge()
    split = a.labels.name
    if split != "test":
        print(f"warning: split '{split}' is for tuning/calibration; publish 'test' numbers only",
              file=sys.stderr)
    spec = PolicySpec.model_validate_json(a.spec.read_text())
    ids, labels = load_split(a.labels)
    preds = []
    for tid in ids:
        t = Transcript.model_validate_json((a.transcripts / f"{tid}.json").read_text())
        if t.business_id != spec.business_id:
            raise SystemExit(f"{tid} belongs to {t.business_id}, spec is {spec.business_id}")
        preds.extend(judge(t, spec, seed=a.seed))
    res = score(labels, preds)
    res.update(split=split, judge_model=llm.model_for("judge"), prompt_version=PROMPT_VERSION,
               seed=a.seed, n_transcripts=len(ids))
    if a.out:
        a.out.write_text(json.dumps({"result": res, "predictions": [p.model_dump() for p in preds]},
                                    indent=1))
    print(json.dumps(res, indent=2) if a.json else format_line(res, split))
    return 0


if __name__ == "__main__":
    sys.exit(main())
