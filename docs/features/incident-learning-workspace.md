# Incident learning workspace

WebhookWise keeps the response loop focused on six operator-facing
capabilities: a prioritized work queue, structured resolution evidence,
reviewable recurrence detection, knowledge-gap discovery, conservative
recommendation calibration, and guided inbound-source onboarding.

None of these features automatically executes remediation, blocks incident
closure, or reopens a historical incident.

## Response work queue

The dashboard work queue turns active incidents into an ordered response list:

```text
GET /v1/response-center/work-queue
```

Query parameters:

- `bucket`: `active`, `my`, `unassigned`, `sla_risk`, or `needs_recovery`;
- `actor`: required for the `my` bucket;
- `offset`: zero-based result offset;
- `limit`: 1–100;
- `sla_risk_minutes`: 5–1440.

Every item contains one deterministic next action, a bounded 0–100 priority
score, and the individual reasons that contributed to that score. The response
also reports exact bucket counts, the number of matching incidents, and
`next_offset`. The queue is ordered in the database before pagination, so a
high-severity incident without an SLA cannot be displaced by a large number of
lower-severity incidents with deadlines. It is a read model over existing
incident state, not another task database.

## Structured incident resolution

Operators can save a partial resolution draft before closing an incident:

```text
GET /v1/incidents/{incident_id}/resolution
PUT /v1/incidents/{incident_id}/resolution
```

The record supports:

- root-cause category and human-confirmed root cause;
- resolution action;
- impact;
- confirmed, suspected, ruled-out, or unknown change association;
- related change ID when the association is confirmed or suspected;
- recovery evidence;
- owner;
- follow-up actions.

`PUT` is a partial update. Send `null` to clear a field. The response includes
`completeness.percent` and `completeness.missing_fields`, but completeness is
advisory and never prevents closure.

`POST /v1/incidents/{incident_id}/close` remains compatible with an empty body
and also accepts the same fields when an operator wants to save the resolution
and close in one transaction. Human-confirmed fields take precedence over
generated analysis in postmortem exports and knowledge-base sedimentation.

## Recurrence review

When a new multi-alert incident is formed, WebhookWise searches a bounded
30-day history for the most recently resolved incident with the same service,
environment, and stable alert identity. A match creates a pending review only.

```text
GET  /v1/incidents/{incident_id}/recurrence
POST /v1/incidents/{incident_id}/recurrence/confirm
POST /v1/incidents/{incident_id}/recurrence/dismiss
```

Confirm and dismiss are idempotent. A conflicting second decision returns a
conflict instead of overwriting the first decision. Neither action changes the
status of the current or previous incident. Incident list rows expose only a
compact recurrence badge; the detail endpoint returns the evidence and review
metadata.

## Knowledge-gap center

The knowledge-gap view highlights recurring, severe, or slow-to-resolve
patterns that do not have an effective published runbook:

```text
GET /v1/response-center/knowledge-gaps
```

Query parameters:

- `window_days`: 7–365, default 90;
- `limit`: 1–100.

Incidents are grouped by service, environment, source, and normalized alert
pattern. The result explains frequency, severity, MTTR, runbook coverage,
execution outcomes, and the reason for each bounded priority score. A published
runbook with an effective **terminal** execution removes the pattern from the
gap list; an in-progress, unproven, or ineffective runbook remains visible.
The response reports separate incident, document, and execution scan bounds so
operators can distinguish a complete result from a provisional one.

## Feedback-driven calibration

Incident intelligence still computes deterministic similarity, change, and
runbook scores. It now applies a conservative service/environment-scoped
calibration derived from:

- explicit relevant/irrelevant and used/not-used feedback;
- effective/ineffective terminal runbook executions.

Calibration is neutral below five samples. Above that threshold it uses a
shrunken posterior and caps the score adjustment at ±0.10. Every candidate
retains `raw_score`, the final `score`, sample counts, adjustment, and an
explanation. A terminal runbook result supersedes the synthetic “used” feedback
from starting that same execution so one operator action is not counted twice.

Resolution updates refresh unpublished incident-derived KB drafts. A published
document remains operator-owned and is never silently downgraded or overwritten
by a later sedimentation sweep.

This is ranking calibration, not an autonomous learning or remediation system.
Operators can always inspect and correct the evidence.

## Inbound setup

See [Inbound source onboarding](../integrations/inbound-source-onboarding.md)
for source-scoped credentials, the setup wizard, first-event evidence, rotation,
revocation, and payload-shape tracking.
