from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from tests.helpers.metric_helpers import MetricCall, StubMetric


@pytest.mark.asyncio
async def test_taskiq_worker_lifecycle_initializes_and_stops_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    from core import taskiq_broker

    startup_calls: list[dict[str, object]] = []
    shutdown_calls: list[dict[str, object]] = []
    sleeps: list[float] = []
    contexts = [SimpleNamespace(config=SimpleNamespace(name="config"))]

    monkeypatch.setattr(
        taskiq_broker,
        "_settings",
        SimpleNamespace(run_mode="scheduler", worker_startup_jitter_seconds=0.0),
    )
    await taskiq_broker.worker_startup_event(object())
    await taskiq_broker.worker_shutdown_event(object())

    monkeypatch.setattr(
        taskiq_broker,
        "_settings",
        SimpleNamespace(run_mode="worker", worker_startup_jitter_seconds=2.0),
    )
    monkeypatch.setattr(taskiq_broker._jitter_rng, "uniform", lambda _a, _b: 1.25)

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(taskiq_broker.asyncio, "sleep", fake_sleep)

    def init_context(_config: object) -> object:
        return contexts[0]

    async def start_services(config: object, **kwargs: object) -> None:
        startup_calls.append({"config": config, **kwargs})

    async def stop_services(config: object, **kwargs: object) -> None:
        shutdown_calls.append({"config": config, **kwargs})

    monkeypatch.setattr("core.app_context.init_default_app_context", init_context)
    monkeypatch.setattr("core.app_context.get_default_app_context", lambda: contexts[0])
    lifecycle = ModuleType("core.service_lifecycle")
    lifecycle.start_runtime_services = start_services
    lifecycle.stop_runtime_services = stop_services
    monkeypatch.setitem(sys.modules, "core.service_lifecycle", lifecycle)
    observability = ModuleType("core.observability")
    observability.setup_observability = lambda: startup_calls.append({"setup_observability": True})
    observability.shutdown_observability = lambda: shutdown_calls.append({"shutdown": True})
    monkeypatch.setitem(sys.modules, "core.observability", observability)
    ai_llm_client = ModuleType("services.analysis.ai_llm_client")
    ai_llm_client.initialize_openai_client = lambda *_args: None
    ai_llm_client.reset_openai_client = lambda *_args: None
    monkeypatch.setitem(sys.modules, "services.analysis.ai_llm_client", ai_llm_client)

    await taskiq_broker.worker_startup_event(object())
    await taskiq_broker.worker_shutdown_event(object())

    assert sleeps == [1.25]
    assert startup_calls[0]["initialize_redis_client"] is True
    assert startup_calls[0]["initialize_ai_client"] is True
    assert shutdown_calls[0]["reset_ai_client"] is True
    assert shutdown_calls[-1] == {"shutdown": True}


