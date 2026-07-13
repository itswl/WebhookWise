from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException


def _body(response: Any) -> dict[str, Any]:
    return json.loads(response.body)


@pytest.mark.asyncio
async def test_admin_prompt_endpoints_normalize_kinds_and_sanitize_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api import INTERNAL_ERROR_MESSAGE
    from api.v1 import admin

    assert admin._normalize_prompt_kind(" ai ") == "user"
    assert admin._normalize_prompt_kind("deep-analysis") == "deep_analysis"
    assert admin._normalize_prompt_kind("incident") == "incident_summary"
    with pytest.raises(ValueError):
        admin._normalize_prompt_kind("other")

    async def reload_prompt_by_kind(kind: str) -> str:
        assert kind == "deep_analysis"
        return "x" * 210

    async def load_prompt_by_kind(kind: str) -> str:
        assert kind == "user"
        return "user prompt"

    monkeypatch.setattr(admin, "_reload_prompt_by_kind", reload_prompt_by_kind)
    monkeypatch.setattr(admin, "_load_prompt_by_kind", load_prompt_by_kind)
    monkeypatch.setattr(admin, "get_prompt_source", lambda kind: f"source:{kind}")

    reloaded = _body(await admin.reload_prompt(kind="deep"))
    loaded = _body(await admin.get_prompt(kind="user"))
    invalid = _body(await admin.get_prompt(kind="unsupported"))

    assert reloaded["success"] is True
    assert reloaded["kind"] == "deep_analysis"
    assert reloaded["template_length"] == 210
    assert reloaded["preview"].endswith("...")
    assert loaded["success"] is True
    assert loaded["status"] == 200
    assert loaded["kind"] == "user"
    assert loaded["template"] == "user prompt"
    assert loaded["source"] == "source:user"
    assert invalid["success"] is False
    assert invalid["error"] == "unsupported prompt kind"

    async def fail_reload(_kind: str) -> str:
        raise RuntimeError("postgresql://user:pass@db.internal/webhooks")

    monkeypatch.setattr(admin, "_reload_prompt_by_kind", fail_reload)
    failed = await admin.reload_prompt(kind="user")
    assert failed.status_code == 500
    assert _body(failed)["error"] == INTERNAL_ERROR_MESSAGE
    assert "postgresql://" not in failed.body.decode()


@pytest.mark.asyncio
async def test_admin_deep_health_reports_dependency_and_queue_state(
    monkeypatch: pytest.MonkeyPatch,
    temp_config: Any,
) -> None:
    from api.v1 import admin
    from core.app_context import AppContext

    monkeypatch.setattr(temp_config.mq, "WEBHOOK_MQ_QUEUE", "queue:test")
    monkeypatch.setattr(temp_config.mq, "WEBHOOK_MQ_CONSUMER_GROUP", "group:test")
    monkeypatch.setattr(temp_config.ai, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(temp_config.openclaw, "OPENCLAW_GATEWAY_TOKEN", "gateway-token")

    async def test_db_connection() -> bool:
        return True

    async def redis_ping() -> bool:
        return True

    monkeypatch.setattr(admin, "test_db_connection", test_db_connection)
    monkeypatch.setattr(admin, "redis_ping", redis_ping)
    monkeypatch.setattr(
        admin,
        "get_redis_health_snapshot",
        lambda: SimpleNamespace(
            state=SimpleNamespace(value="healthy"),
            consecutive_failures=0,
            last_success_at=123.0,
            last_failure_at=None,
            last_error=None,
            last_operation="ping",
        ),
    )
    monkeypatch.setattr(admin.adapter_registry, "status", lambda: {"prometheus": {"enabled": True}})

    async def redis_xlen(stream: str) -> int:
        assert stream == "queue:test"
        return 7

    async def redis_xpending_pending(stream: str, group: str) -> int:
        assert (stream, group) == ("queue:test", "group:test")
        return 2

    async def redis_xinfo_group_lag(stream: str, group: str) -> int:
        assert (stream, group) == ("queue:test", "group:test")
        return 5

    monkeypatch.setattr(admin, "redis_xlen", redis_xlen)
    monkeypatch.setattr(admin, "redis_xpending_pending", redis_xpending_pending)
    monkeypatch.setattr(admin, "redis_xinfo_group_lag", redis_xinfo_group_lag)

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(app_context=AppContext(config=temp_config))))
    response = await admin.deep_health_check(request)  # type: ignore[arg-type]
    payload = _body(response)

    assert response.status_code == 200
    assert payload["data"]["status"] == "ok"
    assert payload["data"]["queue"] == {
        "ok": True,
        "stream": "queue:test",
        "group": "group:test",
        "depth": 7,
        "pending": 2,
        "lag": 5,
    }
    assert payload["data"]["adapters"] == {"prometheus": {"enabled": True}}

    async def fail_xlen(_stream: str) -> int:
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(admin, "redis_xlen", fail_xlen)
    degraded = await admin.deep_health_check(request)  # type: ignore[arg-type]
    degraded_payload = _body(degraded)
    assert degraded.status_code == 503
    assert degraded_payload["data"]["status"] == "degraded"
    assert degraded_payload["data"]["queue"]["ok"] is False


