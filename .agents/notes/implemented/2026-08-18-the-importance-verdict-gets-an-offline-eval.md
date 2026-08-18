---
title: The importance verdict gets an offline eval, and the gate holds it
status: implemented
date: 2026-08-18
scope: services
---

## Decision

`evals/analysis_cases.jsonl` freezes alerts with the importance an operator says
they deserve. `scripts/eval_analysis.py run` replays them through
`analyze_with_rules` and checks the score against `evals/baseline.json`. It runs
in `scripts/gate.sh` and in ci.yml's test job.

Only one combination gates: the rule engine, the whole committed corpus, and a
keyword policy built from the **committed field defaults** rather than
`RuleAnalysisPolicy.from_config()`. `--policy env` and `--engine ai` score and
report without gating, and print why they did not.

The score keeps the direction of each disagreement. `high_recall` is the
headline; `max_miss_rate` is pinned at zero. `min_labeled` fails a corpus that
shrank.

## Why

Importance selects forward rules, decides what a silence swallows, and a `high`
verdict is what triggers deep analysis. Every knob that moves it — the keyword
sets, the content floor, the threshold multiplier, tiered routing, a prompt
edit, a cheaper model — is currently shipped on judgement. `analysis_feedback`
and `/v1/decision-quality` do measure the verdict, but after the fact, on alerts
already delivered or already suppressed. The failure they cannot prevent is the
one that matters: a change lowers a verdict, the alert stops being forwarded,
and nothing complains because nothing is missing from any dashboard.

The gate is offline and env-independent because a gate whose number moves with
whatever a developer exported is not a gate. `analyze_with_rules` already accepts
an injected policy, so the scored behaviour is a committed constant; the run
needs no database, Redis, API key or network.

The model leg deliberately does not gate. It costs money per run and does not
repeat exactly, and a flaky gate gets disabled within a month.

Errors are counted by direction because they are not symmetric. An under-call
reaches nobody; an over-call costs attention. Averaged into one accuracy number
the expensive mistake disappears.

## Consequences

- The seed corpus is a behaviour lock, not a quality measurement. It scores
  100%, which only says today's behaviour is intact. A real quality number needs
  labelled production traffic, which is what `export` is for.
- With 17 labelled cases, `min_exact_rate: 0.95` means any single disagreement
  fails. That is intended for a seed corpus and will be wrong once the corpus is
  large: growing it is a baseline event, re-recorded and reviewed as a diff.
- `export` copies payloads verbatim into a committed file. Alert payloads carry
  hostnames and identifiers, so an export has to be read before it is committed.
  Left as a review step rather than a redaction pass — a redactor that silently
  rewrote payloads would make the corpus stop representing production.
- Labels come from operator corrections (`importance_overrides`,
  `analysis_feedback`), so the eval inherits their bias: conditions nobody
  bothered to correct are absent, and the corpus over-represents the alerts
  somebody was angry about. Recorded here rather than solved.
- Two places now assert the same contract: the gate step (readable failure, names
  the disagreeing cases) and a pytest test (a contributor who only runs pytest
  still sees it). Accepted duplication; the pytest one costs milliseconds.
