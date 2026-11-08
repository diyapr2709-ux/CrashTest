"""Curate the read-only demo layer (cache/demo) and write its provenance manifest.
    python -m gen.demo.curate                                    # dev transcripts + marks -> cache/demo
    python -m gen.demo.curate --tools dev_leaky dev_cautious --dry-run
    python -m gen.demo.curate --transcripts data/dev/transcripts --marks data/dev/marks.json \
        --forge-dirs cache/forge cache/forge_prompt_v3 --specs data/specs --metrics metrics \
        --out cache/demo
Every transcript in --transcripts that validates as a Transcript (only --tools, when given) is
copied byte for byte to <out>/transcripts/<transcript_id>.json, the path app/data.py streams from;
*.json files left there from an earlier curation that are not in this selection are removed, so
the directory always matches the manifest. The marks that cite a curated transcript go to
<out>/marks.json. <out>/manifest.json records where every figure comes from (AGENTS.md 4.8: a
MetricsRun id, a transcript id, a spec version): git SHA, each valid PolicySpec's version, the
transcripts' tools / models / seeds, the marks per rule, one row per Forge result.json (Forge
artifacts stay in cache/forge*; nothing is copied) and VAL's metrics/latest.json run id.
<out>/ root is also common/llm.py's read-only cache layer (<sha>.json files): nothing is ever
written there except manifest.json and marks.json. No model is called. --dry-run prints the
manifest and writes nothing. Files that are skipped are named on stderr."""
import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from contracts.metrics import MetricsRun
from contracts.policyspec import PolicySpec
from contracts.transcript import Transcript
from gen.forge import report
from gen.harness.run import git_sha

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TRANSCRIPTS = ROOT / "data" / "dev" / "transcripts"
DEFAULT_MARKS = ROOT / "data" / "dev" / "marks.json"
DEFAULT_SPECS = ROOT / "data" / "specs"
DEFAULT_METRICS = ROOT / "metrics"
DEFAULT_OUT = ROOT / "cache" / "demo"
TRANSCRIPTS_SUBDIR = "transcripts"
MANIFEST_NAME = "manifest.json"
MARKS_NAME = "marks.json"
MANIFEST_KEYS = ("created_at", "git_sha", "specs", "transcripts", "marks", "forge", "metrics")
MARK_KEYS = {"transcript_id", "turn", "rule_id", "span"}
FORGE_KEYS = ("source", "skeleton", "seed", "prompt_version", "iterations", "passed")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*$")   # a file name, never a path


def _warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


def _why(e: Exception) -> str:
    return " ".join(str(e).split())[:200]


def _rel(p: Path) -> str:
    """Repo-relative when inside the repo, else as given (same rule as gen.forge.report)."""
    return report.source_label(Path(p))


# ---------------------------------------------------------------- inputs
def select_transcripts(src: Path, tools: Optional[Sequence[str]] = None) -> List[Tuple[Path, Transcript]]:
    """(source file, transcript) for each *.json in `src` that validates, sorted by transcript_id,
    only the given tool_ids when `tools` is set. Invalid files, ids that are not plain file names
    and duplicate ids are skipped with a warning."""
    src = Path(src)
    if not src.is_dir():
        _warn(f"no transcripts directory at {src}")
        return []
    wanted = set(tools) if tools else None
    chosen: Dict[str, Tuple[Path, Transcript]] = {}
    for p in sorted(src.glob("*.json")):
        try:
            t = Transcript.model_validate_json(p.read_text())
        except Exception as e:
            _warn(f"skipping {p}: not a valid Transcript ({_why(e)})")
            continue
        if wanted is not None and t.tool_id not in wanted:
            continue
        if not _SAFE_ID.match(t.transcript_id):
            _warn(f"skipping {p}: transcript_id {t.transcript_id!r} is not a plain file name")
            continue
        if t.transcript_id in chosen:
            _warn(f"skipping {p}: duplicate transcript_id {t.transcript_id} "
                  f"(kept {chosen[t.transcript_id][0].name})")
            continue
        chosen[t.transcript_id] = (p, t)
    for tool in sorted((wanted or set()) - {t.tool_id for _, t in chosen.values()}):
        _warn(f"no valid transcripts for tool {tool} in {src}")
    return [chosen[k] for k in sorted(chosen)]


def load_marks(path: Optional[Path]) -> List[dict]:
    """Hand marks [{transcript_id, turn, rule_id, span}] in file order; [] with a warning when the
    file is absent or unreadable. Entries that are not marks are dropped with a warning."""
    if path is None:
        return []
    path = Path(path)
    if not path.is_file():
        _warn(f"no marks file at {path}")
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        _warn(f"unreadable marks file {path} ({_why(e)})")
        return []
    if not isinstance(data, list):
        _warn(f"marks file {path} is not a JSON list")
        return []
    marks = [m for m in data if isinstance(m, dict) and MARK_KEYS <= set(m)]
    if len(marks) < len(data):
        _warn(f"dropped {len(data) - len(marks)} malformed mark(s) from {path}")
    return marks


