---
title: Provider/integration breadth as a goal (100+ connectors)
status: rejected
date: 2026-08-31
scope: whole
---

## Decision

WebhookWise does not pursue connector count. Inbound stays adapters +
per-source fingerprint/inbound rules; outbound stays the existing channel set
(Feishu family, DingTalk, WeCom, generic webhook, relay, deep-analysis
gateways).

## Why

Keep's 100+ providers are its moat and its maintenance bill; both belong to a
platform with many tenants. Here every integration has exactly one deployment
to serve, and an unused connector is not an asset but untested code with an
auth surface. The generic webhook channel plus the relay (which owns
presentation for downstreams) already cover "somewhere new" in both directions.

## Consequences

A genuinely new inbound source costs an adapter (or just fingerprint fields
now); a new outbound destination costs a channel or a relay renderer. That
per-case cost is accepted in exchange for every shipped integration being one
that production actually exercises.
