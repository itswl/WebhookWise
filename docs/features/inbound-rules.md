# Inbound rules: policy about a named alert rule

Forwarding has always been rule-driven — a table, a priority order, a match
vocabulary, an operator UI. The inbound side grew the opposite way: each decision
got its own storage, and the newest of them ("which alerts are never worth a
model call") was a comma-separated environment variable.

`inbound_rules` is that table for the way IN. Same match vocabulary as a forward
rule, because [the matcher is literally the same
function](../architecture/boundaries.md); what differs is the verb.

An operator thinks in alert rules — "stop analysing this one", "this one is never
high", "this one once an hour" — so `match_rule_name` is the addition, matched
against whatever the sender calls the rule (`RuleName`, `alert_name`,
`AlertName`, `alertname`, or `commonLabels.alertname`).

## The verbs

| Action | `action_value` | What it does | Route recorded |
| --- | --- | --- | --- |
| `skip_ai` | — | The paid model is never called for this rule; the deterministic rule pass answers instead | `rule_excluded` |
| `skip_deep_analysis` | — | The rule may reach the model, but never an investigation gateway | — |
| `cap_importance` | `high` \| `medium` \| `low` | A **ceiling** on the severity, applied after judgement | importance carries `_importance_cap` |
| `digest` | window in minutes, `5`–`1440` (empty = `60`) | Chat notifications for this rule are **batched** into one card per window; every alert is still stored, judged and traced | analysis carries `_digest` |

`rule_excluded` is deliberately distinct from `rule_routed`: one says "we decided
never to analyse this rule", the other "the cheap pass judged this occurrence not
worth a model". Reading the cost view, those are different decisions.

## Why `cap_importance` is a ceiling and never a floor

A cap is set once and then forgotten about, so the direction has to be the safe
one. If it could raise severity, an alert the judgement called `low` would start
paging at `medium` forever. An unrecognised severity is treated as `high` when
comparing, so a value this system does not know is never left above the ceiling.

It is applied in a wrapper around `analyze_webhook_with_ai`, not inside its
routes — that function answers through eight of them (cache, `rule_excluded`,
`rule_routed`, `ai`, budget-exhausted, three degradations) and "this rule is never
high" means it regardless of which one answered.

A capped analysis records what was replaced and by which rule:

```json
"_importance_cap": {"capped_to": "medium", "judged": "high", "rule": "cap: …"}
```

Same doctrine as `_importance_override`. A severity nobody can attribute is how
you end up arguing with a model that never said it.

## Digest: a noisy rule can be batched, not just capped

Measured on production on 2026-09-02: two business-threshold rules were **56% of
a week's volume — 187 of 331 alerts** — and each firing became its own chat
card, its own three-alert incident and its own paid summary, for reports that
called them business fluctuation. A cap changes what the card *says*; it does not
change how many cards there are. `digest` does.

A matching alert is processed exactly as before — stored, judged, capped, traced,
forwarded to every matching forward rule — but its **chat** deliveries wait for
the window to close and go out as one card:

```
📦 汇总通知 · 示例充值超限告警            ← header colour = highest importance in the group
🕐 2026-09-02 10:00 – 11:00 UTC+8
共 12 条，其中 2 条恢复
🔴 10:03 · 高 · 当前值 920.00 超过阈值 500 …
🟢 10:07 · 已恢复 · …
…另有 K 条                               ← after 15 lines
🔔 grafana ・ 📨 <forward rule name>
```

Every quoted value — rule name, summary — goes through
`services/notifications/markdown_safety.escape_lark_md`, so a summary containing
`<at id=all>` or `[x](http://…)` renders as text. There are no per-alert links:
the service has no public URL setting.

**Window alignment.** The window is floored from the alert's own timestamp,
aligned to UTC midnight: `60` gives clock hours, `1440` the UTC day, `90` the
sixteen slots after 00:00 UTC. Alignment is what lets two alerts filed by two
workers agree on the same group without talking. Each outbox row records its
group as `digest_key = "<forward_rule_id>:<target_type>:<window start>"` and its
due time as `digest_window_end`; `next_attempt_at` is the window end, so the row
is not claimable before then (migration 0039).

**Which targets are exempt.** Only surfaces a person reads are digested: a Feishu
bot URL, the Feishu custom app, a DingTalk robot, a WeCom bot. Deep-analysis
gateways, the relay and generic webhooks keep immediate, per-alert delivery — a
machine consumer wants every event as it comes. Periodic reminders are not
created for a digested rule: the digest already says "still firing" every window.

**Delivery.** When the window closes, whichever row of the group is claimed first
claims every due sibling in one conditional `UPDATE` (at most 200 per send) and
sends one message through its channel. On success every row is finalised exactly
as a single delivery would be — per-record bookkeeping, per-record metrics, so
delivery counts still count alerts. On failure the leader follows the normal
retry policy and the siblings are parked on the leader's next attempt so the
group coalesces again; if the leader exhausts, the siblings exhaust with it (one
exhausted notice per dead webhook, not one per alert).

**The decision trace.** The analysis records what happened and who decided:

```json
"_digest": {"window_minutes": 60, "rule": "digest: example deposit rule hourly"}
```

Same doctrine as `_importance_cap`: a card that arrived an hour late needs a
trace saying which rule chose that.

**DingTalk keyword security.** The digest title contains 汇总通知, not 告警通知. A
robot secured by the 告警通知 keyword rejects it visibly (the outbox retries,
then exhausts); add the keyword or use a signed robot.

**Where the suggestion comes from.** The noise centre proposes one of these for
any alert rule that fired at least `NOISE_DIGEST_MIN_ALERTS` times in the
analysis window with a repeat share of 40% or more, and it writes the rule
through the same `validate()` / `create_inbound_rule()` path this page
documents — `{"action": "digest", "action_value": "60", "match_rule_name": …}`,
named `digest 60m: <rule>`. It is not offered while an enabled digest rule
already matches that alert rule, applying it twice changes nothing, and undoing
it disables the row rather than deleting it, so the evidence in its `comment`
survives.

## Choosing a ceiling from evidence, not from feel

Do not guess. Every completed deep-analysis report carries the investigator's own
severity, which makes a labelled set that grows by itself:

```bash
python -m scripts.ops.severity_calibration              # the report
python -m scripts.ops.severity_calibration --json       # for a dashboard
```

It scores WebhookWise's severity per alert rule against the reports and proposes
conservative downgrades. It **proposes; a person applies** — a downgrade is a
decision about who gets woken up. Three guards stop it proposing something
unsafe:

- at least `--min-reports` investigations (default 3) — one report is an anecdote;
- never below the investigator's median;
- **never** for a rule the investigator called `high` more than a third of the
  time. A condition that is genuinely severe a third of the time has to keep
  waking someone; that noise is fixed by making the alert more specific, not by
  muting it.

It also prints, per rule, how many distinct `alert_hash` values the reports
covered. When that equals the report count, the alert carries something
per-occurrence in its identity (a user id, an amount) and an
`importance_overrides` entry — which keys on `alert_hash` — would generalise to
nothing. That check is what decides whether a rule-scoped cap is the only
possible fix.

## Writing one

Both headers: the `/v1` router is behind the read key and writes add admin-write
on top.

```bash
curl -fsS -X POST http://localhost:8000/v1/inbound-rules \
  -H "X-API-Key: $API_KEY" -H "X-Admin-Write-Key: $ADMIN_WRITE_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name":"cap: example rule -> medium","action":"cap_importance",
       "action_value":"medium","match_rule_name":"示例充值超限告警",
       "priority":100,"comment":"severity_calibration 2026-08-21: 25 reports, high:5 medium:12 low:8"}'
```

`GET /v1/inbound-rules` lists the rules and, alongside them, `actions` and
`actions_with_value` — so a form knows which verbs need a value without
hard-coding the vocabulary.

Put the evidence in `comment`. A rule whose reason is only in someone's memory is
the one nobody dares delete.

## What the write path refuses

- an unknown verb, or a verb and value that disagree (`skip_ai` with a value, or
  `cap_importance` without one, or `digest` with a window outside 5–1440
  minutes — an empty digest window is stored as `60`);
- a rule with no criteria at all — it would match every alert;
- a `skip_ai` rule filtering on `match_importance`. That action runs *before* the
  alert is judged, so the filter could never match: a rule that looks configured
  and does nothing.

## Precedence

`cap_importance` and `digest` respect `priority` and the first match wins, unlike
the action *set*, where every matching rule contributes and order cannot matter.
Both carry a value, so two matching rules would otherwise disagree and the winner
would depend on iteration order.

Rules are cached per worker with cross-worker Pub/Sub invalidation, so an edit
takes effect everywhere within seconds and no decision waits on the database —
this sits in front of a paid model call.
