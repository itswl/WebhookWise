from __future__ import annotations

import json
from typing import Any

import pytest


def _body(response: Any) -> dict[str, Any]:
    return json.loads(response.body)


@pytest.mark.asyncio
async def test_sandbox_endpoint_returns_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.v1 import sandbox
    from schemas.sandbox import SandboxTestRequest

    data = {
        "source": {"input": "volcengine", "resolved": "volcengine", "adapter": "volcengine", "matched": True},
        "alert_hash": "a" * 64,
        "dedup_key": "b" * 64,
        "identity": {"severity": "critical"},
        "resources": [],
        "metrics": [],
        "match_fields": {"project": "p", "region": "r", "environment": ""},
        "rule_based_analysis": {"importance": "high", "event_type": "x", "summary": None, "note": "no AI"},
        "forwarding": {"should_forward": True, "skip_code": "none", "skip_reason": None, "matched_rules": [], "silenced_by": None},
        "dedup_note": "n/a",
    }

    async def fake_test(_session: object, *, source: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert source == "volcengine"
        assert payload == {"RuleName": "x"}
        return data

    monkeypatch.setattr(sandbox, "test_webhook_payload", fake_test)
    req = SandboxTestRequest(source="volcengine", payload={"RuleName": "x"})
    result = await sandbox.test_webhook_endpoint(req, session=object())  # type: ignore[arg-type]
    assert result == {"success": True, "data": data}


@pytest.mark.asyncio
async def test_sandbox_endpoint_handles_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.v1 import sandbox
    from schemas.sandbox import SandboxTestRequest

    async def boom(_session: object, *, source: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("bad payload")

    monkeypatch.setattr(sandbox, "test_webhook_payload", boom)
    req = SandboxTestRequest(source="x", payload={"a": 1})
    result = await sandbox.test_webhook_endpoint(req, session=object())  # type: ignore[arg-type]
    # Errors are degraded to a 500 envelope, never raised.
    assert result.status_code == 500
    assert _body(result)["success"] is False


def test_sandbox_request_rejects_non_object_payload() -> None:
    from pydantic import ValidationError

    from schemas.sandbox import SandboxTestRequest

    with pytest.raises(ValidationError):
        SandboxTestRequest(source="x", payload=["not", "an", "object"])  # type: ignore[arg-type]
