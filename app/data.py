"""Pure helpers behind the Steward UI (app/leaderboard.py). No streamlit import: everything here
is unit-tested in tests/gen/test_app.py.
Every number the UI shows is a *display string of a field* in a MetricsRun that VAL wrote
(metrics/latest.json, else metrics/smoke_fixtures.json). This module formats; it never computes
dollars. It never writes to a repo file: guardrail tests run in a throw-away temp dir."""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from contracts.guardrail import check_source
from contracts.metrics import MetricsRun, ToolRow
from contracts.policyspec import PolicySpec, Rule, Skeleton
from contracts.transcript import Transcript

ROOT = Path(__file__).resolve().parents[1]
METRICS_LATEST = Path("metrics/latest.json")
METRICS_SMOKE = Path("metrics/smoke_fixtures.json")
DEMO_TRANSCRIPTS = Path("cache/demo/transcripts")
DEV_TRANSCRIPTS = Path("data/dev/transcripts")
FORGE_CACHE = Path("cache/forge")
REFERENCE_GUARDRAIL = Path("gen/forge/guardrails/price_match.py")
REFERENCE_TESTS = Path("tests/gen/test_price_match.py")
CONFTEST = Path("tests/conftest.py")
SPEC_DIR = Path("data/specs")
NOT_MEASURED = "—"  # em dash: the field is null in the metrics file
MAX_OUTPUT_CHARS = 6000


# ---------------------------------------------------------------- metrics
def load_metrics(root: Path = ROOT) -> Tuple[MetricsRun, Path, bool]:
    """(run, source_path, is_smoke). latest.json wins; the smoke fixture file is labelled."""
    for rel, is_smoke in ((METRICS_LATEST, False), (METRICS_SMOKE, True)):
        path = root / rel
        if path.exists():
            return MetricsRun.model_validate_json(path.read_text()), path, is_smoke
    raise FileNotFoundError(f"no metrics file: expected {root / METRICS_LATEST} or {root / METRICS_SMOKE}")


def _usd(x: float) -> str:
    """$10 for whole dollars, $4.75 otherwise. Formatting only."""
    return f"${x:,.0f}" if float(x).is_integer() else f"${x:,.2f}"


def usd_month(x: Optional[float]) -> str:
    return NOT_MEASURED if x is None else f"${x:,.0f}/mo"


def interval(lo: float, hi: float) -> str:
    return f"[${lo:,.0f}, ${hi:,.0f}]"


def worst_failure(row: ToolRow) -> str:
    if not row.worst_rule_id:
        return "none found"
    text = row.worst_rule_id
    if row.worst_span:
        text += f': "{row.worst_span}"'
    if row.worst_usd is not None:
        text += f" ({_usd(row.worst_usd)})"
    return text


def leaderboard_rows(run: MetricsRun, status_overrides: Optional[Dict[str, str]] = None) -> List[dict]:
    """One display row per tool, highest exposure first. Dollar fields are preformatted strings;
    counts stay ints. status_overrides (tool_id -> status) is the UI's session-only approval."""
    overrides = status_overrides or {}
    rows = []
    for t in sorted(run.tools, key=lambda t: (-t.usd_month, t.tool_id)):
        rows.append({
            "tool": t.tool_id,
            "attacks": t.n_attacks,
            "violations": t.n_violations,
            "exposure": usd_month(t.usd_month),
            "interval": interval(t.usd_month_lo, t.usd_month_hi),
            "worst_failure": worst_failure(t),
            "guarded": usd_month(t.guarded_usd_month),
            "status": overrides.get(t.tool_id, t.status),
        })
    return rows


def tool_row(run: MetricsRun, tool_id: str) -> Optional[ToolRow]:
    return next((t for t in run.tools if t.tool_id == tool_id), None)


def _pct(x: Optional[float]) -> str:
    return NOT_MEASURED if x is None else f"{x:.0%}"


def run_summary(run: MetricsRun) -> List[dict]:
    """Headline rates for the sidebar, each a {label, value} display pair."""
    return [
        {"label": "catch rate", "value": _pct(run.catch_rate)},
        {"label": "false block rate", "value": _pct(run.false_block_rate)},
        {"label": "false intervention rate", "value": _pct(run.false_intervention_rate)},
        {"label": "pass@1", "value": _pct(run.pass_at_1)},
        {"label": "judge precision", "value": _pct(run.judge_precision)},
        {"label": "judge recall", "value": _pct(run.judge_recall)},
        {"label": "transcripts", "value": str(run.n_transcripts)},
    ]


