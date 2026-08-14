---
title: Alert content, not just its level, decides whether the model sees it
status: implemented
date: 2026-08-15
scope: services
---

## Decision

`analyze_with_rules` gains a second keyword class, `RULE_CONTENT_HIGH_KEYWORDS`,
matched against the alert's content — rule name, title, body — rather than its
level. A hit floors the verdict at `high`. Recoveries are exempt.

The policy field has **no default**. Every constructor must state its floor.

## Why

The rule pass decided importance from `level` and `RuleName` alone, and its
high-severity keywords were `error,failure,critical,alert,错误,失败,故障` — no
money, no account security. hookjudge's equivalent set has carried those since
the beginning, with a comment saying exactly what dropping them would do.

With `AI_ROUTING_ENABLED=true`, that verdict does not merely mislabel an alert:
it decides whether the model ever sees it. Fifteen days of production:

| state | route | verdict | count |
| --- | --- | --- | --- |
| FIRING | `ai` | high | 259 |
| FIRING | `rule_routed` | **low** | **15** |
| FIRING | `redis_reuse` | low | 3 |
| RESOLVED | `rule_routed` | low | 59 |

Every firing `low` came from the keyword route. Those alerts carried
`Level=info` with `status=firing` — a payment threshold breach labelled `info`
by the sender — and were filed low without anything reading what they said. The
model, which does read the body, called 259 of 259 payment alerts `high`.

Replayed against all 78 real `rule_routed` rows: 19 firing alerts move
`low -> high`, all 59 resolved ones stay `low`.

## Consequences

- Those alerts now cost money and now page someone. Roughly 19 per 15 days at
  ~$0.04 each; the point is that they were meant to page someone all along.
- Recoveries stay free. Once a condition is over, how bad it was is no longer the
  question — and turning 59 recovery notices into paid calls would have been a
  regression sold as a fix.
- The missing default is deliberate. This bug was one keyword class nobody
  noticed for weeks; a policy that can be constructed without stating its floor
  can lose it again the same way.
- Watch for over-escalation: any alert whose body merely mentions 支付 or
  security now reaches the model. That is the trade — the failure it replaces was
  silent, and this one is visible on the AI cost page.
