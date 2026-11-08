# Crash Test — Agents and Their Interactions

Companion to PROJECT.md. That file says what the project is; this one says how the eight agents
work and how they talk to each other. Read PROJECT.md first.

An "agent" here is a component with one job, a typed input, a typed output, and a stop condition.
Some run a language model; some are pure code. The word is used for both because the interaction
rules are the same: no agent trusts another's output until the contract validates it.

---

## 1. The cast in one table

| # | Agent | Job in one line | Runs a model? | Reads | Writes |
| --- | --- | --- | --- | --- | --- |
| 1 | Policy Compiler | owner's documents → PolicySpec | yes (12B) | raw owner text, photos | PolicySpec |
| 2 | Persona Swarm | PolicySpec → attack and benign scenarios | yes (E4B) as the customer | PolicySpec, attack library | scenarios (persona + goal) |
| 3 | Harness | scenario + tool archetype → Transcript | the bots run on E4B | PolicySpec, archetypes, scenarios | Transcripts |
| 4 | Judge | Transcript + PolicySpec → Violations | yes (different family, else 12B) | Transcripts, PolicySpec rules | Violations |
| 5 | Exposure Pricer | Violations + economics → dollars, intervals, leaderboard rows | no | Violations, PolicySpec.economics | MetricsRun.tools, exposure per violation |
| 6 | Forge | failures for one rule → guardrail file + tests | yes (E4B or 12B) | Violations, Transcripts, PolicySpec, the reference guardrail | guardrail module, pytest file, SynthResult |
| 7 | Bypass Hunter | blocked attacks → mutated attacks that still get through | yes (12B) | Transcripts, active guardrails | new attack scenarios, bypass list |
| 8 | Steward (UI) | metrics + caches → the screens the owner and judges use | no | MetricsRun, cache/demo, active.json | approval state, active.json mode changes |

Plus one piece of pure code that is not an agent but every agent depends on: the **guardrail
runtime** (`gen/forge/runtime.py`), which applies guardrails to a bot reply and fails closed.

---

## 2. The interaction map

```
  owner
    │ docs, photos, one sentence
    ▼
 [1] Policy Compiler ──────────────► PolicySpec ◄──────────────────────────────┐
                                        │                                       │
            ┌───────────────────────────┼─────────────────────┐                 │
            ▼                           ▼                     ▼                 │
 [2] Persona Swarm ──scenarios──► [3] Harness ◄── archetypes  │                 │
                                        │                     │                 │
                                   Transcripts                │                 │
                                        │                     │                 │
                                        ▼                     │                 │
                                   [4] Judge ──Violations──► [5] Pricer ── $ ──► MetricsRun
                                        │                                       │
                             violations for one rule                            │
                                        ▼                                       │
                                   [6] Forge ──guardrail + tests──► runtime ◄── active.json
                                        ▲                              │           ▲
                                        │                       rewritten reply    │
                          found bypasses as new tests                  │      mode edits
                                        │                              ▼           │
                                   [7] Bypass Hunter ◄─── blocked attacks    [8] Steward
                                                                                   ▲
                                                                        owner clicks, staff votes
```

Read it as three loops laid on top of each other:

- **The audit loop (1 → 2 → 3 → 4 → 5 → 8):** compile the rules, attack every tool, label, price,
  show. This is what "compare" means in the product.
- **The repair loop (4 → 6 → runtime → 3 → 4):** take a failure, write a guardrail, re-attack, re-judge.
  This is what "adopt" means when no tool fits as-is.
- **The hardening loop (runtime → 7 → 6 → runtime):** take what the guardrails stop, mutate it, find
  what still gets through, turn it into a test. This keeps a passed audit from going stale.

---

## 3. Who owns each shared object

An object has exactly one writer. Everyone else reads. This is what keeps the numbers honest.

