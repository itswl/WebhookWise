from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.mark.asyncio
async def test_ai_cache_hit_marks_result_and_save_strips_internal_keys(
    monkeypatch: pytest.MonkeyPatch,
    temp_config: Any,
) -> None:
    import core.redis_client as redis_client
    from core import json
    from services.analysis.ai_cache import get_cache_key, get_cached_analysis, save_to_cache

    monkeypatch.setattr(temp_config.ai, "CACHE_ENABLED", True)
    monkeypatch.setattr(temp_config.ai, "ANALYSIS_CACHE_TTL_SECONDS", 300)

    # Key includes a model+prompt fingerprint, so derive expected keys via the
    # public helper rather than hardcoding the literal format.
    key_alert_1 = get_cache_key("alert-1")
    key_alert_2 = get_cache_key("alert-2")

    get_calls: list[tuple[str, int]] = []
    writes: dict[str, object] = {}

    async def redis_get_str(key: str) -> str | None:
        assert key == key_alert_1
        return '{"summary":"cached","importance":"high"}'

    async def redis_incr_with_expire(key: str, ttl_seconds: int) -> int:
        get_calls.append((key, ttl_seconds))
        return 4

    async def redis_eval_int(_script: str, _numkeys: int, *args: object) -> int:
        # AI_CACHE_SAVE: KEYS=[blob, counter], ARGV=[ttl, blob bytes]
        blob_key, counter_key = str(args[0]), str(args[1])
        ttl_seconds, blob = int(args[2]), args[3]  # type: ignore[arg-type]
        writes[blob_key] = (ttl_seconds, json.loads(blob))
        writes[f"{counter_key}:str"] = (ttl_seconds, "0")
        return 1

    monkeypatch.setattr(redis_client, "redis_get_str", redis_get_str)
    monkeypatch.setattr(redis_client, "redis_incr_with_expire", redis_incr_with_expire)
    monkeypatch.setattr(redis_client, "redis_eval_int", redis_eval_int)

    cached = await get_cached_analysis("alert-1", ttl_seconds=120)

    assert cached == {
        "summary": "cached",
        "importance": "high",
        "_cache_hit": True,
        "_cache_hit_count": 4,
    }
    assert get_calls == [(f"{key_alert_1}:hits", 120)]

    saved = await save_to_cache(
        "alert-2",
        {
            "summary": "fresh",
            "importance": "low",
            "_cache_hit": True,
            "_openclaw_session_key": "internal",
        },
        ttl_seconds=90,
    )

    assert saved is True
    assert writes[key_alert_2] == (90, {"summary": "fresh", "importance": "low"})
    assert writes[f"{key_alert_2}:hits:str"] == (90, "0")


@pytest.mark.asyncio
async def test_ai_cache_disabled_and_invalid_cached_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.redis_client as redis_client
    from services.analysis.ai_cache import get_cached_analysis, save_to_cache

    assert await get_cached_analysis("disabled", enabled=False) is None
    assert await save_to_cache("disabled", {"summary": "x"}, enabled=False) is False

    async def redis_get_str(_key: str) -> str:
        return '["not", "a", "mapping"]'

    monkeypatch.setattr(redis_client, "redis_get_str", redis_get_str)

    assert await get_cached_analysis("bad-json", enabled=True) is None


@pytest.mark.asyncio
async def test_ai_cache_redis_errors_are_nonfatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.redis_client as redis_client
    from services.analysis.ai_cache import get_cached_analysis, save_to_cache

    async def redis_get_str(_key: str) -> str:
        raise RuntimeError("redis read down")

    async def redis_eval_int(*_: object) -> int:
        raise RuntimeError("redis write down")

    monkeypatch.setattr(redis_client, "redis_get_str", redis_get_str)
    monkeypatch.setattr(redis_client, "redis_eval_int", redis_eval_int)

    assert await get_cached_analysis("read-error", enabled=True) is None
    assert await save_to_cache("write-error", {"summary": "x"}, enabled=True) is False


