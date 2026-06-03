# k6 Load Checks

Run with the local observability stack:

```bash
docker compose -p webhookwise --env-file .env -f deploy/compose/docker-compose.infra.yml -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.observability.yml --profile load run --rm k6
```

The k6 container posts signed synthetic webhook payloads to `webhook-service:8000` and sends k6 metrics to Prometheus remote write. In Grafana or Prometheus, look for metrics prefixed with `k6_`.

Useful overrides:

```bash
K6_BASE_URL=http://webhook-service:8000 \
K6_RUN_ID=local-001 \
docker compose -p webhookwise --env-file .env -f deploy/compose/docker-compose.infra.yml -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.observability.yml --profile load run --rm k6
```
