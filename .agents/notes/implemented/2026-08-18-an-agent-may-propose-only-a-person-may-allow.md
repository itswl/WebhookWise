---
title: An agent may propose an action; only a person may allow it
status: implemented
date: 2026-08-18
scope: services
---

## Decision

`remediation_proposals` records an inert Action Center command: the action, its
arguments, who asked, why, and an expiry. Nothing runs on creation. Approving one
(`POST /v1/action-center/proposals/{id}/approve`, admin-write) calls
`run_remediation` — the same function the dashboard button calls.

MCP gains its first write tool, `propose_remediation`, plus
`list_remediation_proposals`. The write cannot execute anything.

Statuses distinguish permission from outcome: `approved` (allowed and ran),
`failed` (allowed, execution raised), `rejected`, `expired`. The approve endpoint
returns 502 for `failed`.

Bounded by: validation through the executor's own `RemediationRequest`, a required
reason, one pending proposal per action+resource (partial unique index), a pending
queue cap of 50, an expiry enforced on read and on decision, and idempotency by
status.

## Why

The Action Center is a small, audited set of commands behind a human click.
An agent reading the same dead letters and the same stuck outbox can reach the
same conclusion and has no way to say so, which leaves two bad options: a human
retypes the agent's paragraph as a click, or the agent gets execute rights.

A proposal is the third option, and its whole value is a property that can be
stated without hedging: **the set of things that can happen to this deployment is
unchanged.** Approval routes through the existing executor rather than a second
one, so there is no path that a proposer can reach and an operator cannot audit.

Validating through `RemediationRequest` rather than re-checking by hand is what
keeps that true over time. If the proposable set were a second hand-written list,
it would drift from the executable set, and the drift would surface as an
approval that failed — the worst place to learn it.

Expiry is enforced on read and on decision rather than by a sweeper because the
alternative fails in the wrong direction: with a background job, a stopped
scheduler leaves stale proposals looking approvable. Here, nothing running means
nothing approvable.

`failed` is a separate status because an approval that did not execute is not a
refusal. Collapsing them would make the audit trail lie about what a person
decided, and would leave nobody able to tell "we decided against this" from "we
decided for it and it broke".

## Consequences

- **MCP is no longer strictly read-only, and its docs had to stop saying so.**
  `api/mcp/agent_guide.py` and `docs/reference/mcp.md` both promised "you can
  change nothing"; both now describe the write and its inertness. A stale promise
  in an agent-facing guide is worse than no promise.
- **The MCP transport authenticates with the read API key.** So proposing needs
  only read access: a read-key holder can create rows. Accepted, because a
  proposal cannot execute and the writes are bounded by the per-action uniqueness
  rule, the queue cap, the reason requirement and the expiry. The boundary that
  carries the security weight is approval, which needs admin-write.
- **No Feishu approval button.** The card path is HMAC-signed and idempotent but
  incident-shaped throughout — `verify_incident_action_value` refuses any
  `resource_type` but `incident`, and `process_incident_card_action` loads an
  `Incident`. Widening that grammar to a second resource type is a
  security-sensitive change and belongs in its own reviewable diff. Approval goes
  through the API until then, which is the same loop with a worse doorway.
- **No dashboard view.** The queue is API- and MCP-readable only. A dashboard
  panel is the natural home and brings the design-language contracts with it.
- **The reason field is agent-written prose stored verbatim** (capped at 2000
  chars) and rendered nowhere yet. Whatever renders it must escape it: it is
  attacker-influenceable text if a proposal is ever created from alert content.
- `proposed_by` is self-reported and unauthenticated — it identifies, it does not
  attest. The audit row records it as the actor, which is honest only because the
  proposal itself is powerless.