@pytest.mark.asyncio
async def test_send_feishu_deep_analysis_handles_missing_url_and_enqueue_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.operations import deep_analysis_notifications as notifications

    assert await notifications.send_feishu_deep_analysis("", {"analysis_result": {}}) is False

    async def forward_notification(**_: object) -> None:
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(notifications, "forward_notification", forward_notification)

    assert (
        await notifications.send_feishu_deep_analysis(
            "https://example.com/hook",
            {"analysis_result": {"summary": "x"}, "engine": "openclaw"},
        )
        is False
    )


@pytest.mark.asyncio
async def test_deep_analysis_success_notification_records_enqueue_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.operations import deep_analysis_notifications as notifications

    sent: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []

    async def send_feishu_deep_analysis(**kwargs: object) -> bool:
        sent.append(dict(kwargs))
        return False

    async def record_notification_failure(
        webhook_event_id: int,
        target_url: str,
        *,
        failure_reason: str,
        error_message: str,
        analysis_type: str,
    ) -> None:
        failures.append(
            {
                "webhook_event_id": webhook_event_id,
                "target_url": target_url,
                "failure_reason": failure_reason,
                "error_message": error_message,
                "analysis_type": analysis_type,
            }
        )

    monkeypatch.setattr(notifications, "send_feishu_deep_analysis", send_feishu_deep_analysis)
    monkeypatch.setattr(notifications, "_record_notification_failure", record_notification_failure)

    await notifications.send_deep_analysis_success_notification(
        {
            "id": 9,
            "webhook_event_id": 42,
            "analysis_result": {"root_cause": "disk full"},
            "engine": "openclaw",
            "duration_seconds": 6.5,
            notifications.EVENT_IMPORTANCE_KEY: "high",
            notifications.EVENT_IS_DUPLICATE_KEY: False,
            notifications.EVENT_PARSED_DATA_KEY: {"project": "eve-cn", "env": "prod"},
        },
        source="volcengine",
        policy=SimpleNamespace(notification_webhook_url="https://example.com/hook"),
    )

    assert sent == [
        {
            "webhook_url": "https://example.com/hook",
            "analysis_record": {
                "analysis_result": {"root_cause": "disk full"},
                "engine": "openclaw",
                "duration_seconds": 6.5,
            },
            "source": "volcengine",
            "webhook_event_id": 42,
            "importance": "high",
            "is_duplicate": False,
            "parsed_data": {"project": "eve-cn", "env": "prod"},
        }
    ]
    assert failures == [
        {
            "webhook_event_id": 42,
            "target_url": "https://example.com/hook",
            "failure_reason": "feishu_notification_failed",
            "error_message": "Failed to send deep-analysis Feishu notification",
            "analysis_type": "deep_analysis",
        }
    ]


@pytest.mark.asyncio
async def test_deep_analysis_failure_notification_marks_failed_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.operations import deep_analysis_notifications as notifications

    sent: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []

    async def send_feishu_deep_analysis(**kwargs: object) -> bool:
        sent.append(dict(kwargs))
        return False

    async def record_notification_failure(
        webhook_event_id: int,
        target_url: str,
        *,
        failure_reason: str,
        error_message: str,
        analysis_type: str,
    ) -> None:
        failures.append(
            {
                "webhook_event_id": webhook_event_id,
                "target_url": target_url,
                "failure_reason": failure_reason,
                "error_message": error_message,
                "analysis_type": analysis_type,
            }
        )

    monkeypatch.setattr(notifications, "send_feishu_deep_analysis", send_feishu_deep_analysis)
    monkeypatch.setattr(notifications, "_record_notification_failure", record_notification_failure)

    await notifications.send_deep_analysis_failure_notification(
        {
            "id": 10,
            "webhook_event_id": 43,
            "analysis_result": {"root_cause": "unknown"},
            "engine": "openclaw",
            "duration_seconds": 0,
        },
        reason="poll timeout",
        policy=SimpleNamespace(notification_webhook_url="https://example.com/hook"),
    )

    analysis_record = sent[0]["analysis_record"]
    assert analysis_record["analysis_result"] == {
        "root_cause": "unknown",
        "analysis_failed": True,
        "failure_reason": "poll timeout",
    }
    assert failures[0]["failure_reason"] == "feishu_failure_notification_failed"
    assert failures[0]["analysis_type"] == "deep_analysis_failed"
    assert "poll timeout" in str(failures[0]["error_message"])


