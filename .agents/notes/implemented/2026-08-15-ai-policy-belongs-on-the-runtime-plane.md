---
title: AI spend policy moves onto the runtime settings plane
status: implemented
date: 2026-08-15
scope: services
---

## Decision

The five AI *policy* keys — `AI_EXCLUDED_RULES`, `AI_ROUTING_ENABLED`,
`AI_ROUTING_SKIP_IMPORTANCE`, `AI_COST_MONTHLY_BUDGET_USD`,
`AI_COST_BUDGET_ENFORCE` — are registered in the runtime settings plane and read
through `override_or`. Credentials and endpoints stay in env.

`GET /v1/admin/alert-rules` lists the rule names actually seen in recent
traffic, so an exclusion list is written from the traffic rather than from
memory.

## Why

`AI_EXCLUDED_RULES` shipped as an env-only key, which meant: invisible from the
dashboard, an SSH and a container restart to change, no record of who changed
it, and — because matching is exact — a typo that excludes nothing and says
nothing. The plane that fixes all of that already existed and held 35 keys
across 7 domains; AI was the only policy area still outside it, so this was an
omission rather than a design choice.

The typo problem is the one validation cannot reach. A cast can reject a stray
comma or an absurd length, and now does, but nothing in this process knows what
an operator's alert rules are called. Only the traffic knows, so the traffic is
what the endpoint reports: name, how many alerts, how many distinct verdicts,
last seen.

## Consequences

- Changing what is excluded is a dashboard edit that takes effect across
  processes in seconds. The env value stays as the floor, so a deployment that
  never touches the dashboard behaves exactly as before.
- `distinct_verdicts` in that listing is worth reading on its own: a rule that
  has produced more than one verdict is one where the model is adding something,
  and a poor candidate for exclusion.
- API keys and base URLs are deliberately NOT tunable at runtime. A live-editable
  credential is a liability, and a test pins that they stay out of the registry.
