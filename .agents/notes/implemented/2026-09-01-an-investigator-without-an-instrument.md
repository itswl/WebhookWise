---
title: Alerts the investigator has no instrument for are excluded, not answered
status: implemented
date: 2026-09-01
scope: services
---

## Decision

Four `inbound_rules` rows with `action=skip_deep_analysis` now exclude the
alert families whose data lives in an external cloud monitoring account: the
mail-delivery group, the message-broker group, the cache server-side metrics,
and two managed-database CloudWatch metrics. No code changed; the verb and the
outbox gate already existed.

They match on the upstream `rule_group` label via `match_payload`, not on
`match_rule_name`. Two reasons, both discovered by testing rather than by
reading: `match_rule_name` is comma-separated, so a rule whose NAME contains a
comma can never be matched by it — a first attempt produced a row that
validated, saved, and matched nothing. And a group label covers rules added
upstream later, which a name list does not.

## Why

Measured over the 30 days to 2026-09-01: 156 investigations, 82 USD, and 61%
of that spend went to questions this deployment cannot reach — no cloud
credentials, no client binaries, the hostnames do not resolve. Every rated run
in that class was ruled useless. The share was growing, not shrinking: 6% of
runs two weeks before, 87% in the last full week.

The rejected alternative was to give the investigator a read-only token for the
external monitoring system, which would make the questions answerable. It was
rejected for now because it is a credential into a system this project does not
own, and because the cheaper experiment comes first: stop paying for the
answers before deciding to buy the instrument. The rows say so in their comment
and are one DELETE from being reversed.

Also rejected: teaching the model to stop early. That was tried first, as a
section in the investigator's system prompt telling it to name the missing
instrument in its FIRST turn and end. It did not work — the run after it landed
still spent a full-price investigation. A rule that depends on the model
noticing it applies is weaker than one the pipeline enforces before the agent
is ever started.

## Consequences

- Projected on the last 14 days, 68 of 90 investigations would not have run.
  What remains is dominated by a datasource-health condition, which is a
  configuration question and the class the investigator answers well.
- One upstream rule is grouped under the broker label but is actually an
  on-call drill, so it is now excluded too. Harmless — a drill needs no
  investigation — but it is an upstream labelling artefact, not intent.
- `FORWARD_OUTBOX_RECORDS_TOTAL{target_type="deep_analysis",
  result="skipped_ai_excluded"}` is the counter that proves the rows are live.
  If it stays at zero while the spend continues, the rules are not matching.
- The comma limitation in `match_rule_name` is real and unfixed: no rule whose
  name contains a comma can be targeted by name. Worth an escape or quoting
  rule if it bites again.
