"""Summarize Forge runs (<dir>/<skeleton>/seed_<n>/result.json) for the paper and the UI.
    python -m gen.forge.report                                   # every cache/forge* directory
    python -m gen.forge.report --dirs cache/forge cache/forge_prompt_v1 --md out.md --csv out.csv
Rows, one per run: source dir, skeleton, seed, prompt_version (1 when the key is missing: the
prompt-v1 results predate it), iterations, passed, n_violating / n_benign (from the run dir's
corpus.json when present), code_lines. Aggregates per (source dir, prompt_version, skeleton):
seeds run, seeds green, pass rate, mean iterations-to-green over the green seeds.
These are Forge's OWN test results (necessary, not sufficient). pass@1 on hidden tests and every
number in the UI, video or paper comes from VAL's metrics/*.json."""
import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
ROW_KEYS = ["source", "skeleton", "seed", "prompt_version", "iterations", "passed", "n_violating",
            "n_benign", "code_lines"]
AGG_KEYS = ["source", "prompt_version", "skeleton", "seeds_run", "seeds_green", "pass_rate",
            "mean_iters_to_green"]


def default_dirs() -> List[Path]:
    return sorted(p for p in (ROOT / "cache").glob("forge*") if p.is_dir())


def source_label(d: Path) -> str:
    d = Path(d)
    try:
        return str(d.resolve().relative_to(ROOT))
    except ValueError:
        return str(d)


def _seed_from(name: str) -> Optional[int]:
    tail = name.split("_", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _corpus_counts(run_dir: Path, result: dict) -> Tuple[Optional[int], Optional[int]]:
    candidates = [run_dir] + ([Path(result["run_dir"])] if result.get("run_dir") else [])
    for d in candidates:
        p = d / "corpus.json"
        if p.exists():
            try:
                corpus = json.loads(p.read_text())
            except ValueError:
                return None, None
            return len(corpus.get("violating", [])), len(corpus.get("benign", []))
    return None, None


def collect(dirs: Iterable[Path]) -> List[dict]:
    rows = []
    for d in dirs:
        d = Path(d)
        label = source_label(d)
        for rp in sorted(d.glob("*/seed_*/result.json")):
            try:
                r = json.loads(rp.read_text())
            except ValueError:
                continue
            run_dir = rp.parent
            n_v, n_b = _corpus_counts(run_dir, r)
            rows.append({"source": label,
                         "skeleton": r.get("skeleton") or run_dir.parent.name,
                         "seed": r.get("seed", _seed_from(run_dir.name)),
                         "prompt_version": int(r.get("prompt_version") or 1),
                         "iterations": r.get("iterations"),
                         "passed": bool(r.get("passed")),
                         "n_violating": n_v, "n_benign": n_b,
                         "code_lines": len((r.get("code") or "").splitlines())})
    rows.sort(key=lambda r: (r["source"], r["prompt_version"], r["skeleton"],
                             r["seed"] is None, r["seed"] or 0))
    return rows


def aggregate(rows: List[dict]) -> List[dict]:
    groups = {}
    for r in rows:
        groups.setdefault((r["source"], r["prompt_version"], r["skeleton"]), []).append(r)
    out = []
    for (source, pv, skeleton), rs in sorted(groups.items()):
        green = [r for r in rs if r["passed"]]
        iters = [r["iterations"] for r in green if r["iterations"] is not None]
        out.append({"source": source, "prompt_version": pv, "skeleton": skeleton,
                    "seeds_run": len(rs), "seeds_green": len(green),
                    "pass_rate": len(green) / len(rs),
                    "mean_iters_to_green": sum(iters) / len(iters) if iters else None})
    return out


def _cell(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def _table(keys: List[str], rows: List[dict]) -> str:
    lines = ["| " + " | ".join(keys) + " |", "|" + "|".join(" --- " for _ in keys) + "|"]
    lines += ["| " + " | ".join(_cell(r[k]) for k in keys) + " |" for r in rows]
    return "\n".join(lines)


def render_markdown(rows: List[dict], aggs: List[dict]) -> str:
    if not rows:
        return "No Forge runs found.\n"
    parts = ["## Forge runs", "", _table(ROW_KEYS, rows), "",
             "## Per skeleton (source dir x prompt version)", "", _table(AGG_KEYS, aggs), "",
             "Forge's own tests only (necessary, not sufficient); pass@1 on hidden tests comes "
             "from metrics/*.json."]
    return "\n".join(parts) + "\n"


def write_csv(rows: List[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ROW_KEYS)
        w.writeheader()
        w.writerows(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Summarize Forge runs (Forge's own tests only)")
    ap.add_argument("--dirs", nargs="+", type=Path, default=None,
                    help="Forge output dirs (default: every cache/forge* that exists)")
    ap.add_argument("--md", type=Path, help="also write the markdown here")
    ap.add_argument("--csv", type=Path, help="write the per-run rows as CSV here")
    a = ap.parse_args(argv)
    rows = collect(a.dirs if a.dirs is not None else default_dirs())
    md = render_markdown(rows, aggregate(rows))
    print(md, end="")
    if a.md:
        a.md.parent.mkdir(parents=True, exist_ok=True)
        a.md.write_text(md)
    if a.csv:
        write_csv(rows, a.csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
