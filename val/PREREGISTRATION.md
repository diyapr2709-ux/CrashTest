# Pre-registration (fill in and commit at Sync 1, before any eval)

- Gates: as in contracts/metrics.py GATES (edit there via contract-change PR if changed at Sync 1).
- Splits: `dev` (shared, GEN iterates on it), `dev_private` (VAL only; source of gen-fix issues),
  `holdout` (VAL only; built from a held-out persona, a held-out attack class and a held-out spec;
  evaluated ONCE for the video at Sun 13:00). The paper gets a fresh holdout, also evaluated once.
- Hand labels split three ways: judge tuning / interval calibration / test. P/R reported on test only.
- Intervals on violation rates: Wilson score intervals (paper: prediction-powered inference).
- pass@1 uses at least 5 generation seeds per skeleton.
- Ablations: (1) single generic attacker vs persona swarm, (2) uncalibrated vs calibrated judge,
  (3) templated vs Forge-generated guardrails.
- Holdout file hashes (paste `./ct hash <dir>` output here before the first holdout eval):