@pytest.mark.asyncio
async def test_admin_dead_letter_listing_retry_and_replay_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api import INTERNAL_ERROR_MESSAGE
    from api.v1 import admin

    event = SimpleNamespace(
        id=1,
        source="prometheus",
        headers={"Authorization": "redacted"},
        client_ip="203.0.113.10",
        request_id="req-1",
        timestamp=datetime(2026, 5, 27, 12, 0, tzinfo=UTC),
        retry_count=2,
        processing_status="dead_letter",
    )
    enqueued: list[dict[str, object]] = []

    _all_events = {
        1: event,
        2: SimpleNamespace(id=2, processing_status="completed"),
    }

    class Session:
        async def get(self, _model: object, event_id: int) -> object | None:
            return _all_events.get(event_id)

        async def execute(self, _stmt: object) -> object:
            # Batch replay does select(WebhookEvent).where(id.in_(...)); return
            # all known events (the code filters by processing_status itself).
            rows = list(_all_events.values())
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    async def list_dead_letters(
        _session: object,
        *,
        page: int,
        page_size: int,
        source: str | None = None,
        search: str | None = None,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
    ) -> list[dict[str, int]]:
        assert (page, page_size) == (1, 50)
        assert source == "prometheus"
        assert search == "HighCPU"
        assert time_from == datetime(2026, 5, 27, 0, 0)
        assert time_to == datetime(2026, 5, 28, 0, 0)
        return [{"id": 1}, {"id": 2}]

    async def count_dead_letters(
        _session: object,
        *,
        source: str | None = None,
        search: str | None = None,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
    ) -> int:
        assert source == "prometheus"
        assert search == "HighCPU"
        assert time_from == datetime(2026, 5, 27, 0, 0)
        assert time_to == datetime(2026, 5, 28, 0, 0)
        return 2

    async def get_dead_letter_detail(_session: object, event_id: int) -> dict[str, object] | None:
        if event_id == 1:
            return {"id": 1, "source": "prometheus", "raw_body": '{"alertname":"HighCPU"}'}
        return None

    async def load_event_payload(_event: object) -> tuple[dict[str, object], str]:
        return {}, '{"alertname":"HighCPU"}'

    async def kiq(**kwargs: object) -> None:
        enqueued.append(dict(kwargs))

    monkeypatch.setattr(admin, "list_dead_letters", list_dead_letters)
    monkeypatch.setattr(admin, "count_dead_letters", count_dead_letters)
    monkeypatch.setattr(admin, "get_dead_letter_detail", get_dead_letter_detail)
    monkeypatch.setattr(admin, "load_event_payload", load_event_payload)
    monkeypatch.setattr(admin.process_webhook_task, "kiq", kiq)

    listed = _body(
        await admin.get_dead_letters_endpoint(
            page=1,
            page_size=50,
            source="prometheus",
            search="HighCPU",
            time_from="2026-05-27T00:00:00Z",
            time_to="2026-05-28T00:00:00Z",
            session=object(),  # type: ignore[arg-type]
        )
    )
    assert listed["success"] is True
    assert listed["data"] == [{"id": 1}, {"id": 2}]
    assert listed["pagination"] == {"page": 1, "page_size": 50, "total": 2}

    invalid_time = await admin.get_dead_letters_endpoint(time_from="not-a-date", session=object())  # type: ignore[arg-type]
    assert invalid_time.status_code == 400

    detail = _body(await admin.get_dead_letter_detail_endpoint(1, session=object()))  # type: ignore[arg-type]
    assert detail["success"] is True
    assert detail["data"]["raw_body"] == '{"alertname":"HighCPU"}'

    detail_missing = await admin.get_dead_letter_detail_endpoint(404, session=object())  # type: ignore[arg-type]
    assert detail_missing.status_code == 404

    async def requeue_ok(_outbox_id: int) -> bool:
        return True

    monkeypatch.setattr(admin, "requeue_forward_outbox", requeue_ok)
    retry_ok = _body(await admin.retry_outbox_endpoint(10))
    assert retry_ok["message"] == "outbox re-enqueued"
    assert retry_ok["data"] == {"outbox_id": 10}

    async def requeue_missing(_outbox_id: int) -> bool:
        return False

    monkeypatch.setattr(admin, "requeue_forward_outbox", requeue_missing)
    retry_missing = await admin.retry_outbox_endpoint(10)
    assert retry_missing.status_code == 400

    replay = _body(await admin.replay_single_dead_letter(1, session=Session()))  # type: ignore[arg-type]
    assert replay["success"] is True
    assert replay["event_id"] == 1
    assert enqueued[-1]["source_name"] == "prometheus"
    assert enqueued[-1]["raw_body"] == '{"alertname":"HighCPU"}'

    replay_missing = await admin.replay_single_dead_letter(3, session=Session())  # type: ignore[arg-type]
    assert replay_missing.status_code == 404

    batch_replay = _body(
        await admin.replay_dead_letter_batch(
            admin.ReplayBatchRequest(event_ids=[1, 2, 1]),
            session=Session(),  # type: ignore[arg-type]
        )
    )
    assert batch_replay["replayed"] == 1
    assert batch_replay["event_ids"] == [1]
    assert batch_replay["skipped_event_ids"] == [2]

    async def list_dead_letters_for_replay_all(_session: object, *, page: int, page_size: int) -> list[dict[str, int]]:
        assert (page, page_size) == (1, 50)
        return [{"id": 1}, {"id": 2}]

    monkeypatch.setattr(admin, "list_dead_letters", list_dead_letters_for_replay_all)
    replay_all = _body(await admin.replay_all_dead_letters(batch_size=50, session=Session()))  # type: ignore[arg-type]
    assert replay_all["replayed"] == 1
    assert replay_all["event_ids"] == [1]

    async def fail_list(*_: object, **__: object) -> list[dict[str, int]]:
        raise RuntimeError("postgresql://user:pass@db.internal/webhooks")

    monkeypatch.setattr(admin, "list_dead_letters", fail_list)
    failed = await admin.get_dead_letters_endpoint(page=1, page_size=50, session=object())  # type: ignore[arg-type]
    assert failed.status_code == 500
    assert _body(failed)["error"] == INTERNAL_ERROR_MESSAGE
    assert "postgresql://" not in failed.body.decode()


