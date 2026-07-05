# WebhookWise MCP Reference

WebhookWise exposes its **read side** over the Model Context Protocol (MCP) so
any MCP-compatible agent (OpenOcta / Claude / Cursor / a custom client) can query
it directly. Every tool is a thin wrapper over the existing query layer — no
business logic, and, by design, **read-only** (no create-silence / requeue /
reanalyze). Write/action tools are intentionally not exposed; they require an
approval gate first.

## Connecting

| | |
|---|---|
| **URL** | `https://<host>/mcp/` (**trailing slash required** — without it the mount 307-redirects to `/mcp/`, which some clients do not follow) |
| **Transport** | MCP Streamable HTTP (not the deprecated SSE transport) |
| **Auth** | `Authorization: Bearer <API_KEY>` (or `X-API-Key`) — the same management API key as the REST API |

Enable it with `MCP_ENABLED=true` (off by default). Behind a reverse proxy, set
`MCP_ALLOWED_HOSTS` to the public host for DNS-rebinding protection; loopback is
always allowed. The Host check matches exactly or `host:*` (the `:*` form
requires a port), so add **both** the bare host and the `host:port` form when the
proxy may forward either, e.g.
`MCP_ALLOWED_HOSTS=dejavu.example.com,dejavu.example.com:443`.

### Smoke test

```bash
curl -X POST https://<host>/mcp/ \
  -H "Authorization: Bearer <API_KEY>" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}'
```

A `200` with a `result.serverInfo` means you are connected.

### Client config (Claude Desktop / Cursor / any HTTP-MCP client)

```json
{
  "mcpServers": {
    "webhookwise": {
      "url": "https://<host>/mcp/",
      "headers": { "Authorization": "Bearer <API_KEY>" }
    }
  }
}
```

---

## Tools (14)

All list tools cap `page_size` at 200. Time-window `period` accepts
`day` | `week` | `month` | `year` (invalid values fall back to `day`).

### Alerts & decisions

#### `list_recent_alerts`
Recent alert summaries, newest first. Each row also carries a lightweight
`deep_analysis` marker (not the full report — see `get_ai_analysis`).
- **Input**: `importance?`, `source?`, `window?` (`today` | `7d` | `30d` | `all`), `page?`, `page_size?`
- **Returns**: `{ items: [{ id, request_id, source, client_ip, timestamp, importance, is_duplicate, duplicate_of, duplicate_count, duplicate_type, forward_status, summary, created_at, prev_alert_id, prev_alert_timestamp, is_within_window, deep_analysis: { available, status?, engine?, summary_preview?, analysis_id? } }], has_more, next_cursor }`
- `summary` is the lightweight AI's one-line output. `deep_analysis.available` tells you whether a full deep report exists to fetch.

#### `get_alert_decision_trace`
The full decision chain for one alert: why it was forwarded or skipped.
- **Input**: `webhook_event_id` (int)
- **Returns**: `{ result: { id, webhook_event_id, created_at, outcome, skip_code, source, importance, route, importance_override, degraded_reason, silence_id, matched_rules, ... } }`, or `null` if no trace exists.

#### `list_alert_decision_traces`
Recent decision traces, newest first, each with its chain inline.
- **Input**: `outcome?` (`forwarded` | `skipped`), `skip_code?`, `source?`, `delivery?` (`failed` selects forwarded alerts whose delivery ultimately failed), `page?`, `page_size?`
- **Returns**: `{ items: [ <trace> ], has_more, next_cursor }`

#### `get_alert_overview_stats`
One-screen operational summary over a window.
- **Input**: `period?`
- **Returns**: `{ period, total, forwarded, skipped, forward_rate, skip_code_breakdown, top_sources: [{source, count}], delivery: { total, delivered, failed, success_rate } }`

#### `get_decision_quality_stats`
Decision-quality meta-stats (routing / overrides / degradation).
- **Input**: `period?`
- **Returns**: `{ period, total, ai_total, route_breakdown, override_count, override_rate, degraded_total, degraded_rate, degraded_reasons, ... }`

