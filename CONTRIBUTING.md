# Contributing

WebhookWise is a modular monolith with separate API, worker, and scheduler
processes. Keep changes small, tested, and aligned with the ownership rules in
[docs/architecture/boundaries.md](docs/architecture/boundaries.md).

## Local Setup

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.lock -r requirements-dev.lock
cp .env.example .env
```

For local services:

```bash
docker-compose up -d postgres redis
python -m scripts.run_migrations
```

## Checks Before Commit

Run the fast checks for ordinary code changes:

```bash
python -m compileall -q core api services adapters db models scripts tests
ruff check .
mypy
pytest -q
```

When API contracts change, refresh the exported OpenAPI files with the
lock-file dependency set:

```bash
OTEL_ENABLED=false python scripts/export_openapi.py
```

When touching Alembic, Redis/TaskIQ, pipeline, forwarding, or container
startup, run the Docker E2E:

```bash
tests/e2e/run_webhook_to_feishu.sh
```

## Documentation

- API docs: [docs/reference/api.md](docs/reference/api.md)
- Architecture boundaries: [docs/architecture/boundaries.md](docs/architecture/boundaries.md)
- Observability: [docs/architecture/observability.md](docs/architecture/observability.md)
- Changelog: [CHANGELOG.md](CHANGELOG.md)
