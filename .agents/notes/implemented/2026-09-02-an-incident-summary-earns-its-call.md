---
title: An incident summary is a paid call, so an incident has to earn it
status: implemented
date: 2026-09-02
scope: services
---

## Decision

`queue_summary_if_needed` gains an importance floor, `INCIDENT_SUMMARY_MIN_IMPORTANCE`
(runtime-policy, default `low`). A multi-alert incident whose `top_importance` is
below the floor is marked `skipped` with a named reason instead of `pending`. An
unknown or missing importance fails open and is still summarized.

## Why

Measured on production on 2026-09-02: `incident_summary` was 88 of roughly 398
paid model calls in the month, about 22% of spend. Twelve of the twenty newest
incidents were `low` — two payment-threshold rules that fire, escalate to
medium for five minutes and auto-resolve ten minutes later. Their summaries
conclude, in the model's own words, that the episode is a business fluctuation
and not an incident. Nobody acknowledges or reads them (acknowledgement rate
0% over 89 incidents in 30 days). The rule in `AGENTS.md` is that a mechanism
needs a consumer; a summary nobody reads is spend without one.

The floor is a runtime setting rather than a code change so the operator can
move it from the dashboard and the ledger can show the saving before the
default ever changes. The default stays `low` because an upgrade must not
silently stop summarizing anything.

## Consequences

- Setting the floor to `medium` on the production deployment removes roughly
  half of the summary calls; the `summary_status = skipped` rows carry the
  reason, so the decision stays auditable per incident.
- The incident detail page shows no summary for skipped incidents; the
  timeline, decision trace and alert cards are unaffected.
- Importance is decided before the incident goes quiet, so the floor never
  races the analysis. If an incident is later escalated by an operator, the
  eligibility is not re-evaluated — a deliberate simplification to revisit if
  operators start escalating low incidents.
