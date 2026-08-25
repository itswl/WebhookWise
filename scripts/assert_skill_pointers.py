#!/usr/bin/env python3
"""The skills live in .agents/skills; Claude Code looks in ~/.claude/skills.

    python3 scripts/assert_skill_pointers.py

This repository has no `.claude/` directory. Codex reads `.agents/skills`
natively and hookprobe mounts it by absolute path, but Claude Code has no
project-level discovery path that avoids `.claude/` — measured on the installed
binary: 104 references to `.claude/skills`, none to `.agents/skills`, every
skills environment variable a disable switch, and the plugin route requiring an
`extraKnownMarketplaces` entry in a repository `.claude/settings.json`.

So the pointer is per-machine, and a per-machine step is one nobody is told
about at the moment it matters. AGENTS.md carries the loop, and a document is
read once. The likelier failure is not a fresh clone at all: it is somebody
ADDING a skill six weeks from now and never linking it, then wondering why the
new one does not appear while the old four do.

This says so, at the moment somebody is working in the repository — the gate.

Not a hard failure by default. It reports and returns 0 unless --strict, because
a missing symlink in one person's home directory is not a broken repository, and
a check that blocks a push over somebody else's HOME teaches people to skip the
gate. CI has no business having these links at all and is detected and skipped.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / ".agents" / "skills"
# Where Claude Code looks. CLAUDE_CONFIG_DIR moves it, so honour that first.
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def _in_ci() -> bool:
    return any(os.environ.get(name) for name in ("CI", "GITHUB_ACTIONS"))


def audit() -> tuple[list[str], list[str], list[str]]:
    """(missing, wrong target, stale links pointing at nothing)."""
    want = sorted(p.name for p in SKILLS.iterdir() if p.is_dir()) if SKILLS.is_dir() else []
    target = CLAUDE_HOME / "skills"
    missing: list[str] = []
    wrong: list[str] = []
    stale: list[str] = []
    for name in want:
        link = target / name
        if not link.exists() and not link.is_symlink():
            missing.append(name)
            continue
        # resolve() on both sides: the repo may itself be reached through a link.
        if link.resolve() != (SKILLS / name).resolve():
            wrong.append(name)
    if target.is_dir():
        stale.extend(
            link.name for link in sorted(target.iterdir()) if link.is_symlink() and not link.resolve().exists()
        )
    return missing, wrong, stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="exit non-zero when a pointer is missing")
    args = parser.parse_args(argv)

    if _in_ci():
        print("skill pointers: skipped (CI has no ~/.claude to point)")
        return 0
    if not SKILLS.is_dir():
        print("skill pointers: no .agents/skills in this checkout")
        return 0

    missing, wrong, stale = audit()
    total = len(sorted(p.name for p in SKILLS.iterdir() if p.is_dir()))
    if not missing and not wrong and not stale:
        print(f"skill pointers: {total} skill(s) reachable from {CLAUDE_HOME / 'skills'}")
        return 0

    for name in missing:
        print(f"  MISSING  {name} — Claude Code will not see it")
    for name in wrong:
        print(f"  WRONG    {name} — points somewhere other than this checkout")
    for name in stale:
        print(f"  DANGLING {name} — points at nothing; a skill that was renamed or removed")
    print("\n  Fix, from the repository root:")
    print('    mkdir -p "$HOME/.claude/skills"')
    print('    for s in .agents/skills/*/; do ln -sfn "$PWD/$s" "$HOME/.claude/skills/$(basename "$s")"; done')
    print("\n  Codex and hookprobe are unaffected: both read .agents/skills directly.")
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
