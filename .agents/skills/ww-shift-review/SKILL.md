---
name: ww-shift-review
description: Produce a WebhookWise shift-handoff brief — active incidents, alert pressure, delivery health, response metrics, AI cost. Use for "交接班/值班回顾/handoff", "这个班发生了什么", "给下一班写个纪要", or a morning catch-up after time away.
---

# WebhookWise shift review / handoff brief

Build the brief a departing on-call would write for the next shift, from the
read-only `webhookwise` MCP server. Field semantics: MCP resource
`webhookwise://reference/agent-guide`.

## Procedure

1. `get_handoff_brief(hours=H)` — the server's own shift digest is the spine.
   Default H to the shift length the user implies (8 if unstated).
2. `list_incidents(status="active")` — anything open outranks everything else.
   For each active incident note age, assignee (unassigned = a finding), and
   the latest state.
3. Delivery health: `list_forward_outbox(status="failed")` and
   `list_dead_letter_alerts`. Exhausted retries and dead letters are handoff
   items by definition — the next shift inherits them silently otherwise.
4. `get_response_metrics` — MTTA/MTTR/ack-rate. Compare against the brief's
   period, flag only meaningful movement, not decimal jitter.
5. Optional color, one call each, only if the shift was noisy or expensive:
   `get_alert_overview_stats(hours=H)`, `get_ai_cost_stats`.

## Output shape

Chinese, markdown, in this order — worst news first:

1. **必须交接** — open incidents + broken delivery (dead letters, exhausted
   retries, invalid webhooks). Each with id, age, and the one next action.
2. **本班概况** — alert volume vs normal, top noisy sources, notable silences
   or rule changes if the brief mentions them.
3. **响应质量** — MTTA/MTTR/ack-rate, one line, only deltas worth acting on.
4. **建议** — at most 3 items, each pointing at a dashboard page
   (`#/incidents`, `#/delivery`, `#/silences`, `#/noise`).

Keep it under ~25 lines: a handoff nobody reads is a handoff that failed.
Everything is read-only — recommend, never claim to have acted.