@pytest.mark.asyncio
async def test_record_raw_ingest_dead_letter_persists_redacted_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.webhooks import ingest_failure
    from services.webhooks.types import WebhookProcessingStatus

    class FakeSession:
        def __init__(self) -> None:
            self.added: list[Any] = []

        def add(self, item: Any) -> None:
            self.added.append(item)

        async def execute(self, _stmt: Any) -> Any:
            return SimpleNamespace(first=lambda: None)

        async def flush(self) -> None:
            self.added[0].id = 77

    session = FakeSession()

    @asynccontextmanager
    async def session_scope() -> Any:
        yield session

    monkeypatch.setattr(ingest_failure, "session_scope", session_scope)

    event_id = await ingest_failure.record_raw_ingest_dead_letter(
        source="prometheus",
        raw_headers={"Authorization": "Bearer secret", "X-Trace": "keep"},
        raw_body='{"alertname":"HighCPU"}',
        client_ip="203.0.113.7",
        request_id="req-dead",
        received_at="2026-05-27T10:00:00+08:00",
        retry_count=-2,
        retryable=True,
        err=RuntimeError("x" * 3000),
    )

    event = session.added[0]
    assert event_id == 77
    assert event.source == "prometheus"
    assert event.request_id == "req-dead"
    assert event.client_ip == "203.0.113.7"
    assert event.raw_payload == b'{"alertname":"HighCPU"}'
    assert event.parsed_data == {"alertname": "HighCPU"}
    assert event.headers["Authorization"] == "[REDACTED]"
    assert event.headers["X-Trace"] == "keep"
    assert event.processing_status == WebhookProcessingStatus.DEAD_LETTER
    assert event.retry_count == 0
    assert event.failure_reason == "retry_exhausted"
    assert len(event.error_message) == 2000


@pytest.mark.asyncio
async def test_update_existing_dead_letter_marks_existing_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.webhooks import ingest_failure

    class FakeResult:
        def first(self) -> tuple[int] | None:
            return (88,)

    class FakeSession:
        def __init__(self) -> None:
            self.statements: list[Any] = []

        async def execute(self, stmt: Any) -> FakeResult:
            self.statements.append(stmt)
            return FakeResult()

    session = FakeSession()

    @asynccontextmanager
    async def session_scope() -> Any:
        yield session

    monkeypatch.setattr(ingest_failure, "session_scope", session_scope)

    event_id = await ingest_failure._update_existing_dead_letter(
        request_id="req-existing",
        retry_count=5,
        retryable=False,
        err=ValueError("bad payload"),
    )

    assert event_id == 88
    assert len(session.statements) == 1


@pytest.mark.asyncio
async def test_forwarding_target_url_validation_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.v1 import forwarding
    from core.url_security import UnsafeTargetUrlError

    seen_urls: list[str] = []

    async def validate_outbound_url(url: str) -> str:
        seen_urls.append(url)
        return "https://validated.example/hook"

    monkeypatch.setattr(forwarding, "validate_outbound_url", validate_outbound_url)

    assert await forwarding._validated_target_url("openclaw", " session-1 ") == "session-1"
    assert await forwarding._validated_target_url("webhook", "https://example.com/hook") == (
        "https://validated.example/hook"
    )
    assert seen_urls == ["https://example.com/hook"]

    with pytest.raises(UnsafeTargetUrlError):
        await forwarding._validated_target_url("webhook", "")


