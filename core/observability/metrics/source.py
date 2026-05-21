"""Low-cardinality source label normalization."""

from __future__ import annotations

import re
import threading

from core.observability.env import env_int

SOURCE_LABEL_MAX_LENGTH = 50
_SOURCE_LABEL_INVALID_CHARS = re.compile(r"[^a-z0-9_.-]+")
_SOURCE_LABEL_LIMIT = env_int("WEBHOOKWISE_SOURCE_LABEL_LIMIT", 128)
_SOURCE_LABEL_LIMIT_FALLBACK = "other"
_seen_sources: set[str] = set()
_seen_sources_lock = threading.Lock()


def _enforce_source_limit(source: str) -> str:
    if source in {"unknown", _SOURCE_LABEL_LIMIT_FALLBACK}:
        return source
    if _SOURCE_LABEL_LIMIT <= 0:
        return _SOURCE_LABEL_LIMIT_FALLBACK
    with _seen_sources_lock:
        if source in _seen_sources:
            return source
        if len(_seen_sources) >= _SOURCE_LABEL_LIMIT:
            return _SOURCE_LABEL_LIMIT_FALLBACK
        _seen_sources.add(source)
    return source


def sanitize_source(source: str) -> str:
    if not source:
        return "unknown"
    normalized = _SOURCE_LABEL_INVALID_CHARS.sub("-", str(source).lower().strip())
    normalized = normalized.strip("._-")
    if not normalized:
        return "unknown"
    return _enforce_source_limit(normalized[:SOURCE_LABEL_MAX_LENGTH])


def _reset_source_label_cache_for_tests() -> None:
    with _seen_sources_lock:
        _seen_sources.clear()