@pytest.mark.asyncio
async def test_reanalysis_updates_original_duplicates_and_schedules_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.v1 import reanalysis

    event = SimpleNamespace(
        id=99,
        source="grafana",
        importance="low",
        ai_analysis={},
        is_duplicate=False,
        processing_status="completed",
    )
    scheduled: list[list[int]] = []

    class Session:
        def __init__(self) -> None:
            self.commits = 0
            self.executed: list[object] = []

        async def get(self, _model: object, webhook_id: int) -> object | None:
            return event if webhook_id == 99 else None

        async def execute(self, stmt: object) -> object:
            # Duplicates are now updated via a single bulk UPDATE that reports
            # how many rows it touched through rowcount.
            self.executed.append(stmt)
            return SimpleNamespace(rowcount=1)

        async def commit(self) -> None:
            self.commits += 1

    session = Session()

    async def build_webhook_context(_event: object) -> dict[str, object]:
        return {"source": "grafana", "parsed_data": {"alertname": "DiskFull"}}

    async def analyze_webhook_with_ai(_ctx: object, *, skip_cache: bool) -> dict[str, object]:
        assert skip_cache is True
        return {"summary": "new analysis", "importance": "high"}

    async def resolve_forward_decision(**kwargs: object) -> object:
        assert kwargs["importance"] == "high"
        return SimpleNamespace(should_forward=True, skip_reason="")

    async def resolve_and_forward(**kwargs: object) -> dict[str, object]:
        assert kwargs["webhook_id"] == 99
        return {"outbox_ids": [101, 102]}

    async def schedule_forward_outbox_many(outbox_ids: list[int]) -> None:
        scheduled.append(outbox_ids)

    # The reanalysis workflow now lives in services.webhooks.reanalysis_service;
    # patch the collaborators where they are looked up.
    from services.webhooks import reanalysis_service

    monkeypatch.setattr(reanalysis_service, "build_webhook_context", build_webhook_context)
    monkeypatch.setattr(reanalysis_service, "analyze_webhook_with_ai", analyze_webhook_with_ai)
    monkeypatch.setattr(reanalysis_service, "resolve_forward_decision", resolve_forward_decision)
    monkeypatch.setattr(reanalysis_service, "resolve_and_forward", resolve_and_forward)
    monkeypatch.setattr(reanalysis_service, "schedule_forward_outbox_many", schedule_forward_outbox_many)

    result = await reanalysis.reanalyze_webhook(99, session=session)  # type: ignore[arg-type]

    assert result["success"] is True
    assert result["original_importance"] == "low"
    assert result["new_importance"] == "high"
    assert result["updated_duplicates"] == 1
    assert result["forward_outbox_ids"] == [101, 102]
    assert event.importance == "high"
    assert scheduled == [[101, 102]]
    assert session.commits == 1
    # A bulk UPDATE for the duplicates was issued (not a per-row load+mutate).
    assert any(type(stmt).__name__ == "Update" for stmt in session.executed)


