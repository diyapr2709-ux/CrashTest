# Crash Test: rules for every Claude session (read fully before changing anything)

## Product
Before a small business adopts or keeps a customer-facing AI tool, Crash Test compiles the owner's
rules into a PolicySpec, attacks each tool with a persona swarm, prices every violation in dollars,
and has a local Gemma developer agent (Forge) write guardrails plus regression tests. Guardrails
prefer REWRITE (corrected reply) over BLOCK. Video due Sun Nov 8; Gemma paper due Nov 12.

## Two accounts, two repos
- GEN (Diya, this repo): gen/, app/, data/specs/, tests/gen/. Policy Compiler, Persona Swarm,
  Harness, Forge, UI, demo cache.
- VAL (Anirudh, this repo + PRIVATE repo crashtest-val-private): in this repo val/ (gates, pricer,
  preregistration), tests/val/, metrics/. In the private repo: judge prompts, archetype bot prompts,
  hand labels, dev_private split, holdout, hidden tests, Bypass Hunter. Never commit those here.
- contracts/ and common/ are FROZEN: change only via a `contract-change` PR approved by both humans,
  with affected tests updated in the same PR.
- `./ct role gen` or `./ct role val` once per clone; launch Claude Code from the repo root.

## Independence rules (these make the numbers trustworthy)
1. GEN never sees VAL's private repo, labels, judge prompts or hidden tests. Deny-lists are a
   guardrail, not security: the real boundary is the private repo.
2. JUDGE_MODEL differs from ATTACKER_MODEL and BOT_MODEL, ideally from a different model family.
3. VAL publishes DEV-only archetype bots (prompts + knobs) in data/dev/archetypes/. The bots,
   personas and seeds used for dev_private and holdout are different and live only in the private
   repo; VAL generates those transcripts itself with GEN's harness code.
4. gen-fix issues come from `dev_private` only. The holdout is evaluated ONCE (Sun 13:00 for the
   video; a fresh holdout for the paper). Never iterate on holdout results.
5. Every number in the UI, video or paper comes from a metrics/*.json written by VAL.
6. Forge's own tests are necessary, not sufficient: guardrails are scored on VAL's hidden tests,
   including false_intervention_rate (rewrites of benign replies count as harm).

## Engineering rules
- All model calls go through common/llm.py (cache per role in cache/llm/<role>; cache/demo is the
  curated read-only layer for the demo). CACHE_ONLY=1 forbids live calls. No live calls in CI.
- Set NUM_CTX explicitly; fixed seeds; every artifact records seed, models and git SHA.
- Guardrails: gen/forge/guardrails/<name>.py exporting RULE_SKELETON, VERSION and
  check(history, response, spec, rule) -> Verdict. Pure Python, allowlisted imports, no while
  loops, < 50 ms. Read time from Msg.at, never datetime.now().
- active.json sets each skeleton's file and mode (shadow|enforce); promotion and rollback are
  pointer edits. The runtime fails closed (crash, timeout, garbage, no-op rewrite, load failure).
  The source lint is not an OS sandbox: Forge runs each candidate in a fresh subprocess.
- Write the test first. `./ct ci` green before every push. Small PRs prefixed gen:/val:/contract:.
- Cross-review only on shared code (contracts, common, app, pricer); GEN's Claude never reviews
  anything that shows judge prompts or hidden tests.
- Use the cheaper model for routine edits, `/clear` between tasks, run long jobs as scripts.

## Verification loops
- Inner (each change): failing test, code, `./ct test`.
- Middle (each push): GitHub Actions `test` job (required); `gates` job reports metric gates.
- Outer (each sync): VAL evaluates on dev_private, commits metrics/run_<time>.json and
  metrics/latest.json, files one `gen-fix` issue per failure cluster. GEN fixes; VAL re-verifies
  and closes. Nothing is done until VAL closes it. At every sync both run the full suite on main.
