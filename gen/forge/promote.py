"""The HUMAN promotion step: copy a green Forge candidate into gen/forge/guardrails/ and point
active.json at it. Never runs automatically (CLAUDE.md: "a human promotes by copying the file and
editing active.json"); this tool only makes that copy + pointer edit explicit and checked.
    python -m gen.forge.promote --skeleton hours_bound --seed 3                  # -> shadow (default)
    python -m gen.forge.promote --skeleton hours_bound --seed 3 --mode enforce --yes
    python -m gen.forge.promote --skeleton hours_bound --seed 3 --dry-run
    python -m gen.forge.promote --skeleton hours_bound --demote                  # drop the pointer, keep the file
Checks, in order: <forge-dir>/<skeleton>/seed_<n>/result.json exists and passed; the destination
<guard-dir>/<skeleton>_v<seed>.py is free (or --yes); the code passes the static lint; written to a
temp file, it loads in the runtime and declares the skeleton. Only then is the file copied and
active.json rewritten with the other entries and their order preserved. Nothing is written on a
refusal (exit 1). Rollback is --demote or `git checkout gen/forge/guardrails/active.json`."""
import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional, Union

from contracts.guardrail import check_source
from contracts.policyspec import Skeleton
from gen.forge import runtime

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FORGE_DIR = ROOT / "cache" / "forge"
DEFAULT_GUARD_DIR = runtime.GUARD_DIR
DEFAULT_MODE = "shadow"
MODES = ("shadow", "enforce")


class PromoteError(Exception):
    """A refused promotion or demotion. The message says why; nothing has been written."""


def read_pointers(guard_dir: Path) -> dict:
    p = Path(guard_dir) / "active.json"
    return json.loads(p.read_text()) if p.exists() else {}


def write_pointers(guard_dir: Path, pointers: dict) -> None:
    (Path(guard_dir) / "active.json").write_text(json.dumps(pointers, indent=2) + "\n")


def _fmt(pointer: Optional[dict]) -> str:
    return "(none)" if pointer is None else f"{pointer['file']} [{pointer['mode']}]"


def _read_result(forge_dir: Path, skeleton: Skeleton, seed: int) -> dict:
    path = Path(forge_dir) / skeleton.value / f"seed_{seed}" / "result.json"
    if not path.exists():
        raise PromoteError(f"no Forge result at {path}")
    result = json.loads(path.read_text())
    if result.get("passed") is not True:
        raise PromoteError(f"{path} is RED (passed={result.get('passed')!r} after "
                           f"{result.get('iterations')} iteration(s)); only green results are promoted")
    if result.get("skeleton") != skeleton.value:
        raise PromoteError(f"{path} is for {result.get('skeleton')!r}, not {skeleton.value}")
    if not (result.get("code") or "").strip():
        raise PromoteError(f"{path} carries no code")
    return result


def _validate(staged: Path, skeleton: Skeleton) -> None:
    """Lint the source, then import the staged file exactly as the runtime will."""
    problems = check_source(staged.read_text())
    if problems:
        raise PromoteError("static lint: " + "; ".join(problems))
    try:
        mod = runtime.load_file(staged)
    except Exception as e:  # noqa: BLE001 - any import/validation failure is a refusal
        raise PromoteError(f"does not load in the runtime: {type(e).__name__}: {e}") from None
    if mod.RULE_SKELETON != skeleton:
        raise PromoteError(f"candidate implements {mod.RULE_SKELETON.value}, not {skeleton.value}")


def promote(skeleton: Union[str, Skeleton], seed: int, mode: str = DEFAULT_MODE,
            forge_dir: Path = DEFAULT_FORGE_DIR, guard_dir: Path = DEFAULT_GUARD_DIR, *,
            dry_run: bool = False, yes: bool = False, log: Callable[[str], None] = print) -> dict:
    skeleton = Skeleton(skeleton)
    if mode not in MODES:
        raise PromoteError(f"unknown mode {mode!r}; use one of {MODES}")
    guard_dir = Path(guard_dir)
    result_path = Path(forge_dir) / skeleton.value / f"seed_{seed}" / "result.json"
    result = _read_result(Path(forge_dir), skeleton, seed)
    stem = f"{skeleton.value}_v{seed}"
    dest = guard_dir / f"{stem}.py"
    if dest.exists() and not yes:
        raise PromoteError(f"{dest} exists; pass --yes to overwrite it")
    pointers = read_pointers(guard_dir)
    before, after = pointers.get(skeleton.value), {"file": stem, "mode": mode}

    tmp = Path(tempfile.mkdtemp(prefix="ct_promote_"))
    try:
        staged = tmp / dest.name
        staged.write_text(result["code"])
        _validate(staged, skeleton)
        log(f"{result_path} -> {dest}  (seed {seed}, {result.get('iterations')} iteration(s), "
            f"model {result.get('model')}, prompt v{result.get('prompt_version', 1)})")
        log(f"active.json {skeleton.value}: {_fmt(before)} -> {_fmt(after)}")
        if dry_run:
            log("dry run: nothing written")
        else:
            shutil.copy(staged, dest)
            updated = dict(pointers)
            updated[skeleton.value] = after            # replaces in place, or appends
            write_pointers(guard_dir, updated)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"skeleton": skeleton.value, "seed": seed, "mode": mode, "dest": str(dest),
            "before": before, "after": after, "dry_run": dry_run}


def demote(skeleton: Union[str, Skeleton], guard_dir: Path = DEFAULT_GUARD_DIR, *,
           dry_run: bool = False, log: Callable[[str], None] = print) -> dict:
    """Remove the skeleton's pointer from active.json; the guardrail file stays for rollback."""
    skeleton = Skeleton(skeleton)
    guard_dir = Path(guard_dir)
    pointers = read_pointers(guard_dir)
    before = pointers.get(skeleton.value)
    if before is None:
        raise PromoteError(f"{skeleton.value} has no pointer in {guard_dir / 'active.json'}")
    log(f"active.json {skeleton.value}: {_fmt(before)} -> (none); {before['file']}.py is kept")
    if dry_run:
        log("dry run: nothing written")
    else:
        write_pointers(guard_dir, {k: v for k, v in pointers.items() if k != skeleton.value})
    return {"skeleton": skeleton.value, "before": before, "after": None, "dry_run": dry_run}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Promote a green Forge candidate (human step; never automatic)")
    ap.add_argument("--skeleton", required=True, choices=[s.value for s in Skeleton])
    ap.add_argument("--seed", type=int, help="Forge seed whose result.json to promote")
    ap.add_argument("--mode", choices=MODES, default=DEFAULT_MODE, help="shadow (default) or enforce")
    ap.add_argument("--forge-dir", type=Path, default=DEFAULT_FORGE_DIR)
    ap.add_argument("--guard-dir", type=Path, default=DEFAULT_GUARD_DIR)
    ap.add_argument("--dry-run", action="store_true", help="print everything, write nothing")
    ap.add_argument("--yes", action="store_true", help="overwrite an existing destination file")
    ap.add_argument("--demote", action="store_true", help="remove the skeleton's pointer, keep the file")
    a = ap.parse_args(argv)
    if not a.demote and a.seed is None:
        ap.error("--seed is required unless --demote")
    try:
        if a.demote:
            demote(a.skeleton, a.guard_dir, dry_run=a.dry_run)
        else:
            promote(a.skeleton, a.seed, a.mode, a.forge_dir, a.guard_dir, dry_run=a.dry_run, yes=a.yes)
    except PromoteError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