| Object | Written by | Read by | Rule |
| --- | --- | --- | --- |
| PolicySpec | Compiler (and the owner, by correcting cards) | every agent | version bumps on every change; a new version triggers a full re-audit |
| Transcript | Harness | Judge, Forge, Bypass Hunter, Steward (from cache) | never carries labels; records seed, models, spec version, git SHA |
| Violation | Judge (source=judge), a human (source=human), or the generator (source=planted) | Pricer, Forge, Steward | the `source` field is never dropped; the pricer is the only thing that fills `exposure_usd` |
| Verdict | a guardrail, at runtime | runtime, Steward (shadow log) | never carries dollars |
| guardrail module + tests | Forge (into a staging directory), a human promotes | runtime | Forge never writes into the active directory |
| `active.json` | a human, or Steward on an approved click | runtime | the only place "shadow vs enforce" lives; versioned in git; rollback is a revert |
| MetricsRun | evaluation code only | Steward, the video, the paper | the UI never computes a dollar figure |
| attack library | hand-written, extended by Bypass Hunter's findings | Swarm | a found bypass becomes an attack class or a persona variant, with a test |

---

## 4. Each agent in depth

Each section follows the same shape: purpose → inputs → outputs → internal loop → prompt design
(where a model is involved) → determinism → failure modes and what happens → metrics it is
accountable for → who it talks to.

### 4.1 Policy Compiler

**Purpose.** Produce the one object everything else reads, from inputs an owner will actually
provide: a photo of the price board, the handwritten refund sign, a service-area map, a warranty
card, or one spoken sentence ("no same-day, comp a dessert if the wait is over 30 minutes, no
peanuts anywhere").

**Inputs.** Free text (menu lines, hours, policy sentences); images on the roadmap. Optionally a
previous PolicySpec to update rather than rebuild.

**Outputs.** A `PolicySpec` that validates, with `version` incremented if one existed.

**Internal loop.**
1. Build a prompt holding the schema (the pydantic source), one example spec as the shape, and
   the owner's text.
2. Ask the model for JSON.
3. `PolicySpec.model_validate`. On failure, append the pydantic error to the prompt and retry.
   Two retries maximum.
4. Render the result as plain-language rule cards for the owner to confirm or correct. A
   correction edits the spec directly and bumps `version`.

**Prompt design.** Schema-first, one example, explicit "return only JSON". The example is the
single hand-written spec, so the model copies its shape rather than inventing one. Rules must cite
a skeleton from the fixed list; the validator rejects anything else.

**Determinism.** Fixed seed, temperature 0, cached. The same owner text yields the same spec.

**Failure modes.** Invalid JSON after two retries → stop, show the best attempt, ask the owner for
the missing field. A hallucinated catalog item → caught only by the owner reviewing the cards,
which is why the cards exist. Missing economics → the pricer cannot run; the compiler fills
economics only from what the owner stated and leaves the rest for the owner to enter.

**Accountable for.** Spec validity rate on first attempt; fields corrected by owners (a proxy for
extraction accuracy).

**Talks to.** The owner (cards), every downstream agent (the spec). A new spec version is an event:
the Harness re-runs the audit, the Steward marks the leaderboard stale until it does.

### 4.2 Persona Swarm

**Purpose.** Generate the fake customers that attack a tool, and the benign ones that measure
over-caution.

**Inputs.** PolicySpec (to know which rules exist and which attack classes apply), the attack
library (`gen/swarm/library.py`), a seed.

**Outputs.** Scenarios: (persona, goal, opener hint) for attacks; (persona, goal) for benign.

**The library.** Six personas, each with a *style* (how they write) and a *lever* (the pressure
they apply): the Haggler, the Allergy Parent, the Midnight Booker, the Refund Scammer, the Event
Planner, the Regular. Per vertical, five attack classes, each targeting exactly one guardrail
skeleton: for a café, phantom refund → commitment cap; allergen clearance → allergen claim;
booking outside hours → hours bound; catering misquote → price match; invented discount →
forbidden commitment. Each class lists which personas pursue it. Five benign scenarios per
vertical.

**Why typed targets.** A violation found by an attack class maps to a skeleton, so Forge knows
which guardrail to write and the pricer knows which formula to apply. Without the typing, a
violation is just a bad transcript.

**Determinism.** The library is code; the customer's actual words come from the attacker model at
generation time (see Harness), seeded.

**Failure modes.** A persona that breaks character and reveals it is testing → the customer prompt
says never to; detected by reading transcripts. A goal the bot can never violate because the spec
has no such rule → the class is skipped for that business.

**Accountable for.** Coverage: every rule in the spec has at least one attack class aimed at it.

**Talks to.** Harness (scenarios in), Bypass Hunter (new scenarios back).

### 4.3 Harness

**Purpose.** Run one conversation between a fake customer and a candidate tool and produce a
Transcript that anyone can regenerate byte-for-byte.

**Inputs.** PolicySpec, a tool archetype, a scenario, a seed, a simulated wall-clock time.

**Outputs.** A `Transcript`: alternating customer and bot messages, each stamped with a turn index
and a timestamp; metadata recording the seed, both model names, the spec version and the git SHA.

**Archetypes.** A candidate tool is modelled by four knobs: *grounding* (reads the real spec, a
stale copy with perturbed prices and a missing closed day, or nothing), *refusal policy* (never,
sometimes, often), *pressure response* (caves or holds), and temperature. Three shipped
archetypes: a leaky bot (stale, never refuses, caves), a cautious bot (grounded, refuses often,
holds), a local bot (grounded, sometimes refuses, holds). The knobs are what make the leaderboard
have something to rank; the planted violation rates they produce are the ground truth the
evaluation recovers.

**Internal loop.** For each turn: the customer model sees its persona, goal and the conversation so
far, writes one message; the bot model sees its archetype's system prompt (with the facts its
grounding allows) and the conversation, writes one reply. Three turns by default. Every call goes
through the cached LLM client with `seed × 100 + turn`, so the first customer message is identical
across tools for the same persona and seed.

