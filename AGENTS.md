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

Agent material lives in ONE directory: `.agents/`. There is no `.claude/` and no
`.codex/` in this repository. Getting here took four passes and each one was
settled by measurement rather than argument; the measurements are kept below
because the next person will want to undo this.

- `.agents/notes/` — decision records, read by people, shape-checked by
  `scripts/assert_agent_notes.py`, which the gate runs.
- `.agents/skills/` — the operator skills, the only copy.

**Codex needs nothing.** It reads `.agents/skills` natively — the binary also
lists `.agents/plugins/marketplace.json` beside `.claude-plugin/` and
`.cursor-plugin/`, so `.agents/` is a cross-tool convention now, the way
AGENTS.md became one.

**Claude Code needs a pointer, and it cannot live here.** Measured on the
installed binary: 104 references to `.claude/skills`, none to `.agents/skills`;
every skills-related environment variable is a *disable* switch; and the plugin
route needs `extraKnownMarketplaces`, which the setting's own description says
belongs in a repository `.claude/settings.json`. There is no project-level
discovery path that avoids `.claude/`.

So the pointer is per-machine instead. One symlink per skill:

```bash
mkdir -p ~/.claude/skills
for s in .agents/skills/*/; do ln -sfn "$PWD/$s" ~/.claude/skills/"$(basename "$s")"; done
```

Verified after doing exactly that: a fresh session in this repository, with no
`.claude/` in it at all, lists all four `ww-*` skills. The files still version
with the service they drive, which was the whole point of keeping them in-repo.

A document is read once, so the gate says it too. `scripts/assert_skill_pointers.py`
runs in `scripts/gate.sh` and reports any skill in `.agents/skills` with no
pointer, a pointer into a DIFFERENT checkout, or a pointer left aiming at
nothing after a rename. It prints the loop above and returns 0: a missing symlink
in one person's home directory is not a broken repository, and a gate that blocks
a push over somebody else's HOME teaches people to skip the gate. `--strict`
makes it fail for whoever decides otherwise, and CI is detected and skipped
because CI has no business owning these links.

The failure it is really there for is not a fresh clone. It is somebody ADDING a
skill six weeks from now, never linking it, and wondering why the new one is
invisible while the old four work.

**hookprobe mounts `.agents/skills`** read-only as its user skills layer, via
`HOOKPROBE_USER_SKILLS` in the stack env — the real directory, not a symlink
pointing at it. Docker resolves a symlinked source fine, but a mount whose
correctness depends on a link resolving is one nobody can reason about
mid-incident.

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
