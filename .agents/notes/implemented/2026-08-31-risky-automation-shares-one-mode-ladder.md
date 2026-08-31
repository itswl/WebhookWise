---
title: Every risky automation shares one off/shadow/enforce ladder
status: implemented
date: 2026-08-31
scope: services
---

## Decision

`services/operations/feature_modes.py` defines the three-position switch —
`off` computes nothing, `shadow` computes and records what WOULD have happened,
`enforce` does it — resolved through the runtime-settings plane, with an
unknown value degrading to `off` loudly. First consumers: the AI budget brake
(`AI_COST_BUDGET_MODE`, legacy boolean still authoritative when unset) and the
per-source dedup fingerprint. New automated decisions that change behaviour
adopt the ladder instead of inventing a boolean.

## Why

Absorbed from Versus Incident's training→shadow→detect rollout. WebhookWise had
already run the play by hand — the relay/judge shadow pair rode a forward rule —
and it worked; but as a wiring trick it had to be reinvented per feature, and
each feature grew its own boolean with its own failure mode. A named shadow
position means a feature earns `enforce` with a ledger (signals, decision
traces) instead of an argument, and a typo in a mode value fails safe instead of
enforcing something nobody asked for.

## Consequences

Two switches now express the same idea two ways (`AI_COST_BUDGET_ENFORCE` and
`AI_COST_BUDGET_MODE`); the legacy boolean maps onto the ladder and keeps
existing deployments byte-identical, at the cost of one more setting to explain.
Shadow positions spend the compute of the real decision (that is the point), so
`off` remains the default everywhere.
