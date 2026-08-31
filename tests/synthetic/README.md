# Synthetic evaluation suites

Absorbed from OpenSRE's `tests/synthetic/` layout: deterministic scenarios whose
ground truth is known **by construction**, so nothing here waits on a human
labelling backlog. A scenario is a JSON fixture, its id is numeric and ordered
(`NNN-slug`), and the suite doubles as a regression floor in the gate: the rule
engine must score 100% on it, because every scenario was written from the rule
semantics on purpose — a failure means either the rules regressed or a scenario
lies, and both demand a human look.

- `severity/scenarios/` — one alert payload per file with the expected
  importance and triage verdict. Covers the level/name keyword rules (English
  and Chinese), the money/security content floor and its word-boundary
  contract, the recovery exemption, the threshold multiplier, and the red
  herrings that shipped as real regressions once.
- `scripts/eval/score_severity.py` runs the same scenarios outside pytest and
  can additionally score the live AI provider against them (`--ai`), which is
  how the model-vs-rules comparison stays a repeatable measurement instead of a
  one-off experiment.

Scenario payloads deliberately use generic service/rule names. Real estate
identifiers must never appear here — this directory ships in a public
repository.