@pytest.mark.asyncio
async def test_forward_rule_create_and_update_endpoints_use_validated_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.v1 import forwarding
    from schemas.forwarding import ForwardRuleCreateRequest, ForwardRuleUpdateRequest

    class FakeSession:
        def __init__(self) -> None:
            self.commits = 0
            self.added: list[object] = []

        def add(self, record: object) -> None:
            self.added.append(record)

        async def commit(self) -> None:
            self.commits += 1

    session = FakeSession()
    created_rules: list[dict[str, object]] = []
    updated_rules: list[dict[str, object]] = []

    def rule(**overrides: object) -> SimpleNamespace:
        defaults: dict[str, object] = {
            "id": 12,
            "name": "pager",
            "enabled": True,
            "priority": 10,
            "match_event_type": "",
            "match_importance": "",
            "match_duplicate": "all",
            "match_source": "",
            "match_payload": "",
            "target_type": "webhook",
            "target_url": "https://safe.example/hook",
            "target_name": "",
            "stop_on_match": False,
            "created_at": None,
            "updated_at": None,
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    async def validate_target(_target_type: str, target_url: object) -> str:
        return f"{target_url}/validated"

    async def validate_enabled_target(**_: object) -> bool:
        return True

    async def create_forward_rule(**kwargs: object) -> SimpleNamespace:
        created_rules.append(dict(kwargs))
        return rule(target_url=kwargs["target_url"])

    async def get_forward_rule(*_: object, **__: object) -> SimpleNamespace:
        return rule()

    async def update_forward_rule(**kwargs: object) -> SimpleNamespace:
        updated_rules.append(dict(kwargs))
        payload = kwargs["payload"]
        return rule(name=payload["name"], target_url=payload["target_url"])

    monkeypatch.setattr(forwarding, "_validated_target_url", validate_target)
    monkeypatch.setattr(
        forwarding,
        "_validate_enabled_delivery_target",
        validate_enabled_target,
    )
    monkeypatch.setattr(forwarding, "create_forward_rule", create_forward_rule)
    monkeypatch.setattr(forwarding, "get_forward_rule", get_forward_rule)
    monkeypatch.setattr(forwarding, "update_forward_rule", update_forward_rule)

    created = await forwarding.create_forward_rule_endpoint(
        ForwardRuleCreateRequest(
            name="pager",
            target_type="webhook",
            target_url="https://safe.example/hook",
            priority=10,
        ),
        session=session,  # type: ignore[arg-type]
    )
    updated = await forwarding.update_forward_rule_endpoint(
        12,
        ForwardRuleUpdateRequest(name="pager v2", target_url="https://safe.example/next"),
        session=session,  # type: ignore[arg-type]
    )

    assert created["success"] is True
    assert updated["success"] is True
    assert session.commits == 2
    assert created_rules[0]["target_url"] == "https://safe.example/hook/validated"
    assert updated_rules[0]["payload"] == {
        "name": "pager v2",
        "target_url": "https://safe.example/next/validated",
    }


@pytest.mark.asyncio
async def test_forwarding_outbox_endpoint_returns_cursor_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.v1 import forwarding
    from services.forwarding import outbox

    calls: list[dict[str, object]] = []

    async def list_outbox_records(**kwargs: object) -> dict[str, object]:
        calls.append(dict(kwargs))
        return {"items": [{"id": 3}], "has_more": False, "next_cursor": None}

    monkeypatch.setattr(outbox, "list_outbox_records", list_outbox_records)

    response = await forwarding.list_outbox_endpoint(
        page=1,
        page_size=200,
        cursor=3,
        status="pending",
        event_type="alert",
    )

    assert response == {
        "success": True,
        "data": {"items": [{"id": 3}], "has_more": False, "next_cursor": None},
    }
    assert calls == [
        {
            "page": 1,
            "page_size": 200,
            "cursor": 3,
            "status": "pending",
            "event_type": "alert",
        }
    ]


@pytest.mark.asyncio
async def test_forwarding_outbox_endpoint_sanitizes_query_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from api import INTERNAL_ERROR_MESSAGE
    from api.v1 import forwarding
    from services.forwarding import outbox

    async def list_outbox_records(**_: object) -> dict[str, object]:
        raise RuntimeError("postgresql://user:pass@db.internal/webhooks")

    monkeypatch.setattr(outbox, "list_outbox_records", list_outbox_records)

    response = await forwarding.list_outbox_endpoint(page=1, page_size=20)
    body = json.loads(response.body)

    assert response.status_code == 500
    assert body["error"] == INTERNAL_ERROR_MESSAGE
    assert "postgresql://" not in response.body.decode()
