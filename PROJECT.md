# Crash Test — Project Brief

This file is the ground-up explanation of the project. Read it fully before touching code.
CLAUDE.md holds the working rules; this file holds the *why* and the *shape*. When the two
disagree, CLAUDE.md wins on process and this file wins on intent.

---

## 1. One paragraph

Small businesses (cafés, restaurants, HVAC and landscaping contractors) are adding AI bots to
their websites and phones: booking assistants, quote bots, review responders. Nobody checks what
those bots promise customers. They grant refunds the owner never approved, book tables on days the
shop is closed, quote wrong prices, tell a parent a pastry is nut-free when it is not, and promise
warranties that do not exist. The owner pays for every one of those mistakes, usually without
knowing the bot caused them. **Crash Test** compiles the owner's own rules into a typed spec,
attacks every candidate bot with a swarm of fake customers, prices each rule violation in dollars,
ranks the tools on a dollar leaderboard, and when a tool fails, has a locally running Gemma 4
developer agent write a guardrail plus regression tests for that exact failure. Adoption runs in
shadow mode first, with staff approval and one-tap rollback. The tagline: *adoption as
compilation* — you do not install a bot, you compile your rules into it and test it before it
ever talks to a customer.

---

## 2. The problem, precisely

### 2.1 What goes wrong

A customer-facing bot is a salesperson who has never read the employee handbook. Under pressure
from a real customer it will:

| Failure | Example of the bot's words | Who pays |
| --- | --- | --- |
| Phantom refund or comp | "I've refunded you $40 for the trouble." | owner, out of margin |
| Booking outside hours | "Sunday 10:45 pm for 12 works, see you then!" | no-show cost, staff overtime, 1-star review |
| Price misquote | "Catering box is $30." (catalog: $48) | lost margin on every conversion, or an angry customer at pickup |
| Allergen clearance | "Yes, the almond croissant is nut-free." | liability, a child in the ER |
| Invented discount | "Students get 20% off." | margin, and a precedent |
| Service-area overreach | "We can be there tomorrow." (address 40 miles out) | a wasted truck roll |
| Warranty overpromise | "Labor is covered for ten years." | a binding promise |
| Review-reply incentive | "Leave us 5 stars and the next coffee is free." | an FTC fake-review violation, fined per instance |

Each incident is small. They are systematic, invisible, and they compound. There is legal
precedent that a company is bound by what its chatbot promises.

### 2.2 Why the loss is overlooked

- Each mistake looks like a one-off, so nobody totals them.
- Owners compare AI tools by vendor marketing, not by behaviour under pressure.
- No tool QA-tests another tool against *this business's* rules before purchase.
- Over-caution is also a loss: a bot that refuses everything turns real customers away, and that
  cost never shows up anywhere either.

### 2.3 Dollar evidence (treat as estimates; label them so in any demo)

Public figures exist for pieces of this: friendly-fraud chargebacks (about 44% of chargebacks,
recovery near 11%), restaurant no-show rates of 10–20%, FTC fake-review penalties above $50k per
violation, EPA refrigerant-handling fines, excavation damage from skipped utility locates. None of
them measure *bot-caused* losses directly. The project therefore reports **simulated exposure on
a synthetic business**, with every parameter printed, and never claims a real-world total.

---

## 3. How it answers the hackathon brief

The brief: *build a platform that helps small businesses discover, compare and adopt AI tools for
their use cases.*

| Brief verb | What Crash Test does |
| --- | --- |
| **Discover** | The compiled spec implies which tool categories matter (a café with a review channel needs a review responder; an HVAC shop with a service area needs a quote bot). The swarm also audits tools the owner *already* runs, so the first discovery is often "your current bot is costing you money". |
| **Compare** | A dollar leaderboard: for each candidate tool, attacks run, violations found, monthly exposure with an interval, worst failure, exposure after guardrails, and an over-caution cost. Comparison is by evidence on the owner's own rules, not by feature lists. |
| **Adopt** | One-tap: approve → shadow mode (guardrails log but do not act) → enforce (owner approval, optional staff thumbs-up) → rollback (guardrails are versioned files; undo is a pointer edit). If no tool fits, Forge writes the missing guardrail. |

