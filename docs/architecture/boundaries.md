# Architecture Boundaries

WebhookWise is a modular monolith with separate deployable processes. The code
is not intended to be a microservice split, and it should not mix generic
"layered" buckets with feature ownership without an explicit reason.

## Module Ownership

| Path | Owns | Must not own |
|:---|:---|:---|
| `api/` | HTTP routes, request/response binding, auth dependency wiring | Business workflows, database transaction orchestration, external delivery logic |
| `services/webhooks/` | Webhook ingest and processing orchestration, pipeline stages, webhook queries and commands | Provider-specific HTTP clients unrelated to webhook processing |
| `services/analysis/` | AI/rule/OpenClaw analysis policies, prompt loading, cache and usage tracking | FastAPI route handling, notification channel formatting |
| `services/forwarding/` | Forwarding rules, transactional outbox, delivery retry, remote target dispatch | Source webhook normalization |
| `services/notifications/` | Notification channel abstractions, target detection, message formatting | Pipeline orchestration or persistence decisions |
| `services/operations/` | TaskIQ tasks, schedulers, outbox scans, data maintenance jobs | Request parsing or domain decisions hidden inside background tasks |
| `adapters/` | Inbound ecosystem normalization and plugin registry | Business orchestration, notification target detection, or target delivery side effects |
| `models/` | SQLAlchemy persistence schema | Domain decision logic beyond simple model helpers |
| `schemas/` | Pydantic API contracts | ORM behavior or service orchestration |
| `db/` | Engine/session lifecycle | Domain repositories or API dependencies |
| `core/` | Cross-cutting runtime glue: config, logging, metrics, tracing, auth, shared clients | Feature-specific business policy |
| `core/web/` | FastAPI middleware and startup validation | Domain workflows or persistence decisions |

New webhook source support should normally add an adapter plus tests, then wire
existing `services/webhooks/` pipeline behavior. A new business capability
should be placed under the nearest feature package before adding another
top-level technical bucket.

## Core Directory Guardrail

`core/` is allowed to contain framework and runtime glue, but it is not a
business-domain home. When a module starts mentioning webhook semantics,
forwarding rules, AI policy, notification target details, or retry decisions for
a single feature, move that logic into the owning `services/*` package and keep
only the reusable primitive in `core/`.

Allowed examples:

- `core/app.py`: FastAPI application construction and middleware registration.
- `core/web/`: web middleware and startup checks.
- `core/config/`: static settings plus runtime override manager.
- `core/metrics.py` and `core/otel.py`: instrumentation setup.
- `core/taskiq_broker.py`: broker construction.
- `core/http_client.py` and `core/redis_client.py`: shared client lifecycle.

Task registration is intentionally outside `core/`: TaskIQ CLI entrypoints use
`services.operations.taskiq_wiring`, which imports task definitions and
re-exports the broker/scheduler. This keeps `core.taskiq_broker` from depending
on `services.operations.tasks`.

Borderline modules such as `core/webhook_security.py`,
`core/alert_concurrency.py`, and `core/circuit_breaker.py` should stay small and
primitive-oriented. If they grow feature-specific branches, split the policy
into `services/webhooks/` or `services/forwarding/`.

## Ports And Adapters Boundary

The project uses ports-and-adapters selectively:

- Inbound adapters in `adapters/` normalize external webhook payloads into a
  stable internal shape.
- Outbound delivery adapters live in `services/forwarding/` and
  `services/notifications/`, where business rules can choose targets and record
  outcomes through the outbox.

Adapters should transform or deliver. Services should decide and orchestrate.
If a file both decides whether something should happen and performs external
I/O, split the decision into a service/policy object and keep the I/O code in an
adapter/client module.

## Deployment Shape

The default topology is one process per application container:

- `migrate`: one-shot Alembic job.
- `webhook-service`: API process.
- `worker`: TaskIQ worker process, horizontally scalable.
- `scheduler`: singleton TaskIQ scheduler.

`docker-compose.supervisor.yml` is an explicit all-in-one override for small
single-host deployments and demos. It must remain optional. Production changes
should preserve the default multi-container path and should not require
`RUN_MODE=all`.

Deployment shape must not introduce a second runtime semantic path. All
topologies use the same TaskIQ/Redis queue, dynamic scheduling, Redis-backed
cache, and distributed locking behavior.

## Dependency Policy

`requirements.txt` and `requirements-dev.txt` are the human-edited direct
dependency manifests. `requirements.lock` and `requirements-dev.lock` are the
installable lock files generated with uv:

```bash
uv pip compile requirements.txt -o requirements.lock --python-version 3.12
uv pip compile requirements-dev.txt -c requirements.lock -o requirements-dev.lock --python-version 3.12
```

Runtime images install from `requirements.lock`. Local development and CI should
install runtime dependencies from `requirements.lock` and test/dev dependencies
from `requirements-dev.lock`. Do not add a second lock source such as
`uv.lock` unless the project is fully migrated to a `[project]`-based
`pyproject.toml`.

## Migration Policy

Alembic revisions describe the current schema and deliberate forward-only data
changes. Avoid vague names such as "cleanup" or "logic sinking"; those hide the
operational risk. Do not add schema repair logic to application startup. Existing
environments that predate the current baseline must be reset or migrated with a
one-off operator-run script outside the service runtime.

## Prompt Management

Prompt text is a product asset. The current control plane is:

- `AI_SYSTEM_PROMPT`: runtime-configurable system prompt.
- `AI_USER_PROMPT`: inline override for the user prompt.
- `AI_USER_PROMPT_FILE`: file-backed prompt template.
- `DEEP_ANALYSIS_PROMPT`: inline override for the OpenClaw deep-analysis prompt.
- `DEEP_ANALYSIS_PROMPT_FILE`: file-backed OpenClaw deep-analysis prompt template.
- `GET /api/prompt?kind=user|deep_analysis`: inspect the active prompt and source.
- `POST /api/prompt/reload?kind=user|deep_analysis`: reload file-backed prompt content.

Prompt experiments should be represented as explicit config changes or versioned
prompt files with tests. Avoid editing prompt text without a way to identify the
active source in logs and API responses.
