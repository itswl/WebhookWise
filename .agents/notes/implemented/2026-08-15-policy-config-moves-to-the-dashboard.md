---
title: Every policy value an operator tunes moves onto the runtime plane
status: implemented
date: 2026-08-15
scope: core
---

## Decision

Forty more keys join the runtime settings plane, in six domains: rule keywords,
ingest and dedup, delivery and retry, retention, deep analysis, and the rest of
the AI knobs. Credentials, endpoints and anything read once at startup stay in
the environment.

A key is only registered together with its reader, and a test enforces that.

## Why

219 configuration keys, 32 of them tunable from the dashboard. Everything else
needed an SSH, a file edit and a restart — including the keyword sets that
decide an alert's severity before any model sees it, which is where the payment
downgrade lived unseen for weeks.

The criterion is not "is it policy" but **is it read per use**. Every key here
resolves through a `*Policy.from_config()` choke point, so an override applies
on the next alert. A key read once into a module-level variable cannot be made
live by registering it — it would show up in the UI, accept an edit, store it,
and change nothing.

Two keys were dropped for exactly that reason:

- **`CIRCUIT_BREAKER_*` (8 keys).** The most tempting group — thresholds an
  operator reaches for mid-incident — but `llm_cb = LazyCircuitBreaker(...)` is
  built once at import. Making them live means teaching the breaker to rebuild,
  which is a feature, not a registration.
- **`AI_INSTRUCTOR_MODE`.** A mode name whose vocabulary lives in the instructor
  library, not here. With no cast that can validate it, a wrong value would fall
  back silently — the same failure the plane exists to prevent.

## Consequences

- `test_every_registered_key_is_actually_read_through_the_plane` fails the build
  if a key is registered without a reader. Written after the guard itself missed
  two keys because its pattern was `[A-Z_]+` and `AI_COST_PER_1K_INPUT_TOKENS`
  contains a digit.
- The credential exclusion is now a named list rather than a prefix rule.
  `OPENAI_TEMPERATURE` is policy and belongs on the plane; a prefix rule over
  `OPENAI_` would have had to exclude it or protect nothing.
- Prompts stay out. They already have an editor, a reload endpoint and a
  provenance fingerprint; a second editing path for the same text is the
  dual-config-plane debt this repository already paid down once.