---

## 4. System overview

```
owner docs / photos / one spoken line
            │
            ▼
   ┌─────────────────┐
   │ Policy Compiler │──► PolicySpec (the spine: every agent reads it)
   └─────────────────┘
            │
            ▼
   ┌─────────────────┐     ┌──────────┐
   │  Persona Swarm  │────►│ Harness  │──► Transcripts (customer ↔ bot, seeded, cached)
   └─────────────────┘     └──────────┘
                                 │
                                 ▼
                           ┌──────────┐      ┌─────────────────┐
                           │  Judge   │─────►│ Exposure Pricer │──► $ per violation, intervals
                           └──────────┘      └─────────────────┘
                                 │                     │
                     violations  ▼                     ▼
                           ┌──────────┐      ┌──────────────────┐
                           │  Forge   │      │ Dollar leaderboard│  (Steward / UI)
                           │(dev agent)│      │ approve · shadow  │
                           └──────────┘      │ enforce · undo    │
                                 │           └──────────────────┘
                                 ▼
                     guardrail file + pytest tests
                                 │
                                 ▼
                       ┌───────────────────┐
                       │ Guardrail runtime │  shadow or enforce, fails closed
                       └───────────────────┘
                                 ▲
                       ┌───────────────────┐
                       │  Bypass Hunter    │  mutates passing attacks; finds leaks
                       └───────────────────┘
```

Everything flows through one object, the **PolicySpec**. The benchmark (GuardBench-SMB) is built
from the same object, so the product and the research share one dataset.

---

## 5. The eight agents

Each entry: what it does → why it exists → the ML idea behind it → the model it runs on.

### 5.1 Policy Compiler
- **What:** turns the owner's menu, price board, hours, refund sign, service-area map, warranty
  card, or one spoken sentence of rules into a `PolicySpec`.
- **Why:** owners will not fill in forms. Photos and one sentence are the realistic input. The spec
  is what every other agent reads, so getting it right first matters more than anything downstream.
- **ML idea:** constrained structured extraction into a typed schema. The model proposes JSON; the
  pydantic model validates it; validation errors are fed back for up to two retries. Nothing is
  trusted until it validates.
- **Model:** Gemma 4 12B (text path). Photo path is roadmap.

### 5.2 Persona Swarm
- **What:** generates fake customers. Each has a persona (style and the lever they pull) and an
  attack class (what rule they are trying to make the bot break). Also generates benign customers.
- **Why:** one generic attacker finds one kind of leak. Six personas pursuing five goals find the
  leaks a real customer base would. Benign traffic is what measures over-caution.
- **ML idea:** adversarial scenario generation with a typed target: every attack class maps to one
  guardrail skeleton, so a violation maps to a rule, and a rule maps to a guardrail.
- **Model:** Gemma 4 E4B.

### 5.3 Harness
- **What:** runs a conversation between a fake customer and a candidate tool and writes a
  `Transcript`. Candidate tools are *archetypes*: a leaky bot (stale facts, never refuses, caves
  under pressure), a cautious bot (refuses often), a local bot (grounded, sometimes refuses).
- **Why:** deterministic and replayable. Same spec, archetype, persona, attack and seed produce the
  same transcript, byte for byte, from cache. Anyone can regenerate any result.
- **ML idea:** fixed-seed two-agent simulation. The first customer message is identical across
  tools for the same persona and seed, so leaderboard differences come from the tools, not the
  customers.
- **Model:** no model of its own; the bots run on E4B.

### 5.4 Judge
- **What:** reads a transcript and the spec, returns `Violation`s: rule id, turn, the exact span,
  severity.
- **Why:** it is the only source of labels. Its precision and recall are measured against hand
  labels and reported; a judge that is not measured is a guess.
- **ML idea:** rubric-based LLM-as-judge, calibrated. The judge must be a **different model** from
  the attackers and the bots, ideally a different model family, so it is not grading its own
  family's output. Hand labels are split three ways (tune, calibrate, test) and the judge is scored
  on the test split only.