**Determinism.** Total. Replay with `CACHE_ONLY=1` reproduces every file. A transcript id is a hash
of (business, spec version, tool, persona, attack class, seed).

**Failure modes.** Model timeout → the call fails, nothing is written; raise the timeout, never
patch around it. Context overflow → set `NUM_CTX` explicitly. A bot that goes silent → the
transcript is still valid (an empty reply is a reply) and the judge sees it.

**Accountable for.** Replay fidelity (100% or the cache is broken); transcripts per hour.

**Talks to.** Swarm (scenarios), Judge and Forge and Bypass Hunter (transcripts out), Steward (via
`cache/demo`).

### 4.4 Judge

**Purpose.** Label. For every bot turn, decide which rules it broke, quote the exact span, and
assign severity. It is the only source of labels at scale, so its own accuracy is measured and
reported alongside everything it labels.

**Inputs.** A Transcript, the PolicySpec's rules (id, description, typed params), the catalog and
hours for grounding.

**Outputs.** A list of `Violation`s with `source="judge"`, plus a per-turn confidence score kept
internal to the evaluation code (used for calibration, never shown as a fact).

**Internal loop.** Per bot turn: present the rules, the conversation so far, and the reply; ask for
JSON naming any rule broken and the span that breaks it. Validate each against the spec (an
unknown rule id is dropped and counted as a judge error).

**Prompt design.** Rubric-based: the rules are the rubric. The judge is told to cite the rule id
and quote the span; a violation with no span is rejected. It is never shown the attack class or the
persona, so it cannot infer "this was supposed to be a refund attack" and label accordingly.

**Independence.** The judge model must differ from the attacker and bot models, and ideally come
from a different family; `assert_independent_judge()` enforces the name check, and the paper
reports the family. Hand labels are split into tune, calibrate and test; the judge is scored on
the test split only, and a second human's blind labels give inter-rater agreement.

**Determinism.** Temperature 0, seeded, cached.

