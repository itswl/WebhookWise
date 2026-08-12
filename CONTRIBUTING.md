# Contributing

Thanks for considering a contribution. WebhookWise is a modular monolith with
separate API, worker, and scheduler processes. Keep changes small, tested, and
aligned with the ownership rules in
[docs/architecture/boundaries.md](docs/architecture/boundaries.md).

## Development Setup

WebhookWise targets **Python 3.12**. The lock files are the source of truth
for installs (do not install from `requirements.txt` directly):

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.lock -r requirements-dev.lock
cp .env.example .env
```

For local PostgreSQL and Redis plus migrations:

```bash
docker compose up -d postgres redis
python -m scripts.run_migrations
```

Run the processes directly on the host while developing (see the
[Local Development](README.md#local-development) section of the README for
Worker and Scheduler entry points):

```bash
uvicorn api.app:app --reload --port 8000
```

## The Quality Gate

The whole pre-PR checklist is one command:

```bash
bash scripts/gate.sh
```

It is an exact local replica of the CI `test` job — green here means green
there. It runs, in order: `compileall`, `ruff check`, `ruff format --check`,
`shellcheck`, the requirements-lock drift check, `mypy`, `bandit`,
`pip-audit`, the observability contract
(`scripts/observability/webhookwise_observe.py contract`), the OpenAPI
contract (`scripts/export_openapi.py --check`), and `pytest` with the **85%
branch-coverage gate**.

`bash scripts/gate.sh --fast` skips `pip-audit` (the only networked step) for
tight loops. During development you can of course run the individual tools
(`ruff check .`, `mypy`, `pytest -q ...`) — the gate is what must pass before
you open a PR.

When you change API contracts, regenerate the exported OpenAPI files (the gate
only verifies they match):

```bash
OTEL_ENABLED=false python scripts/export_openapi.py
```

## Working Rules

These come from the project's development guide and are enforced by review
(some by contract tests):

- **English everywhere in code.** All code, comments, docstrings, logs,
  exception/HTTP-error messages, API response messages, docs, and config
  comments are written in English.
- **The Chinese you find is deliberate and load-bearing — never "fix" it.**
  The AI prompts in `prompts/` (and the prompt defaults in
  `core/config/defaults.py`) steer the model's Chinese output and are a
  product decision. The Chinese keyword sets (`RULE_*_KEYWORDS`,
  `CLEANUP_KEYWORDS`, `_ERROR_KEYWORDS`, the recovery-normalizer keywords),
  the Feishu-card field-label match keys, and the `deep_analysis_report.v1`
  section titles are matched against incoming Chinese alert text or rendered
  into the Chinese report — translating any of them silently breaks
  classification and parsing.
- **Keep metrics labels stable and machine-readable.** Never derive metric
  dimensions by parsing log text or localized strings.
- **No new observability instruments without a consumer.** Add a metric,
  span, or event only when a dashboard, alert, SLO, or automated decision
  consumes it.
- Prefer explicit policy/config objects over module import side effects.

## Commit Style

Conventional-commit-ish, as in the existing history:

```
type(scope): imperative, lowercase summary
```

Types in use: `feat`, `fix`, `chore`, `test`, `docs`, `style`, `ci`. The scope
is optional but helpful (`fix(feishu): ...`, `test(runtime): ...`).

## Adding a Webhook Source

For a **simple** source, no Python is needed: drop a declarative YAML spec in
`adapters/specs/` — see [adapters/specs/README.md](adapters/specs/README.md)
for the format, detection precedence, and a working example
(`generic_json.yaml`). For anything the mapping can't express (multi-alert
fan-out, computed fields, conditional logic), write a code adapter in
`adapters/simple_adapters.py`. Either way: adapter and tests first, then reuse
the existing pipeline.

## Tests

- The default suite runs on in-memory SQLite and a mocked Redis; `pytest -q`
  needs no services.
- `tests/real_infra/` holds a small semantics suite against **actual
  PostgreSQL and Redis** (things the doubles get wrong: `FOR UPDATE`, VARCHAR
  length enforcement, Lua scripts). It is collected only when opted in:

  ```bash
  REAL_INFRA_TESTS=1 \
  DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/db \
  REDIS_URL=redis://localhost:6379/15 \
  pytest -q tests/real_infra/
  ```

  CI's `integration` job runs it with service containers.
- When touching Alembic, Redis/TaskIQ, the pipeline, forwarding, or container
  startup, also run the Docker E2E: `tests/e2e/run_webhook_to_feishu.sh`.

## Pull Request Expectations

- `bash scripts/gate.sh` is green.
- Changes to core delivery channels — Feishu, deep analysis, forwarding,
  persistence, dashboard static contracts — come with targeted tests.
- User-facing changes add a line under `Unreleased` in
  [CHANGELOG.md](CHANGELOG.md).
- New business capability goes in the nearest `services/*` domain package;
  business logic does not go into `core/`.
- Docs are updated when behavior or configuration changes.

## Documentation

- API docs: [docs/reference/api.md](docs/reference/api.md)
- Architecture boundaries: [docs/architecture/boundaries.md](docs/architecture/boundaries.md)
- Observability: [docs/operations/observability/overview.md](docs/operations/observability/overview.md)
- Changelog: [CHANGELOG.md](CHANGELOG.md)
