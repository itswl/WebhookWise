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
  `MCP_ALLOWED_HOSTS=dejavu.example.com,dejavu.example.com:443`.

It exposes 14 read-only tools (alerts, decision traces, AI analysis + cost,
forward-rule / silence ROI, dead letters, knowledge-base search, payload
sandbox), plus MCP resources and prompts. Write/action tools are intentionally
not exposed; they require an approval gate first.

**See [mcp.md](./mcp.md) for the full reference** — connection details, client
config, and every tool's inputs and return shape.
