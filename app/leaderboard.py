"""Steward UI: the judge-facing page. Cache-first: it replays artifacts that were computed earlier
and shows a "cached" badge on each. The only live work is running a guardrail candidate's pytest
and the swarm replay (gen.forge.replay), both in subprocesses: the runtime's SIGALRM time limit is
main-thread only, so nothing here imports gen.forge.runtime. It never calls a model, never writes
a repo file, and every dollar on screen is a display string of a MetricsRun/ToolRow field written
by VAL (see app/data.py); violation counts are GEN's hand labels and are badged as such.
    .venv/bin/streamlit run app/leaderboard.py"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:                      # `streamlit run app/x.py` puts app/ first
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("CACHE_ONLY", "1")          # belt and braces: no live model calls here

import streamlit as st  # noqa: E402

from app import data  # noqa: E402

st.set_page_config(page_title="Crash Test", layout="wide")

SEVERITY_COLOR = {"money": "orange", "safety": "red", "legal": "violet"}
STATUS_COLOR = {"candidate": "grey", "shadow": "blue", "enforce": "green", "vetoed": "red"}
STATE = st.session_state


def badge(label, color="grey"):
    st.badge(label, color=color)


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def stream_text(placeholder, text: str, delay: float, render):
    """Typing effect: reveal `text` word by word through `render(partial)`."""
    if delay <= 0:
        render(placeholder, text)
        return
    shown = ""
    for word in text.split(" "):
        shown = word if not shown else f"{shown} {word}"
        render(placeholder, shown + " ▌")
        time.sleep(delay)
    render(placeholder, text)


# ---------------------------------------------------------------- data
try:
    run, metrics_path, is_smoke = data.load_metrics(ROOT)
except FileNotFoundError as e:
    st.error(str(e))
    st.stop()

spec_files = data.spec_paths(ROOT)
if not spec_files:
    st.error(f"no PolicySpec under {data.SPEC_DIR}")
    st.stop()

STATE.setdefault("status_overrides", {})   # tool_id -> status (session only, never written)
STATE.setdefault("undo", {})               # tool_id -> previous status
STATE.setdefault("sim", None)              # {"tool_id", "transcript_ids", "animate"}
STATE.setdefault("forge", None)            # {"skeleton", "animate", "result", "lint"}
STATE.setdefault("replay", None)           # {"tool_id", "skeleton", "transcript_ids", "result", "output"}
STATE.setdefault("staff", None)            # {"tool_id", "skeleton", "animate"}: the simulated channel

# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Run")
    st.caption("Metrics source")
    st.code(rel(metrics_path), language=None)
    cols = st.columns(2)
    with cols[0]:
        badge("cached", "blue")
    with cols[1]:
        if is_smoke:
            badge("smoke file", "orange")
    if is_smoke:
        st.caption("metrics/latest.json does not exist yet; showing VAL's smoke fixture numbers.")
    st.markdown(f"**run_id** `{run.run_id}`  \n**git_sha** `{run.git_sha}`  \n"
                f"**split** `{run.split}`  \n**backend** `{run.backend}`  \n**seed** `{run.seed}`")
    if run.models:
        st.caption("models: " + ", ".join(f"{k}={v}" for k, v in run.models.items()))
    with st.expander("Headline rates", expanded=False):
        for item in data.run_summary(run):
            st.markdown(f"{item['label']}: **{item['value']}**")
    if run.notes:
        st.caption(run.notes)
    st.divider()
    spec_path = st.selectbox("PolicySpec", spec_files, index=data.default_spec_index(spec_files),
                             format_func=rel, help="The hand marks cite cafe_demo.json rule ids.")
    typing_delay = st.slider("Typing speed (s per word)", 0.0, 0.1, 0.03, 0.01,
                             help="0 renders cached transcripts instantly")

spec = data.load_spec(spec_path)

# ---------------------------------------------------------------- header
st.title("Crash Test")
st.caption(f"{spec.business_id} ({spec.vertical.value}) - the owner's rules, every tool's dollar "
           f"exposure, and Forge's guardrails. Everything below is replayed from cache except the "
           f"guardrail test run and the swarm replay, which run live in subprocesses.")

# ---------------------------------------------------------------- 1. rule cards
st.subheader("Rules")
cards = data.rule_cards(spec)
per_row = 4
for start in range(0, len(cards), per_row):
    cols = st.columns(per_row)
    for col, card in zip(cols, cards[start:start + per_row]):
        with col, st.container(border=True):
            st.markdown(f"**{card['title']}**")
            badge(card["severity"], SEVERITY_COLOR.get(card["severity"], "grey"))
            st.write(card["text"])
            detail = f"`{card['skeleton']}`" + (f" - {card['params']}" if card["params"] else "")
            st.caption(detail)

# ---------------------------------------------------------------- 2. leaderboard
st.subheader("Exposure leaderboard")
rows = data.leaderboard_rows(run, STATE["status_overrides"])
hcols = st.columns([3, 1])
with hcols[0]:
    st.caption("Monthly dollar exposure per tool from VAL's metrics run; interval is [lo, hi].")
with hcols[1]:
    badge("cached" if not is_smoke else "smoke numbers", "blue" if not is_smoke else "orange")
st.dataframe(
    rows, hide_index=True,
    column_config={
        "tool": st.column_config.TextColumn("Tool"),
        "attacks": st.column_config.NumberColumn("Attacks"),
        "violations": st.column_config.NumberColumn("Violations"),
        "exposure": st.column_config.TextColumn("$ exposure / month"),
        "interval": st.column_config.TextColumn("Interval [lo, hi]"),
        "worst_failure": st.column_config.TextColumn("Worst failure", width="large"),
        "guarded": st.column_config.TextColumn("Guardrailed exposure"),
        "status": st.column_config.TextColumn("Status"),
    },
)

tool_ids = [r["tool"] for r in rows]
if not tool_ids:
    st.info("The metrics run has no tool rows yet.")
    st.stop()
tool_id = st.selectbox("Tool", tool_ids)
row = data.tool_row(run, tool_id)
status_now = STATE["status_overrides"].get(tool_id, row.status)

info = st.columns(4)
info[0].markdown(f"**{tool_id}**")
with info[1]:
    badge(f"status: {status_now}", STATUS_COLOR.get(status_now, "grey"))
info[2].caption(f"worst rule: {row.worst_rule_id or data.NOT_MEASURED}")
info[3].caption(f"worst transcript: {row.worst_transcript_id or data.NOT_MEASURED}")

# ---------------------------------------------------------------- 3. simulate attack (persona swarm)
def render_counter(ph, n: int):
    with ph.container():
        st.metric("violations flagged (hand labels)", n,
                  help="Count of GEN's hand-labelled violations (data/dev/marks.json) on the five "
                       "transcripts shown. Not judge output and not dollars.")


def flash_violation(box, mk: dict, t, animate: bool):
    """A red badge under the bot turn that carries a hand mark; a toast while animating."""
    try:
        severity = spec.rule(mk["rule_id"]).severity
    except KeyError:
        severity = "unknown rule"
    box.badge(f"VIOLATION · {mk['rule_id']}", color="red")
    box.caption(f"{severity} · \"{mk['span']}\" · hand label")
    if animate:
        st.toast(f"VIOLATION · {mk['rule_id']} · {t.persona_id} ({t.attack_class})")


def show_replay_hit(box, hit: dict, skeleton_name: str):
    """What the candidate guardrail did to this turn in the live replay (rewrite or block)."""
    if hit["action"] == "rewrite":
        box.badge(f"rewritten · {skeleton_name}", color="green")
    else:
        box.badge(f"blocked · {skeleton_name}", color="orange")
    if hit["evidence"]:
        box.caption(f"guardrail evidence: {hit['evidence']}")
    if hit["rewrite"]:
        with box.expander("rewritten reply"):
            st.write(hit["rewrite"])


def stream_swarm(cols, picks, marks, delay: float, counter_ph, changed: dict, skeleton_name: str) -> int:
    """Five fake customers at once: every tick reveals one more word in each column, so the
    transcripts type in parallel. Bot turns with a hand mark flash red and bump the counter."""
    flagged = 0
    render_counter(counter_ph, flagged)
    streams = [{"col": col, "t": t, "marks": data.marks_by_turn(marks, t.transcript_id),
                "i": 0, "words": [], "k": 0, "box": None, "ph": None} for col, t in zip(cols, picks)]
    budget = sum(len(m.text.split(" ")) + 1 for t in picks for m in t.messages) + 1
    for _ in range(budget):                                  # bounded: one word per column per tick
        active = False
        for s in streams:
            t = s["t"]
            if s["i"] >= len(t.messages):
                continue
            active = True
            m = t.messages[s["i"]]
            if s["ph"] is None:
                s["box"] = s["col"].chat_message("user" if m.role == "customer" else "assistant")
                s["ph"] = s["box"].empty()
                s["words"], s["k"] = m.text.split(" "), 0
            s["k"] = len(s["words"]) if delay <= 0 else s["k"] + 1
            if s["k"] < len(s["words"]):
                s["ph"].markdown(f"**{m.role}:** {' '.join(s['words'][:s['k']])} ▌")
                continue
            s["ph"].markdown(f"**{m.role}:** {m.text}")
            if m.role == "bot":
                for mk in s["marks"].get(m.turn, []):
                    flagged += 1
                    flash_violation(s["box"], mk, t, delay > 0)
                    render_counter(counter_ph, flagged)
                hit = changed.get((t.transcript_id, m.turn))
                if hit is not None:
                    show_replay_hit(s["box"], hit, skeleton_name)
            s["i"], s["ph"] = s["i"] + 1, None
        if not active:
            break
        if delay > 0:
            time.sleep(delay)
    return flagged


def show_exposure(ph, row_):
    """The tool's exposure, as display strings of ToolRow fields from the metrics file."""
    s = data.exposure_summary(row_)
    with ph.container(border=True):
        st.metric(f"exposure · {row_.tool_id}", s["exposure"],
                  help="usd_month from the metrics file written by VAL; the UI never computes dollars.")
        st.caption(f"{s['interval']} · worst: {s['worst']}")
        bc = st.columns([1, 2])
        with bc[0]:
            badge("cached" if not is_smoke else "smoke numbers", "blue" if not is_smoke else "orange")
        bc[1].caption(f"from {rel(metrics_path)}")


