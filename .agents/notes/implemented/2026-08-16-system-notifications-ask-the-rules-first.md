---
title: System notifications ask the forward rules before falling back to configuration
status: implemented
date: 2026-08-16
scope: services
---

## Decision

Five system events — `incident_created`, `sla_breached`, `report_daily`,
`report_weekly`, `report_monthly`, `ai_cost_budget` — resolve their destination
by matching a forward rule on `event_type`. When no rule claims the event, the
existing configuration cascade decides exactly as before.

The environment variables stay. Removing them is a second decision, and it needs
the rules to exist first.

## Why

An alert's destination has always been a rule: a table, a priority order, a
match vocabulary, a test button, a delivery record. A system notification's
destination was seven `*_FEISHU_WEBHOOK` variables chained by `or`:

```
incident created  <- DEEP_ANALYSIS_... or WEEKLY_REPORT_...
SLA breached      <- SLA_BREACH_... or DEEP_ANALYSIS_... or WEEKLY_REPORT_...
daily report      <- DAILY_REPORT_... or WEEKLY_REPORT_... or DEEP_ANALYSIS_...
AI cost budget    <- AI_COST_BUDGET_... or DAILY_REPORT_... or ...
```

That cascade is what broke. Incident notifications had never been given an
address, so they fell through to `DEEP_ANALYSIS_FEISHU_WEBHOOK` — whose bot
token had been revoked. Twenty-six of them went nowhere over six days and
nothing said so, because a URL in a file has no delivery record to look at.
Found by reading `forward_outboxes` a day later, not by any alarm.

The events already travelled with an `event_type`, and forward rules already
matched on it: the "失败通知" rule is written against
`ai_error,ai_degraded,outbox_exhausted`. Only the last step was missing.

## Consequences

- A rule-routed notification records `forward_rule_id` and the rule's name, so
  a dead address shows up as a failing rule in the dashboard instead of dying
  inside a config value.
- Fails open. A rules lookup that raises falls back to configuration: losing a
  notification to a cache problem is the failure this area is trying to end.
- A rule pointing at a deep-analysis gateway or a raw webhook is ignored for
  this purpose. Those are about alerts.
- Nothing moves on upgrade. Until an operator writes a rule for one of these
  event types, every card goes exactly where it goes today — which is also why
  this alone does not fix the revoked token.
