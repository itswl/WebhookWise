from __future__ import annotations

from typing import Any

from adapters.registry import registry


@registry.register_detector("volcengine")
def _detect_volcengine_cloudmonitor(data: dict[str, Any]) -> bool:
    if not isinstance(data, dict):
        return False
    namespace = str(data.get("Namespace") or "")
    if not namespace.startswith("VCM_"):
        return False
    resources = data.get("Resources")
    return isinstance(resources, list) and bool(resources)


@registry.register("volcengine", aliases={"volc", "volcengine_cloudmonitor", "cloudmonitor", "vcm"})
def _normalize_volcengine_cloudmonitor(data: dict[str, Any]) -> dict[str, Any]:
    return dict(data)

