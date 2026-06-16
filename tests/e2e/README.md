# Docker E2E

This layer of tests verifies the core business path rather than individual functions:

```text
HTTP /v1/webhook/prometheus
  -> migrate job applies Alembic
  -> PostgreSQL quick receive
  -> Redis / TaskIQ
  -> Worker pipeline
  -> Scheduler process starts
  -> fake OpenAI structured AI analysis
  -> Redis analysis cache and noise reduction path enabled
  -> Feishu interactive card
  -> fake Feishu HTTP server
```

Run:

```bash
tests/e2e/run_webhook_to_feishu.sh
```

The script starts a one-off Docker Compose environment:

- `postgres`: a clean PostgreSQL 15
- `migrate`: a one-off Alembic migration task
- `redis`: a real Redis 7
- `webhook-service`: the API container
- `worker`: the TaskIQ Worker container
- `scheduler`: the TaskIQ Scheduler container
- `fake-openai`: a local OpenAI-compatible chat completions server
- `fake-feishu`: a local HTTP server that records the webhook payloads it receives

Pass conditions:

- API `/ready` is available;
- the migrate task exits successfully;
- the scheduler container stays running;
- the webhook request returns `200`;
- the worker consumes from Redis and completes processing;
- fake OpenAI receives a `/v1/chat/completions` request;
- fake Feishu receives a card payload with `msg_type=interactive`.

On failure, the script prints `docker compose ps` and the most recent container logs, and automatically runs:

```bash
docker compose -f tests/e2e/docker-compose.yml down -v --remove-orphans
```

This test depends on Docker and takes noticeably longer to run than a normal `pytest`. CI does not need to run it by default; you must run it when changing Alembic, TaskIQ, Redis, the pipeline, or the forwarding logic.
