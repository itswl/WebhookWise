# k6 Load Checks

Run with the local observability stack:

```bash
docker compose -p webhookwise-observability --env-file .env -f deploy/compose/docker-compose.observability.yml --profile load run --rm k6
```

The k6 container posts signed synthetic webhook payloads to `webhook-service:8000` and sends k6 metrics to Prometheus remote write. In Grafana or Prometheus, look for metrics prefixed with `k6_`.

Useful overrides:

```bash
K6_BASE_URL=http://webhook-service:8000 \
K6_RUN_ID=local-001 \
WEBHOOK_SECRET=please-change-webhook-secret \
docker compose -p webhookwise-observability --env-file .env -f deploy/compose/docker-compose.observability.yml --profile load run --rm k6
```

`WEBHOOK_SECRET` must equal the app's `WEBHOOK_SECRET` (compose defaults both to
`please-change-webhook-secret`); a mismatch fails HMAC signature auth with 401 on
every request.

The script signs the body only and sends no `X-Webhook-Timestamp`, so it is
incompatible with `WEBHOOK_REPLAY_PROTECTION_ENABLED=true` (every request would 401).
Run it against a target with replay protection disabled (the default).
