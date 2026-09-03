---
title: Shadow-first promotion follows a named ladder, not a feeling
status: implemented
date: 2026-08-31
scope: whole
---

## Decision

Every default-off mechanism climbs the same ladder, and each rung names its
evidence: **off → shadow** (flip the mode; ledgers accumulate, behavior
identical) → **sampled** (a person reads the shadow ledger against reality on
a stated cadence) → **enforce** (only when the ledger's disagreement rate says
the mechanism is right, and only with the readback/audit trail that catches it
being wrong later). Applied today: `DEDUP_FINGERPRINT_MODE=shadow` and
`AI_COST_BUDGET_MODE=shadow` in production; the AI PR reviewer starts at the
same rung. Tier vocabulary for autonomy follows the industry frame (advisory →
deterministic guard → gate-enforced; observe → read-only diagnose → act via
pre-approved routes): hookprobe investigates read-only, remediation acts only
through the person-approved Action Center path.

## Why

The absorb landed five mechanisms default-off with "shadow-first activation
pending" and no definition of what ends the pending. Anthropic's AI-native
SDLC playbook and SDLC-security posts (claude.com/blog/the-ai-native-sdlc-playbook,
claude.com/blog/how-anthropic-secures-its-ai-native-software-development-lifecycle)
describe the same progression run at scale — shadow mode for new reviewers,
sampled approvals, response tiers by deviation — which makes the ladder a
citable convention instead of a house habit. Naming the rungs matters because
the failure mode of shadow-first is not bad promotion, it is NO promotion:
shadow ledgers nobody reads rot into permanent default-off, which is the demo
state this repository's own "what consumes it" rule forbids.

## Related

The switch these rungs are climbed on is
`2026-08-31-risky-automation-shares-one-mode-ladder.md` — `feature_modes.py`,
its three positions, and the fail-to-`off` on an unknown value.

## Consequences

Each shadow mode now owes a review date, or it is admitting nobody will look:
dedup fingerprint and budget mode get their first ledger read after two weeks
of production traffic (mid-September 2026). Enforce decisions are per-mechanism
— a ladder is not a conveyor belt, and "stays in shadow forever" is a valid
verdict when the ledger says the legacy behavior wins. The pseudonymizer stays
off-prod until trialed elsewhere: its known trade (correction-prior/KB recall
degrades on masked terms) is a quality regression no shadow ledger here would
surface.
