---
title: An analysis records the prompt behind it, and the read path says if it still applies
status: implemented
date: 2026-08-15
scope: services
---

## Decision

Analyses carry `_prompt_kind` and `_prompt_version`, stamped before the result is
cached. `GET /api/v1/admin/prompt/versions` reports what every prompt says now,
and the dashboard prints the comparison — "still current" / "edited since" — not
the hash.

## Why

`get_prompt_version()` already existed: `ai_cache` keys on it so that editing a
prompt invalidates stale results. The value was computed and discarded, so
nothing could answer the question asked whenever a report reads wrong — which
instructions produced this?

WebhookWise is unusually exposed to that. Three prompt kinds, each overridable by
env, each reloadable at runtime through the admin API: the text behind an
analysis can change with no deploy and no trace.

hookprobe learned this the expensive way. A stale line in a memory file made
every report come back in the wrong language while the request looked identical,
and nothing in the result explained it.

A fingerprint alone is not evidence: it is the same hex on every analysis until
somebody edits a prompt, which reads as noise. Only the comparison is
informative, and only the read path can make it — a record cannot hold both
sides.

## Consequences

- Stamped **before** the cache write, unlike `_usage`, which is stamped after. A
  cached analysis is still the output of the prompt that produced it and should
  say so; what it must not claim is a second purchase.
- The versions endpoint loads each template before fingerprinting it. An unread
  template reports `unloaded`, which would make every analysis look edited.
- Only the user-analysis prompt is stamped so far. The deep-analysis and
  incident-summary paths produce their own artefacts and are not covered.