@pytest.mark.asyncio
async def test_reanalysis_error_paths_are_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    from api import INTERNAL_ERROR_MESSAGE
    from api.v1 import reanalysis

    class MissingSession:
        async def get(self, _model: object, _webhook_id: int) -> None:
            return None

    with pytest.raises(HTTPException):
        await reanalysis.reanalyze_webhook(1, session=MissingSession())  # type: ignore[arg-type]

    class BrokenSession:
        async def get(self, _model: object, _webhook_id: int) -> object:
            raise RuntimeError("postgresql://user:pass@db.internal/webhooks")

    failed = await reanalysis.reanalyze_webhook(1, session=BrokenSession())  # type: ignore[arg-type]
    assert failed.status_code == 500
    assert _body(failed)["error"] == INTERNAL_ERROR_MESSAGE
    assert "postgresql://" not in failed.body.decode()


@pytest.mark.asyncio
async def test_manual_forward_webhook_handles_success_skipped_delivery_and_url_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api import DELIVERY_ERROR_MESSAGE, TARGET_URL_UNAVAILABLE_MESSAGE
    from api.v1 import reanalysis
    from core.url_security import UnsafeTargetUrlError

    event = SimpleNamespace(
        id=7,
        source="prometheus",
        ai_analysis={"summary": "ok"},
        importance="medium",
        is_duplicate=False,
        forward_status="",
    )

    class Session:
        def __init__(self) -> None:
            self.commits = 0

        async def get(self, _model: object, webhook_id: int) -> object | None:
            return event if webhook_id == 7 else None

        async def commit(self) -> None:
            self.commits += 1

    session = Session()
    forward_results = [{"status": "success", "message": "sent"}]

    async def build_webhook_context(_event: object) -> dict[str, object]:
        return {"source": "prometheus", "parsed_data": {"alertname": "HighCPU"}}

    async def forward_notification(**kwargs: object) -> dict[str, object]:
        assert kwargs["event_type"] == "manual_forward"
        assert kwargs["wait"] is True
        return forward_results.pop(0)

    async def validate_outbound_url(url: str) -> str:
        if "blocked" in url:
            raise UnsafeTargetUrlError("target host resolves to a non-public IP")
        return f"{url}/validated"

    monkeypatch.setattr(reanalysis, "build_webhook_context", build_webhook_context)
    monkeypatch.setattr(reanalysis, "forward_notification", forward_notification)
    monkeypatch.setattr(reanalysis, "validate_outbound_url", validate_outbound_url)

    success = await reanalysis.manual_forward_webhook(
        7,
        data={"target_url": "https://example.com/hook"},
        session=session,  # type: ignore[arg-type]
    )
    assert success["success"] is True
    assert event.forward_status == "success"
    assert session.commits == 1

    forward_results.append({"status": "skipped", "reason": "rule"})
    skipped = await reanalysis.manual_forward_webhook(7, data={}, session=session)  # type: ignore[arg-type]
    assert skipped.status_code == 400
    assert _body(skipped)["error"] == "Forwarding skipped"

    forward_results.append({"status": "failed", "message": "boom"})
    failed = await reanalysis.manual_forward_webhook(7, data={}, session=session)  # type: ignore[arg-type]
    assert failed.status_code == 502
    assert _body(failed)["error"] == DELIVERY_ERROR_MESSAGE

    invalid = await reanalysis.manual_forward_webhook(
        7,
        data={"target_url": "ftp://example.com/hook"},
        session=session,  # type: ignore[arg-type]
    )
    assert invalid.status_code == 400
    assert _body(invalid)["error"] == "Invalid URL format"

    unsafe = await reanalysis.manual_forward_webhook(
        7,
        data={"target_url": "https://blocked.example/hook"},
        session=session,  # type: ignore[arg-type]
    )
    assert unsafe.status_code == 400
    assert _body(unsafe)["error"] == TARGET_URL_UNAVAILABLE_MESSAGE
    assert "non-public" not in unsafe.body.decode()
