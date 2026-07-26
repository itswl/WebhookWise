# WebhookWise API Docs

FastAPI exposes interactive OpenAPI docs automatically when the API service is running:

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`
- OpenAPI JSON: `http://localhost:8000/openapi.json`

WebhookWise business endpoints are versioned under `/v1`. Health checks
(`/live`, `/ready`) and dashboard assets are operational endpoints and are not
part of the business API version.

Offline exports are generated on demand and are not checked in:

```bash
OTEL_ENABLED=false python scripts/export_openapi.py
```

The default output directory is `build/openapi`. Pass `--output-dir <dir>` to write somewhere else.

## Incident intelligence

`GET /v1/incidents/{incident_id}/intelligence` returns a compact command
summary, a derived service profile, active runbook executions, and three
independently ranked groups:

- similar quiet or closed incidents;
- deployment, configuration, feature-flag, and infrastructure changes near the
  incident start;
- published knowledge-base runbooks.

Each candidate includes a deterministic score and machine-readable evidence.
The endpoint does not call an LLM and does not execute a remediation. Operators
can label a candidate with
`POST /v1/incidents/{incident_id}/intelligence/feedback`.

Published runbooks are tracked as manual operator checklists:

```text
GET  /v1/incidents/{incident_id}/runbook-executions
POST /v1/incidents/{incident_id}/runbook-executions
PUT  /v1/incidents/{incident_id}/runbook-executions/{execution_id}
```

WebhookWise extracts structured Markdown steps but never executes commands.

Change and service read APIs are:

```text
GET /v1/changes/{change_id}/impact
GET /v1/services
GET /v1/service-profile?service=<name>&environment=<env>
```

The change assessment is an explainable association signal. Insufficient
samples return an unknown result rather than low risk.

CI/CD and change systems can idempotently ingest a normalized change through
`POST /v1/changes`. The `(source, external_id)` pair is the idempotency key.
Use the dedicated least-privilege change-ingestion token:

```bash
curl -X POST http://localhost:8000/v1/changes \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CHANGE_INGEST_TOKEN" \
  -d '{
    "external_id": "deploy-20260725-42",
    "source": "github-actions",
    "change_type": "deployment",
    "service": "checkout",
    "project": "store",
    "environment": "prod",
    "version_from": "v41",
    "version_to": "v42",
    "started_at": "2026-07-25T13:40:00Z",
    "source_url": "https://github.example/actions/runs/42"
  }'
```

`X-Change-Ingest-Token` is also accepted when a system cannot set a Bearer
header. `ADMIN_WRITE_KEY` remains a supported operator fallback, while
`API_KEY` alone is intentionally insufficient. See
[Change-event integrations](../integrations/change-events.md) for GitHub
Actions, GitLab CI, Jenkins, and Argo CD examples.

The optional Feishu custom-app callback is
`POST /v1/integrations/feishu/card-actions`. It uses a dedicated Feishu
verification token and signed action values instead of management API keys.
See [Feishu interactive incident cards](../integrations/feishu-interactive-cards.md).

## Read-only MCP server

WebhookWise can expose its read side over the Model Context Protocol (MCP) so
MCP-compatible agents (e.g. an OpenOcta / Claude / Cursor client) can query it
directly. It is a thin wrapper over the existing query layer — no business logic
and, by design, **read-only** (no create-silence / requeue / reanalyze tools).

- Transport: Streamable HTTP, mounted at `/mcp`.
- Enable it with `MCP_ENABLED=true`. It is off by default.
- Auth: the same management API key as the REST API (`Authorization: Bearer <API_KEY>`
  or `X-API-Key`).
- Host allowlist (DNS-rebinding protection): loopback is always allowed. Behind a
  reverse proxy set `MCP_ALLOWED_HOSTS` to the public host. Because the check
  matches the `Host` header exactly (or `host:*` for any port), add **both** the
  bare host and the `host:port` form when the proxy may forward either, e.g.
  `MCP_ALLOWED_HOSTS=webhookwise.example.com,webhookwise.example.com:443`.

It exposes 14 read-only tools (alerts, decision traces, AI analysis + cost,
forward-rule / silence ROI, dead letters, knowledge-base search, payload
sandbox), plus MCP resources and prompts. Write/action tools are intentionally
not exposed; they require an approval gate first.

**See [mcp.md](./mcp.md) for the full reference** — connection details, client
config, and every tool's inputs and return shape.
