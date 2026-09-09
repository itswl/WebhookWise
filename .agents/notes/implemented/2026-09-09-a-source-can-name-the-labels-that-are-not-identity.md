---
title: A source can name the labels that are NOT its dedup identity
status: implemented
date: 2026-09-09
scope: services
---

## Decision

`DEDUP_FINGERPRINT_EXCLUDE_LABELS` maps a source to the label names that are
**not** part of its alert identity. When set (and `DEDUP_FINGERPRINT_MODE` says
so), the dedup_key is derived from the alert's remaining labels
(`alerts.0.labels`, else `commonLabels`, else `labels`) instead of the upstream
fingerprint. `alert_hash` never moves. `DEDUP_FINGERPRINT_FIELDS` wins if a
source sets both — naming the identity outright is the more specific statement.
Excluding every label, or finding no label map, falls back to the built-in key.

The complement of [[2026-08-31-a-source-can-name-its-dedup-identity]], on the
same off/shadow/enforce ladder and the same `dedup.fingerprint` signal.

## Why

Grafana computes its `fingerprint` over the alert's **labels**, and
`_norm_grafana` adopts it as the dedup identity. An alert rule that embeds
volatile detail in a label therefore hands us a new identity on every firing.
Measured on production over 30 days, two rules in folder `demo-alarm` carried a
`payload` label holding the violation text (userid, amounts, timestamp):

| rule | events | distinct dedup keys | originals |
| --- | --- | --- | --- |
| 示例充值超限告警 | 689 | 360 | 360 |
| 示例提现超限告警 | 377 | 222 | 222 |

Keys equalled originals exactly: dedup threaded only the ~5-minute repeat of an
unchanged payload, then fragmented. For contrast, `DatasourceNoData` ran 184
events over 4 keys, and `[MQ] Unacked backlog` 26 over 1.

**The rejected fix is the one the issue proposed.** Keying grafana on the stable
fields via `DEDUP_FINGERPRINT_FIELDS` — `["alerts.0.labels.alertname",
"alerts.0.labels.grafana_folder"]` — is per-*source*, and the same 30 days show
what it would have cost:

- `CertificateExpiredAlertRule`: 2 keys = **two different certificates**
  (`domain_name` example-a.test / example-b.test, different `certificate_arn`).
  Collapsed to 1 — the second expiring cert swallowed as a duplicate.
- `DatasourceNoData`: 4 keys = four different `rulename`s (a JVM probe, a
  conversion check, two RDS metrics). Collapsed to 1 — three broken datasources
  hidden behind one alert.

An inclusion list must enumerate identity for a whole source, and a source's
alerts do not share one identity. Exclusion states only what is *not* identity
and leaves every other label doing its job, so the cert and datasource threads
stay separate while the volatile label stops fragmenting its own rule. One
config also covers both `超限告警` siblings, which have the identical shape.

**Also rejected: per-rule inclusion keys** (`{"grafana:alertname": [...]}`).
Narrower blast radius, but it has to be written again for every new offender and
each entry re-enumerates the stable fields, which is the collapse above waiting
to happen one rule at a time.

**Also rejected, and it was already staged on production:**
`DEDUP_FINGERPRINT_FIELDS={"grafana": ["groupKey"]}`, sitting in `shadow` when
this landed. It looks like the cheap fix, because a Grafana `groupKey` reads
`{}/{}:{alertname="...", grafana_folder="..."}` and so does exclude the volatile
`payload` label. But that IS alertname+folder, the collapse measured above, and
promoting it to `enforce` was the obvious next step for whoever staged it.

Counted over 30 days, per rule, distinct dedup keys today vs. under each config:

| rule | today | excl. `payload` | `groupKey` |
| --- | --- | --- | --- |
| 示例充值超限告警 | 359 | 1 | 1 |
| 示例提现超限告警 | 223 | 1 | 1 |
| `DatasourceNoData` | 4 | 4 | 2 |
| `CertificateExpiredAlertRule` | 2 | 2 | 1 |
| `[SES] Bounce rate > 10%` | 3 | 3 | 1 |
| a voice-escalation rule | 4 | 4 | 1 |

Nine rules lose threads under `groupKey` and none do under the exclusion. The
override was cleared rather than left in shadow: with `FIELDS` winning over
`EXCLUDE_LABELS` for the same source, leaving it would also have silenced the
fix.

Upstream remains the better cure and is not ours to apply: moving the detail
into **annotations** would fix the identity at the source, but these webhooks
arrive from an external Grafana — 325 events in 7 days from a single off-estate
address — not from the observability stack in this repository. Worth asking its
owner for; not a fix this repository can ship.

## Consequences

Identity now depends on the label set, so an upstream rule that *adds* a label
re-threads that alert family — the same mid-window re-threading the sibling note
describes, from a new direction. A label named `payload` on some other grafana
rule is excluded there too; exclusion is per source, not per rule, which is the
price of it being one line of config.

Emptying the label map falls back rather than collapsing, so the dangerous
failure (one bucket per source, every alert after the first swallowed) cannot
happen from a typo — but like the inclusion list, the safe failure is silent,
and `unextractable` remains the only tell.

Verified on production after promoting to `enforce`, by running the deployed
key generation over 400 real stored payloads with the live override loaded: the
two offending rules collapse 88 keys to 1 and 59 to 1, `CertificateExpiredAlertRule`
and `DatasourceNoData` keep 2 each, no rule gains a key, and `alert_hash` moved
on 0 of the 400.
