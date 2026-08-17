---
name: ww-noise-tuning
description: Audit WebhookWise alert noise — find zombie forward rules, dead silences, and repeat-alert clusters, and propose tuning actions. Use for "降噪/噪音治理", "哪些规则没用了", "告警太多了怎么收敛", or a periodic rule/silence hygiene review.
---

# WebhookWise noise tuning audit

Goal: a short, evidence-backed list of **what to prune, what to silence, and
what to fix upstream**, from the read-only `webhookwise` MCP server. Field
semantics: MCP resource `webhookwise://reference/agent-guide`.

## Procedure

1. `get_forward_rule_roi` — for every forward rule: matches vs deliveries vs
   failures over the window. Classify:
   - **zombie**: ~zero matches in 90d → candidate for deletion;
   - **firehose**: huge match count, low ack/interaction → candidate for
     tighter conditions or a silence;
   - **broken**: matches but deliveries persistently fail → delivery problem,
     not a noise problem — route it to `#/delivery`, don't hide it.
2. `get_silence_roi` + `list_active_silences` — silences that suppressed
   nothing in their whole life are clutter; silences suppressing thousands may
   be masking a real ongoing failure. Flag both, opposite reasons.
3. `get_decision_quality_stats` — dedup/suppression hit rates: is the existing
   stack already absorbing the noise, or is it leaking through to humans?
4. `get_alert_overview_stats(hours=168)` + `list_alert_decision_traces` —
   name the top repeat offenders (source + summary cluster). For the worst
   one or two, `get_ai_analysis` on a sample event to judge: real recurring
   incident vs noisy emitter.
5. `get_ai_cost_stats` — if AI spend matters, note which noisy source burns it
   and whether an inbound skip-AI rule (`#/inbound`) would pay for itself.

## Judgement rules

- Never recommend silencing something whose deliveries are *failing* — fix
  delivery first, or the silence hides an outage.
- Prefer the narrowest lever: tighten one rule > add a silence > drop a source.
- A recommendation without a number (matches, %, $) is an opinion — cut it.

## Output shape

Chinese report, three sections, each item = one line of evidence + one action
with its dashboard page:

1. **可以删** — zombie rules/silences (`#/rules`, `#/silences`).
2. **需要收紧** — firehose rules, repeat clusters (`#/rules`, `#/silences`,
   `#/inbound` for skip-AI, `#/noise` for the dedup view).
3. **别降噪，去修** — failing deliveries and suspicious mega-silences
   (`#/delivery`, upstream owners).

Read-only: propose, never claim to have changed anything.
