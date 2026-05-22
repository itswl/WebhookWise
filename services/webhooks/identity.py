"""Webhook identity helpers owned by the service layer."""

from __future__ import annotations

import hashlib
from typing import Any

from adapters.normalized import extract_alert_identity
from core import json
from core.logger import get_logger

logger = get_logger("webhooks.identity")


def generate_alert_hash(data: dict[str, Any], source: str) -> str:
    identity = extract_alert_identity(data)
    if identity:
        key_fields: dict[str, object] = dict(identity)
        key_fields.setdefault("source", source.strip().lower())
    else:
        logger.warning("缺少 adapter 产出的告警 identity，使用完整 payload hash 兜底 source=%s", source)
        key_fields = {"source": source.strip().lower(), "payload": data}

    return hashlib.sha256(json.dumps_bytes(key_fields, sort_keys=True)).hexdigest()
