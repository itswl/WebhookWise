#!/usr/bin/env python3
"""This repository is PUBLIC. Nothing in it may name a real estate.

An audit found the seed runbooks publishing an internal org chart, real bucket
names, internal service names and product codenames across nineteen files, plus a
live Feishu webhook token pasted into a masking test. None of it is secret-shaped,
so no secret scanner would ever have flagged it.

Two kinds of check, because they need different treatment:

**Shapes** are regexes, published openly — a Feishu token, a deploy path, an
@-mentionable handle. Naming the shape gives nothing away.

**Names** are stored as salted hashes, never as literals. A denylist of real
project and service names would otherwise be exactly the disclosure the audit was
cleaning up: after the history rewrite, this file would have been the last place
those names appeared. So a name is registered by hash and matched by hashing
every sub-run of every token in a file — `sample` is caught inside
`sample-cn-prod-object`, and a reader of this file learns only that nine names
are forbidden, not what they are.

    python3 scripts/assert_no_estate_identifiers.py
    python3 scripts/assert_no_estate_identifiers.py --add <name>   # print a hash to paste

Add a name the moment a real one gets scrubbed, or the scrub decays into a
one-off cleanup that the next paste undoes.
"""

from __future__ import annotations

import hashlib
import re
import subprocess  # nosec B404
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Salt makes the hashes useless for confirming a guessed name against another
# repository; it is not a secret and does not need to be.
SALT = b"webhookwise-estate-identifier-v1:"

# sha256(SALT + name.lower())[:16] for each forbidden name. Regenerate with --add.
FORBIDDEN_NAME_HASHES: dict[str, str] = {
    "1cdfe77b3db7f251": "real project codename — use demo-cn",
    "028bc22ac639090f": "real project codename — use sample",
    "70d87d8543b1789e": "real project codename — use demo-web-cn",
    "cf8b17d939cb09a7": "real internal service name — use vector-consumer",
    "fdfcfaf9f242b2fc": "real internal service name — use chat-service",
    "c879a9dd0a833e0e": "real bucket name — use shared-prod-object",
    "60947209957a77f9": "real bucket prefix — use flink-meta",
    "6b748956a3403905": "real deployment hostname — use *.internal.example",
    "219766dc9cb3316c": "production server address",
}

# Shapes are safe to publish; naming the pattern discloses nothing.
FORBIDDEN_SHAPES: tuple[tuple[str, str], ...] = (
    (
        r"@(?:role-handle)\b",
        "internal chat handle — name a role, not a reachable handle",
    ),
    (r"内部\s*Wiki", "points at the internal wiki — describe the shape instead"),
    (r"/opt/(?:docker-compose|deploy)/[A-Za-z]", "production deployment path"),
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

# Files whose whole job is to contain these patterns.
EXEMPT = {"scripts/assert_no_estate_identifiers.py"}

TOKEN = re.compile(r"[a-z0-9]+(?:[-.][a-z0-9]+)*")


def name_hash(name: str) -> str:
    return hashlib.sha256(SALT + name.strip().lower().encode()).hexdigest()[:16]


def sub_runs(token: str) -> set[str]:
    """Every contiguous sub-run of a token, separators preserved.

    A whole-token hash misses both directions of nesting: a name registered as
    `sample` hides inside `sample-cn-prod-object`, and a registered
    `host.eu.org` hides inside `grafana.host.eu.org`. Splitting on `-` and `.`
    and rejoining every window catches both. Tokens with more than eight parts
    are skipped — nothing real is named that way, and it bounds the work.
    """
    parts = re.split(r"([-.])", token)
    words, seps = parts[::2], parts[1::2]
    if len(words) > 8:
        return {token}
    runs = set()
    for start in range(len(words)):
        current = words[start]
        runs.add(current)
        for index in range(start, len(words) - 1):
            current += seps[index] + words[index + 1]
            runs.add(current)
    return runs


def tracked_files() -> list[str]:
    # Fixed argv, no shell, no untrusted input: the only "input" is the
    # repository itself. `git` is resolved from PATH, as everywhere else here.
    out = subprocess.run(  # nosec B603 B607
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [line for line in out.splitlines() if line]


def scan_line(line: str, shapes: list[tuple[re.Pattern[str], str]]) -> list[str]:
    reasons = [reason for pattern, reason in shapes if pattern.search(line)]
    for token in TOKEN.findall(line.lower()):
        for candidate in sub_runs(token):
            reason = FORBIDDEN_NAME_HASHES.get(name_hash(candidate))
            if reason:
                reasons.append(reason)
    return reasons


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "--add":
        print(f'    "{name_hash(argv[2])}": "describe the replacement here",')
        return 0

    shapes = [(re.compile(pattern, re.I), reason) for pattern, reason in FORBIDDEN_SHAPES]
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
            problems.extend(f"{name}:{line_no}: {reason}" for reason in scan_line(line, shapes))

    for problem in problems:
        print(f"  FAIL  {problem}")
    if problems:
        print(f"\n{len(problems)} estate identifier(s) in a public repository")
        return 1
    print(f"no estate identifiers: {len(tracked_files())} tracked file(s) clean")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
