from __future__ import annotations

import hashlib
from typing import Any

from core import json
from core.logger import get_logger
from core.observability.metrics import WEBHOOK_IDENTITY_DEGRADED_TOTAL, sanitize_source

logger = get_logger("dedup.key")


def generate_dedup_key(data: dict[str, Any], source: str, alert_hash: str) -> str:
    from adapters.normalized import extract_alert_identity

    identity = extract_alert_identity(data)
    if identity:
        key_fields: dict[str, object] = {}
        source_value = str(identity.get("source", source)).strip().lower()
        name_value = str(identity.get("name", "")).strip()
        if source_value:
            key_fields["source"] = source_value
        if name_value:
            key_fields["name"] = name_value
        resource = str(identity.get("resource", "") or "").strip()
        if resource:
            key_fields["resource"] = resource
        fingerprint = str(identity.get("fingerprint", "") or "").strip()
        if fingerprint:
            key_fields["fingerprint"] = fingerprint
        if not key_fields:
            return alert_hash
        return hashlib.sha256(json.dumps_bytes(key_fields, sort_keys=True)).hexdigest()

    WEBHOOK_IDENTITY_DEGRADED_TOTAL.labels(sanitize_source(source)).inc()
    logger.debug("缺少 adapter 产出的告警 identity，使用 alert_hash 作为 dedup_key source=%s", source)
    return alert_hash
