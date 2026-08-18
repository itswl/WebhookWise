#!/usr/bin/env python3
"""This repository is PUBLIC. Nothing in it may name a real estate.

An audit on 2026-08-18 found the seed runbooks publishing an internal org chart
(team names with chat handles), real object-storage bucket names, internal service
names, a Grafana hostname, and product codenames spread across nineteen files --
plus a live Feishu webhook token pasted into a masking test. None of it was
secret-shaped, so no secret scanner would ever have flagged it.

Placeholders must be obviously fictional. `demo-cn` and `sample-cn` are; a real
project name is not, and neither is a team handle somebody can @-mention.

    python3 scripts/assert_no_estate_identifiers.py

THE PATTERN LIST IS NOT IN THIS REPOSITORY. An earlier version of this file
carried the patterns inline and noted that it "necessarily contains the patterns
it forbids". That is not necessary, and it was the last public copy of exactly
the words the scrub existed to remove -- together with the replacement for each
one, which is a decoding table. The mechanism is public; the list is not.

The list lives in `.estate-identifiers` (git-ignored, format in
`.estate-identifiers.example`), and in CI in a repository secret written to that
path before this runs. Set ESTATE_GUARD_REQUIRED=1 there: without it a missing
list would make this exit 0 and the protection would evaporate silently, which
is the failure mode the guard is supposed to prevent.

Add a pattern the moment a real name gets scrubbed, or the scrub decays into a
one-off cleanup that the next paste undoes.
"""

from __future__ import annotations

import os
import re
import subprocess  # nosec B404
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PATTERN_FILE = Path(os.environ.get("ESTATE_PATTERNS_FILE", ROOT / ".estate-identifiers"))


def load_patterns() -> tuple[tuple[str, str], ...] | None:
    """(regex, why it must not appear) from the un-tracked list. None if absent."""
    if not PATTERN_FILE.is_file():
        return None
    rules: list[tuple[str, str]] = []
    for raw in PATTERN_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        pattern, _, reason = line.partition("\t")
        if not pattern or not reason:
            continue  # a line without a tab cannot say why, so it is not a rule
        rules.append((pattern, reason.strip()))
    return tuple(rules)


# The list is external now, so this file no longer needs exempting from itself.
EXEMPT: set[str] = {".estate-identifiers.example"}


def tracked_files() -> list[str]:
    # Fixed argv, no shell, no untrusted input: the only "input" is the
    # repository itself. `git` is resolved from PATH, as everywhere else here.
    out = subprocess.run(  # nosec B603 B607
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [line for line in out.splitlines() if line]


def main() -> int:
    rules = load_patterns()
    if rules is None:
        required = os.environ.get("ESTATE_GUARD_REQUIRED") == "1"
        where = PATTERN_FILE
        if required:
            print(f"  FAIL  no pattern list at {where} and ESTATE_GUARD_REQUIRED=1")
            print("        CI must write the list from its secret before this runs.")
            return 1
        print(f"  SKIP  no pattern list at {where}")
        print("        copy .estate-identifiers.example and fill in the real names.")
        return 0
    compiled = [(re.compile(pattern, re.I), reason) for pattern, reason in rules]
    problems: list[str] = []

    for name in tracked_files():
        if name in EXEMPT:
            continue
        path = ROOT / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable: nothing to read identifiers out of
        for line_no, line in enumerate(text.splitlines(), start=1):
            for pattern, reason in compiled:
                if pattern.search(line):
                    problems.append(f"{name}:{line_no}: {reason}")

    for problem in problems:
        print(f"  FAIL  {problem}")
    if problems:
        print(f"\n{len(problems)} estate identifier(s) in a public repository")
        return 1
    print(f"no estate identifiers: {len(tracked_files())} tracked file(s) clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
