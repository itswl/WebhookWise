---
title: Outbound HTTP clients are owned by core/http_client, and the relay shares the internal-hop client
status: implemented
date: 2026-08-17
scope: core
---

## Decision

`_FeishuRelayChannel` stopped constructing a throwaway `httpx.AsyncClient`
per delivery and now posts through `get_deep_analysis_client()` — the shared
internal-hop client (pooled, `trust_env=False`, `follow_redirects=False`,
trace headers, deliberately not DNS-hardened). That client is now closed on
shutdown (`aclose_deep_analysis_client` from `stop_runtime_services`), and a
contract test bans `httpx.AsyncClient(` construction anywhere outside
`core/http_client.py`.

## Why

The ad-hoc client was the only outbound path honouring `HTTP(S)_PROXY` from
the environment (httpx defaults `trust_env=True`), re-handshook TCP+TLS per
alert, and skipped trace propagation. The relay and the deep-analysis gateway
have the same trust shape — an operator-configured private hop — so they
share the client built for exactly that shape rather than growing a second
one.

Two things were considered and NOT done:

- **No delivery-time `validate_outbound_url` for the relay.** The door is
  private infrastructure by design; the public-IP guard exists for
  payload-derived targets and would reject every legitimate relay URL. The
  admin-write gate plus HMAC signing remain the boundary (same rationale as
  the save-time skip in `target_validation.py`).
- **No circuit breaker on the relay hop.** The outbox already provides
  backoff, retry caps, and dead-lettering for this channel; a breaker would
  add a second failure state machine on a single-target hop for little gain.
  Revisit if relay outages ever produce retry storms the outbox pacing does
  not absorb.

## Consequences

- The relay shares pool limits with deep-analysis traffic (100 connections,
  20 keep-alive). Fine at current volumes; split clients if one hop ever
  starves the other.
- The relay request now carries trace headers; the door may log them.
- Tests stub `core.http_client.get_deep_analysis_client` instead of
  monkeypatching `httpx.AsyncClient`.