@pytest.mark.asyncio
async def test_outbox_worker_scheduling_deliver_and_process_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    from models import ForwardOutbox
    from services.forwarding import outbox

    metric_calls: list[MetricCall] = []
    monkeypatch.setattr(outbox, "FORWARD_OUTBOX_RECORDS_TOTAL", StubMetric(metric_calls, "RECORDS"))
    monkeypatch.setattr(outbox, "FORWARD_OUTBOX_PROCESS_DURATION_SECONDS", StubMetric(metric_calls, "DURATION"))

    class Task:
        def __init__(self, fail_on: int | None = None) -> None:
            self.fail_on = fail_on
            self.calls: list[int] = []

        async def kiq(self, *, outbox_id: int) -> None:
            self.calls.append(outbox_id)
            if outbox_id == self.fail_on:
                raise RuntimeError("schedule failed")

    task = Task(fail_on=2)
    monkeypatch.setattr("services.operations.tasks.process_forward_outbox_task", task)
    await outbox.schedule_forward_outbox_many([])
    await outbox.schedule_forward_outbox_many([1, 2])
    assert task.calls == [1, 2]

    retry_calls: list[tuple[int, int]] = []

    async def retry_schedule(outbox_id: int, delay_seconds: int) -> None:
        retry_calls.append((outbox_id, delay_seconds))
        if outbox_id == 4:
            raise RuntimeError("retry failed")

    monkeypatch.setattr("services.operations.taskiq_retry_scheduler.schedule_forward_outbox", retry_schedule)
    await outbox.schedule_forward_outbox_retry(3, 5)
    await outbox.schedule_forward_outbox_retry(4, 7)
    assert retry_calls == [(3, 5), (4, 7)]

    claimed = ForwardOutbox(id=10, target_type="webhook", channel_name="webhook")
    claim_results = [None, claimed, claimed, claimed]
    finalized: list[tuple[str, int, object]] = []

    async def claim(_outbox_id: int, **_kwargs: object) -> object:
        return claim_results.pop(0)

    async def deliver(record: ForwardOutbox) -> dict[str, object]:
        if len(finalized) == 0:
            raise RuntimeError("deliver boom")
        if len(finalized) == 1:
            return {"status": "failed", "message": "nope"}
        return {"status": "success"}

    async def success(record: ForwardOutbox, result: dict[str, object]) -> None:
        finalized.append(("success", record.id, result["status"]))

    async def failure(outbox_id: int, message: str, **_kwargs: object) -> None:
        finalized.append(("failure", outbox_id, message))

    monkeypatch.setattr(outbox, "_claim_outbox", claim)
    monkeypatch.setattr(outbox, "deliver_outbox_record", deliver)
    monkeypatch.setattr(outbox, "_finalize_outbox_success", success)
    monkeypatch.setattr(outbox, "_finalize_outbox_failure", failure)

    assert await outbox._deliver_one(1, policy=object()) == {"status": "not_claimed", "outbox_id": 1}
    failed_exception = await outbox._deliver_one(10, policy=object())
    failed_status = await outbox._deliver_one(10, policy=object())
    ok = await outbox._deliver_one(10, policy=object())
    assert failed_exception["status"] == "failed"
    assert failed_status["status"] == "failed"
    assert ok["status"] == "success"
    assert finalized[0][0] == "failure"
    assert finalized[-1][0] == "success"

    process_claims = [None, claimed, claimed, claimed]

    async def process_claim(_outbox_id: int, **_kwargs: object) -> object:
        return process_claims.pop(0)

    process_results = [RuntimeError("process boom"), {"status": "failed", "message": "bad"}, {"status": "success"}]

    async def process_deliver(_record: ForwardOutbox) -> object:
        item = process_results.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(outbox, "_claim_outbox", process_claim)
    monkeypatch.setattr(outbox, "deliver_outbox_record", process_deliver)
    await outbox.process_forward_outbox_by_id(99)
    await outbox.process_forward_outbox_by_id(10)
    await outbox.process_forward_outbox_by_id(10)
    await outbox.process_forward_outbox_by_id(10)
    statuses = [args[1] for name, args, _kwargs, action, _value in metric_calls if name == "RECORDS" and action == "inc"]
    assert {"not_claimed", "failed", "sent"} <= set(statuses)


