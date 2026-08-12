# System Architecture

WebhookWise is a modular monolith deployed as separate API, worker, and
scheduler processes. PostgreSQL is the durable system of record. Redis provides
the TaskIQ queue, dynamic schedules, caches, distributed locks, and short-lived
coordination state.

The architecture is centered on one product loop:

```text
alert ingest -> noise reduction -> incident response -> verified resolution
             -> reusable knowledge -> better future recommendations
```

## Runtime Topology

```mermaid
flowchart TB
    subgraph producers["Event and change producers"]
        monitors["Monitoring systems<br/>Grafana / Alertmanager / Prometheus / vendors"]
        cicd["CI/CD and change systems"]
        feishu["Feishu interactive actions"]
        operators["Operators and dashboard users"]
    end

    subgraph business["WebhookWise business stack"]
        api["FastAPI API and dashboard<br/>webhook-service"]
        redis["Redis<br/>TaskIQ Stream / schedules / cache / locks"]
        worker["TaskIQ worker<br/>horizontally scalable"]
        scheduler["TaskIQ scheduler<br/>singleton dispatcher"]
        postgres["PostgreSQL<br/>durable system of record"]
        migrate["Alembic migrate<br/>one-shot startup gate"]
    end

    subgraph external["External decision and delivery systems"]
        ai["LLM provider"]
        gateway["Deep-analysis gateway"]
        targets["Webhook / Feishu / DingTalk / WeCom targets"]
    end

    monitors -->|"global or source-scoped webhook"| api
    cicd -->|"normalized change event"| api
    feishu -->|"signed, idempotent callback"| api
    operators -->|"dashboard and management API"| api

    migrate -->|"schema ready"| api
    migrate -->|"schema ready"| worker
    migrate -->|"schema ready"| scheduler

    api -->|"enqueue accepted envelope"| redis
    api <-->|"management reads and writes"| postgres
    scheduler -->|"dispatch periodic and delayed tasks"| redis
    worker -->|"consume and acknowledge tasks"| redis
    worker <-->|"events, incidents, outbox, knowledge"| postgres
    worker -->|"analysis request"| ai
    worker -->|"submit and poll"| gateway
    worker -->|"deliver persisted outbox items"| targets
```

The API returns `200 OK` after Redis accepts the webhook envelope. That response
means accepted and queued, not persisted in PostgreSQL. Event persistence,
analysis, incident grouping, and forwarding happen asynchronously.

## Webhook Processing Sequence

```mermaid
sequenceDiagram
    autonumber
    actor Source as Webhook source
    participant API as FastAPI ingress
    participant Queue as Redis Stream / TaskIQ
    participant Worker as TaskIQ worker
    participant DB as PostgreSQL
    participant Target as Delivery target

    Source->>API: POST /v1/webhook/{source}<br/>or /v1/source-webhooks/{public_id}
    API->>API: Rate limit, authenticate, bound body size
    API->>Queue: Enqueue raw envelope
    Queue-->>API: XADD accepted
    API-->>Source: 200 accepted + request_id

    Queue->>Worker: Deliver webhook_process_task
    Worker->>Worker: Parse and normalize adapter payload
    Worker->>Worker: Build stable identity and deduplicate
    Worker->>Worker: AI, rule fallback, silence, and noise decisions
    Worker->>DB: Commit event state and forwarding intents
    DB-->>Worker: Transaction committed
    Worker->>Target: Deliver pending outbox item with idempotency key
    Target-->>Worker: Delivery result
    Worker->>DB: Persist sent, retrying, or expired state
    Worker-->>Queue: Acknowledge completed task

    alt Retryable processing or delivery failure
        Worker->>Queue: Schedule bounded backoff retry
    else Retry budget exhausted
        Worker->>DB: Persist dead-letter or expired state
    end
```

The event update and `ForwardOutbox` intent are committed in one database
transaction. The remote HTTP side effect is deliberately outside that
transaction and is retried with a stable idempotency key.

