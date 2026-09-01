#!/usr/bin/env python3
"""docker-compose.quickstart.yml must stay startable by someone with no checkout.

    python3 scripts/assert_quickstart_compose.py

The quickstart's whole claim is one command against published images: no clone,
no build, no .env, no keys. Every part of that claim is a property of the file,
and each one decays silently.

- A `build:` stanza turns "docker compose up" into "clone first" — and it still
  works locally, where the source is sitting right there, so the person who
  adds it never sees the failure.
- A pinned image tag rots at the next release. The published tag comes from
  pyproject.toml, so that is what the default must equal. This one is not
  hypothetical: the sibling repository shipped a release whose images could not
  build because nobody checked the two halves still agreed.
- An env_file or a bind mount reintroduces the checkout through the back door.
- The demo credentials fail closed only while they keep the placeholder shape
  that startup_checks rejects under APP_ENV=production. Replace them with
  something that looks real and this file quietly becomes promotable.

That last check imports the real predicate rather than restating its prefixes,
so the demo and the guard cannot drift apart.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# core.web.startup_checks builds a logger at module scope, which constructs the
# whole AppConfig — so importing a pure string predicate needs a DATABASE_URL.
# Nothing here connects to it. Same convention as scripts/eval_analysis.py.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://guard:guard@localhost:5432/guard")

from core.web.startup_checks import looks_like_placeholder_secret  # noqa: E402

COMPOSE = ROOT / "docker-compose.quickstart.yml"

# Every setting whose value must stay unusable in production. WEBHOOK_SECRET,
# API_KEY and ADMIN_WRITE_KEY are the three startup_checks refuses to boot on;
# the database password is here because it appears in DATABASE_URL too, and a
# real-looking one there invites the same copy-paste.
GUARDED_SECRETS = (
    "WEBHOOK_SECRET",
    "API_KEY",
    "ADMIN_WRITE_KEY",
    "POSTGRES_PASSWORD",
)


def _declared_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def _walk(node: Any, path: str = ""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield path, key, value
            yield from _walk(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item, path)


def main() -> int:
    if not COMPOSE.exists():
        print(f"FAIL  {COMPOSE.name} is missing")
        return 1

    raw = COMPOSE.read_text(encoding="utf-8")
    doc = yaml.safe_load(raw)
    services = doc.get("services", {})
    failures: list[str] = []

    for name, service in services.items():
        if "build" in service:
            failures.append(f"service '{name}' has a build: stanza — the quickstart must not need a checkout")
        if "env_file" in service:
            failures.append(f"service '{name}' has env_file — the quickstart must not need a .env")
        for volume in service.get("volumes", []) or []:
            source = volume.split(":")[0] if isinstance(volume, str) else ""
            if source.startswith((".", "/", "~", "$")):
                failures.append(f"service '{name}' bind-mounts {source!r} — named volumes only")

    version = _declared_version()
    tags = set(re.findall(r"image:\s*ghcr\.io/itswl/webhookwise:\$\{WEBHOOKWISE_TAG:-([^}]+)\}", raw))
    if not tags:
        failures.append("no ghcr.io/itswl/webhookwise image with an overridable ${WEBHOOKWISE_TAG:-...} default")
    failures.extend(
        f"default image tag {tag} != pyproject version {version} — "
        f"the quickstart would pull an image that is not this release"
        for tag in sorted(tags - {version})
    )

    for _, key, value in _walk(doc):
        if key in GUARDED_SECRETS and isinstance(value, str) and not looks_like_placeholder_secret(value):
            failures.append(
                f"{key} is {value!r}, which startup_checks would ACCEPT under "
                f"APP_ENV=production — demo credentials must fail closed"
            )

    # The credential block is a YAML anchor merged into every app service, so a
    # single bad value surfaces once per service. Report the fault, not its fan-out.
    if failures:
        print(f"FAIL  {COMPOSE.name}")
        for line in dict.fromkeys(failures):
            print(f"  - {line}")
        return 1

    print(f"OK  {COMPOSE.name}: {len(services)} services, no build, tag {version}, credentials fail closed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