# ---------------------------------------------------------------- spec / rule cards
def spec_paths(root: Path = ROOT) -> List[Path]:
    return sorted((root / SPEC_DIR).glob("*.json"))


def load_spec(path: Path) -> PolicySpec:
    return PolicySpec.model_validate_json(path.read_text())


DEMO_SPEC_NAME = "cafe_demo.json"   # the spec the hand marks (data/dev/marks.json) cite rule ids from


def default_spec_index(paths: List[Path]) -> int:
    """Index of the demo spec in the selectbox list (0 when it is absent)."""
    return next((i for i, p in enumerate(paths) if p.name == DEMO_SPEC_NAME), 0)


def rule_title(rule_id: str) -> str:
    name = re.sub(r"^r_", "", rule_id).replace("_", " ").strip() or rule_id
    return name[:1].upper() + name[1:]


def _words(token: str) -> str:
    return str(token).replace("_", " ")


def params_in_words(rule: Rule) -> str:
    p = rule.params
    parts = []
    if rule.skeleton == Skeleton.COMMITMENT_CAP:
        parts.append(f"{_words(p['type'])} cap {_usd(p['max_usd'])}")
    elif rule.skeleton == Skeleton.ESCALATION_REQUIRED:
        parts.append(f"min order {_usd(p['min_order_usd'])}")
    elif rule.skeleton == Skeleton.FORBIDDEN_COMMITMENT:
        parts.append("types: " + ", ".join(_words(t) for t in p["types"]))
    elif rule.skeleton == Skeleton.LEAD_TIME_BOUND:
        n = p["min_days"]
        unit = ("business day" if p.get("business_days") else "day") + ("" if n == 1 else "s")
        parts.append(f"{_words(p['service'])}: min {n} {unit}")
    elif p:
        parts.append(", ".join(f"{_words(k)} {v}" for k, v in p.items()))
    if rule.fine_usd is not None:
        parts.append(f"fine {_usd(rule.fine_usd)}")
    if rule.enforcement_prob is not None:
        parts.append(f"enforcement {rule.enforcement_prob:.0%}")
    return "; ".join(parts)


def rule_cards(spec: PolicySpec) -> List[dict]:
    return [{
        "rule_id": r.rule_id,
        "title": rule_title(r.rule_id),
        "text": r.description,
        "severity": r.severity,
        "skeleton": r.skeleton.value,
        "params": params_in_words(r),
    } for r in spec.rules]


def default_skeleton(spec: PolicySpec, row: Optional[ToolRow]) -> str:
    """The skeleton of the tool's worst rule, else price_match (the only reference guardrail)."""
    if row is not None and row.worst_rule_id:
        try:
            return spec.rule(row.worst_rule_id).skeleton.value
        except KeyError:
            pass
    return Skeleton.PRICE_MATCH.value


def skeleton_choices(spec: PolicySpec) -> List[str]:
    """Skeletons the spec uses first (in rule order), then the rest of the contract's list."""
    used = []
    for r in spec.rules:
        if r.skeleton.value not in used:
            used.append(r.skeleton.value)
    return used + [s.value for s in Skeleton if s.value not in used]


# ---------------------------------------------------------------- transcripts
def transcripts_dir(root: Path = ROOT) -> Optional[Path]:
    """cache/demo/transcripts when it holds any JSON, else data/dev/transcripts, else None."""
    for rel in (DEMO_TRANSCRIPTS, DEV_TRANSCRIPTS):
        d = root / rel
        if d.is_dir() and any(d.glob("*.json")):
            return d
    return None


def demo_transcripts(root: Path, tool_id: str) -> List[Transcript]:
    """Cached transcripts for one tool, attacks first. Files that fail validation are skipped."""
    d = transcripts_dir(root)
    if d is None:
        return []
    ts = []
    for p in sorted(d.glob("*.json")):
        try:
            t = Transcript.model_validate_json(p.read_text())
        except Exception:
            continue
        if t.tool_id == tool_id:
            ts.append(t)
    return sorted(ts, key=lambda t: (t.benign, t.attack_class or "", t.persona_id, t.transcript_id))


def pick_transcript(ts: List[Transcript], preferred_id: Optional[str]) -> Optional[Transcript]:
    if not ts:
        return None
    return next((t for t in ts if t.transcript_id == preferred_id), ts[0])


