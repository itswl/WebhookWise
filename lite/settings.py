"""Configuration — environment variables only, every one optional.

The defaults are chosen so `docker run -p 8000:8000 webhookwise-lite` is a
working install: no AI key, no rules, no token. You add a forwarding rule and
it starts notifying; you add an OpenAI-compatible key and the triage gets
smarter. Nothing has to be configured for the system to be honest about what
it decided.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    db_path: str = os.environ.get("DB_PATH", "/data/webhookwise.db")
    # Empty = unauthenticated ingest. Set it for anything internet-facing.
    ingest_token: str = os.environ.get("INGEST_TOKEN", "")
    admin_token: str = os.environ.get("ADMIN_TOKEN", "")
    # Empty = the dashboard and read APIs are open (fine on a laptop). Set it
    # the moment the port is reachable from a network you do not control:
    # decision rows carry your alert content verbatim.
    read_token: str = os.environ.get("READ_TOKEN", "")

    # The two suppression windows do different jobs and must not be set to the
    # same magnitude: dedup collapses a BURST of the identical alert, cooldown
    # paces RE-notification of an identity that keeps firing. Cooldown only ever
    # gets a say when it is the longer of the two — with dedup >= cooldown the
    # dedup gate would catch every candidate first and cooldown could never fire.
    dedup_window_seconds: int = _int("DEDUP_WINDOW_SECONDS", 300)
    cooldown_seconds: int = _int("COOLDOWN_SECONDS", 1800)

    openai_api_key: str = os.environ.get("OPENAI_API_KEY", "")
    openai_api_url: str = os.environ.get("OPENAI_API_URL", "https://api.openai.com/v1")
    openai_model: str = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    ai_timeout_seconds: int = _int("AI_TIMEOUT_SECONDS", 20)

    outbox_poll_seconds: int = _int("OUTBOX_POLL_SECONDS", 2)
    outbox_backoff_seconds: int = _int("OUTBOX_BACKOFF_SECONDS", 15)


settings = Settings()
