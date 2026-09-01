---
title: The Feishu card grammar learns a second resource, with stricter guards
status: implemented
date: 2026-09-01
scope: services
---

## Decision

Remediation proposals can now be decided from their own Feishu card. A new
signed value grammar (`resource_type=remediation_proposal`, actions
`approve|reject`) rides the existing verified callback: same HMAC canon, same
idempotency receipts, same `decide_proposal` execution path the dashboard
uses, `actor=feishu:<open_id>`. Two guards are deliberately stricter than the
incident grammar: an empty `FEISHU_ALLOWED_OPERATOR_OPEN_IDS` REFUSES proposal
decisions (incident actions treat empty as allow-all), and the signed button
expiry is clamped to the proposal's own `expires_at`, never the days-long
card-action TTL. Outbound, `propose_remediation` queues one idempotent card
(`remediation_proposed`, key `remediation-proposal:{id}`) through the same
rule-else-fallback routing as incident cards; buttons render only on the app
channel.

## Why

[2026-08-18-an-agent-may-propose-only-a-person-may-allow](2026-08-18-an-agent-may-propose-only-a-person-may-allow.md)
deferred exactly this with a named condition: widening the incident-shaped
grammar "is a security-sensitive change and belongs in its own reviewable
diff". This is that diff — reviewed as a PR with the shadow reviewer on it,
not folded into something else. The person-approves boundary is unchanged;
what moved is the doorway: the operator already lives in Feishu, where the
alerts that justify a proposal arrive, and a decision that waits on someone
opening the dashboard is a decision made later than it needed to be.

The two extra guards exist because the incident grammar's permissiveness does
not transfer: acknowledging an incident with no allowlist configured is a
workflow write; approving a proposal EXECUTES a command. Empty-means-allow-all
was already a documented startup warning — for this one action class it is now
a refusal.

## Consequences

Two verifiers now share a canon but not an allowlist — deliberate duplication,
so neither resource's action set can widen by accident; anyone adding a third
resource type should copy the pattern, not generalize it. A deployment that
wants chat-side approval MUST enumerate its operators; that is the feature
asking for the allowlist the startup check always nagged about. The card's
buttons survive the decision (no card refresh yet) — a second press gets the
"already decided" toast, which is idempotent but not pretty. Webhook-only
channels get a button-less card by construction.
