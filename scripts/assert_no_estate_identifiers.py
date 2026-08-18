#!/usr/bin/env python3
"""This repository is PUBLIC. Nothing in it may name a real estate.

An audit on 2026-08-18 found the seed runbooks publishing an internal org chart
(team names with chat handles), real object-storage bucket names, internal service
names, a Grafana hostname, and product codenames spread across nineteen files —
plus a live Feishu webhook token pasted into a masking test. None of it was
secret-shaped, so no secret scanner would ever have flagged it.

Placeholders must be obviously fictional. `demo-cn` and `sample-cn` are; a real
project name is not, and neither is a team handle somebody can @-mention.

    python3 scripts/assert_no_estate_identifiers.py

Add a pattern here the moment a real name gets scrubbed, or the scrub decays into
a one-off cleanup that the next paste undoes.
"""

from __future__ import annotations

import re
import subprocess  # nosec B404
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (regex, why it must not appear). Case-insensitive.
FORBIDDEN: tuple[tuple[str, str], ...] = (
    # Product / project codenames from the real estate.
    (r"\bdemo-cn\b", "real project codename — use demo-cn"),
    (r"\bsample\b", "real project codename — use sample"),
    (r"\bdemo-(?:web-cn|backend)\b", "real project/service name — use demo-web-cn / vector-consumer"),
    (r"\bchat-service\b", "real internal service name — use chat-service"),
    # Object-storage buckets.
    (r"\bshared-prod-object\b", "real bucket name — use shared-prod-object"),
    (r"\bflink-meta\b", "real bucket prefix — use flink-meta"),
    # Org chart: a handle somebody can actually @-mention.
    (
        r"@(?:role-handle)\b",
        "internal chat handle — name a role, not a reachable handle",
    ),
    (r"内部\s*Wiki", "points at the internal wiki — describe the shape instead"),
    # Infrastructure.
    (r"internal\.example", "real deployment hostname — use grafana.internal.example"),
    (r"\b138\.2\.25\.190\b", "production server IP"),
    (r"/opt/deploy/", "production deployment path"),
    # A real Feishu webhook token is a 36-char hex UUID. Any long run of hex and
    # dashes after the hook path is treated as real; a fixture must use non-hex
    # characters (xxxxxxxx-xxxx-...) so it cannot match this at all. An earlier
    # version tried to whitelist short fakes with a lookahead and silently matched
    # nothing — a UUID's first dash is a word boundary, so the guard let the real
    # token through. Keep this pattern dumb.
    (
        r"open-apis/bot/v2/hook/[0-9a-f][0-9a-f-]{19,}",
        "looks like a real Feishu webhook token — use a visibly fake fixture",
    ),
)

# This file necessarily contains the patterns it forbids.
EXEMPT = {"scripts/assert_no_estate_identifiers.py"}


def tracked_files() -> list[str]:
    # Fixed argv, no shell, no untrusted input: the only "input" is the
    # repository itself. `git` is resolved from PATH, as everywhere else here.
    out = subprocess.run(  # nosec B603 B607
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [line for line in out.splitlines() if line]


def main() -> int:
    compiled = [(re.compile(pattern, re.I), reason) for pattern, reason in FORBIDDEN]
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
