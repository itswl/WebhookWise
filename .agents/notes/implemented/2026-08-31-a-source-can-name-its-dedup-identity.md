---
title: A noisy source can name the fields that ARE its dedup identity
status: implemented
date: 2026-08-31
scope: services
---

## Decision

`DEDUP_FINGERPRINT_FIELDS` maps a source to the dot-paths that define its alert
identity; when set (and `DEDUP_FINGERPRINT_MODE` says so), the dedup_key is
derived from those fields instead of the adapter identity. `alert_hash` never
moves. Shadow mode computes both keys and counts disagreement
(`dedup.fingerprint/diverged`) without changing behaviour; a payload the
configured paths do not match falls back to the built-in key in every mode.

## Why

Absorbed from Keep's per-provider `FINGERPRINT_FIELDS`. The built-in adapter
identity is right for well-known senders and wrong for the long tail: a source
that embeds timestamps or sequence numbers where the adapter looks fragments
every thread, and the only prior fix was code — a new adapter for every noisy
webhook. Naming the identity fields is operator configuration, reviewable on
the dashboard, and the shadow ledger proves a config right before it changes
anything.

## Consequences

Fallback-on-no-match means a half-matching config fragments rather than
collapsing everything into one bucket — the safe failure, but also a silent
one; the `unextractable` signal is the only tell. Enforcing a new fingerprint
mid-window re-threads new arrivals under new keys, so one alert family can
briefly appear as two threads across the switch.
