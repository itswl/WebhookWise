# Inbound source onboarding

WebhookWise can provision a separate, revocable credential for each inbound
alert source. This is the preferred setup for new Grafana, Alertmanager, Uptime
Kuma, and generic JSON connections because one source can be rotated or
revoked without changing every sender that uses the global webhook secret.

The dashboard **Inbound setup** wizard guides an operator through:

1. choosing and naming a source;
2. copying its URL and one-time credential;
3. configuring the sender;
4. validating a sample payload in the non-persistent sandbox;
5. sending and observing the first real event;
6. testing the forwarding destination.

The existing `/v1/webhook` and `/v1/webhook/{source}` endpoints remain
supported. Managed sources use `/v1/source-webhooks/{public_id}`.

## Authentication model

Management endpoints require the normal read API key. Create, update, rotate,
and revoke operations additionally require `ADMIN_WRITE_KEY`.

Each managed source receives a high-entropy token beginning with `whsrc_`.
WebhookWise stores only its SHA-256 digest and a short display hint. The
plaintext token is returned only by create and rotate responses; it cannot be
read back later.

The dashboard keeps that one-time token in page memory only. It is cleared
after the first-event test succeeds, when the setup panel is closed, or when a
different source is selected. Source tokens are never written to local storage,
session storage, URLs, or exported configuration.

Send the token as:

```http
Authorization: Bearer whsrc_<credential>
```

`X-Source-Token` and `Token` are accepted for senders that cannot configure a
Bearer header. Source credentials grant access only to their own inbound URL;
they do not grant dashboard, management, or change-ingestion access.

If a sender cannot attach any supported authentication header, place an
authenticated relay in front of the managed ingress endpoint. Do not put the
source token in a query string.

## API flow

List the supported source presets:

```text
GET /v1/onboarding/source-types
```

Create a source:

```bash
curl -X POST https://webhookwise.example/v1/onboarding/sources \
  -H "Authorization: Bearer $ADMIN_WRITE_KEY" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Production Grafana",
    "source_type": "grafana",
    "actor": "alice"
  }'
```

Copy `data.setup.webhook_url` and
`data.setup.authorization.credentials` from that response. Then configure the
sender:

```bash
curl -X POST "$SOURCE_WEBHOOK_URL" \
  -H "Authorization: Bearer $SOURCE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"alertname":"checkout latency","severity":"warning"}'
```

For a Grafana webhook contact point, set Authorization scheme to `Bearer` and
credentials to the complete `whsrc_...` token.

The remaining management endpoints are:

```text
GET   /v1/onboarding/sources
GET   /v1/onboarding/sources/{id}
PATCH /v1/onboarding/sources/{id}
GET   /v1/onboarding/sources/{id}/status
POST  /v1/onboarding/sources/{id}/rotate
POST  /v1/onboarding/sources/{id}/revoke
```

Rotation invalidates the previous token immediately and returns a new token
once. Revocation is terminal for the current credential; rotate it to reconnect
the source. A source may also be temporarily disabled and re-enabled before it
is revoked.

`credential_state` reports `active`, `disabled`, or `revoked`.
`onboarding_status` reports whether a first event has ever been accepted; it is
historical connection evidence and is intentionally independent of the current
credential state.

## Connection evidence

The source status records:

- first and most recent accepted event time;
- latest request ID and accepted event count;
- authentication failure count and latest failure time;
- structural payload changes.

Payload structure tracking hashes a bounded JSON shape, never field values.
Changing alert values does not count as drift; adding, removing, or changing
field shapes does. This is a setup and compatibility signal, not a copy of the
incoming payload.

Every accepted managed event carries its internal `source_connection_id`
through queueing, persistence, incident grouping, recurrence review, response
views, and archival. Request IDs, duplicate detection, backpressure, and recent
noise context are namespaced by the managed connection. Two independent
Grafana connections therefore cannot suppress or merge each other even though
both retain `grafana` as their adapter source.

If a reverse proxy or WAF fronts WebhookWise, allow the managed ingress path and
the sender's User-Agent just as for the legacy webhook path:

```text
/v1/source-webhooks/*
```