# ---------------------------------------------------------------- swarm: hand marks + five columns
MARKS_PATH = Path("data/dev/marks.json")
_MARK_KEYS = {"transcript_id", "turn", "rule_id", "span"}


def load_marks(root: Path = ROOT) -> List[dict]:
    """GEN's hand-labelled violations [{transcript_id, turn, rule_id, span}] (DEV only). These are
    *labels*, not judge output: the UI badges them "hand labels". [] when absent or unreadable."""
    path = root / MARKS_PATH
    if not path.is_file():
        return []
    try:
        marks = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(marks, list):
        return []
    return [m for m in marks if isinstance(m, dict) and _MARK_KEYS <= set(m)]


def swarm_transcripts(transcripts: List[Transcript], marks: List[dict], k: int = 5) -> List[Transcript]:
    """Up to k attack transcripts, one per attack_class (classes in alphabetical order), preferring
    the transcript in each class with the most hand marks; ties broken by persona then id, so the
    result does not depend on the input order. Benign transcripts never join the swarm."""
    n_marks: Dict[str, int] = {}
    for m in marks:
        n_marks[m["transcript_id"]] = n_marks.get(m["transcript_id"], 0) + 1
    by_class: Dict[str, List[Transcript]] = {}
    for t in transcripts:
        if t.benign or not t.attack_class:
            continue
        by_class.setdefault(t.attack_class, []).append(t)
    picks = []
    for cls in sorted(by_class):
        picks.append(min(by_class[cls], key=lambda t: (-n_marks.get(t.transcript_id, 0),
                                                       t.persona_id, t.transcript_id)))
    return picks[:k]


def marks_by_turn(marks: List[dict], transcript_id: str) -> Dict[int, List[dict]]:
    """turn -> marks on that turn, for one transcript (mark order preserved)."""
    out: Dict[int, List[dict]] = {}
    for m in marks:
        if m["transcript_id"] == transcript_id:
            out.setdefault(int(m["turn"]), []).append(m)
    return out


def marks_for(marks: List[dict], spec: PolicySpec, skeleton) -> List[dict]:
    """The marks whose rule belongs to `skeleton` in this spec (unknown rule_ids drop out)."""
    name = skeleton.value if isinstance(skeleton, Skeleton) else str(skeleton)
    rule_ids = {r.rule_id for r in spec.rules if r.skeleton.value == name}
    return [m for m in marks if m["rule_id"] in rule_ids]


def exposure_summary(row: ToolRow) -> Dict[str, str]:
    """The tool's exposure as display strings of ToolRow fields (shown when the swarm finishes)."""
    return {
        "exposure": usd_month(row.usd_month),
        "interval": interval(row.usd_month_lo, row.usd_month_hi),
        "worst": worst_failure(row),
        "guarded": usd_month(row.guarded_usd_month),
    }


# ---------------------------------------------------------------- swarm replay (gen.forge.replay)
REPLAY_MODULE = Path("gen/forge/replay.py")
REPLAY_UNAVAILABLE = "swarm replay not available yet"
REPLAY_ACTIONS = ("allow", "rewrite", "block")
_REPLAY_INTS = ("n_bot_turns", "n_flagged", "n_rewritten", "n_blocked")
_REPLAY_KEYS = {"skeleton", "tool_id", "turns", *_REPLAY_INTS}
_TURN_KEYS = {"transcript_id", "turn", "action", "rule_id", "evidence", "rewrite"}


