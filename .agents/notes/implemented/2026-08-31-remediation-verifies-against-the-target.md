---
title: An executed remediation is verified against the target, not the API reply
status: implemented
date: 2026-08-31
scope: services
---

## Decision

Every executed Action Center command (approved proposal or dashboard button)
arms a readback task `REMEDIATION_VERIFY_DELAY_SECONDS` later that reads the
SAME target — outbox status, replayed events' processing_status, incident
summary_status, rule enabled state, workflow status — and records `verified` /
`unrecovered` / `unverifiable` with what it saw. The verdict lands on the
proposal row, in the audit trail always, and unrecovered ones surface as a
critical Action Center card.

## Why

Absorbed from Flawless/CISRE's stance that API success, model claims, or a
previous health check do not equal fixed infrastructure. `changed=True` meant
an API call worked; whether the replayed dead letters actually completed was
the operator's homework, and homework nobody is assigned does not get done. The
proposal row already separated "allowed" from "worked"; "held" is the third
question and needed its own column, because re-approving a fix that never fixes
is how a broken action becomes a ritual.

## Consequences

Scheduling is best-effort: the execution a person just clicked must not fail
because the readback could not be armed, so a proposal can sit at `scheduled`
forever if the delayed-task plane is down — visible, but only to someone who
looks. Verdicts are point-in-time: a target that recovers after the readback
stays `unrecovered` until a human re-runs or ignores it, which is the
conservative direction to be wrong in.