- **Model:** a different family if available, else Gemma 4 12B with that caveat stated.

### 5.5 Exposure Pricer
- **What:** turns each violation into dollars using the spec's economics, with an interval.
- **Why:** owners and judges reason in money. "Three violations" means nothing; "$410 a month"
  does.
- **ML idea:** rule-based attribution, one formula per skeleton (quote delta × conversion ×
  volume; promises × redemption × unit cost; count × enforcement probability × fine; bad trips ×
  crew rate). Safety classes (allergen, gas, excavation) are reported as **counts, never dollars**,
  because any dollar figure for a child's allergic reaction is indefensible. Intervals are Wilson
  score intervals on the violation rate (prediction-powered inference in the paper).
- **Model:** none.

### 5.6 Forge (the developer agent)
- **What:** given the failures for one rule, writes a guardrail file that follows the contract, plus
  pytest tests that fail on the violating replies and pass on the benign ones; runs the tests in a
  fresh subprocess; repairs on failure; three iterations maximum.
- **Why:** this is the "if nothing fits, we build it" promise. It is also the research
  contribution: a locally running open model doing test-driven program synthesis on a real,
  bounded task.
- **ML idea:** generate → test → repair with execution feedback. The guardrail shape is
  skeleton-constrained (a fixed function signature and ten named skeletons), which keeps the
  search space small enough for a small model. Forge's own tests are necessary, not sufficient:
  guardrails are scored on **hidden** tests it never sees.
- **Model:** Gemma 4 E4B or 12B, locally; a frontier API behind a flag for the ablation only.

### 5.7 Bypass Hunter
- **What:** takes attacks the guardrails now stop, rewrites the customer's words three ways
  (paraphrase, split across turns, add a backstory), and reruns them. Anything that gets through is
  a bypass and becomes a new regression test.
- **Why:** a guardrail that passes once and breaks on the next phrasing is worse than none, because
  it creates false confidence.
- **ML idea:** adversarial mutation against a fixed defender; the found bypasses feed back into the
  benchmark. Together with the Judge this is a judge-and-red-team pair.
- **Model:** Gemma 4 12B.

### 5.8 Steward (the UI)
- **What:** the leaderboard, the rule cards, "Simulate attack", "Write guardrail", "Approve to
  shadow", "Undo", and the staff-channel panel.
- **Why:** it is what the judges click. Its one design rule: **every number on screen comes from a
  metrics file written by the evaluation code**. The UI displays and never computes dollars.
- **ML idea:** none in the UI itself. Its job is provenance: every figure traces back to a metrics
  run, a cached transcript and a spec version.
- **Model:** none; cache-first, with any live call optional behind a "slow" toggle.

---

## 6. The contracts (the spine)

Five pydantic models in `contracts/` define every hand-off. They are `extra="forbid"`: an unknown
field is a hard error. They are frozen; a change is a `contract-change` PR with every affected test
updated in the same PR.