st.subheader("Simulate attack")
transcripts = data.demo_transcripts(ROOT, tool_id)
tdir = data.transcripts_dir(ROOT)
marks = data.load_marks(ROOT)
swarm = data.swarm_transcripts(transcripts, marks, k=5)
swarm_ids = [t.transcript_id for t in swarm]
sim_live = False
if not swarm:
    st.info(f"No cached attack transcripts for {tool_id} yet (looked in {data.DEMO_TRANSCRIPTS} and "
            f"{data.DEV_TRANSCRIPTS}). Generate the dev set with gen.harness.run, or copy curated "
            f"ones into cache/demo/transcripts.")
else:
    bcols = st.columns([1, 1, 2])
    if bcols[0].button("Simulate attack", type="primary"):
        STATE["sim"] = {"tool_id": tool_id, "transcript_ids": swarm_ids, "animate": True}
        STATE["replay"] = None
    with bcols[1]:
        badge("cached", "blue")
        st.caption(rel(tdir))
    with bcols[2]:
        badge("hand labels", "orange")
        st.caption(f"{len(swarm)} fake customers, one per attack class; violations are GEN's hand marks "
                   f"from {data.MARKS_PATH} ({len(marks)} total), not judge output.")

    sim = STATE["sim"]
    sim_live = bool(sim and sim["tool_id"] == tool_id and sim["transcript_ids"] == swarm_ids)
    rp = STATE["replay"]
    replay_live = bool(sim_live and rp and rp["tool_id"] == tool_id and rp["result"] is not None
                       and rp["transcript_ids"] == swarm_ids)
    changed = data.replay_by_turn(rp["result"]) if replay_live else {}

    top = st.columns(3)
    counter_ph, remaining_ph, exposure_ph = top[0].empty(), top[1].empty(), top[2].empty()
    cols = st.columns(len(swarm))
    for col, t in zip(cols, swarm):
        with col:
            st.markdown(f"**{t.persona_id}**")
            st.caption(f"{t.attack_class} · {t.transcript_id} · seed {t.seed}")
    if sim_live:
        delay = typing_delay if sim["animate"] else 0.0
        stream_swarm(cols, swarm, marks, delay, counter_ph, changed, rp["skeleton"] if replay_live else "")
        sim["animate"] = False
        show_exposure(exposure_ph, row)
        if replay_live:
            subset = [m for m in data.marks_for(marks, spec, rp["skeleton"]) if m["transcript_id"] in swarm_ids]
            remaining, total = data.violations_remaining(subset, rp["result"]["turns"])
            with remaining_ph.container():
                st.metric(f"violations remaining · {rp['skeleton']}", f"{remaining} of {total}",
                          help="Hand labels for this skeleton's rules on the five transcripts shown, "
                               "re-checked by the live replay: a mark counts as fixed only when the "
                               "guardrail rewrote or blocked that turn.")
    else:
        render_counter(counter_ph, 0)
        exposure_ph.caption("Exposure appears here when all five customers finish.")

