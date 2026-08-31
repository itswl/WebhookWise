---
title: A webhook secret rotates with an overlap window, and the cutover is a metric
status: implemented
date: 2026-08-31
scope: core
---

## Decision

`WEBHOOK_SECRET_PREVIOUS` (empty by default) is a second accepted webhook
secret. Verification tries the active secret first, then the previous one, on
all three auth forms (bearer/token, body HMAC, timestamped HMAC). A request
that authenticates with the previous secret is counted under the
`webhook_auth` / `allowed_previous_secret` security check; startup logs a
warning while the overlap is active. Removing the variable ends the overlap.

## Why

The production `WEBHOOK_SECRET` sat in a public repository's first commit for
seven months and must be rotated. The senders are not ours to restart: the
dominant one is an estate-side Grafana (hundreds of events a week) whose
contact point only its operator can edit, plus an AWS Health hook configured
in a cloud console. A hard cutover would 401 estate alerts silently — the
exact failure class this service exists to prevent — so the rotation has to be
two-phase: accept both, move senders, watch the previous-secret counter reach
zero, then delete the old value. The counter is the consumer that makes the
overlap safe to end; without it "has everyone moved?" is a guess.

## Consequences

While the overlap is active the leaked value still authenticates, so the
window must be closed deliberately — the startup warning and the counter are
the nags, but nothing expires it automatically (an auto-expiry that silently
drops a still-active sender would recreate the failure the overlap prevents).
Two secrets double the HMAC comparisons on the signature path; at this
service's ingest volume that cost is unmeasurable.