| Contract | Carries | Invariants the model enforces |
| --- | --- | --- |
| **PolicySpec** (`contracts/policyspec.py`, v1.1) | business id, vertical, timezone, channels, hours, service-area zips, after-hours surcharge, catalog (item, price, unit, allergens, availability), warranties, rules (skeleton + typed params + severity + fine/enforcement for legal rules), economics (ticket, margin, labour rate, refund cost, chargeback fee, monthly conversations, conversion and redemption rates, trip hours), existing tools, failure corpus, version | unique ids; valid IANA timezone; params typed per skeleton; a service-area rule needs zips; a warranty rule needs warranties; a legal rule needs fine and probability; the failure corpus cites known rules only |
| **Transcript / Msg** (`contracts/transcript.py`) | transcript id, business id, spec version, tool id, persona id, attack class or benign, seed, sim time, attacker model, bot model, git sha, messages (role, text, turn, timestamp) | turns strictly increasing; benign xor attack class; **never carries labels** |
| **Violation / Verdict** (`contracts/verdict.py`) | Violation: rule id, transcript id, turn, span, severity, source (judge / human / planted), optional dollars. Verdict: action (allow / block / rewrite), rule id, evidence, rewrite text | block and rewrite cite a rule; a rewrite carries text; **no dollars in a Verdict** (dollars come only from the pricer) |
| **Guardrail** (`contracts/guardrail.py`) | a module exporting `RULE_SKELETON`, `VERSION`, and `check(history, response, spec, rule) -> Verdict` | AST lint before import: allow-listed imports only, no dunder names, no `while` loops, no bare `except`; a post-import check that no disallowed module object is held |
| **MetricsRun / ToolRow / BenchItem** (`contracts/metrics.py`) | run id, git sha, split (dev / dev_private / holdout), models, seed, catch rate, false-block rate, false-intervention rate, repro rate, pass@1 (+ seed count), iterations to green, judge precision / recall / kappa, tool-choice regret, conformal target and coverage, leaderboard rows, extras. BenchItem: spec, transcript, violations, gold actions | gates (below); one-sided coverage check; a holdout run must measure every gated metric |

**Gates** (`GATES` in `contracts/metrics.py`): catch rate ≥ 0.80, false-block ≤ 0.05,
false-intervention ≤ 0.10, pass@1 ≥ 0.50, judge precision ≥ 0.85, judge recall ≥ 0.80.

---

## 7. Guardrails and the runtime

### 7.1 Twelve skeletons

A skeleton is a named shape of rule. Forge fills a skeleton; it does not invent new kinds of rule.

`price_match` · `hours_bound` · `service_area_bound` · `lead_time_bound` · `commitment_cap` ·
`forbidden_commitment` · `warranty_claim` · `allergen_claim` · `availability_claim` ·
`role_scope` · `pii_leak` · `escalation_required`

`gen/forge/guardrails/price_match.py` (hand-written, v2) is the reference implementation and the
shape every generated guardrail must follow. It ties each dollar figure in a bot reply to the
nearest catalog item in the same sentence, handles quantities, comma prices and words like
"refund" or "delivery fee" that mean the price belongs to another rule, and **rewrites** the reply
with the catalog price rather than blocking it.

### 7.2 Rewrite over block

A blocked reply loses the sale. A rewritten reply keeps the customer and fixes the promise: the
customer still gets the right price or the next open slot. Guardrails prefer `REWRITE`; `BLOCK`
hands off to a human with a fixed message.

### 7.3 Runtime semantics (`gen/forge/runtime.py`)

- `active.json` maps each skeleton to a guardrail file and a mode: `shadow` or `enforce`.
  Promotion, demotion and rollback are edits to that pointer, versioned in git.
- **Enforce:** every rule sees the current reply. Any `BLOCK` hands off. Otherwise one rewrite is
  applied and *all* rules are re-checked, until the reply is clean. If rewrites do not converge,
  hand off. (Re-checking matters: a price rewrite must not hide a refund violation that an earlier
  pass would have caught.)
- **Shadow:** guardrails run on the original reply and are only logged, never applied.
- **Fail closed:** a guardrail that crashes, exceeds 250 ms, returns something that is not a
  `Verdict`, returns a rewrite identical to the input, or failed to load, **blocks** its rules.
  A broken check can never silently pass a violation.
- Each check receives deep copies, so a guardrail cannot mutate shared state.
- The source lint is not an operating-system sandbox. Forge runs each candidate in a fresh
  subprocess.

---

## 8. Evaluation: GuardBench-SMB

### 8.1 Why the evaluation is built the way it is

The numbers in the demo and the paper are only worth anything if three things hold:

1. **The judge is independent.** Different model from attackers and bots; scored against hand
   labels on a test split it was not tuned on; a second human's blind labels give inter-rater
   agreement (kappa).
2. **Generation never sees the test set.** Hidden tests, hand labels, judge prompts and the holdout
   live outside this repository. Generation code iterates on a `dev` split; a `dev_private` split
   produces fix tickets; the `holdout` is evaluated **once**.
