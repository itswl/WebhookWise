#!/usr/bin/env bash
# The quality gate — an exact local replica of the CI `test` job plus the
# OpenAPI contract check, so "green here" means "green there".
#
# History says this script must exist: bandit (twice) and the OpenAPI
# contract each went red in CI while the local checklist passed, because the
# local list and ci.yml had drifted apart. Any check added to ci.yml's test
# job MUST be added here in the same change (and vice versa) — the
# release-version contract test pins several of these invocations.
#
# Usage:
#   bash scripts/gate.sh          # full gate (what CI runs)
#   bash scripts/gate.sh --fast   # skip pip-audit (network) for tight loops

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

FAST=0
if [[ "${1:-}" == "--fast" ]]; then
  FAST=1
fi

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

step "compileall"
python -m compileall -q .

step "ruff check"
ruff check .

step "ruff format --check"
ruff format --check .

step "frontend headless"
node tests/frontend/run-all.mjs

step "frontend lint"
npx --yes eslint@9.39.2 templates/static/js

step "shellcheck"
shellcheck entrypoint.sh scripts/gate.sh tests/e2e/*.sh scripts/ci/*.sh

step "requirements locks"
python scripts/check_requirements_locks.py

step "agent notes"
python scripts/assert_agent_notes.py

step "skill pointers"
# This repo has no .claude/ — Codex and hookprobe read .agents/skills directly,
# and Claude Code needs a per-machine symlink that no clone inherits. Reports and
# continues: a missing link in somebody's HOME is not a broken repository, and a
# gate that blocks a push over it teaches people to skip the gate. Skipped in CI.
python3 scripts/assert_skill_pointers.py

step "estate identifiers"
# This repository is public: no real project, bucket, service, team handle,
# hostname or webhook token may appear in it.
# The pattern list is not tracked (it would publish what the scrub removed).
# Without .estate-identifiers this step SKIPs -- copy .estate-identifiers.example
# and fill it in, or the check is doing nothing locally.
python scripts/assert_no_estate_identifiers.py

step "mypy"
mypy

step "bandit"
bandit -r core api services models adapters db scripts contracts -x tests -q -s B106

if [[ "$FAST" -eq 0 ]]; then
  step "pip-audit"
  pip-audit -r requirements.lock -r requirements-dev.lock
else
  printf '\n(skipping pip-audit in --fast mode)\n'
fi

step "observability contract"
python scripts/observability/webhookwise_observe.py contract

step "analysis eval"
# Replays evals/analysis_cases.jsonl through the rule engine and holds the
# importance verdict to evals/baseline.json. Deterministic and offline: the
# keyword policy comes from the committed defaults, not from the environment.
python scripts/eval_analysis.py run

step "ai eval freshness"
# The ai score cannot gate (it spends money and does not repeat), but the
# prompts that produce it are committed. This fails when they moved and the
# recorded score did not follow — same shape as the OpenAPI check.
python scripts/eval_analysis.py assert-fresh

step "OpenAPI contract"
# Config validation needs a DATABASE_URL; no connection is made. CI provides
# one via env — mirror that with a placeholder when unset.
DATABASE_URL="${DATABASE_URL:-postgresql+asyncpg://gate:gate@localhost:5432/gate}" \
  python scripts/export_openapi.py --check

step "pytest (CI invocation)"
pytest -n auto --dist loadfile -q \
  --cov=core --cov=api --cov=services --cov=models --cov=adapters --cov=db --cov=contracts \
  --cov-branch --cov-report=term --cov-fail-under=85

printf '\n\033[1;32mGATE GREEN\033[0m — matches the CI test job.\n'
