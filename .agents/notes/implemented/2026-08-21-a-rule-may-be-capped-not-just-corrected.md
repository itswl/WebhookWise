---
title: An alert rule gets a severity ceiling, calibrated against the investigator
status: implemented
date: 2026-08-21
scope: services
---

## Decision

`inbound_rules` gains a third verb, `cap_importance`, plus `action_value` to say
"at what" (migration 0034). It lowers an alert's importance to that ceiling —
never raises it — and marks the result `_importance_cap` with the judgement it
replaced and the rule that did it.

It is applied at ONE place: a wrapper around `analyze_webhook_with_ai`, not
inside its routes.

`scripts/ops/severity_calibration.py` (read-only) scores WebhookWise's severity
against the deep-analysis reports and proposes the ceilings. It proposes; a
person applies.

## Why

Measured on production, 2026-08-21: **330 of 367 alerts in a week were filed
`high` (90%)**, and across **80 completed investigations the model that actually
read them called 21 of them high (26%)**. `high` had stopped carrying
information.

The reports are not noise — the investigator discriminates correctly. It calls
the SES bounce rules critical (AWS really does pause sending) and `[MQ] Ready
backlog growing` medium. The inflation is concentrated in two business-signal
money rules, promoted by `content_high_keywords` in `analyze_with_rules`.

Cost: **36 of those 80 investigations (45%) were the two money rules**, at
~$0.46 each, to learn "medium".

### Rejected: edit `RULE_CONTENT_HIGH_KEYWORDS`

Too blunt, and provably wrong here. Over 25 reports the deposit alert is medium
(high 5, medium 12, low 8) while its sibling withdrawal alert is genuinely high
**4 times in 11** — a third of the time. A keyword list cannot tell them apart,
and the calibration script's own guard refuses to propose a downgrade for the
withdrawal rule for exactly that reason. One knob, two answers needed.

### Rejected: `importance_overrides`

The mechanism already exists and cannot reach this. It keys on `alert_hash`, and
these alerts carry the user id in their identity: **163 distinct hashes across
170 alerts**. An override keyed there generalises to nothing. Infrastructure
alerts do collapse (DatasourceNoData: 44 alerts, 3 hashes) so the table stays
useful — it is the wrong grain for this class, not a bad table. The calibration
report prints that check per rule, because it decides which fix is possible.

### Why the wrapper, not per-route

`analyze_webhook_with_ai` answers through eight routes — cache, rule_excluded,
rule_routed, ai, budget-exhausted, three degradations. "This rule is never high"
means it regardless of which one answered. Verified in production: the deposit
alert takes `rule_excluded`, so a cap inserted only after the AI path would have
missed the very rule it was built for.

### Why a ceiling and never a floor

A cap is set once and forgotten. If it could raise severity, an alert the
judgement called `low` would start paging at `medium` forever. An unrecognised
severity is treated as `high` when comparing, so a value this system does not
know is never left above the ceiling. Both are negative-verified tests.

## Consequences

Easy: retune a noisy rule without touching keyword policy or waiting for a model
change; the reasoning travels in the rule's `comment` field, which is where the
three applied caps cite their report counts.

Hard / watch for:

- A cap hides a genuine escalation of that condition. The guard is the
  `high_share > 1/3` refusal in the calibration script, which is a heuristic on
  a small sample — re-run the report as reports accumulate and revisit.
- The caps applied today (deposit → medium, DatasourceNoData → low, MQ ready
  backlog → medium) change what rule 22 forwards to deep analysis, since it
  matches `high,critical`. That is the intended cost saving, and it means the
  investigation stream for those rules stops — including the 20% that were
  genuinely high. Sampling them instead is unbuilt.
- `_importance_cap` is a fourth thing that can set importance, after the
  judgement, the resource-risk override and the correction prior. If a fifth
  appears, the precedence needs writing down.
