---
title: An engine swap gets measured, and the cutover timestamp is the part that cannot be recomputed
status: implemented
date: 2026-08-24
scope: services
---

## Decision

`scripts/ops/engine_quality_compare.py --cutover <iso>` recomputes deep-analysis
report quality either side of an engine change: how many analyses completed, how
often each field of the `deep_analysis_report.v1` contract was actually filled,
median duration and size, and the declared-failure rate.

The cutovers on this deployment, recorded here because they are the input the
script cannot derive:

| when | investigator engine |
| --- | --- |
| until 2026-08-17 | OpenClaw |
| 2026-08-17 | hookprobe, on Anthropic |
| ~2026-08-14 → 2026-08-24 | hookprobe, on DeepSeek (`api.deepseek.com`) |
| **2026-08-24 08:20 UTC** | hookprobe, on BigModel (`open.bigmodel.cn`, glm-5.3) |

Baseline measured the day of the BigModel switch, over the 110 completed
analyses in the 14 days before it: summary 100%, root_cause 99.1%, impact 97.3%,
recommendations 97.3%, evidence 94.5%, next_checks 94.5%, median 166.8s / 7770
bytes, 0% declared failed.

## Why

The investigator's reports are load-bearing twice over: they are the free labels
`severity_calibration` scores the cheap verdict against, and they are what every
runbook is distilled from. The model behind them changed three times in one month
and not one of those changes was measured on either side.

The failure mode is quiet, which is the whole problem. A weaker model still
returns a report, it still parses, the column still fills, the pipeline stays
green. What degrades is whether the report FOUND anything — and the first place
that shows up is a severity calibration that stops proposing anything, or a
runbook distilled from three investigations that each concluded nothing.

A script rather than a number in a note, because a number expires: the useful
window rolls forward and by the time a swap is worth judging its baseline has
fallen out of it. The timestamps are in a note rather than the script, because
those are the facts nothing can recompute.

## Consequences

Deliberately not measured: correctness. Nothing here knows whether a root cause
was the right one. That judgement is what the run rulings on the investigator's
own ledger are for, and conflating "the field was filled" with "the answer was
right" would make this number worse than useless — it would make it reassuring.

Two numbers are explicitly NOT signals, and the report says so where it is read:
median bytes and median seconds. A terser model that still answers every field
has not regressed, and neither has a slower one. Only a filled-field percentage
dropping is a regression.

## What would change the answer

- Field-fill rates staying at 100% while the reports get visibly worse. That
  would mean the contract is too easy to satisfy, and the next measurement is a
  rubric applied by a judge rather than a presence check applied by SQL.
- A swap being reverted before the window fills. Then this measures a mixture and
  says nothing; the cutover table above is what makes that visible rather than
  silently averaged.
