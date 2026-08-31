---
title: A general workflow engine (Keep-style YAML automations)
status: rejected
date: 2026-08-31
scope: whole
---

## Decision

WebhookWise does not grow a declarative workflow engine ("GitHub Actions for
monitoring"), despite it being the headline feature of the most-starred
comparable (Keep).

## Why

Every routing and reaction need observed in production is already expressed by
narrower, safer planes: forward rules (matching + targets), inbound rules,
silences/maintenance windows, the outbox retry machinery, and the
approval-gated remediation path. A workflow engine would re-house those
behind a new DSL whose author and sole consumer is one operator — maintenance
surface without a consumer, and an arbitrary-action executor to secure besides.
The house rule stands: no concept without a consumer.

## Consequences

Multi-step reactions that span planes (e.g. "on X, silence Y then propose Z")
must be composed from the existing primitives or proposed as one narrow
feature. If a second operator with genuinely divergent workflows ever
materializes, this decision is the first to revisit.
