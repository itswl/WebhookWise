---
title: An incident has to earn its name, so grouping gains an importance floor
status: implemented
date: 2026-09-03
scope: services
---

## Decision

`services/incidents/grouping.py` gains an importance floor,
`INCIDENT_MIN_IMPORTANCE` (runtime-policy, default `low` — today's behaviour).

A non-recovery alert below the floor is skipped by the grouping scan entirely:
it never becomes a candidate for a new incident and never joins an existing
one. It stays a plain alert with its dedup thread, its judgement, its trace and
every forward it matched — nothing about delivery changes.

Recoveries are exempt. A recovery resolves whatever incident it matches
regardless of its own importance, because the alternative is an incident that
can be opened but not closed. An unknown or missing importance fails open and
is treated as `high`, the same comparison direction `cap_importance` uses: a
severity this system does not recognise must never be quietly demoted.

## Why

Measured on the production deployment, 2026-09-02: **12 of the 20 newest
incidents were low-importance episodes that auto-resolved in about ten
minutes** — a business-threshold rule firing, escalating for a few minutes, and
recovering on its own. Their own AI summaries called them business fluctuation
rather than incidents. Nobody acknowledged them; acknowledgement rate over the
preceding 30 days was 0%.

An incident is not a free record. It opens a row in the work queue, arms an
auto-SLA timer, queues a chat card, and (above `INCIDENT_SUMMARY_MIN_IMPORTANCE`)
buys a paid summary. `INCIDENT_SUMMARY_MIN_IMPORTANCE` (2026-09-02) stopped the
spend, which was the loudest cost — but the queue row, the SLA timer and the
card all survived it. The floor addresses the layer underneath: whether the
episode was ever an incident.

The default stays `low` because an upgrade must not silently stop grouping
anything, and because the number that justifies moving it is per-estate. The
setting is runtime-policy for the same reason the summary floor is: an operator
can raise it from the dashboard, watch the incident rate, and lower it again
without a deploy.

### Rejected: filter at close time instead of at grouping time

Letting the incident form and then deleting or hiding the low ones reads
tempting — the correlation data is still collected. It was turned down because
every consequence of an incident happens at CREATION: the notification is
queued, the SLA timer is armed, the work-queue row exists. Suppressing the
record afterwards would leave the card that already went out and the escalation
already scheduled, which is exactly the noise the floor exists to remove.

### Rejected: an alert-rule allow-list instead of a severity floor

"These rules never make incidents" would be more precise, and the inbound-rules
table could carry it as a fifth verb. It was turned down because the policy
plane for "how severe is this" already exists and is already maintained:
`cap_importance` rules, the correction prior, and the severity-calibration
script all converge on one number per alert. A floor consumes that number; an
allow-list would be a second, hand-maintained list that drifts away from it.

## Consequences

- Raising the floor to `medium` on a deployment shaped like the measured one
  removes roughly 60% of new incidents, and with them their queue rows, SLA
  timers and notifications. The alerts themselves are untouched and still
  visible in the alert list, the timeline and the decision trace.
- A low-importance alert that WOULD have correlated into an incident now
  correlates into nothing. If an operator later wants that history, it has to
  be reconstructed from the alerts; there is no retroactive grouping pass.
- An alert judged `low` that is later corrected upwards by an operator is not
  re-considered for grouping — the scan has already passed over it. The same
  simplification the summary floor took, and the same thing to revisit if
  operators start correcting importance routinely.
- The floor is read once per grouping scan, not once per event, so a change
  mid-scan cannot split one batch across two rules.