def parse_replay(obj) -> dict:
    """Validate gen.forge.replay's output JSON against its contract. Counts only, never dollars:
    {skeleton, tool_id, n_bot_turns, n_flagged, n_rewritten, n_blocked, turns: [{transcript_id,
    turn, action, rule_id, evidence, rewrite}]}. Raises ValueError naming the first problem."""
    if not isinstance(obj, dict):
        raise ValueError("replay output is not a JSON object")
    missing = sorted(_REPLAY_KEYS - set(obj))
    if missing:
        raise ValueError(f"replay output missing keys: {', '.join(missing)}")
    for key in ("skeleton", "tool_id"):
        if not isinstance(obj[key], str):
            raise ValueError(f"replay {key} must be a string")
    for key in _REPLAY_INTS:
        if isinstance(obj[key], bool) or not isinstance(obj[key], int):
            raise ValueError(f"replay {key} must be an int")
    if not isinstance(obj["turns"], list):
        raise ValueError("replay turns must be a list")
    turns = []
    for i, t in enumerate(obj["turns"]):
        if not isinstance(t, dict):
            raise ValueError(f"replay turns[{i}] is not an object")
        missing = sorted(_TURN_KEYS - set(t))
        if missing:
            raise ValueError(f"replay turns[{i}] missing keys: {', '.join(missing)}")
        if t["action"] not in REPLAY_ACTIONS:
            raise ValueError(f"replay turns[{i}] action {t['action']!r} not in {REPLAY_ACTIONS}")
        if not isinstance(t["transcript_id"], str) or isinstance(t["turn"], bool) or not isinstance(t["turn"], int):
            raise ValueError(f"replay turns[{i}] needs a string transcript_id and an int turn")
        turns.append({
            "transcript_id": t["transcript_id"], "turn": t["turn"], "action": t["action"],
            "rule_id": t["rule_id"] if t["rule_id"] is None else str(t["rule_id"]),
            "evidence": str(t["evidence"] or ""),
            "rewrite": t["rewrite"] if t["rewrite"] is None else str(t["rewrite"]),
        })
    out = {key: obj[key] for key in _REPLAY_KEYS if key != "turns"}
    out["turns"] = turns
    return out


def replay_by_turn(replay: dict) -> Dict[Tuple[str, int], dict]:
    """(transcript_id, turn) -> turn record, for the turns the guardrail changed (rewrite/block)."""
    return {(t["transcript_id"], t["turn"]): t for t in replay["turns"] if t["action"] != "allow"}


def violations_remaining(marks_subset: List[dict], replay_turns: List[dict]) -> Tuple[int, int]:
    """(remaining, total): total = the hand marks given; remaining = those whose (transcript_id,
    turn) the replay left on `allow`. A turn the replay never reported counts as remaining: the
    guardrail did not change it, so the labelled violation is still there."""
    actions = {(t["transcript_id"], t["turn"]): t["action"] for t in replay_turns}
    remaining = sum(1 for m in marks_subset
                    if actions.get((m["transcript_id"], int(m["turn"])), "allow") == "allow")
    return remaining, len(marks_subset)


def staff_reactions(skeleton: str, tool_id: str) -> List[str]:
    """Canned crew lines for the simulated staff channel (clearly labelled simulated in the UI)."""
    first = {
        "forbidden_commitment": "Rosa: finally, no more free croissants",
        "commitment_cap": "Rosa: so it stops handing out $96 refunds without asking me? good",
        "hours_bound": "Rosa: no more 'open till 8 tonight' on a Tuesday, thank you",
        "price_match": "Rosa: cappuccino is $4.75, not $3.80. about time",
        "escalation_required": "Rosa: big orders come to me now instead of the bot? finally",
        "allergen_claim": "Rosa: it stopped telling people the croissant is nut-free. thank god",
    }.get(skeleton, f"Rosa: about time the bot stopped promising things we do not do ({skeleton})")
    return [
        first,
        f"Sam: shadow mode only? ok, watching the log for {tool_id}",
        f"Priya: {skeleton} guardrail is in shadow: rewrites are logged, not applied, until we promote it",
        "Marco: ping me if it starts rewriting normal replies, the regulars will notice",
    ]


# ---------------------------------------------------------------- forge artifacts
_SEED_DIR = re.compile(r"^seed_(\d+)$")
_RESULT_KEYS = {"skeleton", "seed", "iterations", "passed", "code", "tests"}


def _forge_results(root: Path, skeleton: str) -> List[dict]:
    out = []
    base = root / FORGE_CACHE / skeleton
    if not base.is_dir():
        return out
    for d in base.iterdir():
        m = _SEED_DIR.match(d.name)
        path = d / "result.json"
        if not m or not path.is_file():
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not _RESULT_KEYS <= set(data):
            continue
        data = dict(data)
        data["seed"] = int(m.group(1))
        data["path"] = str(path.relative_to(root))
        out.append(data)
    return out


