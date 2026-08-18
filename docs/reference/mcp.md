# WebhookWise MCP Reference

WebhookWise exposes its **read side** over the Model Context Protocol (MCP) so
any MCP-compatible agent (OpenOcta / Claude / Cursor / a custom client) can query
it directly. Every tool is a thin wrapper over the existing query layer — no
business logic, and effectively **read-only**: no create-silence, no requeue, no
reanalyze, nothing an agent calls here changes what this deployment does.

The one exception is `propose_remediation`, which writes a row and executes
nothing. It records an inert proposal that an operator must approve through the
admin-write API before anything runs, and approval executes the same
`run_remediation` path the dashboard button does. Since this transport
authenticates with the read API key, that approval is the boundary that matters:
an agent can ask, only a person with write credentials can allow.

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
`MCP_ALLOWED_HOSTS=webhookwise.example.com,webhookwise.example.com:443`.

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

## Tools (20)

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
- **Returns**: `{ trace: { id, webhook_event_id, created_at, outcome, skip_code, source, importance, route, importance_override, degraded_reason, silence_id, matched_rules, ... } | null }` — null when no trace exists.

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

### Incidents & response

#### `list_incidents`
Incidents (grouped alerts) newest first: status, workflow state
(open/acknowledged/resolved/ignored), assignee, alert count, top importance,
timing. Optional `status` filter (`active` | `quiet` | `closed`). The tool for
"what is going on / what happened" questions.

#### `get_handoff_brief`
Shift-handoff digest over the last `hours` (default 8, max 168): counts, top
sources, active/quieted incidents, and a ready-to-paste markdown brief in
`summary_text`.

#### `get_response_metrics`
Global MTTA / MTTR / acknowledgement rate over `window_days` (default 30).
Nulls mean nothing in the window was acknowledged/resolved — an honest
answer, not an error.

### Delivery

#### `list_forward_outbox`
Outbound delivery intents (the transactional outbox) newest first: target,
status (`pending`…`sent`/`exhausted`/`expired`), attempts, last error.
Answers "did the notification actually go out"; dead-lettered *alerts*
(processing failures) are `list_dead_letter_alerts`.

### AI

#### `get_ai_analysis`
Analyses now carry `triage_verdict` (`act_now` | `monitor` | `defer`) and
`triage_confidence` (0-1) alongside importance.
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
Per-forward-rule match counts over a rolling 90-day window + recency (zombie-rule
detection). Note the asymmetry with `get_silence_roi`, whose counts are lifetime:
a forward rule that last matched over 90 days ago reports `count: 0`.
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
- **Returns**: `{ alert: {...} | null }` — null when the event is not a dead letter.

### Knowledge base

#### `search_knowledge_base`
Semantic search over WebhookWise's internal KB / runbooks.
- **Input**: `query` (str)
- **Returns**: `{ items: [ { title, content, source_ref, score } ] }` (empty when KB is disabled or nothing clears the similarity floor)

### Remediation proposals

#### `propose_remediation`
Propose that one Action Center command should run. **Executes nothing**: it
records an inert proposal for a human to approve.
- **Input**: `action` (`retry_outbox` | `retry_dead_letters` | `retry_stuck_events` | `retry_incident_summaries` | `test_enable_rule` | `disable_rule` | `acknowledge`), `reason` (required — what the reviewer reads), `resource_id?`, `resource_type?` (`webhook_event` | `incident`), `batch_size?`, `proposed_by?`, `ttl_hours?` (default 24, max 168)
- **Returns**: `{ proposal: {...} | null, executed: false, next_step }`, or `{ error, allowed_actions }` when the request is not runnable — returned rather than raised, so the agent can read the reason and fix it.
- `retry_outbox` / `test_enable_rule` / `disable_rule` / `acknowledge` need a `resource_id`; `acknowledge` also needs a `resource_type`. Arguments are validated against the executor's own request model, so a proposal that could not run cannot be created.
- Bounded: one pending proposal per action+resource (409 on a repeat), a capped pending queue, and an expiry — a stale suggestion cannot be approved.

#### `list_remediation_proposals`
Proposals newest first with their status.
- **Input**: `status?` (`pending` | `approved` | `rejected` | `expired` | `failed`), `limit?`
- **Returns**: `{ items: [{ id, action, resource_type, resource_id, reason, proposed_by, status, expires_at, decided_by, decided_at, result, created_at }] }`
- `approved` means a person allowed it **and** it ran (see `result.changed`); `failed` means it was allowed and the execution errored. Those are deliberately different states.

### Sandbox

#### `test_alert_payload`
Dry-run a raw payload through the pre-AI pipeline with **zero side effects** (no
enqueue, no AI call, no persistence).
- **Input**: `source` (str), `payload` (JSON object)
- **Returns**: a report with `source` (input/resolved/adapter/matched), `alert_hash`, `dedup_key`, the extracted identity, the rule-based importance, and which forward rules / silences would match.

---

## Resources

#### `webhookwise://reference/agent-guide`
**Read this first from an agent.** The usage guide for LLMs: which tool
answers which question, investigation recipes, field semantics
(`triage_verdict`, `skip_code`, `route`), and where the write boundary is. The
authoritative copy lives in `api/mcp/agent_guide.py` (docs/ does not ship in
the image).


- `webhookwise://reference/decision-trace-fields` — a Markdown field guide for
  interpreting decision-trace fields (`outcome`, `skip_code`, `route`, etc.).

## Prompts

- `investigate_alert(webhook_event_id)` — a root-cause investigation template
  that walks the agent through the decision trace → AI analysis → silences → KB.
- `review_silence_roi()` — a template for finding zombie silence rules.

---

## Notes

- Deep analysis is **sparse by design**: a `DeepAnalysis` record is created only
  when an alert forwards to a deep-analysis-target rule (typically high-importance).
  Most alerts therefore have only the lightweight `summary` (visible in
  `list_recent_alerts` and via `get_ai_analysis`'s `lightweight` fallback), which
  is expected — not a gap.
- Concurrency: the tools reuse the service's existing DB statement timeout, pool
  limits, and admin rate limiter, so a recursing agent cannot exhaust the pool.
