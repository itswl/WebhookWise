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

Agent material lives in ONE place: `.agents/`. It was three directories until
2026-08-25, and each step of the merge was decided by measurement rather than
taste.

- `.agents/notes/` — decision records, read by people, shape-checked by
  `scripts/assert_agent_notes.py`, which the gate runs.
- `.agents/skills/` — the real operator skills.
- `.claude/skills` — a SYMLINK to the above, and the only vendor path left.

`strings` on both installed CLIs is what settled the shape:

| discovery path | Claude Code | Codex |
| --- | --- | --- |
| `.claude/skills` | 104 references | — |
| `.agents/skills` | none | yes |

Codex reads the neutral path natively — the same binary lists
`.agents/plugins/marketplace.json` beside `.claude-plugin/` and
`.cursor-plugin/`, so `.agents/` is a cross-tool convention now, the way
AGENTS.md became one. `.codex/` was therefore deleted outright rather than kept
as a pointer.

Claude Code has no configurable skills path (the binary carries `skillsDirs`
internally; the exposed settings are `disableBundledSkills` and friends), so
`.claude/skills` has to exist. That it can be a symlink was PROVED rather than
assumed: a probe skill in a scratch directory behind a symlinked
`.claude/skills` showed up in a fresh session's skill list. Before that test this
guide said the inversion would be "a gamble", and it was right to until somebody
ran it.

Two details that bite:

- **`.gitignore` needs `!.claude/skills` with NO trailing slash.** The old
  `!.claude/skills/` matched a directory only, so the moment this became a
  symlink it was silently ignored again and the pointer would not have shipped.
  Git tracks it as mode 120000; a clone with `core.symlinks=false` gets a text
  file instead.
- **hookprobe bind-mounts the skills tree** read-only as its user skills layer,
  via `HOOKPROBE_USER_SKILLS` in the stack env. That points at `.agents/skills`
  now. Docker would have resolved the symlink, but a mount that depends on a
  link resolving is a mount nobody can reason about during an incident.

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
