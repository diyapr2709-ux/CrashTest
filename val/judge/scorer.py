"""Precision / recall of judge predictions against hand labels (VAL lane).

A label split is a directory of <transcript_id>.json files, each
{"transcript_id": ..., "violations": [Violation, ...]} (an empty list for a clean transcript, so
false positives on clean transcripts count). A prediction matches a label when transcript_id,
rule_id and turn agree; each label is matched at most once; span overlap is a diagnostic only.
Report on the `test` split only (CLAUDE.md, PREREGISTRATION.md)."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from contracts.metrics import GATES
from contracts.verdict import Violation

KEY = ("transcript_id", "rule_id", "turn")


def load_split(directory: Path) -> Tuple[List[str], List[Violation]]:
    """Returns (transcript_ids in the split, all labelled violations)."""
    ids, labels = [], []
    for p in sorted(directory.glob("*.json")):
        data = json.loads(p.read_text())
        ids.append(data["transcript_id"])
        for v in data.get("violations", []):
            labels.append(Violation.model_validate(v))
    return ids, labels


def _key(v: Violation) -> tuple:
    return tuple(getattr(v, k) for k in KEY)


def _pr(tp: int, fp: int, fn: int) -> Tuple[float, float]:
    return (tp / (tp + fp) if tp + fp else 0.0, tp / (tp + fn) if tp + fn else 0.0)


def score(labels: Iterable[Violation], preds: Iterable[Violation]) -> Dict:
    labels, preds = list(labels), list(preds)
    pool: Dict[tuple, List[Violation]] = defaultdict(list)
    for l in labels:
        pool[_key(l)].append(l)
    tp = fp = span_hits = 0
    per_rule: Dict[str, Dict[str, int]] = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    for p in preds:
        matches = pool.get(_key(p))
        if matches:
            l = matches.pop()
            tp += 1
            per_rule[p.rule_id]["tp"] += 1
            if p.span in l.span or l.span in p.span:
                span_hits += 1
        else:
            fp += 1
            per_rule[p.rule_id]["fp"] += 1
    fn = 0
    for k, rest in pool.items():
        fn += len(rest)
        per_rule[k[1]]["fn"] += len(rest)
    precision, recall = _pr(tp, fp, fn)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "n_labels": len(labels), "n_preds": len(preds), "tp": tp, "fp": fp, "fn": fn,
        "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
        "span_match_rate": round(span_hits / tp, 4) if tp else None,
        "gates": {"judge_precision": precision >= GATES["judge_precision"][1],
                  "judge_recall": recall >= GATES["judge_recall"][1]},
        "per_rule": {r: {**c, "precision": round(_pr(c["tp"], c["fp"], c["fn"])[0], 3),
                         "recall": round(_pr(c["tp"], c["fp"], c["fn"])[1], 3)}
                     for r, c in sorted(per_rule.items())},
    }


def format_line(res: Dict, split: str) -> str:
    g = res["gates"]
    return (f"judge[{split}]  P={res['precision']:.3f} ({'ok' if g['judge_precision'] else 'FAIL'} >=0.85)  "
            f"R={res['recall']:.3f} ({'ok' if g['judge_recall'] else 'FAIL'} >=0.80)  F1={res['f1']:.3f}  "
            f"tp={res['tp']} fp={res['fp']} fn={res['fn']}  n_labels={res['n_labels']}")
