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
pytest -q
```

Before EVERY push, run the full gate — it is an exact replica of the CI test
job (bandit, pip-audit, and the OpenAPI contract have each gone red in CI
while a hand-picked local list passed; do not hand-pick):

```bash
bash scripts/gate.sh          # full gate
bash scripts/gate.sh --fast   # skips pip-audit for tight loops
```

When a check is added to ci.yml's test job, add it to scripts/gate.sh in the
same change (and vice versa).

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
- Add targeted tests for core delivery channels, especially Feishu, deep analysis, forwarding, persistence, and dashboard static contracts.
- Do not introduce new observability instruments unless a dashboard, alert, SLO, or automated decision consumes them.
- Decisions that outlive their diff go in `.agents/notes/` — especially the
  rejected ones, which a commit log never keeps. `scripts/assert_agent_notes.py`
  enforces the shape and the gate runs it; see `.agents/README.md`.
- Dashboard UI work follows `docs/design-language.md` (dark-first tokens,
  colour-in-points, the icon sprite, the type scale). The contract tests in
  `tests/runtime/test_dashboard_static_contracts.py` enforce it; a red
  contract is the design system talking, not an obstacle to delete.
