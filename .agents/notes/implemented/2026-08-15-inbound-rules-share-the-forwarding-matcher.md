---
title: Inbound rules reuse the forwarding matcher, with an action instead of a target
status: implemented
date: 2026-08-15
scope: services
---

## Decision

An `inbound_rules` table shaped like `forward_rules` — priority, the same seven
`match_*` columns, enabled — carrying an `action` rather than a target. Two
actions: `skip_ai` and `skip_deep_analysis`. `match_rule_name` is the one column
forwarding does not have.

Not `drop`, and not `mute`.

## Why

Forwarding was rule-driven from the start; the inbound side grew the other way,
one bespoke store per decision — `silences` for muting, `importance_overrides`
keyed by alert hash, and, most recently, an environment variable for "never
analyse these rules".

The matcher, though, was already shared: `_first_matching_silence` builds a
`ForwardRuleSnapshot` and runs it through `_rule_matches`, the same function
forwarding uses, negation and environment canonicalisation included. So an
inbound rule type costs a table and no matching logic — which is why this was
worth doing rather than growing a third bespoke store.

The gain is expressiveness. `AI_EXCLUDED_RULES` could only say "this exact rule
name"; a rule can say "from grafana, in prod, whose payload carries type=signal".

Two actions and no more:

- **`drop` is refused.** An alert that is never stored cannot be found
  afterwards, and "why did nobody see this" is the question this system exists
  to answer.
- **`mute` is refused.** Silences already do it, with expiry and a lift
  action; a second muting path with different semantics is how two systems
  disagree about whether someone was told.

## Consequences

- Actions accumulate rather than stopping at the first match — they are
  independent decisions, unlike a target list, which has an order.
- A `skip_ai` rule cannot filter on importance, and the write path says so
  instead of saving it: importance is decided after that rule runs, so such a
  rule would look configured and never match. The same silent shape as a
  mistyped exclusion name, refused at the door this time.
- A rule with no criteria is refused too. It would match everything, which
  quietly turns the AI layer off.
- `AI_EXCLUDED_RULES` still works and is still read. Two ways to say one thing
  is a real cost, accepted here because the setting shipped days ago and is
  live in production; the rules table is the documented one.
- Fails open: if the rules cache cannot load, no actions match and analysis
  proceeds. Losing analysis because a cache is unhappy is the worse failure.
