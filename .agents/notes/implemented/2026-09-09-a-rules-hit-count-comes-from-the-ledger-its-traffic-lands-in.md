---
title: A rule's hit count comes from the ledger its traffic actually lands in
status: implemented
date: 2026-09-09
scope: services
---

## Decision

`get_forward_rule_roi` is the one place that answers "how many times has this
rule fired". A rule matching **only** system event types is counted from its
`forward_outboxes` rows; every other rule from its `decision_trace` rows. The
answer carries `hit_count_source` (`decision_trace` | `system_event_delivery`)
so the badge can name the right noun — "matched" is about alerts and would be a
lie about an incident card. Both the dashboard and the MCP `get_forward_rule_roi`
tool read it, and every rule gets an entry, zeros included.

`SYSTEM_EVENT_TYPES` and `matches_only_system_events` move to
`services/forwarding/types.py`; the noise centre's private copy (see
[[2026-09-02-a-noisy-rule-can-be-batched-not-just-capped]]) now imports them.

The action centre's delivery-backlog item additionally requires
`next_attempt_at <= now`.

## Why

Two paths reach a target and only one keeps a decision trace. An alert goes
through decisioning, which writes a trace naming the rules it matched. An
internal system event goes through `resolve_notification_target` straight to the
outbox and writes no trace at all. A rule matching only system events therefore
counted 0 traces forever.

Measured on production 2026-09-08: rule 29 (`示例事件通知规则`,
`match_event_type = incident_created,incident_resolved`) had **44 sent outbox
rows and zero failures** while the ROI panel showed 「未命中任何告警」. Two
badges on one row, from two ledgers, contradicting each other — and the noise
centre had already learned this lesson for the same rules a week earlier, in its
own private copy of the event-type set. One copy, two panels.

Exempting such rules from the badge was the cheaper option and was rejected: the
operator's question is "is this rule doing anything", and 44 is a better answer
than a blank.

The backlog item is a second instance of the same mistake — a number read from
the wrong premise. "Pending or retrying deliveries older than five minutes"
describes a digest row exactly, and a digest row is *held on purpose* until its
window closes. Measured on production 2026-09-09 at 02:19Z, all three pending
rows were digest rows for rule `所有告警通知` with `next_attempt_at =
digest_window_end = 03:00`, `attempts = 0`, no error; the day's sent rows show
the same signature, batches created across an hour and all sent at `:00`, every
one on its first attempt. The window is hourly, so rows accumulate inside it all
hour and the warning was effectively always on. The GitHub issue guessed a
transient during the 2026-09-08 webhook swap; the data says otherwise.

## Consequences

A system-event rule's count is deliveries, so it moves when a delivery is
*queued*, not when it succeeds — deliberately, matching what a "forwarded" trace
means. The delivery-health badge beside it is what answers whether the delivery
landed, and the two are now computed from the same rows, so they can no longer
disagree.

The ROI mapping now includes zero-count rules, which is what the MCP tool's
description already promised ("a rule idle for over 90 days reports 0") and did
not do. A consumer that treated key-presence as "has hits" would need to read
the count instead.

Watch for a rule that starts system-only and later gains an alert event type:
its count silently switches ledgers and drops to whatever its traces say, which
will read as a cliff. The noun on the badge changes with it, which is the only
warning an operator gets.
