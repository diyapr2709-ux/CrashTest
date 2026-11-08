# Crash Test

Before a small business adopts or keeps a customer-facing AI tool, Crash Test compiles the
owner's rules into a typed PolicySpec, attacks each tool with a swarm of simulated customers, and
prices every rule violation in dollars. A local Gemma developer agent (Forge) then writes a
guardrail for each failure, plus its regression tests, preferring a corrected reply (REWRITE) over
a refusal (BLOCK). The full brief is [PROJECT.md](PROJECT.md); how the agents interact, who owns
which object, and the provenance rules are in [AGENTS.md](AGENTS.md).

Every number in the UI, video or paper comes from a `metrics/*.json` file written by VAL's
evaluation code. The UI displays numbers. It never computes them.

## Quickstart

```bash
./ct setup            # create .venv (prefers python3.11) and install requirements.txt
./ct role gen         # or: ./ct role val. Once per clone; sets CT_ROLE and the Claude Code deny-list
./ct ci               # the full test suite with CACHE_ONLY=1 (what GitHub Actions runs)
./ct test             # the same suite, live calls allowed on a cache miss
./ct gates            # metric gates on metrics/latest.json (VAL)
```

`.env` (git-ignored) sets the models. Every model call goes through `common/llm.py`, which loads
this file:

```bash
BACKEND=gemma               # gemma = local Ollama (default); frontier is not wired yet
ATTACKER_MODEL=<ollama tag> # simulated customers (gen.harness)
BOT_MODEL=<ollama tag>      # the dev archetype bots under test
FORGE_MODEL=<ollama tag>    # Forge, the guardrail writer
COMPILER_MODEL=<ollama tag> # optional: the Policy Compiler; falls back to FORGE_MODEL
JUDGE_MODEL=<ollama tag>    # VAL only; must differ from ATTACKER_MODEL and BOT_MODEL
NUM_CTX=8192                # set explicitly: the default context silently truncates Forge prompts
LLM_TIMEOUT=300             # seconds per model call
CT_ROLE=gen                 # written by ./ct role; picks the cache dir cache/llm/<role>
```

`CACHE_ONLY=1` forbids live calls: outputs are read from `cache/demo/` (the curated, read-only
layer) and then `cache/llm/<role>/`. CI always runs this way.

## Run the app (Steward UI)

```bash
.venv/bin/streamlit run app/leaderboard.py
```

The app works from cache: it never calls a model and never writes a repo file. It reads
`metrics/latest.json`, or `metrics/smoke_fixtures.json` (labelled as smoke fixtures) when no
latest run exists, plus `cache/demo/transcripts/` and `cache/forge/`.

## The GEN pipeline

Run each step from the repo root as `.venv/bin/python -m <module>`. Run `--help` on any of them
for the full flags.

1. `gen.harness.run data/specs/cafe_demo.json [--out data/dev/transcripts] [--seed 1] [--personas 1] [--turns 3] [--tools dev_leaky ...]`
   plays simulated customers (attacks and benign scenarios) against each dev archetype bot in
   `data/dev/archetypes/` and writes one Transcript JSON per conversation.
2. `gen.harness.show data/dev/transcripts/t_xxx.json`, or `gen.harness.show --all [--dir data/dev/transcripts]`,
   prints transcripts as `customer:` / `bot:` lines for a human read.
3. Hand-mark the violations in `data/dev/marks.json`: a JSON list of
   `{"transcript_id": "t_...", "turn": <bot turn>, "rule_id": "r_...", "span": "<quoted text>"}`,
   with rule ids from `data/specs/cafe_demo.json`. These are GEN's DEV hand labels. They are not
   judge output, and transcripts never carry labels.
4. `gen.forge.synth --skeleton hours_bound [--spec data/specs/cafe_demo.json] [--transcripts data/dev/transcripts] [--marks data/dev/marks.json] [--seed 1] [--max-iters 3] [--out cache/forge]`
   runs Forge for one skeleton. It logs every prompt, response and pytest run under
   `cache/forge/<skeleton>/seed_<n>/` (including `result.json`) and never writes into
   `gen/forge/guardrails/`.
5. `nohup .venv/bin/python -m gen.forge.batch [--skeletons ...] [--seeds 1 2 3 4 5] [--out cache/forge] > cache/forge/batch.log 2>&1 &`
   runs synth over skeletons x seeds. It is resumable (it skips a run that already has a
   `result.json`) and writes `batch_summary.csv` and `forge_log.json`.
6. `gen.forge.replay --skeleton hours_bound --candidate cache/forge/hours_bound/seed_1/hours_bound_v1.py --transcripts data/dev/transcripts --spec data/specs/cafe_demo.json [--tool dev_leaky] --out <replay.json>`
   replays one candidate over cached transcripts and reports allow, rewrite or block for each bot
   turn. No model is called.
7. `gen.forge.promote --skeleton hours_bound --seed 3 [--mode shadow|enforce] [--dry-run] [--yes] [--demote]`
   is the human promotion step. It copies a green candidate into `gen/forge/guardrails/` and
   points `active.json` at it, in shadow mode by default. To roll back, run `--demote` or revert
   `active.json`.
8. `gen.forge.report [--dirs cache/forge ...] [--md out.md] [--csv out.csv]` summarizes Forge
   runs per skeleton and seed. These are Forge's own tests, which are necessary but not
   sufficient.
9. `gen.compiler.compile data/specs/owner_docs/cafe_demo.txt --business-id cafe_demo_02 --vertical cafe --timezone America/New_York --out data/specs/cafe_compiled_e4b.json [--seed 1] [--max-iters 3] [--economics-from data/specs/cafe_demo.json] [--log-dir cache/compiler]`
   is the Policy Compiler: it turns owner documents into a validated PolicySpec inside a repair
   loop.
10. `gen.demo.curate [--transcripts data/dev/transcripts] [--marks data/dev/marks.json] [--forge-dirs cache/forge ...] [--specs data/specs] [--metrics metrics] [--out cache/demo] [--tools dev_leaky dev_cautious dev_local] [--dry-run]`
    copies the valid transcripts to `cache/demo/transcripts/<transcript_id>.json` and the marks
    on them to `cache/demo/marks.json`. It also writes `cache/demo/manifest.json`, the provenance
    record: git SHA, spec versions, transcript tools, models and seeds, marks per rule, Forge
    runs, and VAL's metrics run id. Forge artifacts stay in `cache/forge*` and are not copied.
    Nothing else is written at the `cache/demo/` root, which also holds the read-only LLM cache.

VAL's side of the repo (`val/`, `tests/val/`, `metrics/`) evaluates these outputs and writes
`metrics/run_<time>.json` and `metrics/latest.json`.

## Rules of the road

- Read [CLAUDE.md](CLAUDE.md) before changing anything. Its **Independence rules** are what make
  the numbers trustworthy: GEN never sees VAL's private material, the judge is a different model,
  and the holdout is evaluated once.
- `contracts/` and `common/` are frozen. To change them, open a `contract-change` PR approved by
  both humans, with the affected tests updated in the same PR.
- All model calls go through `common/llm.py`, with fixed seeds and an explicit `NUM_CTX`. CI makes
  no live calls.
- Write the test first, keep `./ct ci` green before every push, and keep PRs small, prefixed
  `gen:`, `val:` or `contract:`.
- Guardrails are pure Python under `gen/forge/guardrails/`. `active.json` alone decides shadow or
  enforce, and promotion is a human step.
- Every number in the UI comes from `metrics/*.json` written by VAL. If a metric is missing, the
  UI marks it as not measured instead of filling it in.
