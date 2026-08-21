#!/usr/bin/env python3
"""Agent Notes have to keep their shape, or the practice decays into a folder.

A decision record is only useful if it can be found and trusted: the bucket says
its status, the filename says its date, and the three sections answer what was
decided, why, and what it costs. Drift on any of those and the note becomes a
blog post nobody maintains.

    python3 scripts/assert_agent_notes.py

See .agents/README.md for the convention this enforces.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BUCKETS = ("proposed", "implemented", "rejected", "archived")
# WebhookWise's own module boundaries, the ones CLAUDE.md tells changes to
# stay inside. A note that cannot name one is a note about nothing.
SCOPES = (
    "api",
    "services",
    "core",
    "models",
    "templates",
    "deploy",
    "whole",
)
SECTIONS = ("## Decision", "## Why", "## Consequences")
FILENAME = re.compile(r"^(\d{4}-\d{2}-\d{2})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")


def front_matter(text: str) -> dict[str, str] | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end < 0:
        return None
    fields: dict[str, str] = {}
    for line in text[4:end].splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


def check(path: Path, bucket: str, known: set[str]) -> list[str]:
    problems: list[str] = []
    name = path.name
    match = FILENAME.match(name)
    if not match:
        return [f"{path}: name must be YYYY-MM-DD-lower-slug.md"]

    text = path.read_text(encoding="utf-8")
    fields = front_matter(text)
    if fields is None:
        return [f"{path}: missing --- front matter block"]

    problems.extend(
        f"{path}: front matter needs {key}" for key in ("title", "status", "date", "scope") if not fields.get(key)
    )
    if fields.get("status") not in (None, bucket):
        problems.append(f"{path}: status {fields['status']!r} does not match its {bucket}/ bucket")
    if fields.get("date") and fields["date"] != match.group(1):
        problems.append(f"{path}: date {fields['date']} disagrees with the filename")
    if fields.get("scope") and fields["scope"] not in SCOPES:
        problems.append(f"{path}: scope {fields['scope']!r} is not one of {', '.join(SCOPES)}")
    problems.extend(f"{path}: missing a {section} section" for section in SECTIONS if section not in text)

    superseded = fields.get("supersedes")
    if superseded and superseded not in known:
        problems.append(f"{path}: supersedes {superseded!r}, which is not a note in this repository")
    return problems


def _assert_one_agent_guide() -> list[str]:
    """AGENTS.md and CLAUDE.md must be the same file, not two copies.

    They were two copies, and they drifted: the older one still told an agent to
    hand-pick a few local checks, which is the habit that let bandit, pip-audit
    and the OpenAPI contract each go red in CI while local was green. CLAUDE.md
    is now a symlink.

    Checked by CONTENT rather than by asking whether it is a link, because a
    checkout without symlink support materialises the link as a text file
    holding the target's path — which reads as "fine" to anything that only
    tests for existence, and would silently reintroduce the drift.
    """
    root = Path(__file__).resolve().parent.parent
    agents, claude = root / "AGENTS.md", root / "CLAUDE.md"
    if not claude.exists():
        return ["CLAUDE.md is missing; it must be a symlink to AGENTS.md"]
    if claude.read_text(encoding="utf-8") != agents.read_text(encoding="utf-8"):
        return [
            "CLAUDE.md and AGENTS.md have diverged. They are one file with two "
            "names (CLAUDE.md is a symlink); recreate it with "
            "`ln -sf AGENTS.md CLAUDE.md` rather than editing both."
        ]
    return []


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    notes = root / ".agents" / "notes"
    if not notes.is_dir():
        print(f"  FAIL  {notes} is missing")
        return 1

    stray = list(notes.glob("*.md"))
    files: list[tuple[Path, str]] = []
    for bucket in BUCKETS:
        files += [(p, bucket) for p in sorted((notes / bucket).glob("*.md"))]
    known = {p.stem for p, _ in files}

    problems = _assert_one_agent_guide()
    problems += [f"{p}: notes live in one of {', '.join(BUCKETS)}/" for p in stray]
    for path, bucket in files:
        problems += check(path, bucket, known)

    for line in problems:
        print(f"  FAIL  {line}")
    if problems:
        print(f"\n{len(problems)} agent-note problem(s)")
        return 1
    counts = ", ".join(f"{sum(1 for _, b in files if b == bucket)} {bucket}" for bucket in BUCKETS)
    print(f"agent notes: {len(files)} well-formed ({counts})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