def forge_artifact(root: Path, skeleton: str) -> dict:
    """Forge's cached candidate for a skeleton: a passing one with the lowest seed, else the lowest
    seed overall (source="forge"). Without any, the hand-written reference guardrail and its tests
    (source="reference", passed=None until the UI runs them)."""
    results = _forge_results(root, skeleton)
    if results:
        passing = [r for r in results if r["passed"] is True]
        best = min(passing or results, key=lambda r: r["seed"])
        return {
            "skeleton": best["skeleton"], "seed": best["seed"], "iterations": best["iterations"],
            "passed": bool(best["passed"]), "code": best["code"], "tests": best["tests"],
            "source": "forge", "path": best["path"],
            # Forge's tests read corpus.json next to themselves and load the candidate by its
            # versioned file name (load_file(HERE / "<skeleton>_v1.py")); run_guardrail_tests
            # recreates both in the temp dir.
            "run_dir": str(Path(best["path"]).parent),
            "module_file": Path(best["code_path"]).name if best.get("code_path") else None,
        }
    code_path, tests_path = root / REFERENCE_GUARDRAIL, root / REFERENCE_TESTS
    if not (code_path.is_file() and tests_path.is_file()):
        raise FileNotFoundError(f"no Forge artifact for {skeleton} under {root / FORGE_CACHE} "
                                f"and no reference guardrail at {code_path}")
    return {
        "skeleton": Skeleton.PRICE_MATCH.value, "seed": None, "iterations": None, "passed": None,
        "code": code_path.read_text(), "tests": tests_path.read_text(),
        "source": "reference", "path": str(REFERENCE_GUARDRAIL), "run_dir": None, "module_file": None,
    }


def lint_guardrail(code: str) -> List[str]:
    """The contract's static lint (same one the runtime applies before loading a guardrail)."""
    return check_source(code)


# ---------------------------------------------------------------- running a candidate's tests
_RULE_SKELETON = re.compile(r"RULE_SKELETON\s*=\s*Skeleton\.([A-Z_]+)")
_TEST_IMPORT = re.compile(r"from\s+gen\.forge\.guardrails\s+import\s+(\w+)")


def guess_skeleton(code: str, tests: str = "") -> str:
    m = _RULE_SKELETON.search(code)
    if m:
        return m.group(1).lower()
    m = _TEST_IMPORT.search(tests)
    return m.group(1) if m else "guardrail"


def rewrite_imports(tests: str, skeleton: str) -> str:
    """Point the tests at the candidate module in the temp dir instead of the repo's copy."""
    tests = re.sub(rf"^(\s*)from gen\.forge\.guardrails import {skeleton}\b",
                   rf"\1import {skeleton}", tests, flags=re.M)
    tests = re.sub(rf"^(\s*)from gen\.forge\.guardrails\.{skeleton} import",
                   rf"\1from {skeleton} import", tests, flags=re.M)
    tests = re.sub(rf"^(\s*)import gen\.forge\.guardrails\.{skeleton}(\s+as\s+\w+)",
                   rf"\1import {skeleton}\2", tests, flags=re.M)
    return tests


_FALLBACK_CONFTEST = '''import copy, json
from pathlib import Path
import pytest
from contracts.policyspec import PolicySpec
SPEC_PATH = Path("data/specs/cafe_demo.json")

@pytest.fixture
def spec_dict():
    return copy.deepcopy(json.loads(SPEC_PATH.read_text()))

@pytest.fixture
def spec(spec_dict):
    return PolicySpec.model_validate(spec_dict)
'''


