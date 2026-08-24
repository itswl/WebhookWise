# Agent Guide

This is the single agent-facing instruction file. `CLAUDE.md` is a symlink to it,
because two copies drifted: the older one still told an agent to hand-pick a few
local checks, which is exactly the habit that let bandit, pip-audit and the
OpenAPI contract each go red in CI while local was green.

## Project Shape

WebhookWise is a single Python service with FastAPI HTTP entrypoints, TaskIQ worker/scheduler processes, PostgreSQL persistence, Redis coordination, and OpenTelemetry-first observability. Keep changes scoped to the existing module boundaries:

- `api/`: HTTP handlers and response contracts.
- `services/`: business workflows, forwarding, analysis, notification formatting.
- `core/`: shared runtime, config, logging, security, observability, process wiring.
- `models/`, `schemas/`, `db/`: persistence and API data contracts.
- `templates/`: dashboard HTML/CSS/JS.

Three dot-directories carry agent material, and each one is load-bearing — the
question "why not merge them" has a different answer in each case, so none of
them is tidy-up material:

- `.agents/` — decision records, read by people, no tool reads them. Shape
  enforced by `scripts/assert_agent_notes.py`, which the gate runs.
- `.claude/skills/` — the ONE real copy of the operator skills. It sits under a
  vendor name because that path is a deployment contract, not a preference:
  hookprobe bind-mounts it read-only as `/data/home/.claude/skills`, its user
  skills layer. `.gitignore` carves it out with `.claude/*` + `!.claude/skills/`
  so local Claude state stays out while the skills tree ships. Moving it means
  editing hookprobe's compose in the same change; the reasoning is written up in
  `.agents/notes/implemented/2026-08-17-skills-live-in-repo-guide-is-a-destination.md`.
- `.codex/skills` — a symlink to the above, so the installed Codex CLI sees the
  same skills. A tracked symlink, so a clone with `core.symlinks=false` gets a
  text file instead of a directory.

Nothing else belongs in `.claude/`. A local prompt file lived in
`.claude/prompts/` until 2026-08-24 telling an agent to hand-pick four local
checks and to `git add -A`, both of which this guide forbids, and it carried the
production IP and path in the working tree where the estate guard cannot see it
— the guard reads `git ls-files`, so an ignored file is invisible to it.

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

- Editing a container entrypoint or an e2e shell script? `bash scripts/gate.sh`
  runs shellcheck over them; CI does too.

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
