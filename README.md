# WebhookWise

**English** | [中文](README.zh.md)

[![CI](https://github.com/itswl/WebhookWise/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/itswl/WebhookWise/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Latest release](https://img.shields.io/github/v/release/itswl/WebhookWise)](https://github.com/itswl/WebhookWise/releases)

*Self-hosted alert intelligence between your monitoring and your chat — dedup, noise reduction, AI triage, and a decision trace that can explain every notification (or its absence).*

WebhookWise is an intelligent Webhook receive, analysis, and forwarding service built for production operations. It normalizes events from Prometheus, Grafana, Alertmanager, Feishu, or any third-party system into a unified shape, writes them asynchronously to a queue and database, and then uses AI analysis, noise reduction and deduplication, transactional forwarding, and observability to turn alerts into operational events that can be tracked, audited, and acted on.

It is not a simple Webhook relay, but a small AIOps control plane:

- The API quickly returns `200 OK` once the request is enqueued, while time-consuming processing moves into TaskIQ/Redis Stream. Enqueueing is the durability boundary; see [Delivery Semantics](#delivery-semantics).
- The Worker pipeline handles normalization, persistence, deduplication, AI/rule analysis, noise reduction, and forwarding decisions.
- The Forward Outbox decouples business state from external HTTP/Feishu/OpenClaw side effects.
- OTel-first observability ties together metrics, traces, logs, events, signals, and profiles.

**Works well with:** WebhookWise sits upstream of executor platforms — it decides which alerts deserve attention and hands them off with context, so a downstream auto-investigation platform such as Ongrid can act on what gets through.

## Quick Links

| What you want to do | Where to go |
| --- | --- |
| Start the local environment | [Quick Start](#quick-start) |
| View the API | After startup, visit `http://localhost:8000/docs`; for export notes see [docs/reference/api.md](docs/reference/api.md) |
| Understand the full system | [docs/architecture/system-overview.md](docs/architecture/system-overview.md) |
| Understand module boundaries | [docs/architecture/boundaries.md](docs/architecture/boundaries.md) |
| Open the observability stack | [docs/operations/observability/local-lab/README.md](docs/operations/observability/local-lab/README.md) |
| Query observability data | [docs/operations/observability/query-tools.md](docs/operations/observability/query-tools.md) |
| Troubleshoot issues | [docs/operations/troubleshooting.md](docs/operations/troubleshooting.md) |
| Deploy to Kubernetes | [deploy/k8s/README.md](deploy/k8s/README.md) |
| Contribute to development | [CONTRIBUTING.md](CONTRIBUTING.md) |
| See version changes | [CHANGELOG.md](CHANGELOG.md) |

## Core Capabilities

| Capability | Description |
| --- | --- |
| Asynchronous Webhook receiving | The API only handles authentication, rate limiting, enqueueing, and basic persistence, releasing the upstream request quickly. |
| Multi-source normalization | Adapters normalize payloads from different ecosystems into a unified internal structure. |
| AI + rule dual analysis | Structured LLM analysis is preferred; it automatically falls back to rule-based analysis when the external service has problems. |
| OpenClaw deep analysis | Optionally integrate OpenClaw and poll for analysis results via TaskIQ delayed tasks. |
| Deduplication and noise reduction | Identifies duplicate and derived alerts based on alert hash, time window, similarity, and optional semantic signals. |
| Rule-based forwarding | Supports generic Webhook, Feishu card, DingTalk/WeCom bot (URL auto-detected), and OpenClaw targets. |
| Silencing and maintenance windows | One-off silences (with backtest + suppression debt report) plus recurring maintenance windows materialized into expiring silences by the scheduler. |
| Escalation-lite | Optional auto-SLA per importance arms the SLA-breach escalation card (@all / dedicated webhook) for unacknowledged incidents; status-flapping identities are detected and can be muted while they oscillate. |
| Learn loop | Resolved incidents sediment into KB drafts; published KB entries are attached to outgoing Feishu alert cards and one-click incident postmortem drafts (Markdown) close the review loop. |
| Incident intelligence | Incident detail ranks similar resolved incidents, suspected recent changes, and published runbooks with explicit evidence and operator feedback. |
| Incident response loop | A compact command summary, change impact, derived service profiles, manual runbook progress, optional signed Feishu actions, and product-value reporting connect detection to reusable knowledge. |
| Response and learning workspace | A prioritized work queue, structured resolution evidence, recurrence review, knowledge-gap discovery, and bounded feedback calibration turn incident handling into reusable operational knowledge. |
| Guided inbound onboarding | Source-scoped, revocable credentials and a first-event wizard connect new senders without sharing the global webhook secret. |
| Read-only alert quality center | Scores source payload completeness and highlights unstable identities, unmatched recoveries, timestamp anomalies, schema drift, and response gaps without changing source configuration. |
| Transactional Outbox | Processing results and forwarding intent are written to the database in the same transaction, then delivered and retried asynchronously by the Worker. |
| OTel-first observability | The application emits telemetry over OTLP; Alloy routes metrics, logs, and traces to Prometheus, Loki, and Tempo, while Pyroscope, Beyla, Alertmanager, and Grafana complete the diagnostic loop. |

## System Flow

```mermaid
flowchart LR
    sources["Alert sources<br/>global or source-scoped credentials"]
    api["FastAPI ingress<br/>authenticate, rate limit, enqueue"]
    queue["Redis Stream / TaskIQ"]
    worker["Worker pipeline"]
    process["Normalize -> identify -> deduplicate<br/>analyze -> reduce noise"]
    db["PostgreSQL<br/>events, incidents, knowledge"]
    outbox["Transactional outbox"]
    targets["Webhook / Feishu / DingTalk<br/>WeCom / OpenClaw"]
    response["Response center<br/>investigation and resolution"]
    learning["Recurrence, postmortem,<br/>KB drafts, calibration"]

    sources --> api
    api -->|"200 after queue acceptance"| queue
    queue --> worker
    worker --> process
    process --> db
    process --> outbox
    outbox --> targets
    db --> response
    response --> learning
    learning -->|"bounded evidence and ranking feedback"| response
```

For process topology, persistence relationships, scheduler duties, security
boundaries, and the complete observability flow, see
[System Architecture](docs/architecture/system-overview.md).

## Quick Start

### 1. Prepare configuration

```bash
cp .env.example .env
```

At a minimum you need to replace:

| Variable | Purpose |
| --- | --- |
| `API_KEY` | Token for read access to the management API. |
| `ADMIN_WRITE_KEY` | Token for management actions such as writes, replays, forwarding, and re-analysis. |
| `CHANGE_INGEST_TOKEN` | Least-privilege token for CI/CD systems that only submit change events. |
| `WEBHOOK_SECRET` | Webhook HMAC-SHA256 signing key and ingress token. |
| `OPENAI_API_KEY` | Optional; fill in when enabling AI analysis. |

The management API is limited to 300 requests per IP per minute by default. Expensive per-alert actions also have a 60-second distributed cooldown, with a five-minute minimum for starting deep analysis; the base values are configurable through `ADMIN_API_RATE_LIMIT_PER_MINUTE` and `ADMIN_ACTION_COOLDOWN_SECONDS`.

Webhook ingress accepts `X-Webhook-Signature: <HMAC-SHA256>` and token authentication through either `Token: <WEBHOOK_SECRET>`, `Authorization: Token <WEBHOOK_SECRET>`, or `Authorization: Bearer <WEBHOOK_SECRET>`. Grafana webhook contact points should use the Authorization header with scheme `Bearer` and the webhook secret as credentials; HTTP Basic authentication is intentionally not accepted.

For the complete configuration, see [.env.example.all](.env.example.all). Configuration is read only at process startup; after changes you must restart the process or perform a rolling release.

### 2. Start the full local stack

```bash
docker compose up -d --build
curl http://localhost:8000/ready
```

Compose first starts PostgreSQL and Redis, then runs `migrate`, and after the migration succeeds it starts the API, Worker, and Scheduler. When using a cloud database or managed Redis, you can run only `docker compose -p webhookwise --env-file .env -f deploy/compose/docker-compose.yml up -d --build` and point `DATABASE_URL` / `REDIS_URL` in `.env` at the external instances.

The root `compose.yaml` is the everyday entry point; it only includes the business stack such as PostgreSQL, Redis, API, Worker, and Scheduler, so `docker compose ps/logs/exec` by default only sees this set of containers. The full Compose fragments still live in `deploy/compose/`, and the observability stack is started as a separate Compose project.

### 3. Send a test event

```bash
curl -X POST http://localhost:8000/v1/webhook \
  -H "Content-Type: application/json" \
  -d '{"alertname":"TestAlert","severity":"critical","host":"prod-01"}'
```

Or seed a realistic five-minute demo (dedup storm, recoveries, a flapping
identity, multi-vendor payloads) through the real ingest path:

```bash
python scripts/seed_demo_data.py --base-url http://localhost:8000
```

The business API is only exposed under `/v1`; if Webhook authentication is enabled, you need to add a signature or Token according to the current configuration.

Out-of-the-box source formats: volcengine, Grafana, Prometheus Alertmanager, Datadog, PagerDuty, Feishu cards (code adapters), plus declarative YAML specs for Zabbix, Uptime-Kuma, Alibaba CloudMonitor, Tencent Cloud Monitor, Jenkins, and Sentry under `adapters/specs/` — add your own simple source with one YAML file (see `adapters/specs/README.md`).

### 4. Open the entry points

| Entry point | Address |
| --- | --- |
| Dashboard | `http://localhost:8000/` or `http://localhost:8000/dashboard` |
| Swagger UI | `http://localhost:8000/docs` |
| ReDoc | `http://localhost:8000/redoc` |
| Health | `http://localhost:8000/live` / `http://localhost:8000/ready` |

## Local Development

If the API/Worker run directly on the host while PostgreSQL/Redis are still provided by `deploy/compose/docker-compose.infra.yml`, change the host in `DATABASE_URL` to `localhost` and set `REDIS_URL` to `redis://localhost:6379/0` in your local environment or `.env`.

```bash
pip install -r requirements.lock
pip install -r requirements-dev.lock

uvicorn api.app:app --reload --port 8000
```

In another terminal, start the Worker:

```bash
taskiq worker services.operations.taskiq_wiring:broker
```

Scheduler entry point:

```bash
taskiq scheduler services.operations.taskiq_wiring:scheduler
```

Dependency policy:

- `requirements.txt` / `requirements-dev.txt` are the manually maintained direct dependencies and uniformly express the minimum supported versions.
- `requirements.lock` / `requirements-dev.lock` pin the exact resolution result and are the source of truth for local installs, CI, Docker builds, and deployment; do not use `requirements.txt` as a reproducible install entry point.
- The lock files are generated by uv. The project is currently not a `[project]`-style uv project, so it does not maintain `uv.lock`.
- GitHub Actions installs from the lock files, the Dockerfile installs only `requirements.lock`, and `scripts/check_requirements_locks.py` checks that these paths have not drifted.
- Dependabot scans the root pip dependencies weekly; a dependency upgrade PR needs to update both the direct dependency declarations and the corresponding lock files.

Update the lock files:

```bash
uv pip compile requirements.txt -o requirements.lock --python-version 3.12
uv pip compile requirements-dev.txt -c requirements.lock -o requirements-dev.lock --python-version 3.12
```

## Common Verification

| Level | Command | Coverage |
| --- | --- | --- |
| Static checks | `ruff check .` / `mypy` | Code style, type boundaries. |
| Unit and in-process integration | `pytest` | Pure functions, core services, the in-process path from FastAPI to the pipeline. |
| Docker E2E | `tests/e2e/run_webhook_to_feishu.sh` | The full path across PostgreSQL, Redis, API, Worker, Scheduler, and fake Feishu. |

It is recommended to run the Docker E2E before a release or when changing migrations, the queue, or the forwarding path.

## Deployment

### Docker Compose

```bash
docker compose up -d --build
docker compose ps
```

The observability stack uses the separate `webhookwise-observability` project:

```bash
docker compose -p webhookwise-observability --env-file .env -f deploy/compose/docker-compose.observability.yml up -d
```

### Database Backups

`scripts.ops.backup_db` uses `pg_dump` to generate PostgreSQL custom-format backups and writes a matching `.dump.sha256` checksum file for each `.dump`.

```bash
python -m scripts.ops.backup_db --verbose
python -m scripts.ops.backup_db --verify backups
python -m scripts.ops.backup_db --cleanup-only
```

For configuration options, see the `DB Backup` section of `.env.example.all`. Setting `AWS_BUCKET` uploads the backup and checksum; the command returns non-zero when the object-storage upload fails.

### Kubernetes

`deploy/k8s/` provides base manifests: API, Worker, Scheduler, migration Job, Redis, PostgreSQL, ConfigMap, Secret example, and ServiceAccount.

```bash
cp deploy/k8s/secret.example.yaml /tmp/webhookwise-secret.yaml
$EDITOR /tmp/webhookwise-secret.yaml
kubectl apply -f /tmp/webhookwise-secret.yaml
kubectl apply -k deploy/k8s
```

Application images must use a release tag or digest; avoid using `latest`. For more details, see [deploy/k8s/README.md](deploy/k8s/README.md).

## Project Structure

```text
.
├── api/                  # FastAPI routes, request/response binding, and auth dependencies
├── adapters/             # External Webhook payload normalization and plugin registration
├── alembic/              # Database migrations
├── contracts/            # Stable normalized payload and cross-module contracts
├── core/                 # Runtime infrastructure such as config, logging, auth, Redis, OTel, and HTTP client
├── db/                   # SQLAlchemy engine/session lifecycle
├── deploy/               # Compose, Kubernetes, and observability deployment resources
├── docs/                 # Architecture, operations, and reference docs
├── models/               # SQLAlchemy ORM models
├── prompts/              # AI and deep-analysis prompt templates
├── schemas/              # Pydantic API schema
├── scripts/              # Operations, export, and observability query scripts
├── services/
│   ├── analysis/         # AI/rule/OpenClaw analysis, caching, and usage
│   ├── forwarding/       # Forwarding rules, Outbox, remote delivery, and retries
│   ├── incidents/        # Grouping, response, intelligence, recurrence, runbooks, and postmortems
│   ├── kb/               # Knowledge ingestion, retrieval, and incident sedimentation
│   ├── notifications/    # Notification channels and message formatting
│   ├── operations/       # TaskIQ tasks, scheduling, recovery, and maintenance
│   ├── silences/         # Silence and maintenance-window policies
│   └── webhooks/         # Webhook ingest, pipeline, queries, and commands
├── templates/            # Dashboard HTML and static assets
└── tests/
    ├── adapters/         # External payload adapter tests
    ├── analysis/         # AI, OpenClaw, noise reduction, and analysis strategy tests
    ├── api/              # FastAPI routes and API contract tests
    ├── forwarding/       # Forwarding rules, Outbox, retries, and URL safety tests
    ├── integration/      # In-process business path integration tests
    ├── observability/    # Observability, documentation, and operations contract tests
    ├── runtime/          # Config, logging, Redis, migration, and runtime infrastructure tests
    ├── webhooks/         # Webhook parsing, pipeline, deduplication, and suppression tests
    ├── e2e/              # Docker E2E
    ├── helpers/          # pytest helpers
    └── k6/               # Load-testing scripts
```

For stricter ownership rules, see [docs/architecture/boundaries.md](docs/architecture/boundaries.md).

## The Suppression Stack — "why didn't I get notified?"

Eight mechanisms can stop an alert from reaching chat. They are not eight
mysteries: every alert passes the same gates in the same order, and the
decision trace records exactly which gate acted (dashboard → Decision Trace).

```mermaid
flowchart TD
    A[Alert received] --> G1{"① Ingress backpressure<br/>(per-alert storm / queue high water, opt-in)"}
    G1 -->|suppressed| X1[dropped at ingress · 200 with outcome=suppressed]
    G1 --> G2{"② Dedup"}
    G2 -->|duplicate| X2[joins the existing thread<br/>cooldown / periodic reminder decide re-notify]
    G2 --> G3{"③ Smart noise reduction<br/>(derived alerts of one root cause)"}
    G3 -->|suppressed| X3[skip_code=noise_suppressed]
    G3 --> G4{"④ Silences & maintenance windows"}
    G4 -->|matched| X4[skip_code=silenced]
    G4 --> G5{"⑤ Flapping mute<br/>(firing↔recovered oscillation, opt-in)"}
    G5 -->|flapping| X5[skip_code=flapping]
    G5 --> G6{"⑥ Cooldown<br/>(just notified)"}
    G6 -->|inside window| X6[skip_code=cooldown]
    G6 --> G7{"⑦ Forwarding rules"}
    G7 -->|no rule matches| X7[skip_code=no_match / duplicate_no_rule]
    G7 --> D[Delivered — outbox → Feishu / DingTalk / WeCom / webhook]
```

Rules of thumb: gates ①⑤ are **opt-in** (default off); ②③⑥ are automatic
noise control; ④⑦⑧ are operator-authored policy. Every skip is visible —
nothing is dropped without a decision-trace row naming the gate.

## Runtime Settings — live operator policy

Most config is static process configuration (env is its home, change = redeploy).
The exception is **operator policy** — the knobs you tune while running the
system (flapping, auto-SLA, backpressure fractions, noise weights, notify
cadence, KB cards, trace retention). Every key tagged `[runtime-policy]` in
[.env.example.all](.env.example.all) is served by a DB-backed override plane:

- **Resolution order:** DB override → env value → code default. The env value
  stays the bootstrap default; an override is a sparse row on top of it.
- **Where:** dashboard *Operations → Settings*, or the API —
  `GET /v1/runtime-settings` (list with env/override/effective per key),
  `PUT /v1/runtime-settings/{KEY}` / `DELETE …/{KEY}` (admin write key
  required). Writes are validated against a typed registry and audited.
- **Propagation:** all processes (api / worker / scheduler) apply a change
  within ~60 seconds — a Redis pub/sub nudge plus an interval refresh; no
  restart, no file edit.
- **Failure posture:** fail-open. If the DB or Redis is unhealthy the last
  snapshot (or plain env config) keeps serving; the hot path never depends on
  this plane.

## Delivery Semantics

Understanding the durability boundaries of this path is what lets you correctly assess the risk of loss and duplication:

- **Receive → enqueue: accepted (not a durability promise).** The API returns `200 OK` as soon as the request is written to the Redis Stream (`XADD`); DB persistence happens on the Worker side. So `200 OK` means "accepted and enqueued", not "persisted". When the Redis `XADD` fails, the API returns 5xx and the upstream should retry.
- **`WEBHOOK_MQ_STREAM_MAXLEN` is a data-loss knob, not just a memory knob.** The stream is trimmed by an approximate cap (`MAXLEN ~`): when sustained bursts exceed the Worker consumption rate and the backlog exceeds that cap, the oldest *un-acked* entries are trimmed, and the corresponding webhooks that already returned `200` are silently lost. During capacity planning, set this value based on peak backlog and pair it with queue backlog alerts (`queue.pending` / `queue.lag`).
- **Make the backlog visible, and optionally refuse before trimming.** The dashboard surfaces live queue depth, pending, and lag (Overview tile), and the Action Center raises a critical item once the *unconsumed* backlog (undelivered `lag` + un-acked `pending`) crosses `WEBHOOK_MQ_BACKLOG_WARN_FRACTION` of `MAXLEN` (default `0.8`) — *before* the silent trim. The signal is the unconsumed backlog, not total stream length: a busy stream's length sits at `MAXLEN` of already-acked entries, which is normal retention, not a backlog. To turn silent loss into visible backpressure, set `WEBHOOK_MQ_INGRESS_HIGH_WATER_FRACTION` (default `0`, disabled): above that fraction of `MAXLEN` the API rejects new webhooks with `503 Retry-After` so a retrying upstream holds them, instead of the stream trimming its *oldest* un-acked entries. It reads a short-TTL cached backlog (no per-request Redis round trip) and fails open (a probe error never blocks ingress). Enable it only after capacity-planning `MAXLEN` and confirming your senders retry on 503.
- **Redis persistence determines the crash boundary.** The bundled Redis runs with AOF enabled (`--appendonly yes --appendfsync everysec`) on a durable named volume (see `deploy/compose/docker-compose.infra.yml`; the Kubernetes StatefulSet matches), so a Redis crash loses at most ~1 second of writes — the in-flight Stream entries not yet fsynced by the last `everysec` flush. For a stricter boundary set `--appendfsync always` (fsync every write, higher latency) or use a managed Redis with synchronous replication.
- **After enqueue: at-least-once.** Failed Worker processing retries with backoff and goes to dead-letter once exhausted; forwarding is delivered through the transactional Outbox, and stale-recovery plus retries may deliver duplicates. Downstream should deduplicate based on the `Idempotency-Key` request header (see [services/forwarding](services/forwarding)).

When you need "zero loss at ingress", you should add retries/acknowledgements upstream or place a durable queue in front of the API; the current implementation trades this off for low ingress latency.

## Runtime Contract

- The API receive layer does not do long-running analysis and does not directly execute external forwarding side effects.
- The receive layer is at-most-once-until-consumed: `200 OK` means accepted (enqueued), not persisted; `WEBHOOK_MQ_STREAM_MAXLEN` and Redis's AOF fsync cadence together determine the loss boundary (see [Delivery Semantics](#delivery-semantics)).
- The Worker is the main execution surface of the business pipeline; the Scheduler only dispatches periodic tasks.
- The Forward Outbox is the audit boundary for external delivery; retries and expired states must be persisted to the database.
- Configuration is static process configuration, with one deliberate exception: keys tagged `[runtime-policy]` accept DB-backed live overrides through the runtime-settings plane (see [Runtime Settings](#runtime-settings--live-operator-policy)); everything else changes only with a redeploy.
- The application emits telemetry only over OTLP and does not directly expose `/metrics`.
- For a new Webhook source, prefer adding an adapter and tests first, then reusing the existing pipeline.
- For a new business capability, prefer placing it in the nearest `services/*` domain package and avoid stuffing business logic into `core/`.

## Documentation Map

For the complete documentation entry point, see [docs/README.md](docs/README.md).

| Category | Documents |
| --- | --- |
| Architecture | [System Architecture](docs/architecture/system-overview.md), [Module Boundaries](docs/architecture/boundaries.md) |
| Operations | [Observability](docs/operations/observability/overview.md), [Grafana Dashboards](docs/operations/observability/dashboards.md), [Query Tools](docs/operations/observability/query-tools.md), [Troubleshooting](docs/operations/troubleshooting.md) |
| Reference | [API Docs](docs/reference/api.md), [Kubernetes](deploy/k8s/README.md), [Contributing Guide](CONTRIBUTING.md), [Changelog](CHANGELOG.md) |

## Community

- Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md); the entire quality gate is one command, `bash scripts/gate.sh`.
- Security issues go **privately** through [GitHub Security Advisories](https://github.com/itswl/WebhookWise/security/advisories/new), never public issues — see [SECURITY.md](SECURITY.md).
- Bugs and feature requests: [GitHub Issues](https://github.com/itswl/WebhookWise/issues).
- We follow the [Contributor Covenant](CODE_OF_CONDUCT.md).

## License

MIT License — see [LICENSE](LICENSE).