## Product and Learning Loop

```mermaid
flowchart LR
    onboard["Guided source onboarding<br/>scoped credentials"]
    ingest["Alert ingest and normalization"]
    quality["Read-only alert quality center"]
    reduce["Identity, deduplication,<br/>silence, and noise reduction"]
    group["Incident grouping"]
    queue["Prioritized response queue"]
    investigate["Similar incidents, changes,<br/>runbooks, and deep analysis"]
    respond["Acknowledge, assign, SLA,<br/>and manual runbook progress"]
    resolve["Human-confirmed resolution"]
    learn["Recurrence review,<br/>postmortem, and KB draft"]
    calibrate["Bounded recommendation calibration"]

    onboard --> ingest
    ingest --> reduce
    ingest -.->|"source and event diagnostics"| quality
    reduce --> group
    group --> queue
    queue --> investigate
    investigate --> respond
    respond --> resolve
    resolve --> learn
    learn --> calibrate
    calibrate -->|"ranking evidence only"| investigate
    quality -.->|"recommendations only; no source mutation"| onboard
```

The learning path is intentionally conservative:

- recurrence detection creates a review candidate and never reopens an old
  incident;
- resolution evidence is operator-owned and takes precedence over generated
  summaries;
- unpublished incident-derived knowledge remains excluded from retrieval;
- feedback calibrates ranking within a bounded range and never executes
  remediation;
- the alert quality center is read-only.

## Durable Data Map

```mermaid
flowchart LR
    source_connections["source_connections<br/>credential digest and source health"]
    webhook_events["webhook_events<br/>normalized event and workflow state"]
    deep_analyses["deep_analyses<br/>local or gateway result"]
    forward_rules["forward_rules"]
    outboxes["forward_outboxes<br/>delivery intent and outcome"]
    incident_members["incident_members"]
    incidents["incidents<br/>group, summary, and resolution record"]
    recurrences["incident_recurrences"]
    feedback["incident_intelligence_feedback"]
    executions["runbook_executions"]
    changes["change_events"]
    kb["kb_documents<br/>draft or published"]
    audit["audit_logs and action receipts"]

    source_connections --> webhook_events
    source_connections --> incidents
    webhook_events --> deep_analyses
    webhook_events --> outboxes
    forward_rules --> outboxes
    webhook_events --> incident_members
    incident_members --> incidents
    incidents --> recurrences
    incidents --> feedback
    incidents --> executions
    changes -.->|"bounded read-time correlation"| incidents
    incidents -.->|"sediment unpublished draft"| kb
    audit -.->|"records operator mutations"| incidents
    audit -.->|"records management actions"| source_connections
```

Solid arrows represent direct persisted references. Dashed arrows represent a
workflow or query-time relationship rather than a required foreign key.

PostgreSQL owns durable business truth. Redis may accelerate or coordinate a
decision, but clearing Redis must not redefine incident resolution, forwarding
outcomes, source credentials, or published knowledge.

## Scheduler Responsibilities

The scheduler is a singleton dispatcher. Workers execute the scheduled tasks:

```mermaid
flowchart LR
    scheduler["Scheduler"]
    redis["Redis dynamic schedules"]
    workers["Workers"]

    scheduler --> redis --> workers
    workers --> outbox["Outbox retry and stale scan"]
    workers --> grouping["Incident grouping and summaries"]
    workers --> deep["Deep-analysis polling"]
    workers --> knowledge["KB sedimentation"]
    workers --> windows["Maintenance-window materialization"]
    workers --> reports["Daily / weekly / monthly reports"]
    workers --> maintenance["Retention and data maintenance"]
    workers --> metrics["Derived metric refresh"]
```

Scaling the worker is supported. Scaling the scheduler is not recommended;
distributed task locks are a safety net, not a replacement for singleton
deployment.

## Observability Topology