@pytest.mark.asyncio
async def test_deliver_outbox_record_openclaw_feishu_remote_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    from models import ForwardOutbox
    from services.forwarding import outbox

    async def openclaw_forward(data: dict[str, object], analysis: dict[str, object]) -> dict[str, object]:
        assert data["source"] == "prometheus"
        assert analysis["importance"] == "high"
        return {"status": "pending"}

    async def send_feishu(url: str, payload: dict[str, object]) -> dict[str, object]:
        assert "feishu" in url
        assert payload
        return {"status": "success", "channel": "feishu"}

    async def post_remote(url: str, payload: dict[str, object], **kwargs: object) -> dict[str, object]:
        assert url in {"https://remote.test/hook", "https://empty.test/hook"}
        if url == "https://remote.test/hook":
            assert payload["is_periodic_reminder"] is True
        else:
            assert payload == {}
        assert kwargs["target_type_label"] == "webhook"
        return {"status": "success", "channel": "remote"}

    monkeypatch.setattr("services.analysis.openclaw.forward_to_openclaw", openclaw_forward)
    monkeypatch.setattr("services.notifications.feishu.is_feishu_url", lambda url: "feishu" in url)
    monkeypatch.setattr("services.notifications.feishu.build_feishu_card", lambda *_args, **_kwargs: {"card": True})
    monkeypatch.setattr("services.notifications.feishu.send_to_feishu", send_feishu)
    monkeypatch.setattr("services.forwarding.circuit_breakers.build_remote_forward_dependencies", lambda _url: "deps")
    monkeypatch.setattr("services.forwarding.remote.post_json_to_remote", post_remote)

    openclaw_record = ForwardOutbox(
        target_type="openclaw",
        channel_name="openclaw",
        forward_data={"source": "prometheus", "parsed_data": {"RuleName": "HighCPU"}},
        analysis_result={"importance": "high"},
    )
    feishu_record = ForwardOutbox(
        target_type="webhook",
        channel_name="webhook",
        target_url="https://feishu.test/hook",
        forward_data={"source": "prometheus", "parsed_data": {"RuleName": "HighCPU"}},
        analysis_result={"importance": "high"},
    )
    remote_record = ForwardOutbox(
        target_type="webhook",
        channel_name="webhook",
        target_url="https://remote.test/hook",
        forward_data={"source": "prometheus", "parsed_data": {"RuleName": "HighCPU"}},
        analysis_result={"importance": "high"},
        is_periodic_reminder=True,
    )
    empty_record = ForwardOutbox(target_type="webhook", channel_name="webhook", target_url="https://empty.test/hook")

    assert (await outbox.deliver_outbox_record(openclaw_record))["status"] == "pending"
    assert (await outbox.deliver_outbox_record(feishu_record))["channel"] == "feishu"
    assert (await outbox.deliver_outbox_record(remote_record))["channel"] == "remote"
    assert (await outbox.deliver_outbox_record(empty_record))["channel"] == "remote"


def test_forward_rule_matching_payload_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.webhooks import decisioning
    from services.webhooks.decisioning import ForwardRuleSnapshot

    metric_calls: list[MetricCall] = []
    monkeypatch.setattr(decisioning, "FORWARD_RULE_MATCH_TOTAL", StubMetric(metric_calls, "RULES"))

    def rule(**overrides: object) -> ForwardRuleSnapshot:
        values: dict[str, object] = {
            "id": 1,
            "name": "rule",
            "match_event_type": "alert",
            "match_importance": "high",
            "match_source": "prometheus",
            "match_duplicate": "new",
            "match_payload": "labels.service=api,nested=hit",
            "target_type": "webhook",
            "target_url": "https://target.test/hook",
            "stop_on_match": False,
            "target_name": "target",
        }
        values.update(overrides)
        return ForwardRuleSnapshot(**values)

    payload = {"labels": {"service": "api"}, "items": [{"nested": "hit"}]}
    assert decisioning._find_in_payload(payload, "") is None
    assert decisioning._find_in_payload(payload, "nested") == "hit"
    assert decisioning._get_by_path(payload, "") is None
    assert decisioning._get_by_path(payload, "labels.missing") is None
    assert decisioning._payload_matches("", payload)
    assert not decisioning._payload_matches("badpair", payload)
    assert not decisioning._payload_matches("=value", payload)
    assert not decisioning._payload_matches("missing=value", payload)
    assert not decisioning._rule_matches(rule(), event_type="other", importance="high", source="prometheus")
    assert not decisioning._rule_matches(rule(), event_type="alert", importance="low", source="prometheus")
    assert not decisioning._rule_matches(rule(), event_type="alert", importance="high", source="other")
    assert not decisioning._rule_matches(rule(), event_type="alert", importance="high", source="prometheus", is_duplicate=True)
    assert decisioning._rule_matches(
        rule(match_duplicate="all"),
        event_type="alert",
        importance="high",
        source="prometheus",
        parsed_data=payload,
    )

    selected = decisioning.select_forward_rules(
        [rule(match_duplicate="all"), rule(name="stop", stop_on_match=True), rule(name="after")],
        event_type="alert",
        importance="high",
        source="prometheus",
        parsed_data=payload,
    )
    assert [item.name for item in selected] == ["rule", "stop"]
    assert metric_calls


def test_taskiq_wiring_exports_registered_entrypoints() -> None:
    import services.operations.taskiq_wiring as wiring

    assert wiring.__all__ == ("broker", "dynamic_schedule_source", "scheduler")
    assert wiring.broker is not None
    assert wiring.dynamic_schedule_source is not None
    assert wiring.scheduler is not None
