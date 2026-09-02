---
title: A noisy alert rule can be batched into a digest, not just capped
status: implemented
date: 2026-09-02
scope: services
---

## Decision

`inbound_rules` gains a fourth verb, `digest`. `action_value` is the window in
whole minutes (5–1440; empty is stored as 60). A matching alert is processed
exactly as before — stored, judged, capped, traced, matched against every
forward rule — but its CHAT deliveries (Feishu bot URL, Feishu custom app,
DingTalk robot, WeCom bot) wait for the window to close and go out as one card
per forward rule per window. Deep-analysis gateways, the relay and generic
webhooks are never digested.

The window is the alert's own timestamp floored to the window, aligned to UTC
midnight, so two workers file two alerts into the same group without talking.
The outbox row records the group (`digest_key`, indexed) and its due time
(`digest_window_end`; `next_attempt_at` is set to it) — migration 0039. When the
window closes, whichever row is claimed first claims every due sibling with one
conditional UPDATE (at most 200 per send) and sends one message through its
channel; success finalises every row as a single delivery would, failure
retries the leader and parks the siblings on the leader's next attempt.

The decision is recorded on the analysis as `_digest = {"window_minutes",
"rule"}`, mirroring `_importance_cap`. Periodic reminders are not created for a
digested rule.

## Why

Measured on the production deployment, 2026-09-02: **two business-threshold
alert rules were 56% of a week's volume — 187 of 331 alerts.** Each firing
became its own chat card, its own three-alert incident and its own paid summary,
and the deep-analysis reports called them business fluctuation, not incidents.
The operator's ask was one card per hour per rule, without losing the per-alert
record — the record is what the reports are built from.

`cap_importance` (2026-08-21) had already moved these rules off `high`. A cap
changes what the card SAYS; it does not change how many cards there are. The
missing verb was about cadence, and the per-alert-rule policy plane already
existed to carry it.

### Rejected: digest as a forward-rule property

The obvious shape — a `digest_minutes` column on `forward_rules` — does not
work here, and the reason is structural rather than a matter of taste: all
matching forward rules fire, and there is no exclusion. The estate routes
everything through a catch-all chat rule plus specific ones. A digest flag on
one forward rule would batch that rule's copy while the catch-all rule still
paged per alert, so the operator would see both the digest AND every card it
was meant to replace. The inbound side names the ALERT rule, so the decision
travels with the alert into every chat target it reaches.

### Rejected: silence the rule, or `skip_ai` it harder

A silence drops the notification entirely and expires; the operator wants to
keep seeing these, less often. `skip_ai` already applies to one of the two
rules and changed cost, not volume — 187 cards is 187 cards whether or not a
model read them.

### Why chat targets only

A machine consumer wants every event as it comes: the investigator needs the
alert it was asked about, the relay keeps its own ledger per event, a generic
webhook is somebody else's pipeline. Only the surfaces a person reads gain from
"twelve of these this hour". `is_chat_target` decides from the rule's target
before any row exists, so the exemption is not a delivery-time special case.

### Why the first row delivers, not a scheduled job

The outbox already has the two mechanisms a digest needs: a conditional-UPDATE
claim (so two kicks cannot both take a row) and a scheduled scan that re-kicks
any pending row whose `next_attempt_at` has passed. Setting `next_attempt_at`
to the window end makes every row of a group due at the same instant; the first
one claimed extends the same claim to its siblings. A separate "send digests"
job would have been a second delivery path with its own failure semantics. The
pipeline additionally schedules one delayed kick per group (for the row that
opened it), so delivery does not wait for the scan interval.

### Why the siblings exhaust with the leader

A digest group failed ONE send to ONE target. Returning the siblings to
`pending` after the leader exhausted would let each of them take a turn as
leader against the same dead webhook — N exhausted notices for one failure.
They are exhausted together, with the leader's error; the exhausted notice goes
out once.

## Consequences

Easy: silence the cadence of a noisy rule in one row, with the reasoning in its
`comment`; keep every alert, every trace and every investigation decision
exactly as they were; read "why did this card arrive an hour late" off the
analysis.

Hard / watch for:

- A digested `high` waits up to a full window. The rule matches on the CAPPED
  importance, so "digest the mediums" can be written; a rule that digests
  everything for a rule that is genuinely high a third of the time is the
  operator's call, and the calibration script's `high_share > 1/3` guard is the
  hint to look at first.
- `digest_key` is per forward rule per window, so an alert reaching two chat
  rules produces two digests — one per chat, which is what each chat expects.
- DingTalk robots secured by the 告警通知 keyword reject a card titled 汇总通知;
  the failure is visible (retries, then an exhausted notice) and the fix is a
  keyword or a signature on the robot.
- The window is UTC-aligned. Hourly windows read the same in UTC+8; a 1440
  window is the UTC day, which is 08:00–08:00 locally.
- `_digest` is a second underscore marker set at the converging layer after the
  cap. If a third appears there, the order needs writing down.
