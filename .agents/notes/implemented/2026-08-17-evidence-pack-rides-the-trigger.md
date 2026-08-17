---
title: The investigation evidence pack rides the trigger, not the investigator
status: implemented
date: 2026-08-17
scope: services
---

## Decision

Every deep-analysis trigger appends a "系统侧上下文" section to the gateway
message: the event's decision trace, its 7-day repeat count on the same alert
hash, the prior verdict on that hash, incident membership, and up to two KB
hits (500-char excerpts). Assembled best-effort in `build_evidence_pack`
(`services/analysis/deep_analysis_trigger.py`); any failure logs a warning
and the trigger proceeds without it. The event id is threaded explicitly from
the outbox row (`record.webhook_event_id`) because `forward_data` carries
only the alert payload body.

## Why

- hookprobe can fetch all of this itself over the read-only MCP surface, but
  that costs a round of tool calls on *every* investigation — model latency
  and tokens spent re-fetching what the gateway already knows at trigger
  time. Shipping it costs one JSON block.
- The pack is fenced inside the existing untrusted-data boundary and passed
  through `neutralize_untrusted_text`: trace fields are ours, but alert
  summaries and KB excerpts quote external text.
- Caps (2 KB hits, 500 chars each) keep a pathological KB entry from
  crowding the alert itself out of the investigator's context.

## Consequences

- Manual/API-triggered investigations that never pass an event id simply get
  no evidence section — same message as before this change.
- MCP stays the *interactive* path (follow-up questions mid-run); the pack is
  the *warm start*. They complement, not duplicate.

## Rejected

- Enriching `forward_data` at enqueue time: evidence would go stale in the
  outbox during retries/backlog, and every non-deep channel would carry dead
  weight.
- Having hookprobe always self-fetch (status quo): correct but slower and
  costlier per run, and it silently degrades when the MCP config is absent —
  the pack works even for a gateway with no MCP access.