def _trim(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "...\n" + text[-limit:]


_HERE_FILE = re.compile(r"""HERE\s*/\s*["']([\w\-]+\.py)["']""")


def candidate_filenames(tests: str, skeleton: str, module_file: Optional[str] = None) -> List[str]:
    """Every name the candidate must be written under in the temp dir: <skeleton>.py (for `import
    <skeleton>`), the artifact's own file name, and any HERE / "<name>.py" the tests load_file."""
    names = [f"{skeleton}.py"]
    for name in ([module_file] if module_file else []) + _HERE_FILE.findall(tests):
        if name not in names and not name.startswith("test_"):
            names.append(name)
    return names


def sidecar_files(run_dir: Optional[Path]) -> List[Path]:
    """The JSON data files a Forge run left next to its tests (corpus.json: spec + cases), which the
    generated tests read via Path(__file__).parent. result.json is the artifact itself, not data."""
    if run_dir is None or not Path(run_dir).is_dir():
        return []
    return sorted(p for p in Path(run_dir).glob("*.json") if p.name != "result.json")


def run_guardrail_tests(code: str, tests: str, timeout: int = 60, *, root: Path = ROOT,
                        skeleton: Optional[str] = None, run_dir: Optional[Path] = None,
                        module_file: Optional[str] = None) -> Tuple[bool, str]:
    """Write the candidate (as <skeleton>.py plus every file name the tests load it by) and
    test_<skeleton>.py to a fresh temp dir and run pytest there in a subprocess (cwd=repo root,
    PYTHONPATH=repo root, CACHE_ONLY=1). The repo's tests/conftest.py rides along so the `spec`
    fixtures work, and so do the JSON sidecars of `run_dir` (a Forge artifact's corpus.json,
    relative to root when not absolute). Returns (passed, trimmed output)."""
    skeleton = skeleton or guess_skeleton(code, tests)
    if run_dir is not None and not Path(run_dir).is_absolute():
        run_dir = root / run_dir
    with tempfile.TemporaryDirectory(prefix="ct_forge_") as tmp:
        tmp_path = Path(tmp)
        for name in candidate_filenames(tests, skeleton, module_file):
            (tmp_path / name).write_text(code)
        test_file = tmp_path / f"test_{skeleton}.py"
        test_file.write_text(rewrite_imports(tests, skeleton))
        conftest = root / CONFTEST
        (tmp_path / "conftest.py").write_text(conftest.read_text() if conftest.is_file()
                                              else _FALLBACK_CONFTEST)
        for sidecar in sidecar_files(run_dir):
            (tmp_path / sidecar.name).write_bytes(sidecar.read_bytes())
        env = dict(os.environ, PYTHONPATH=str(root), CACHE_ONLY="1", PYTHONDONTWRITEBYTECODE="1")
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(test_file)]
        try:
            proc = subprocess.run(cmd, cwd=str(root), env=env, capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired as e:
            partial = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            return False, _trim(f"timed out after {timeout}s\n{partial}")
        output = proc.stdout + (("\n" + proc.stderr) if proc.stderr.strip() else "")
        return proc.returncode == 0, _trim(output)


def run_replay(code: str, *, skeleton: str, transcripts_dir: Path, spec_path: Path, tool_id: str,
               timeout: int = 120, root: Path = ROOT) -> Tuple[Optional[dict], str]:
    """Re-run the swarm's cached transcripts through a candidate guardrail with gen.forge.replay in a
    subprocess (cwd=repo root, PYTHONPATH=repo root, CACHE_ONLY=1); the runtime's SIGALRM time
    limit only works on a main thread, so it must never run inside Streamlit. The candidate code is
    written to a temp file that is deleted afterwards. Returns (parsed replay | None, output); the
    output explains every None (module missing, load error, timeout, garbage)."""
    if not (root / REPLAY_MODULE).is_file():
        return None, REPLAY_UNAVAILABLE
    with tempfile.TemporaryDirectory(prefix="ct_replay_") as tmp:
        candidate = Path(tmp) / f"{skeleton}.py"
        candidate.write_text(code)
        out_path = Path(tmp) / "replay.json"
        cmd = [sys.executable, "-m", "gen.forge.replay", "--skeleton", skeleton,
               "--candidate", str(candidate), "--transcripts", str(transcripts_dir),
               "--spec", str(spec_path), "--tool", tool_id, "--out", str(out_path)]
        env = dict(os.environ, PYTHONPATH=str(root), CACHE_ONLY="1", PYTHONDONTWRITEBYTECODE="1")
        try:
            proc = subprocess.run(cmd, cwd=str(root), env=env, capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired as e:
            partial = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            return None, _trim(f"timed out after {timeout}s\n{partial}")
        output = proc.stdout + (("\n" + proc.stderr) if proc.stderr.strip() else "")
        payload = None
        if out_path.is_file():
            try:
                payload = json.loads(out_path.read_text())
            except ValueError as e:
                return None, _trim(f"replay wrote invalid JSON: {e}\n{output}")
        else:
            try:                                           # a load error may come on stdout instead
                payload = json.loads(proc.stdout) if proc.stdout.strip() else None
            except ValueError:
                payload = None
        if isinstance(payload, dict) and "error" in payload:
            return None, _trim(f"replay error (exit {proc.returncode}): {payload['error']}\n{output}")
        if payload is None:
            return None, _trim(f"replay exited {proc.returncode} without writing {out_path.name}\n{output}")
        try:
            return parse_replay(payload), _trim(output)
        except ValueError as e:
            return None, _trim(f"replay output malformed: {e}\n{output}")
