from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException


def _body(response: Any) -> dict[str, Any]:
    return json.loads(response.body)


def _record(**overrides: object) -> SimpleNamespace:
    data: dict[str, object] = {
        "id": 1,
        "webhook_event_id": 10,
        "engine": "openclaw",
        "user_question": "why",
        "analysis_result": {"summary": "old"},
        "duration_seconds": 1.2,
        "created_at": datetime(2026, 5, 27, 12, 0, tzinfo=UTC),
        "openclaw_run_id": "",
        "openclaw_session_key": "",
        "status": "completed",
        "poll_attempts": 0,
        "next_poll_at": None,
        "last_polled_at": None,
        "source": None,
        "is_duplicate": False,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


@pytest.mark.asyncio
async def test_deep_analyze_webhook_validation_pending_and_error_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api import INTERNAL_ERROR_MESSAGE
    from api.v1 import deep_analysis

    event = SimpleNamespace(id=10, source="prometheus", headers={"x": "1"}, parsed_data={"alertname": "HighCPU"})

    class Session:
        def __init__(self, existing: object | None) -> None:
            self.existing = existing
            self.added: list[object] = []
            self.commits = 0

        async def get(self, _model: object, _id: int) -> object | None:
            return self.existing

        def add(self, record: object) -> None:
            self.added.append(record)

        async def flush(self) -> None:
            self.added[-1].id = 501

        async def commit(self) -> None:
            self.commits += 1

    async def build_deep_analysis_context(_event: object) -> dict[str, object]:
        return {"source": "prometheus", "parsed_data": {}}

    monkeypatch.setattr(deep_analysis, "_build_deep_analysis_context", build_deep_analysis_context)
    monkeypatch.setattr(
        deep_analysis.OpenClawTriggerPolicy,
        "from_config",
        classmethod(lambda cls: SimpleNamespace(enabled=True)),
    )

    missing = await deep_analysis.deep_analyze_webhook(10, payload={}, session=Session(None))  # type: ignore[arg-type]
    assert missing.status_code == 404

    unsupported = await deep_analysis.deep_analyze_webhook(
        10,
        payload={"engine": "other"},
        session=Session(event),  # type: ignore[arg-type]
    )
    assert unsupported.status_code == 400

    monkeypatch.setattr(
        deep_analysis.OpenClawTriggerPolicy,
        "from_config",
        classmethod(lambda cls: SimpleNamespace(enabled=False)),
    )
    disabled = await deep_analysis.deep_analyze_webhook(10, payload={}, session=Session(event))  # type: ignore[arg-type]
    assert disabled.status_code == 503

    monkeypatch.setattr(
        deep_analysis.OpenClawTriggerPolicy,
        "from_config",
        classmethod(lambda cls: SimpleNamespace(enabled=True)),
    )

    async def fail_run(*_: object, **__: object) -> tuple[dict[str, object], str]:
        raise deep_analysis.deep_analysis_workflow.DeepAnalysisExecutionError(
            "postgresql://user:pass@db.internal/webhooks"
        )

    monkeypatch.setattr(deep_analysis, "_run_openclaw_deep_analysis", fail_run)
    failed = await deep_analysis.deep_analyze_webhook(10, payload={}, session=Session(event))  # type: ignore[arg-type]
    assert failed.status_code == 500
    assert _body(failed)["error"] == INTERNAL_ERROR_MESSAGE
    assert "postgresql://" not in failed.body.decode()

    scheduled: list[tuple[int, int]] = []

    async def pending_run(*_: object, **__: object) -> tuple[dict[str, object], str]:
        return {
            "status": "pending",
            "_pending": True,
            "_openclaw_run_id": "run-1",
            "_openclaw_session_key": "s-1",
        }, "openclaw"

    async def schedule_openclaw_poll_best_effort(analysis_id: int, delay: int) -> None:
        scheduled.append((analysis_id, delay))

    monkeypatch.setattr(deep_analysis, "_run_openclaw_deep_analysis", pending_run)
    monkeypatch.setattr(
        deep_analysis.taskiq_retry_scheduler, "schedule_openclaw_poll_best_effort", schedule_openclaw_poll_best_effort
    )

    session = Session(event)
    result = await deep_analysis.deep_analyze_webhook(
        10,
        payload={"user_question": "why"},
        session=session,  # type: ignore[arg-type]
    )

    assert result["success"] is True
    assert result["data"]["id"] == 501
    assert result["data"]["status"] == "pending"
    assert session.commits == 1
    assert scheduled and scheduled[0][0] == 501
    assert deep_analysis._prepare_openclaw_poll_if_pending(_record(status="completed")) is None


@pytest.mark.asyncio
async def test_openclaw_deep_analysis_helper_falls_back_and_notifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.analysis import deep_analysis_workflow
    from services.analysis import openclaw_analysis as openclaw_service

    calls: list[dict[str, object]] = []

    async def degraded_openclaw(_webhook_data: object, _question: str) -> dict[str, object]:
        return {"status": "degraded", "_degraded": True, "_degraded_reason": "timeout"}

    async def healthy_openclaw(_webhook_data: object, _question: str) -> dict[str, object]:
        return {"summary": "openclaw ok", "importance": "high"}

    async def analyze_webhook_with_ai(webhook_data: object) -> dict[str, object]:
        calls.append({"fallback": webhook_data})
        return {"summary": "local fallback", "importance": "medium"}

    monkeypatch.setattr(openclaw_service, "analyze_with_openclaw", degraded_openclaw)
    monkeypatch.setattr(deep_analysis_workflow, "analyze_webhook_with_ai", analyze_webhook_with_ai)

    fallback_result, fallback_engine = await deep_analysis_workflow.run_openclaw_deep_analysis(
        {"source": "grafana", "parsed_data": {"alertname": "HighCPU"}},
        {"x": "1"},
        "why",
    )

    assert fallback_engine == "local (fallback)"
    assert fallback_result == {"summary": "local fallback", "importance": "medium"}
    assert calls

    monkeypatch.setattr(openclaw_service, "analyze_with_openclaw", healthy_openclaw)
    result, engine = await deep_analysis_workflow.run_openclaw_deep_analysis(
        {"source": "grafana", "parsed_data": {}},
        {},
        "",
    )
    assert engine == "openclaw"
    assert result == {"summary": "openclaw ok", "importance": "high"}

    notifications: list[dict[str, object]] = []

    async def send_deep_analysis_success_notification(record: dict[str, object], source: str) -> None:
        notifications.append({"record": record, "source": source})

    class Session:
        async def get(self, _model: object, _id: int) -> object:
            return SimpleNamespace(source="grafana")

    monkeypatch.setattr(
        "services.operations.deep_analysis_notifications.send_deep_analysis_success_notification",
        send_deep_analysis_success_notification,
    )

    await deep_analysis_workflow.notify_completed_deep_analysis(
        Session(),  # type: ignore[arg-type]
        _record(id=9, webhook_event_id=10, engine="openclaw", analysis_result={"summary": "done"}),
    )

    assert notifications == [
        {
            "record": {
                "id": 9,
                "webhook_event_id": 10,
                "engine": "openclaw",
                "analysis_result": {"summary": "done"},
                "duration_seconds": 1.2,
                "_event_importance": "",
                "_event_is_duplicate": False,
                "_event_parsed_data": {},
            },
            "source": "grafana",
        }
    ]


@pytest.mark.asyncio
async def test_deep_analysis_list_and_get_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.v1 import deep_analysis

    async def get_deep_analysis_list(*args: object, **kwargs: object) -> dict[str, object]:
        assert args[1:5] == (1, 200, None, "pending")
        return {"items": [], "per_page": 200, "has_more": False}

    async def get_deep_analyses_for_webhook(_session: object, webhook_id: int, *, limit: int = 50) -> list[object]:
        return [_record(id=2, webhook_event_id=webhook_id)]

    monkeypatch.setattr(deep_analysis, "get_deep_analysis_list", get_deep_analysis_list)
    monkeypatch.setattr(deep_analysis, "get_deep_analyses_for_webhook", get_deep_analyses_for_webhook)

    listed = await deep_analysis.list_all_deep_analyses(
        page=1,
        per_page=200,
        cursor=None,
        status="pending",
        engine="openclaw",
        session=object(),  # type: ignore[arg-type]
    )
    records = await deep_analysis.get_deep_analyses(42, limit=50, session=object())  # type: ignore[arg-type]

    assert listed == {"success": True, "data": {"items": [], "per_page": 200, "has_more": False}}
    assert records["data"][0]["webhook_event_id"] == 42

    async def fail_list(*_: object, **__: object) -> dict[str, object]:
        raise ValueError("bad status")

    monkeypatch.setattr(deep_analysis, "get_deep_analysis_list", fail_list)
    with pytest.raises(HTTPException):
        await deep_analysis.list_all_deep_analyses(session=object())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_retry_deep_analysis_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.v1 import deep_analysis
    from services.webhooks.types import DeepAnalysisStatus

    event = SimpleNamespace(id=10, source="grafana", headers={}, parsed_data={"alertname": "HighCPU"})
    scheduled: list[tuple[int, int]] = []
    cleared: list[int] = []

    class Session:
        def __init__(self, record: object | None, linked_event: object | None = event) -> None:
            self.record = record
            self.linked_event = linked_event
            self.commits = 0
            self.flushes = 0

        async def get(self, model: object, _id: int) -> object | None:
            return self.linked_event if getattr(model, "__name__", "") == "WebhookEvent" else self.record

        async def flush(self) -> None:
            self.flushes += 1

        async def commit(self) -> None:
            self.commits += 1

    async def build_deep_analysis_context(_event: object) -> dict[str, object]:
        return {"source": "grafana", "parsed_data": {"alertname": "HighCPU"}}

    async def pending_run(*_: object, **__: object) -> tuple[dict[str, object], str]:
        return {
            "status": "pending",
            "_pending": True,
            "_openclaw_run_id": "run-2",
            "_openclaw_session_key": "s-2",
        }, "openclaw"

    async def completed_run(*_: object, **__: object) -> tuple[dict[str, object], str]:
        return {"summary": "done", "importance": "high"}, "openclaw"

    async def schedule_openclaw_poll_best_effort(analysis_id: int, delay: int) -> None:
        scheduled.append((analysis_id, delay))

    async def clear_openclaw_poll_state(analysis_id: int) -> None:
        cleared.append(analysis_id)

    monkeypatch.setattr(
        "services.analysis.deep_analysis_workflow.build_deep_analysis_context", build_deep_analysis_context
    )
    monkeypatch.setattr(
        "services.analysis.deep_analysis_workflow.taskiq_retry_scheduler.schedule_openclaw_poll_best_effort",
        schedule_openclaw_poll_best_effort,
    )
    monkeypatch.setattr("services.analysis.openclaw_poll.clear_openclaw_poll_state", clear_openclaw_poll_state)

    missing = await deep_analysis.retry_deep_analysis(1, session=Session(None))  # type: ignore[arg-type]
    assert missing.status_code == 404

    invalid = await deep_analysis.retry_deep_analysis(
        1,
        session=Session(_record(status="processing")),  # type: ignore[arg-type]
    )
    assert invalid.status_code == 400

    no_event = await deep_analysis.retry_deep_analysis(
        1,
        session=Session(_record(openclaw_session_key=""), linked_event=None),  # type: ignore[arg-type]
    )
    assert no_event.status_code == 404

    monkeypatch.setattr("services.analysis.deep_analysis_workflow.run_openclaw_deep_analysis", pending_run)
    pending_record = _record(id=3, status=DeepAnalysisStatus.FAILED, openclaw_session_key="")
    pending_response = await deep_analysis.retry_deep_analysis(
        3,
        session=Session(pending_record),  # type: ignore[arg-type]
    )
    assert pending_response["success"] is True
    assert pending_record.status == DeepAnalysisStatus.PENDING
    assert pending_record.openclaw_run_id == "run-2"
    assert scheduled[-1][0] == 3

    monkeypatch.setattr("services.analysis.deep_analysis_workflow.run_openclaw_deep_analysis", completed_run)

    async def skip_notification(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        "services.analysis.deep_analysis_workflow.notify_completed_deep_analysis_best_effort", skip_notification
    )
    completed_record = _record(id=4, status=DeepAnalysisStatus.FAILED, openclaw_session_key="")
    completed_response = await deep_analysis.retry_deep_analysis(
        4,
        session=Session(completed_record),  # type: ignore[arg-type]
    )
    assert completed_response["message"] == "Analysis complete"
    assert completed_record.status == DeepAnalysisStatus.COMPLETED
    assert completed_record.analysis_result == {"summary": "done", "importance": "high"}

    session_key_record = _record(id=5, status=DeepAnalysisStatus.TIMEOUT, openclaw_session_key="s-old")
    background_response = await deep_analysis.retry_deep_analysis(
        5,
        session=Session(session_key_record),  # type: ignore[arg-type]
    )
    assert background_response["success"] is True
    assert background_response["data"]["status"] == DeepAnalysisStatus.PENDING
    assert cleared == [5]
    assert scheduled[-1] == (5, 0)


@pytest.mark.asyncio
async def test_forward_deep_analysis_validation_success_and_delivery_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api import DELIVERY_ERROR_MESSAGE, TARGET_URL_UNAVAILABLE_MESSAGE
    from api.v1 import deep_analysis
    from core.url_security import UnsafeTargetUrlError
    from services.webhooks.types import DeepAnalysisStatus

    analysis = _record(
        id=8,
        webhook_event_id=10,
        status=DeepAnalysisStatus.COMPLETED,
        analysis_result={"root_cause": "disk full"},
    )
    event = SimpleNamespace(id=10, source="volcengine")
    forward_results = [{"status": "queued", "outbox_id": 77}]
    idempotency_extras: list[str] = []

    class Session:
        async def get(self, model: object, _id: int) -> object | None:
            if getattr(model, "__name__", "") == "WebhookEvent":
                return event
            return analysis

    async def validate_outbound_url(url: str) -> str:
        if "blocked" in url:
            raise UnsafeTargetUrlError("target host is not in FORWARD_TARGET_ALLOWLIST")
        return f"{url}/validated"

    async def forward_notification(**kwargs: object) -> dict[str, object]:
        assert kwargs["event_type"] == "deep_analysis_manual"
        assert kwargs["target_url"].endswith("/validated")
        extra = kwargs["idempotency_extra"]
        assert isinstance(extra, str)
        assert extra.startswith("manual-deep-analysis:8:")
        idempotency_extras.append(extra)
        return forward_results.pop(0)

    monkeypatch.setattr("services.analysis.deep_analysis_workflow.validate_outbound_url", validate_outbound_url)
    monkeypatch.setattr("services.forwarding.outbox.forward_notification", forward_notification)

    empty = await deep_analysis.forward_deep_analysis(8, payload={}, session=Session())  # type: ignore[arg-type]
    assert empty.status_code == 400

    invalid = await deep_analysis.forward_deep_analysis(
        8,
        payload={"target_url": "ftp://example.com/hook"},
        session=Session(),  # type: ignore[arg-type]
    )
    assert invalid.status_code == 400

    unsafe = await deep_analysis.forward_deep_analysis(
        8,
        payload={"target_url": "https://blocked.example/hook"},
        session=Session(),  # type: ignore[arg-type]
    )
    assert unsafe.status_code == 400
    assert _body(unsafe)["error"] == TARGET_URL_UNAVAILABLE_MESSAGE
    assert "FORWARD_TARGET_ALLOWLIST" not in unsafe.body.decode()

    class MissingSession:
        async def get(self, _model: object, _id: int) -> None:
            return None

    missing = await deep_analysis.forward_deep_analysis(
        8,
        payload={"target_url": "https://example.com/hook"},
        session=MissingSession(),  # type: ignore[arg-type]
    )
    assert missing.status_code == 404

    success = await deep_analysis.forward_deep_analysis(
        8,
        payload={"target_url": "https://example.com/hook"},
        session=Session(),  # type: ignore[arg-type]
    )
    assert success["success"] is True
    assert success["outbox_id"] == 77

    forward_results.append({"status": "skipped", "reason": "dedup"})
    skipped = await deep_analysis.forward_deep_analysis(
        8,
        payload={"target_url": "https://example.com/hook"},
        session=Session(),  # type: ignore[arg-type]
    )
    assert skipped.status_code == 400
    assert _body(skipped)["error"] == DELIVERY_ERROR_MESSAGE
    assert len(set(idempotency_extras)) == 2

    async def fail_forward_notification(**_: object) -> dict[str, object]:
        raise RuntimeError("queue down")

    monkeypatch.setattr("services.forwarding.outbox.forward_notification", fail_forward_notification)
    failed = await deep_analysis.forward_deep_analysis(
        8,
        payload={"target_url": "https://example.com/hook"},
        session=Session(),  # type: ignore[arg-type]
    )
    assert failed.status_code == 500

    analysis.status = DeepAnalysisStatus.PENDING
    not_completed = await deep_analysis.forward_deep_analysis(
        8,
        payload={"target_url": "https://example.com/hook"},
        session=Session(),  # type: ignore[arg-type]
    )
    assert not_completed.status_code == 400
