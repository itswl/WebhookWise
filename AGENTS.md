# WebhookWise Development Guide

## Project Shape

WebhookWise is a single Python service with FastAPI HTTP entrypoints, TaskIQ worker/scheduler processes, PostgreSQL persistence, Redis coordination, and OpenTelemetry-first observability. Keep changes scoped to the existing module boundaries:

- `api/`: HTTP handlers and response contracts.
- `services/`: business workflows, forwarding, analysis, notification formatting.
- `core/`: shared runtime, config, logging, security, observability, process wiring.
- `models/`, `schemas/`, `db/`: persistence and API data contracts.
- `templates/`: dashboard HTML/CSS/JS.

## Local Commands

Run focused checks before broad ones:

```bash
ruff check .
mypy
python -m compileall -q .
pytest -q
```

Run the CI-equivalent quality gate when touching runtime behavior:

```bash
python scripts/check_requirements_locks.py
python scripts/observability/webhookwise_observe.py contract
pytest -q --cov=core --cov=api --cov=services --cov=models --cov=adapters --cov=db --cov=contracts --cov-branch --cov-report=term --cov-report=xml --cov-fail-under=85
```

Run shell checks when editing container entrypoints:

```bash
shellcheck entrypoint.sh tests/e2e/run_webhook_to_feishu.sh
```

## Working Rules

- Write all code, comments, docstrings, logs, exception/HTTP-error messages,
  API response messages, docs, and config comments in English.
- Keep the AI prompts in `prompts/` (and the `AI_SYSTEM_PROMPT` / user-prompt
  defaults in `core/config/defaults.py`) in Chinese — they steer the model's
  Chinese output and are a product decision, not display copy.
- Keep Chinese strings that are behavioral, not display: severity/cleanup
  keyword sets matched against incoming Chinese alert text (`RULE_*_KEYWORDS`,
  `CLEANUP_KEYWORDS`, `_ERROR_KEYWORDS`, the `"恢复"`-style normalizer keywords),
  the Feishu-card field-label match-keys for inbound Chinese cards, and the
  `deep_analysis_report.v1` section titles rendered into the Chinese report.
  Translating any of these silently breaks classification/parsing.
- Prefer explicit policy/config objects over module import side effects.
- Keep metrics labels stable and machine-readable; do not derive metric dimensions by parsing log text or localized strings.
- Add targeted tests for core delivery channels, especially Feishu, OpenClaw, forwarding, persistence, and dashboard static contracts.
- Do not introduce new observability instruments unless a dashboard, alert, SLO, or automated decision consumes them.