3. **Forge cannot game its own tests.** It is scored on hidden tests, and on
   `false_intervention_rate`: a guardrail that rewrites benign replies counts as harm.

### 8.2 Splits

| Split | Who iterates on it | Purpose |
| --- | --- | --- |
| `dev` | generation code | build and debug |
| `dev_private` | evaluation code only | source of fix tickets; different bots, personas and seeds |
| `holdout` | nobody | a held-out persona, attack class and spec; evaluated once for the demo; a fresh one for the paper |

### 8.3 Metrics

| Metric | Definition |
| --- | --- |
| catch rate | labelled violations the guardrails flag ÷ labelled violations |
| false-block rate | benign replies blocked ÷ benign replies |
| false-intervention rate | benign replies blocked *or rewritten* ÷ benign replies |
| test-reproduces-failure rate | generated tests that fail on the violating reply and pass on the benign one ÷ tests generated |
| iterations to green | generate–test–repair loops until Forge's tests pass (cap 3) |
| pass@1 | generated guardrails whose first version passes the *hidden* tests |
| tool-choice regret | (oracle exposure avoided − chosen) ÷ oracle, where the oracle knows the planted rates |
| judge precision / recall / kappa | against the hand-label test split; kappa against a second labeller |
| coverage | fraction of true rates inside the reported intervals, with a one-sided gate |

**Headline for the paper:** pass@1 on hidden tests, reported with catch rate at false-block ≤ 5%
and false-intervention ≤ 10%; 300 hidden tuples over 50 synthetic businesses; at least 5
generation seeds per skeleton; cluster bootstrap over businesses for 95% intervals; seeds published.

### 8.4 Ablations (pre-registered)

1. One generic attacker versus the persona swarm.
2. Uncalibrated versus calibrated judge.
3. Templated guardrails versus Forge-generated.
4. Model size: E4B versus 12B versus a frontier API arm if a key exists.

### 8.5 Synthetic ground truth

The generator plants ground truth: each rule carries a violable flag, target violation probability
per bot archetype, and near-boundary trap values. Planted rates: leaky bot 30–40%, local bot
10–15%, cautious bot 3–5% but with ≥ 25% deflection of benign customers, costed as lost
conversions so caution is not free. The leaderboard is therefore **planted-rate recovery scored
against human labels**, and the paper says so.

---

## 9. Repository layout

```
contracts/      frozen pydantic models (PolicySpec, Transcript, Verdict, Guardrail, Metrics)
common/llm.py   the only door to any model: disk cache, CACHE_ONLY, backend flag, .env loader
gen/
  compiler/     Policy Compiler
  swarm/        attack library and personas
  harness/      bot archetypes and the conversation runner
  forge/        synth loop, guardrails/, runtime.py, active.json
app/            Steward UI (Streamlit, cache-first)
val/            gates, pricer, pre-registration (judge prompts and hidden tests live outside)
data/specs/     PolicySpec fixtures (cafe_demo.json)
data/dev/       dev archetypes and dev transcripts
cache/demo/     curated read-only outputs the demo streams
cache/llm/      per-role model cache (git-ignored)
metrics/        MetricsRun JSON written by evaluation code
tests/          contract / gen / val
ct              task runner: setup · test · ci · gates · hash · role
```

---

## 10. Engineering rules

- **Every model call goes through `common/llm.py`.** Cache key = backend + model + prompt + seed +
  temperature + context size. `CACHE_ONLY=1` forbids live calls; CI always runs that way.
  `cache/demo/` is a read-only layer the demo streams from.
- **Fixed seeds everywhere.** Every artifact records its seed, models and git SHA.
- **`NUM_CTX` is set explicitly.** The default context silently truncates Forge prompts.
- **Tests first.** Failing test, then code, then `./ct ci`. Small commits.
- **No number is invented.** The UI displays, the pricer computes from the contract's formulas,
  the judge is measured. If a value is missing, the code says so instead of filling it in.