### AI

#### `get_ai_analysis`
The AI analysis for one alert. Prefers the full deep-analysis reports; falls
back to the lightweight per-alert AI when there is no deep analysis, so a single
lookup is never empty for an event that exists.
- **Input**: `webhook_event_id` (int), `limit?` (default 10, max 50)
- **Returns**: `{ analysis_level: "deep" | "lightweight" | "none", items: [...] }`
  - `deep`: `items` are full reports (`analysis_result` with `summary, root_cause, evidence, timeline, impact, confidence, unknowns, assumptions`, plus `engine`, `status`, timestamps).
  - `lightweight`: one item `{ webhook_event_id, source, importance, summary, analysis }` from the event's lightweight AI.
  - `none`: unknown event or no AI at all → `items: []`.

#### `get_ai_cost_stats`
AI usage / cost over a window.
- **Input**: `period?`
- **Returns**: `{ total_calls, route_breakdown, percentages, tokens, cost, cache_statistics, trend }`

### Routing & silences

#### `get_forward_rule_roi`
Per-forward-rule lifetime match counts + recency (zombie-rule detection).
- **Input**: none
- **Returns**: `{ "<rule_name>": { count, last_matched_at } }`

#### `list_active_silences`
Silence rules currently in effect, each with its suppression ROI.
- **Input**: none
- **Returns**: `{ items: [ { <silence fields>, suppressed_count, last_suppressed_at } ] }`

#### `get_silence_roi`
Per-silence lifetime suppression counts (zombie-silence detection).
- **Input**: none
- **Returns**: `{ "<silence_id>": { count, last_suppressed_at } }` (keys are stringified silence ids)

### Dead letters

#### `list_dead_letter_alerts`
Alerts whose processing permanently failed.
- **Input**: `source?`, `search?` (matches error message / failure reason), `page?`, `page_size?`
- **Returns**: `{ items: [ { id, source, timestamp, created_at, alert_hash, importance, retry_count, processing_status, failure_reason, error_message } ] }`

#### `get_dead_letter_alert`
Full detail of one dead-letter alert.
- **Input**: `event_id` (int)
- **Returns**: the event detail dict, or `null` if the event is not a dead letter.

### Knowledge base

#### `search_knowledge_base`
Semantic search over WebhookWise's internal KB / runbooks.
- **Input**: `query` (str)
- **Returns**: `{ items: [ { title, content, source_ref, score } ] }` (empty when KB is disabled or nothing clears the similarity floor)

### Sandbox

#### `test_alert_payload`
Dry-run a raw payload through the pre-AI pipeline with **zero side effects** (no
enqueue, no AI call, no persistence).
- **Input**: `source` (str), `payload` (JSON object)
- **Returns**: a report with `source` (input/resolved/adapter/matched), `alert_hash`, `dedup_key`, the extracted identity, the rule-based importance, and which forward rules / silences would match.

---

## Resources

- `webhookwise://reference/decision-trace-fields` — a Markdown field guide for
  interpreting decision-trace fields (`outcome`, `skip_code`, `route`, etc.).

## Prompts

- `investigate_alert(webhook_event_id)` — a root-cause investigation template
  that walks the agent through the decision trace → AI analysis → silences → KB.
- `review_silence_roi()` — a template for finding zombie silence rules.

---

## Notes

- Deep analysis is **sparse by design**: a `DeepAnalysis` record is created only
  when an alert forwards to an OpenClaw-target rule (typically high-importance).
  Most alerts therefore have only the lightweight `summary` (visible in
  `list_recent_alerts` and via `get_ai_analysis`'s `lightweight` fallback), which
  is expected — not a gap.
- Concurrency: the tools reuse the service's existing DB statement timeout, pool
  limits, and admin rate limiter, so a recursing agent cannot exhaust the pool.
