"""Redis-first webhook deduplication helpers."""

from __future__ import annotations

import hashlib
from typing import Any

from adapters.normalized import extract_alert_identity
from core import json
from core.app_context import get_config_manager
from core.logger import get_logger

logger = get_logger("webhooks.deduplication")


def duplicate_window_hours() -> int:
    return max(1, int(get_config_manager().retry.DEDUP_WINDOW_SECONDS) // 3600)


def generate_alert_hash(data: dict[str, Any], source: str) -> str:
    identity = extract_alert_identity(data)
    if identity:
        key_fields: dict[str, object] = dict(identity)
        key_fields.setdefault("source", source.strip().lower())
    else:
        from core.observability.metrics import WEBHOOK_IDENTITY_DEGRADED_TOTAL, sanitize_source

        WEBHOOK_IDENTITY_DEGRADED_TOTAL.labels(sanitize_source(source)).inc()
        logger.debug("缺少 adapter 产出的告警 identity，使用完整 payload hash 兜底 source=%s", source)
        key_fields = {"source": source.strip().lower(), "payload": data}
    return hashlib.sha256(json.dumps_bytes(key_fields, sort_keys=True)).hexdigest()
