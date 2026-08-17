"""The agent-facing usage guide, served as an MCP resource.

One authoritative copy, here, because docs/ does not ship in the image and a
guide an agent cannot fetch is a guide that does not exist. Humans read the
same text through the resource (webhookwise://reference/agent-guide) or this
file; docs/reference/mcp.md links here instead of duplicating it.
"""

AGENT_GUIDE = """\
# Using WebhookWise from an agent (MCP)

WebhookWise is a self-hosted alert gateway: it ingests monitoring webhooks,
deduplicates and denoises them, runs AI + rule triage, groups alerts into
incidents, and forwards what matters to chat (Feishu/DingTalk/WeCom/webhook).
Everything it decides is recorded and queryable. This MCP surface is
**read-only by design**: you can see everything, you can change nothing.
Acknowledge/resolve/silence/replay happen in the human dashboard.

## Which tool answers which question

Situational awareness:
- "What is going on right now / what happened?" -> `list_incidents`
  (grouped alerts with workflow state), then per-alert tools for detail.
- "Summarize the last shift" -> `get_handoff_brief` (hours=8/12/24) — its
  `summary_text` is paste-ready markdown.
- "How fast are we responding?" -> `get_response_metrics` (MTTA/MTTR/ack
  rate; nulls mean nothing was acknowledged/resolved in the window).
- "Volume / rates overview" -> `get_alert_overview_stats` (period).

Per-alert investigation:
- "Why was/wasn't alert N notified?" -> `get_alert_decision_trace` — the
  ordered gate decisions (skip_code names the gate that stopped it).
- "What did the AI conclude?" -> `get_ai_analysis` — summary, root cause,
  recommendations, `triage_verdict` (act_now|monitor|defer) +
  `triage_confidence` (0-1), and the full deep-analysis report when one ran.
- Recent alerts with filters -> `list_recent_alerts` (importance, source,
  window; each row carries a deep_analysis marker).
- Trace lists with filters -> `list_alert_decision_traces`.

Delivery ("did the notification actually go out?"):
- `list_forward_outbox` — outbound intents with status
  (pending|processing|retrying|sent|exhausted|expired) and last error.
- `list_dead_letter_alerts` / `get_dead_letter_alert` — alerts whose
  PROCESSING failed permanently (different failure class than delivery).

Hygiene and meta:
- `get_forward_rule_roi` / `get_silence_roi` — which rules/silences earn
  their keep; zero-count entries are zombies.
- `list_active_silences` — what is muted right now, with match criteria.
- `get_decision_quality_stats` — AI vs rule routing, override and
  degradation rates.
- `get_ai_cost_stats` — token spend over a period.
- `search_knowledge_base` — semantic search over published runbooks/KB.

Dry run:
- `test_alert_payload` — push a raw payload through the pre-AI pipeline with
  zero side effects: which adapter parses it, the identity extracted, which
  forward rules/silences would match.

## Recipes

Investigate one alert (also available as the `investigate_alert` prompt):
1. `get_alert_decision_trace` — forwarded or skipped, and which gate acted.
2. `get_ai_analysis` — reuse the existing verdict; do not re-derive it.
3. `list_incidents` — does it belong to a wider incident? Sibling alerts and
   workflow state are the strongest context.
4. If silenced: `list_active_silences` to name the muting rule.
5. `search_knowledge_base` for a runbook. Then explain plainly.

Shift summary: `get_handoff_brief` -> relay `summary_text`, then
`list_incidents(status="active")` for anything still open.

Noise review (also the `review_silence_roi` prompt): `get_silence_roi` +
`list_active_silences` -> flag active silences with zero suppressions and
long-expired intentions; `get_forward_rule_roi` for zombie forward rules.

## Field semantics worth knowing

- `triage_verdict`: act_now | monitor | defer — "should a human act now",
  distinct from importance (a recovered high-importance alert is a defer).
  Absent on analyses cached before the field existed; never invent one.
- `skip_code`: silenced, duplicate, noise_reduced, cooldown,
  periodic_reminder, no_rule_match, flapping — the gate that stopped it.
- `route`: ai (model), rule (deterministic), redis_reuse (cached verdict).
- Incident workflow_status: open -> acknowledged -> resolved | ignored.

## Limits

- Read-only; there is no tool that mutates state. Recommend actions, name
  the dashboard destination (e.g. "lift silence #12 under 静默"), and stop.
- `page_size` caps at 200; periods are day|week|month|year; handoff hours
  cap at 168; response-metrics window caps at 365 days.
- The resource `webhookwise://reference/decision-trace-fields` documents the
  trace fields in full.
"""