```mermaid
flowchart LR
    subgraph emitters["Telemetry emitters"]
        app["API / Worker / Scheduler<br/>OpenTelemetry SDK"]
        browser["Dashboard browser<br/>Faro Web SDK"]
        beyla["Beyla eBPF sidecar<br/>API process auto-instrumentation"]
        pyrosdk["API / Worker / Scheduler<br/>Pyroscope SDK"]
        k6["k6 load profile"]
    end

    alloy["Grafana Alloy<br/>OTLP and Faro receivers"]
    prometheus["Prometheus<br/>metrics and alert rules"]
    loki["Loki<br/>logs"]
    tempo["Tempo<br/>traces"]
    pyroscope["Pyroscope<br/>continuous profiles"]
    alertmanager["Alertmanager"]
    grafana["Grafana"]
    ingress["WebhookWise Alertmanager ingress"]

    app -->|"OTLP metrics, logs, traces"| alloy
    browser -->|"Faro events and traces"| alloy
    beyla -->|"OTLP metrics and traces"| alloy
    alloy --> prometheus
    alloy --> loki
    alloy --> tempo
    pyrosdk --> pyroscope
    k6 -->|"remote write"| prometheus
    prometheus -->|"firing alerts"| alertmanager
    alertmanager -->|"webhook with resolved events"| ingress
    grafana --> prometheus
    grafana --> loki
    grafana --> tempo
    grafana --> pyroscope
```

Application code exports telemetry over OTLP and does not expose a Prometheus
`/metrics` endpoint. Pyroscope is the intentional direct-backend exception.
Beyla shares the API container PID namespace and must be recreated after the API
container is replaced. Its read-write `/sys/fs/bpf` mount enables pinned-map
features such as log enrichment and profile correlation.

See [Observability](../operations/observability/overview.md) for configuration,
profiles, restart order, query tools, and signal semantics.

## Authentication Boundaries

| Surface | Credential and safety boundary |
| --- | --- |
| `/v1/webhook` and `/v1/webhook/{source}` | Global webhook token or HMAC authentication plus ingress rate limiting. |
| `/v1/source-webhooks/{public_id}` | Revocable source-scoped bearer token; only its digest and display hint are persisted. |
| `POST /v1/changes` | Least-privilege change-ingest token and idempotent external ID. |
| Read-only management API and dashboard data | `API_KEY`, with per-IP management rate limiting. |
| Management mutations | `ADMIN_WRITE_KEY` in addition to the management boundary. |
| Feishu interactive actions | Signed callback, replay protection, idempotency receipt, and audit log. |
| MCP | Optional, read-only, explicitly enabled, and protected by the management API key. |

## Compose Deployment Profiles

| Profile or project | Components | Lifecycle |
| --- | --- | --- |
| Root `compose.yaml` | PostgreSQL, Redis, migrate, API, worker, scheduler | Everyday business stack. |
| `backup` profile | Scheduled `pg_dump` container | Optional durable backup job. |
| `webhookwise-observability` | Alloy, Prometheus, Alertmanager, Loki, Grafana | Default observability backends. |
| `diagnostics` profile | Tempo, Pyroscope, Beyla | Enable when traces, profiles, or eBPF diagnostics are required. |
| `load` profile | k6 | One-shot synthetic load and smoke checks. |

All projects join `webhookwise_webhook_net`. The observability project depends
on the business network already existing, so start the business stack first.

## Code Ownership

- `api/` binds HTTP contracts and authentication dependencies.
- `services/webhooks/` owns ingress orchestration, processing stages, source
  onboarding, and alert-quality read models.
- `services/incidents/` owns grouping, intelligence, response, resolution,
  recurrence, runbook progress, and postmortems.
- `services/analysis/`, `services/forwarding/`, `services/notifications/`,
  `services/kb/`, and `services/silences/` own their respective domain policies.
- `services/operations/` registers TaskIQ work and periodic dispatch.
- `core/` owns shared runtime primitives, not product policy.

See [Architecture Boundaries](boundaries.md) for the complete ownership and
dependency rules.