def marks_for(marks: List[dict], transcript_ids: Iterable[str]) -> List[dict]:
    """The marks citing a curated transcript (a mark that cites nothing in the layer cannot be
    traced to a transcript, so it is left out, with a warning)."""
    ids = set(transcript_ids)
    kept = [m for m in marks if m["transcript_id"] in ids]
    if len(kept) < len(marks):
        _warn(f"left out {len(marks) - len(kept)} mark(s) whose transcript is not curated")
    return kept


# ---------------------------------------------------------------- manifest sections
def spec_entries(specs_dir: Path) -> Dict[str, dict]:
    """business_id -> {path, version, schema_version, n_rules} for each *.json that validates as a
    PolicySpec (others are skipped with a warning; on a repeated business_id the first file by
    name wins)."""
    specs_dir = Path(specs_dir)
    out: Dict[str, dict] = {}
    if not specs_dir.is_dir():
        _warn(f"no specs directory at {specs_dir}")
        return out
    for p in sorted(specs_dir.glob("*.json")):
        try:
            spec = PolicySpec.model_validate_json(p.read_text())
        except Exception as e:
            _warn(f"skipping spec {p}: not a valid PolicySpec ({_why(e)})")
            continue
        if spec.business_id in out:
            _warn(f"skipping spec {p}: business_id {spec.business_id} already comes from "
                  f"{out[spec.business_id]['path']}")
            continue
        out[spec.business_id] = {"path": _rel(p), "version": spec.version,
                                 "schema_version": spec.schema_version, "n_rules": len(spec.rules)}
    return dict(sorted(out.items()))


def transcripts_entry(ts: Sequence[Transcript]) -> dict:
    attack = sum(1 for t in ts if not t.benign)
    return {"count": len(ts),
            "by_tool": dict(sorted(Counter(t.tool_id for t in ts).items())),
            "attack": attack,
            "benign": len(ts) - attack,
            "attacker_models": sorted({t.attacker_model for t in ts}),
            "bot_models": sorted({t.bot_model for t in ts}),
            "seeds": sorted({t.seed for t in ts})}


def marks_entry(marks: Sequence[dict]) -> dict:
    return {"count": len(marks),
            "by_rule": dict(sorted(Counter(str(m["rule_id"]) for m in marks).items()))}


def forge_rows(dirs: Iterable[Path]) -> List[dict]:
    """One row per <dir>/<skeleton>/seed_<n>/result.json via gen.forge.report.collect (which skips
    unreadable files, e.g. one a running batch is writing). Rows without an int seed or iteration
    count are skipped with a warning."""
    rows = []
    for r in report.collect(dirs):
        if not all(isinstance(r[k], int) and not isinstance(r[k], bool) for k in ("seed", "iterations")):
            _warn(f"skipping Forge run {r['source']}/{r['skeleton']} seed {r['seed']}: "
                  "result.json lacks an int seed or iterations")
            continue
        rows.append({k: r[k] for k in FORGE_KEYS})
    return rows


def metrics_entry(metrics_dir: Path) -> dict:
    """VAL's metrics/latest.json run (null fields when absent or invalid; never filled in) and every
    metrics/*.json file."""
    metrics_dir = Path(metrics_dir)
    files = sorted(metrics_dir.glob("*.json")) if metrics_dir.is_dir() else []
    out = {"latest": None, "run_id": None, "git_sha": None, "split": None,
           "files": [_rel(p) for p in files]}
    latest = metrics_dir / "latest.json"
    if not latest.is_file():
        _warn(f"no {latest}: the UI falls back to the smoke fixtures")
        return out
    out["latest"] = _rel(latest)
    try:
        run = MetricsRun.model_validate_json(latest.read_text())
    except Exception as e:
        _warn(f"{latest} is not a valid MetricsRun ({_why(e)})")
        return out
    out.update(run_id=run.run_id, git_sha=run.git_sha, split=run.split)
    return out


def _check_spec_versions(ts: Sequence[Transcript], specs: Dict[str, dict]) -> None:
    cited = sorted({(t.business_id, t.spec_version) for t in ts})
    for business_id, version in cited:
        have = specs.get(business_id)
        if have is None or have["version"] != version:
            found = f"v{have['version']} at {have['path']}" if have else "no spec"
            _warn(f"transcripts cite {business_id} v{version}; the specs directory has {found}")


def _manifest(selected: Sequence[Tuple[Path, Transcript]], marks: Sequence[dict],
              forge_dirs: Optional[Sequence[Path]], specs: Path, metrics: Path) -> dict:
    ts = [t for _, t in selected]
    spec_map = spec_entries(specs)
    _check_spec_versions(ts, spec_map)
    return {"created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "git_sha": git_sha(),
            "specs": spec_map,
            "transcripts": transcripts_entry(ts),
            "marks": marks_entry(marks),
            "forge": forge_rows(report.default_dirs() if forge_dirs is None else forge_dirs),
            "metrics": metrics_entry(metrics)}


