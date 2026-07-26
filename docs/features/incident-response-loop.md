# Incident Response Loop

WebhookWise keeps the product focused on one operational loop:

```text
alert ingest -> noise reduction -> incident grouping -> investigation
             -> operator response -> reusable knowledge
```

The incident detail page turns that loop into a compact command surface. The
first screen shows a deterministic four-part command summary:

- what happened;
- the most likely current cause;
- the strongest nearby change;
- the recommended next operator action.

The full evidence remains available below the summary, but it is collapsed by
default so the primary actions are not buried by diagnostics.

## Change impact assessment

`GET /v1/changes/{change_id}/impact` compares matching service alerts in a
bounded window before and after a normalized change. The response includes:

- alert-volume and high-severity deltas;
- new alert identities;
- incidents that started near the change;
- a detected rollback and recovery signal;
- a transparent score, confidence, and evidence list.

This is an association signal, not a causal claim. A completed observation with
too few matching samples returns `status=insufficient_data` and `level=unknown`
instead of presenting missing evidence as low risk.

The same assessment is attached to related changes in incident intelligence.

## Service profiles

WebhookWise derives a lightweight service profile from existing incidents,
changes, and published runbooks. It does not introduce a second CMDB.

- `GET /v1/services` lists recently discovered services.
- `GET /v1/service-profile?service=<name>&environment=<env>` returns health,
  active and historical incidents, MTTA/MTTR, historical handling ownership,
  common root causes, recent changes, and matching runbooks.

The owner field is explicitly historical handling data. It is not presented as
an authoritative service catalog owner.

## Manual runbook execution

Published knowledge-base documents can be started from an incident. WebhookWise
extracts Markdown checkboxes and list items into at most 30 operator-owned
steps. It never executes commands from the document.

- `GET /v1/incidents/{incident_id}/runbook-executions`
- `POST /v1/incidents/{incident_id}/runbook-executions`
- `PUT /v1/incidents/{incident_id}/runbook-executions/{execution_id}`

Starting the same candidate twice is idempotent. Step progress, completion,
abandonment, outcome, notes, and actor are durable and audited. Starting a
runbook records it as used; its final effectiveness is tracked independently so
"used but ineffective" is not mislabeled as "not used".

## Product-value reporting

Daily, weekly, and monthly reports include operational outcome measures in
addition to volume and cost:

- MTTA and MTTR average, P50, and sample size;
- deep-analysis success over terminal analyses;
- human-confirmed change association rate;
- runbook and similar-incident reuse rate;
- runbook executions started, completed, and rated effective.

An empty denominator is shown as unavailable, not `0%`.

## Safety boundaries

- Change impact is deterministic and read-time; it does not trigger rollback.
- Runbooks are checklists only; no shell command or remote action is executed.
- Feishu writes require a separately configured custom app and signed card
  actions. Incoming-webhook bots remain a view-only fallback.
- Every operator mutation still requires a dedicated write credential or a
  verified integration callback and is written to the audit log.
