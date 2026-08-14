---
title: Do not absorb hookrelay and hookjudge into WebhookWise
status: rejected
date: 2026-08-15
scope: whole
---

## Decision

WebhookWise is the mainline, and the hookstack services feed into it — as
mechanisms, not as code. hookrelay and hookjudge are not merged. hookprobe stays
a separate deployable that WebhookWise calls over the gateway.

## Why

The obvious reading of "consolidate on one product" is to pull the three small
services in. On inspection that is a regression in two of the three cases and a
risk in the third.

`services/forwarding/` already does what hookrelay does, with retry durability,
an outbox and delivery policies behind it. `services/analysis/` already does what
hookjudge does, with an alert-quality centre, cross-source correlation and a
cache. Merging would replace the stronger implementation with the smaller one, or
leave two of each.

hookprobe is the one that is genuinely not duplicated — and it is the one that
most needs its own blast radius: the Claude Agent SDK, a mutable volume of
skills and memory, `kubectl` in the image, and an agent loop that runs arbitrary
read-only commands. Inside the API process those become this service's problems.
The existing relationship — a gateway URL and a token — is the boundary.

What the small services are for is being minimal and independently deployable.
That is not a capability WebhookWise can absorb; it is the opposite of what
WebhookWise is.

## Consequences

- The port is a list of mechanisms, and the first four are done: the content
  severity floor, prompt provenance, the budget brake, and the progress stream —
  each of which arrived here because a small service proved it cheaply first.
- Two repositories stay. The cost is real: a fix like the content severity floor
  had to be made twice, and the second time only because someone went looking.
- The flow is already two-way — the design tokens and static contract tests went
  from here to hookstack — so treat hookstack as the cheap lab rather than as a
  product line to fold in.