**Failure modes.** Low precision → it invents violations and the leaderboard overstates exposure.
Low recall → leaks go unpriced. Either shows up in the reported precision/recall; neither is hidden.
Judging a rewritten reply (after a guardrail) uses the same judge, so false interventions are
labelled by the same standard as violations.

**Accountable for.** Precision, recall, kappa; the calibration of its confidence scores
(coverage of the intervals built on them).

**Talks to.** Pricer (violations out), Forge (the violating turns for one rule), Bypass Hunter
(re-judges mutated attacks).

### 4.5 Exposure Pricer

**Purpose.** Turn violations into dollars the owner can argue with, and into the rows of the
leaderboard.

**Inputs.** Violations, `PolicySpec.economics`, the rule's severity and (for legal rules) fine and
enforcement probability, the archetype's violation counts over the attack set.

**Outputs.** `exposure_usd` on each money or legal violation; `ToolRow`s: attacks run, violations,
monthly exposure with an interval, worst failure (rule, span, dollars), exposure after guardrails,
safety counts, status.

**Formulas, one per skeleton.**
- price match: quote delta × conversion rate × monthly conversations
- commitment cap, forbidden commitment: promises × redemption rate × unit cost (refund cost or the
  item's price)
- hours bound: count × (refund cost + one hour of labour)
- service area bound: count × trip hours × labour rate
- legal (review incentive, refrigerant advice, unlicensed pesticide): count × enforcement
  probability × fine
- safety (allergen, gas, excavation): **count only, no dollars**

**Intervals.** The underlying quantity is a violation *rate* over the attack set; the interval is a
Wilson score interval on that rate, scaled through the formula. The paper moves to
prediction-powered inference, which corrects the judge's labels using the hand-labelled subset.

**Over-caution.** A cautious bot's deflections of benign customers are priced as lost conversions
(deflections × conversion rate × average ticket × margin) and shown in the same row, so caution is
never free on the leaderboard.

**Determinism.** Pure code.

**Failure modes.** Missing economics field → the row shows "not priced" for that class rather than
zero. A safety violation reaching a dollar formula → rejected by type.

**Accountable for.** Every dollar on any screen traces to one violation, one formula and one
economics field.

**Talks to.** Judge (in), Steward (out via MetricsRun).

### 4.6 Forge

**Purpose.** When a tool breaks a rule, write the guardrail that stops it, prove the guardrail with
tests, and hand both to a human for promotion. This is the developer agent and the research core.

**Inputs.** One skeleton; the PolicySpec; the violating transcripts for that skeleton with the
violating turn marked; benign transcripts; the reference guardrail (`price_match.py`) as the shape;
the guardrail contract's docstring.

**Outputs.** A `SynthResult`: the guardrail source, the generated pytest file, the iteration count,
pass/fail on its own tests, the seed. Written to a staging directory, never into the active
guardrail directory.

**Internal loop (max 3 iterations).**
1. Prompt the model with the contract, the reference guardrail verbatim, the rule's typed params,
   three violating replies and two benign ones. Ask for one Python module.
2. Extract the code block. Run the static lint (`check_source`): allow-listed imports only, no
   dunder names, no `while`, no bare `except`. Problems go back into the prompt.
3. Write the module to a temp directory. Generate tests: for each violating reply, assert the
   verdict is not `allow`; for each benign reply, assert it is `allow`.
4. Run pytest in a **fresh subprocess** with a 60-second timeout. The subprocess is the real
   sandbox; the lint is not.
5. On failure, feed the pytest output back and repeat. On the third failure, stop and return the
   best attempt.

**Prompt design.** Shape-by-example: the model is shown exactly one working guardrail and told to
produce another of the same shape for a different skeleton. The signature is fixed
(`check(history, response, spec, rule) -> Verdict`), the skeleton list is fixed, so the model
fills a slot rather than designing a system. The prompt says to prefer `REWRITE` and shows how the
reference guardrail builds its rewrite.

**Why the tests it writes are not enough.** Forge is optimizing against its own tests, so passing
them proves little. Guardrails are scored on *hidden* tests Forge never sees, and on
`false_intervention_rate` over benign traffic the generation side never saw either. Those two
numbers are the ones that count.

**Determinism.** Seeded; every iteration's prompt and response is logged under
`cache/forge/<skeleton>/<seed>/`, so a result can be replayed and a paper figure regenerated.

**Failure modes.** Never reaches green → best attempt is shown, a human repairs it, and the result
is labelled "model draft, human-repaired" everywhere it appears. Reaches green but fails hidden
tests → that is the pass@1 metric doing its job. Generates code the lint rejects three times → the
lint diagnostics are the output; usually the model tried to import something.

**Accountable for.** pass@1 on hidden tests, test-reproduces-failure rate, iterations to green,
and the false-intervention rate of what it produced.

**Talks to.** Judge (violations in), runtime (guardrail out, after promotion), Bypass Hunter
(its bypasses come back as new failing cases for the next Forge run).

### 4.7 Bypass Hunter

**Purpose.** Keep a passed audit from going stale. A guardrail that stops one phrasing and misses
the next is worse than none, because it creates confidence.

**Inputs.** Attack transcripts the active guardrails now block or rewrite; the active guardrails.

**Outputs.** A list of bypasses (mutated customer message, bot reply, the guardrail that should have
fired, the verdict it gave); new attack scenarios derived from them.

**Internal loop.** For each stopped attack: ask the model to rewrite the customer's final message
three ways that pursue the same goal with different wording (a paraphrase, the request split across
two turns, a sympathetic backstory added). Re-run the bot's reply through the runtime. Anything the
guardrail now allows is a bypass. Cap at a fixed number of transcripts per run.

**What happens to a bypass.** It becomes a failing test case, which becomes the input to the next
Forge iteration for that skeleton, which produces a new guardrail version, which the human promotes
by editing `active.json`. The old version stays in git; rollback is a pointer edit.

**Determinism.** Seeded mutations, cached.

**Failure modes.** The mutator changes the goal, not just the wording → the "bypass" is not one; the
judge re-labels every mutated transcript, so a mutation that no longer violates the rule is
discarded.

**Accountable for.** Bypass rate per guardrail per run, and its trend across versions.

**Talks to.** Runtime (re-runs replies), Judge (re-labels), Swarm (new scenarios), Forge (new
failing cases).

### 4.8 Steward (the UI)

**Purpose.** Everything the owner and the judges see and click. Its one rule: it displays, it never
computes a dollar.

**Inputs.** `metrics/latest.json` (the leaderboard rows), `cache/demo/` (transcripts, violations,
Forge code and test logs to stream), `active.json` (current modes), a local approval-state file.

**Outputs.** Screens; edits to `active.json` on an approved click; a shadow log; a staff-channel
message per shadow intercept.

**Screens.**
- Rule cards: the spec in plain language, tap to correct (which calls the Compiler and bumps the
  version).
- Leaderboard: tool, attacks, violations, monthly exposure with low–high, worst failure, exposure
  after guardrails, status, staff approval.
- Simulate attack: a cached transcript streams in; turns listed in its violations file flash with
  the dollar tag; the exposure counter climbs.
- Write guardrail: the cached Forge code streams; its tests genuinely execute in a subprocess; the
  before/after exposure is shown.
- Approve to shadow / Promote to enforce / Veto / Undo: edits to `active.json`, each a git-visible
  change.
- Staff channel: each shadow intercept posted as "the bot wanted X, the guardrail changed it to Y,
  ok?"; thumbs up and down feed the acceptance rate that gates promotion.

**Determinism.** Cache-first. Any live call is behind a "slow" toggle and falls back to cache after
eight seconds with a visible "cached" badge.

**Failure modes.** A metric missing from the file → the cell shows "not measured", never a number
made up in the UI. Live backend unreachable → every action still completes from cache.

**Accountable for.** Provenance: every figure on screen can be traced to a MetricsRun id, a
transcript id and a spec version.

**Talks to.** The owner and staff (clicks and votes), the runtime (mode edits), the Compiler
(card corrections).

### 4.9 The guardrail runtime (not an agent, but every agent's floor)

`gen/forge/runtime.py` applies the active guardrails to a bot reply.

- `active.json` maps each skeleton to a file and a mode. **Shadow** guardrails run on the original
  reply and are only logged. **Enforce** guardrails act.
- Enforce semantics: every rule sees the current reply. Any `BLOCK` hands off to a human with a
  fixed message. Otherwise one rewrite is applied and *all* rules are re-checked, until the reply
  is clean; if the rewrites do not converge within rule-count + 1 passes, hand off. Re-checking is
  what stops a price rewrite from hiding a refund violation.
- **Fail closed.** A guardrail that crashes, exceeds 250 ms, returns something that is not a
  Verdict, returns a rewrite identical to the input, or failed to load at all, blocks its rules.
  A broken check can never silently pass a violation.
- Each check receives deep copies of history, reply, spec and rule; a guardrail cannot mutate
  shared state.
- The source lint is a guard against accidental I/O by generated code, not an operating-system
  sandbox; Forge's subprocess is the sandbox.

---

## 5. The protocols: how the agents interact, step by step

### 5.1 Audit a tool (the "compare" path)

1. Compiler produces PolicySpec v*n* (or the owner corrects a card and bumps to v*n*+1).
2. Swarm reads the spec, selects the attack classes whose target skeleton appears in the rules,
   and emits scenarios for each persona plus the benign set.
3. Harness runs every (archetype × scenario) with the configured seed(s) and writes Transcripts.
4. Judge labels every bot turn; Violations carry `source="judge"`.
5. Pricer prices each violation and builds one ToolRow per archetype, including the over-caution
   cost from benign deflections.
6. Evaluation code writes a MetricsRun; Steward renders it. The leaderboard shows spec v*n*; a
   newer spec version marks it stale until the audit reruns.

### 5.2 Repair a failure (the "adopt" path when nothing fits)

1. From the MetricsRun, the owner (or the Steward's "Write guardrail" button) picks a tool and a
   failing rule.
2. Forge receives that rule's skeleton, the violating transcripts with turns marked, and benign
   transcripts; runs its three-iteration loop; returns a SynthResult into staging.
3. A human reviews the code and tests, copies the module into the active guardrail directory with a
   `VERSION`, and adds it to `active.json` in **shadow** mode.
4. Harness re-runs the same attacks; the runtime applies the guardrail in shadow (logging only);
   the Judge labels both the original reply and what the rewrite *would* have been.
5. Pricer recomputes "exposure after guardrails"; the leaderboard shows before and after.
6. After the agreed shadow period and, if enabled, the staff approval threshold, the owner
   promotes the guardrail to **enforce** by editing `active.json` (the Steward does the edit on the
   click). Rollback is reverting that edit.

### 5.3 Harden (the loop that keeps the audit true)

1. Bypass Hunter takes every attack the active guardrails now stop, mutates the customer's words
   three ways, reruns the bot reply through the runtime.
2. Anything that now passes is re-labelled by the Judge; if it still violates the rule, it is a
   confirmed bypass.
3. Confirmed bypasses become failing test cases and new Swarm scenarios.
4. Forge runs again on the enlarged failure set and produces the next guardrail version.
5. The human promotes it (shadow first, then enforce) as in 5.2. The bypass rate per version is
   recorded.

### 5.4 Spec changes

A price change, a holiday closure or a new policy sentence → Compiler bumps the version → the
Steward marks every leaderboard row stale → the Harness reruns overnight → the Judge and Pricer
refresh the rows. Guardrails read the spec at check time, so a price change is enforced the moment
the spec changes, before any re-audit.

### 5.5 Vendor updates

If a candidate tool's behaviour changes (the vendor ships a new version), the next scheduled audit
produces different rows. The Steward shows the delta against the previous MetricsRun and flags any
tool whose exposure rose.

---

## 6. Memory: what persists, and where

| Kind | Lives in | Written by | Used for |
| --- | --- | --- | --- |
| Episodic (this business) | `PolicySpec.failure_corpus`, the shadow log, MetricsRun history | Judge, runtime, evaluation code | "what has gone wrong here before"; Forge's inputs; the weekly report |
| Semantic (across businesses) | the attack library, GuardBench items | hand-curated, extended by Bypass Hunter | better attacks for the next business; the paper's benchmark |
| Procedural | guardrail modules, their tests, `active.json` history in git | Forge, promoted by a human | the actual defences; rollback; reuse of a guardrail across businesses with the same skeleton |

Prompts are not memory. Tests and typed objects are. Anything an agent "learned" that is not in one
of those three places is lost on purpose.

---

## 7. Separation rules between agents

These are properties of the system, not conveniences. Each removes one way the numbers could lie.

1. **Judge ≠ attacker ≠ bot.** Different model, ideally different family. A model grading its own
   family's output has correlated blind spots.
2. **Judge never sees intent.** It is shown the rules and the conversation, never the attack class
   or persona, so it cannot label by expectation.
3. **Forge never sees the hidden tests or the benign set used for scoring.** It sees a handful of
   violating and benign examples to shape the guardrail; it is scored on others.
4. **Pricer is the only writer of dollars.** The UI displays; Verdicts carry none; the judge's
   confidence is never shown as a dollar.
5. **Generation never touches the evaluation split it is scored on.** Development iterates on one
   split; fix tickets come from a second; the holdout is evaluated once.
6. **Humans promote; agents propose.** Forge writes into staging; a human moves a guardrail into
   the active directory and `active.json`; the Steward's click is a human's click.

---

## 8. Stop conditions and failure handling, across all agents

| Agent | Cap | On hitting it |
| --- | --- | --- |
| Compiler | 2 validation retries | show best attempt, ask the owner for the missing field |
| Harness | one model timeout per call | fail the run, raise the timeout; never patch the harness |
| Judge | one call per turn | an unparseable answer counts as "no violation" and is logged as a judge error |
| Forge | 3 iterations; 60 s per pytest run | return the best attempt; a human repairs; label it so |
| Bypass Hunter | fixed transcript count per run | stop; the rest wait for the next run |
| runtime | 250 ms per check; rule-count + 1 rewrite passes | block the affected rules; hand off |
| Steward | 8 s per live call | fall back to cache with a visible badge |

The common principle: an agent that cannot finish reports what it has and stops. It does not loop,
guess, or widen its own permissions to get unblocked.

---

## 9. Autonomy ladder

Each guardrail, per business, sits on one rung. Promotion is a human decision informed by measured
history; demotion can be automatic.

| Rung | What the guardrail does | How it gets here |
| --- | --- | --- |
| L0 suggest | listed in the leaderboard as available | Forge produced it and it passed hidden tests |
| L1 shadow | runs, logs what it would have done, changes nothing | owner approves |
| L2 enforce with staff gate | rewrites or blocks; each intercept posted to the staff channel | owner promotes after N clean shadow days and ≥ 80% staff thumbs-up, if the gate is on |
| L3 enforce | rewrites or blocks silently; weekly report | owner promotes; any staff veto or a false-intervention spike demotes to L1 |

Autonomy is earned per guardrail from its own record, never granted to the system as a whole.

---

## 10. State of the build

As of scaffold v1.1 plus step 2: the contracts, the cached LLM client, the reference guardrail, the
fail-closed runtime with shadow mode, the attack library, the three stub archetypes and the Harness
exist and are tested. The Compiler, Judge, Pricer, Forge, Bypass Hunter and Steward are specified
here and in PROJECT.md and are built in that order of priority for the demo: Steward and
Judge/Pricer first (so the leaderboard exists), Forge second (the wow moment), Compiler's text path
third, Bypass Hunter last or on the roadmap slide.
