---
title: The monthly AI budget stops spending, not just sends a card
status: implemented
date: 2026-08-15
scope: services
---

## Decision

`AI_COST_BUDGET_ENFORCE` (default off) makes the analysis path check
month-to-date spend before paying for a model call. At 100% of
`AI_COST_MONTHLY_BUDGET_USD` it degrades to the rule route and says so in
`_degraded_reason`. The spend query is cached for 60 seconds, and a failure to
read it lets the call through.

## Why

`check_ai_cost_budget` posts a card at 80% and again at 100%. A card is a message
to somebody who may be asleep; it does not stop the spending it describes. The
failure-count circuit breaker does not help either — an overspend is not an
outage.

A budget refusal is marked **degraded**, unlike tiered routing, which is an
intentional route and deliberately is not. If the two looked alike, an operator
reading a rule verdict could not tell whether the alert was judged cheap or the
account was empty.

## Consequences

- It fails open. A broken meter lets the call through: losing analysis over
  accounting is worse than overspending by one interval.
- The 60-second cache bounds an overrun to one interval of calls rather than a
  month, and keeps a growing `SUM` out of the path of every alert.
- Off by default, because an unanalysed alert is a real cost too and which one
  matters is a deployment's decision. Turning it on without a budget set does
  nothing.