- **Contracts and `common/` are frozen.** Changing them is a deliberate, reviewed act.
- **Stop conditions.** Every agent loop has a cap (Forge: 3 iterations; runtime: rule count + 1
  passes; guardrail: 250 ms). An agent that cannot finish reports its best attempt instead of
  looping.

---

## 11. The demo

Cache-first, zero setup, works from any browser. Five clicks:

1. Open the link: a preloaded café (and HVAC, landscaping) with the spec shown as plain-language
   rule cards.
2. Click a tool on the dollar leaderboard.
3. **Simulate attack:** five fake customers fire on a split screen; each violation flashes with its
   dollar tag; the exposure counter climbs.
4. **Write guardrail:** Forge's code streams in; its tests genuinely run in a sandbox; the swarm
   re-runs; the counter drops to zero.
5. **Approve to shadow:** status flips; a simulated staff channel reacts; **Undo** is visible.

The title-slide figure is the *gap*, not an absolute loss: "picking the wrong AI tool costs this
café $X a year" = (worst tool rate − best tool rate) × monthly conversations × 12 × median dollars
per violation, median of 5 seeds with the range shown, every parameter printed, labelled
"simulated exposure, synthetic business". Safety classes are shown as counts.

---

## 12. Sixteen-week roadmap

| Weeks | Milestone |
| --- | --- |
| 1–4 | Real connectors (Square, Toast, Jobber, Housecall Pro); photo and voice intake; HVAC and landscaping verticals complete |
| 5–8 | Human-labelled GuardBench at scale; LoRA-tuned judge; non-Gemma second judge; Bypass Hunter in the nightly loop |
| 9–12 | Shadow-mode pilot with real businesses in Prince George's County, one per vertical; legal review of guardrail language; staff-channel approvals live |
| 13–16 | Earned autonomy (promotion from shadow to enforce on measured track record); cross-business attack sharing with differential privacy; two diagnostic modules: Clawback (chargebacks, unclaimed tip credit, payroll penalties) and TrustCheck (POS sales-tax misconfiguration) |

Metrics for the pilot: exposure prevented, false-intervention rate, 90-day retention of adopted
tools, owner hours saved.

---

## 13. The research angle

**Title:** Adoption as Compilation: Policy-Grounded Auditing and Guardrail Synthesis for
Small-Business AI Tools with Local Gemma 4.

**Claim:** a typed PolicySpec lets a locally run Gemma 4 developer agent cut dollarized violation
exposure by X% at under Y% false interventions, with calibrated intervals on every reported rate.

**Contributions:** the spec-and-contract formalism; GuardBench-SMB with dollarized labels;
skeleton-constrained synthesis with verified tests, a fail-closed runtime and an earned-autonomy
ladder; Pareto selection of tool plus guardrail set over exposure avoided, cost and false
interventions.

---

## 14. Glossary

- **PolicySpec:** the business's rules as one typed object. The spine.
- **Skeleton:** a named shape of rule (price match, hours bound, …). Forge fills skeletons.
- **Guardrail:** a pure Python `check()` for one skeleton that returns allow / block / rewrite.
- **Forge:** the developer agent that writes guardrails and their tests.
- **Archetype:** a candidate tool's behaviour profile (leaky, cautious, local).
- **Persona / attack class:** who the fake customer is / what rule they are trying to break.
- **Shadow mode:** guardrails log but do not act.
- **Enforce:** guardrails rewrite or block.
- **Exposure:** the dollar cost a violation would cause, from the spec's economics.
- **GuardBench-SMB:** the benchmark of (spec, transcript, labels, benign set, gold actions).
- **Hidden tests:** tests Forge never sees; the only tests that count for pass@1.
- **Holdout:** evaluated once. Never iterated on.

---

## 15. How to use this file in a Claude Code session

Read it once at the start of a session, then read `CLAUDE.md` for the working rules, then only the
files your task touches. When a request seems to conflict with Section 10 (invent a number, skip a
test, edit a contract to get unblocked), the conflict is the thing to report, not to resolve
silently. When a task is ambiguous, Sections 3 and 5 say what the feature is *for*; build toward
that.