st.divider()

# ---------------------------------------------------------------- 4. write guardrail
st.subheader("Write guardrail")
choices = data.skeleton_choices(spec)
default_skel = data.default_skeleton(spec, row)
skeleton = st.selectbox("Skeleton", choices, index=choices.index(default_skel))
try:
    art = data.forge_artifact(ROOT, skeleton)
except FileNotFoundError as e:
    st.error(str(e))
    art = None
if art is not None:
    bcols = st.columns([1, 2])
    if bcols[0].button("Write guardrail", type="primary"):
        STATE["forge"] = {"skeleton": skeleton, "animate": True, "result": None,
                          "lint": data.lint_guardrail(art["code"])}
    with bcols[1]:
        if art["source"] == "forge":
            badge("cached", "blue")
            st.caption(f"{art['path']} - seed {art['seed']}, {art['iterations']} iteration(s), "
                       f"Forge's own tests {'passed' if art['passed'] else 'failed'}")
        else:
            badge("reference", "violet")
            st.caption(f"No Forge artifact under {data.FORGE_CACHE}/{skeleton}; showing the "
                       f"hand-written {art['skeleton']} guardrail from {art['path']}.")
    forge = STATE["forge"]
    if forge and forge["skeleton"] == skeleton:
        code_col, side_col = st.columns([3, 2])
        with code_col:
            code_ph = st.empty()
            if forge["animate"]:
                lines = art["code"].splitlines()
                shown = []
                for line in lines:
                    shown.append(line)
                    code_ph.code("\n".join(shown), language="python")
                    time.sleep(min(typing_delay, 0.02))
                forge["animate"] = False
            code_ph.code(art["code"], language="python")
        with side_col:
            if forge["lint"]:
                st.warning("Contract lint would refuse to load this file: " + "; ".join(forge["lint"]))
            with st.expander("Regression tests", expanded=False):
                st.code(art["tests"], language="python")
            if forge["result"] is None:
                with st.spinner("Running the guardrail's pytest in a subprocess..."):
                    forge["result"] = data.run_guardrail_tests(art["code"], art["tests"],
                                                               run_dir=art["run_dir"],
                                                               module_file=art["module_file"])
            passed, output = forge["result"]
            rcols = st.columns([3, 1])
            with rcols[0]:
                if passed:
                    st.success("pytest passed (live subprocess run, not cached)")
                else:
                    st.error("pytest failed (live subprocess run, not cached)")
            with rcols[1]:
                badge("live pytest", "green" if passed else "red")
            with st.expander("pytest output", expanded=not passed):
                st.code(output or "(no output)", language=None)

            # -------------------------------------------------- 4b. re-run the swarm through it
            st.markdown("**Re-run the swarm with this guardrail**")
            same_skeleton = art["skeleton"] == skeleton
            can_replay = bool(passed and sim_live and tdir is not None and same_skeleton)
            if not passed:
                st.caption("Available once the guardrail's own tests pass.")
            elif not same_skeleton:
                st.caption(f"The reference guardrail implements {art['skeleton']}, not {skeleton}; the "
                           f"replay needs a Forge artifact for {skeleton} under {data.FORGE_CACHE}.")
            elif not sim_live:
                st.caption("Simulate the attack first: the replay re-checks those five transcripts.")
            if st.button("Re-run the swarm with this guardrail", disabled=not can_replay):
                with st.spinner(f"Replaying {tool_id} through {skeleton} in a subprocess "
                                f"(gen.forge.replay, CACHE_ONLY=1)..."):
                    result, rp_output = data.run_replay(
                        art["code"], skeleton=skeleton, transcripts_dir=tdir, spec_path=spec_path,
                        tool_id=tool_id)
                STATE["replay"] = {"tool_id": tool_id, "skeleton": skeleton, "transcript_ids": list(swarm_ids),
                                   "result": result, "output": rp_output}
                st.rerun()
            rp = STATE["replay"]
            if rp and rp["tool_id"] == tool_id and rp["skeleton"] == skeleton:
                if rp["result"] is None:
                    if rp["output"] == data.REPLAY_UNAVAILABLE:
                        st.info(rp["output"])
                    else:
                        st.error("The replay did not produce a result (fail closed).")
                        st.code(rp["output"] or "(no output)", language=None)
                else:
                    r = rp["result"]
                    subset = [m for m in data.marks_for(marks, spec, skeleton)
                              if m["transcript_id"] in rp["transcript_ids"]]
                    remaining, total = data.violations_remaining(subset, r["turns"])
                    st.success(f"violations remaining: {remaining} of {total} (hand labels for "
                               f"{skeleton} rules on the five transcripts shown)")
                    st.caption(f"replay counts: {r['n_bot_turns']} bot turns checked, {r['n_flagged']} "
                               f"flagged, {r['n_rewritten']} rewritten, {r['n_blocked']} blocked. "
                               f"Counts only; the rewritten/blocked badges are now on the swarm above.")
                    badge("live replay", "green")
                    with st.expander("replay output", expanded=False):
                        st.code(rp["output"] or "(no output)", language=None)

        # ------------------------------------------------------ 5. approve to shadow / undo
        st.markdown("**Promotion**")
        st.caption("Session only: this flips the status on screen. The real promotion is a "
                   "pointer edit in gen/forge/guardrails/active.json through a PR; the app never "
                   "writes repo files.")
        acols = st.columns([1, 1, 3])
        can_approve = status_now == "candidate" and passed
        if not passed:
            st.caption("Approval is disabled while the guardrail's tests fail (fail closed).")
        if acols[0].button("Approve to shadow", disabled=not can_approve, type="primary"):
            STATE["undo"][tool_id] = status_now
            STATE["status_overrides"][tool_id] = "shadow"
            STATE["staff"] = {"tool_id": tool_id, "skeleton": skeleton, "animate": True}
            st.rerun()
        if tool_id in STATE["undo"]:
            if acols[1].button("Undo"):
                STATE["status_overrides"][tool_id] = STATE["undo"].pop(tool_id)
                if STATE["status_overrides"][tool_id] == row.status:
                    del STATE["status_overrides"][tool_id]
                STATE["staff"] = None
                st.rerun()
        if tool_id in STATE["status_overrides"]:
            st.info(f"{tool_id} is now **{STATE['status_overrides'][tool_id]}** for this session "
                    f"(metrics file still says {row.status}).")

        staff = STATE["staff"]
        if staff and staff["tool_id"] == tool_id and STATE["status_overrides"].get(tool_id) == "shadow":
            with st.container(border=True):
                hc = st.columns([3, 1])
                hc[0].markdown("**Staff channel (simulated)**")
                with hc[1]:
                    badge("simulated", "orange")
                st.caption("Simulated: canned reactions, not real messages; no staff channel is wired "
                           "up. Undo clears this panel.")
                for line in data.staff_reactions(staff["skeleton"], tool_id):
                    ph = st.empty()
                    if staff["animate"]:
                        time.sleep(0.7)
                    who, _, said = line.partition(": ")
                    ph.markdown(f"> **{who}:** {said}")
                staff["animate"] = False
