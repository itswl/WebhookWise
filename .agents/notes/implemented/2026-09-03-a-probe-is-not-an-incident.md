---
title: A probe is not an incident, and not a noise problem either
status: implemented
date: 2026-09-03
scope: services
---

## Decision

`SYNTHETIC_SOURCES` (runtime-policy, comma-separated source names, matched
case-insensitively against the event's source, default empty) names the sources
that exist to exercise the pipeline rather than to report on it — a
credential-rotation probe, a synthetic canary.

A listed source keeps the whole pipeline: its events are stored, deduplicated,
judged, capped, traced and forwarded exactly like any other. **A probe that
does not exercise delivery proves nothing**, so the one thing it must never be
excluded from is the delivery path.

What it is excluded from is every place where its traffic would be read as
operational signal:

- **incident grouping** — it never opens an incident and never joins one;
- **the quality centre** — no source diagnostics row, no findings, no score;
- **the noise centre** — out of the metrics window, out of the sources and
  noisy-rules tables, and out of every suggestion built from them.

The setting lives in `NoiseConfig`; the reading helpers are
`synthetic_sources()` / `is_synthetic_source()` in `services/webhooks/policies.py`,
so all three call sites answer the question the same way.

## Why

Measured on the production deployment, 2026-09-02/03: a synthetic source named
`rotation-probe` created a real incident, which sat open in the work queue
waiting for a person who had nothing to do about it. The probe was working
correctly; that was the point of it.

The same traffic distorts both analytics pages, and in opposite directions. In
the noise centre a probe is a perfect noise generator — a fixed cadence, a
fixed payload, a high repeat rate — so it climbs the noisiest-rules table and
attracts suggestions to silence the one alert that must never be silenced. In
the quality centre a probe usually has a hand-made payload with no service
label and no upstream severity, so it books `missing_service` and
`missing_severity` findings against a "source" that has no upstream to fix them
in.

The exclusion is a runtime setting rather than a naming convention (a
`probe-` prefix, say) because the names already exist, in Grafana and in a
rotation script, and renaming a source rewrites its history: `WebhookEvent.source`
is what every stored alert carries.

### Rejected: drop probe events at ingest

The cheapest fix, and it defeats the purpose. The probe exists to prove that
ingest, judgement and delivery still work after a credential rotation; a probe
that is dropped at the door tests the door only. Everything downstream of ingest
has to run, and be visible in the trace, for the probe to be worth firing.

### Rejected: a `synthetic` boolean on `source_connections`

Structurally tidier, and it was turned down for reach: not every probe arrives
through a managed connection, and the ones that do not are exactly the
hand-rolled `curl` in a rotation script. Matching on the source name covers
both, at the cost of a name that has to be kept in step with the sender.

### Why the noise centre excludes them from the summary too, not just the tables

The metrics window filter is applied to the event and trace windows once, so
the totals, the per-source table and the per-rule table all count the same
events. Excluding a probe from the tables while leaving it in the headline
"alerts observed" would leave a page whose rows do not add up to its own
summary, which is worse than either answer alone.

## Consequences

- Empty by default: nothing is synthetic until an operator says so, and an
  upgrade changes no number anywhere.
- A name that no longer matches any source excludes nothing, silently — the
  usual failure of a name-matched list. `_cast_csv_names` refuses the shapes
  that are obviously wrong (empty entries, a stray comma) on write, which is as
  far as validation can go without asking what sources exist.
- A probe's alerts stay visible in the alert list, the timeline, the decision
  trace and the cost view. Only the two analytics pages and incident grouping
  skip them, so "did the probe deliver" is still answerable in one place.
- If a probe ever starts reporting something real, the excluded pages will not
  say so. That is the intended trade, and the reason the setting names sources
  one at a time instead of matching a pattern.
