---
title: Severity evals get ground truth by construction, not by labelling
status: implemented
date: 2026-08-31
scope: whole
---

## Decision

`tests/synthetic/severity/` holds deterministic alert scenarios whose expected
verdicts are true by construction — each was written from the documented rule
semantics. The pytest suite runs in the gate and must stay at 100% (the rules
are the severity floor); `scripts/eval/score_severity.py` runs the same
fixtures offline and can score the live AI provider against them (`--ai`).

## Why

Absorbed from OpenSRE's `tests/synthetic` layout. The eval spine was starved by
labelling: real verdicts wait on a human with 795 alerts to label, so every
unlabelled verdict priced at zero and the "does the model beat the rules"
question stayed a one-off anecdote (glm missed 2 of 8 highs; the rules missed
none). Synthetic scenarios invert the economics — writing one from the rule
semantics takes minutes, its truth never decays, and regressions in the floor
(the payment-as-info incident, the reorder/payments-web word-boundary bug)
become permanent, named test cases.

## Consequences

The suite pins today's rule semantics, including their quirks — changing a
keyword list deliberately now means updating scenarios, which is the intended
friction. Synthetic truth cannot measure what only production can (real
payload diversity, drift), so the labelled eval set remains the second half;
this unblocks the loop, it does not replace it.
