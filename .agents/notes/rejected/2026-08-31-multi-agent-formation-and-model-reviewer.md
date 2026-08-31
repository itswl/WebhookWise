---
title: Multi-agent formation (coordinator/specialists) and a model reviewer gate
status: rejected
date: 2026-08-31
scope: whole
---

## Decision

No coordinator/specialist agent formation inside WebhookWise, and no
model-as-reviewer in front of remediation. The agent surface stays: hookprobe
as the single investigator, the read-only MCP server, inert proposals, and a
person as the only approver.

## Why

Ongrid's formation (coordinator, domain specialists, reviewer agent) is built
for fleets and teams. Here the fleet is one estate and the team is one person:
a second model in the approval path adds a failure mode and an illusion of
review without adding judgement, and specialist agents would each need the
tooling, guardrails and evals that currently exist once, for one investigator.
The verification loop just shipped closes the actual gap (nobody re-checked
outcomes) without adding a single model to the trust chain.

## Consequences

Investigation depth scales by giving hookprobe better tools and skills, not by
adding peers. If proposal volume ever exceeds what one person reviews, the
pressure point is triage UX and dedup of proposals — revisit THEN, with the
proposal ledger as evidence.