def build_manifest(*, transcripts: Path = DEFAULT_TRANSCRIPTS, marks: Optional[Path] = DEFAULT_MARKS,
                   forge_dirs: Optional[Sequence[Path]] = None, specs: Path = DEFAULT_SPECS,
                   metrics: Path = DEFAULT_METRICS, tools: Optional[Sequence[str]] = None) -> dict:
    """The manifest a curation with these inputs would write. Reads only; writes nothing.
    forge_dirs=None means every cache/forge* directory that exists."""
    selected = select_transcripts(transcripts, tools)
    kept = marks_for(load_marks(marks), (t.transcript_id for _, t in selected))
    return _manifest(selected, kept, forge_dirs, specs, metrics)


# ---------------------------------------------------------------- writing
def marks_text(marks: Sequence[dict]) -> str:
    """One mark per line, the same layout as data/dev/marks.json."""
    if not marks:
        return "[]\n"
    return "[\n" + ",\n".join(" " + json.dumps(m, ensure_ascii=False) for m in marks) + "\n]\n"


def _write_if_changed(path: Path, data: bytes) -> bool:
    if path.is_file() and path.read_bytes() == data:
        return False
    path.write_bytes(data)
    return True


def summary(manifest: dict, out: Path, n_stale: int, dry_run: bool = False) -> str:
    t = manifest["transcripts"]
    tools = ", ".join(f"{k} {v}" for k, v in t["by_tool"].items()) or "no tools"
    stale = f"; {'would remove' if dry_run else 'removed'} {n_stale} stale" if n_stale else ""
    return (f"{'dry run, would curate' if dry_run else 'curated'} {t['count']} transcripts "
            f"({t['attack']} attack, {t['benign']} benign; {tools}), {manifest['marks']['count']} "
            f"marks, {len(manifest['specs'])} specs, {len(manifest['forge'])} Forge runs, metrics "
            f"run {manifest['metrics']['run_id'] or 'none'} -> {_rel(out)}{stale}")


def curate(*, transcripts: Path = DEFAULT_TRANSCRIPTS, marks: Optional[Path] = DEFAULT_MARKS,
           forge_dirs: Optional[Sequence[Path]] = None, specs: Path = DEFAULT_SPECS,
           metrics: Path = DEFAULT_METRICS, out: Path = DEFAULT_OUT,
           tools: Optional[Sequence[str]] = None, dry_run: bool = False) -> dict:
    """Write <out>/transcripts/<id>.json, <out>/marks.json and <out>/manifest.json (only files
    whose bytes change are rewritten) and return the manifest. dry_run: print it, write nothing.
    Raises ValueError when --transcripts is <out>/transcripts itself (pruning would eat it)."""
    out = Path(out)
    dest = out / TRANSCRIPTS_SUBDIR
    if Path(transcripts).resolve() == dest.resolve():
        raise ValueError(f"the source transcripts directory is the destination {dest}; "
                         "pick another --out or --transcripts")
    selected = select_transcripts(transcripts, tools)
    ids = {t.transcript_id for _, t in selected}
    kept = marks_for(load_marks(marks), ids)
    manifest = _manifest(selected, kept, forge_dirs, specs, metrics)
    stale = [p for p in sorted(dest.glob("*.json")) if p.stem not in ids] if dest.is_dir() else []
    if dry_run:
        print(json.dumps(manifest, indent=1))
        print(summary(manifest, out, len(stale), dry_run=True))
        return manifest
    dest.mkdir(parents=True, exist_ok=True)
    for src, t in selected:
        _write_if_changed(dest / f"{t.transcript_id}.json", src.read_bytes())
    for p in stale:
        p.unlink()
    _write_if_changed(out / MARKS_NAME, marks_text(kept).encode())
    _write_if_changed(out / MANIFEST_NAME, (json.dumps(manifest, indent=1) + "\n").encode())
    print(summary(manifest, out, len(stale)))
    return manifest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Curate cache/demo (the demo's read-only layer) and "
                                             "write its provenance manifest. No model is called.")
    ap.add_argument("--transcripts", type=Path, default=DEFAULT_TRANSCRIPTS,
                    help="source transcripts (default: data/dev/transcripts)")
    ap.add_argument("--marks", type=Path, default=DEFAULT_MARKS,
                    help="hand marks (default: data/dev/marks.json)")
    ap.add_argument("--forge-dirs", nargs="+", type=Path, default=None,
                    help="Forge output dirs, read only (default: every cache/forge* that exists)")
    ap.add_argument("--specs", type=Path, default=DEFAULT_SPECS, help="default: data/specs")
    ap.add_argument("--metrics", type=Path, default=DEFAULT_METRICS, help="default: metrics")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="default: cache/demo")
    ap.add_argument("--tools", nargs="+", default=None, help="only these tool_ids (default: all)")
    ap.add_argument("--dry-run", action="store_true", help="print the manifest, write nothing")
    a = ap.parse_args(argv)
    try:
        curate(transcripts=a.transcripts, marks=a.marks, forge_dirs=a.forge_dirs, specs=a.specs,
               metrics=a.metrics, out=a.out, tools=a.tools, dry_run=a.dry_run)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
