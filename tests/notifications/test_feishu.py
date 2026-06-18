from __future__ import annotations

import json
from typing import Any

import pytest


def test_feishu_url_accepts_only_trusted_hosts() -> None:
    from services.notifications.feishu import is_feishu_url

    assert is_feishu_url("https://open.feishu.cn/open-apis/bot/v2/hook/token")
    assert is_feishu_url("https://tenant.larksuite.com/open-apis/bot/v2/hook/token")
    assert is_feishu_url("https://feishu.cn/open-apis/bot/v2/hook/token")

    assert not is_feishu_url("https://feishu.cn.evil.example/open-apis/bot/v2/hook/token")
    assert not is_feishu_url("https://example.com/open-apis/bot/v2/hook/token")
    assert not is_feishu_url("not a url")


def test_build_feishu_card_formats_identity_and_periodic_reminder() -> None:
    from services.notifications.feishu import build_feishu_card

    card = build_feishu_card(
        {
            "source": "volcengine",
            "timestamp": "2026-06-03T01:02:03Z",
            "parsed_data": {
                "RuleName": "HighCPU",
                "Type": "MetricAlert",
                "Resources": [
                    {
                        "ProjectName": "eve-cn",
                        "Region": "cn-shanghai",
                        "Name": "api-0",
                        "Id": "i-001",
                        "Metrics": [{"Name": "cpu_usage"}],
                    }
                ],
                "Level": "critical",
            },
        },
        {
            "importance": "critical",
            "summary": "CPU 持续超过阈值",
            "impact_scope": "api-0 请求延迟升高",
            "alert_identity": {"service": "webhook-api", "status": "firing"},
        },
        is_periodic_reminder=True,
    )

    rendered = str(card)
    assert card["msg_type"] == "interactive"
    assert card["card"]["header"]["title"]["content"] == "🔁 [周期提醒] 📡 告警通知"
    assert card["card"]["header"]["template"] == "red"
    # Identity is one breadcrumb line: location · service · resource(+id) · rule · metric · severity.
    assert "🏷️ eve-cn/cn-shanghai · webhook-api · api-0 i-001 · HighCPU · cpu_usage · critical" in rendered
    # The headline leads with the importance tag + summary.
    assert "CPU 持续超过阈值" in rendered
    # source · type · time live in the de-emphasized footer note.
    assert "2026-06-03 09:02:03 UTC+8" in rendered


def test_build_deep_analysis_card_extracts_openclaw_json_text() -> None:
    from services.notifications.feishu import build_deep_analysis_card
    from services.webhooks.types import OPENCLAW_TEXT

    openclaw_text = """```json
{
  "summary": "在线用户数处于早高峰爬坡，无实际风险。",
  "root_cause": {"description": "短窗口基线偏低导致波动率误报。"},
  "impact": {"description": "服务正常，无业务影响。"},
  "recommendations": [{"action": "改用日周期基线", "reason": "降低误报"}],
  "confidence": 0.87
}
```"""

    card = build_deep_analysis_card(
        {"analysis_result": {OPENCLAW_TEXT: openclaw_text}, "engine": "openclaw", "duration_seconds": 12.3},
        source="prometheus",
        webhook_event_id=46708,
    )

    rendered = str(card)
    assert "```json" not in rendered
    assert "在线用户数处于早高峰爬坡" in rendered
    assert "短窗口基线偏低" in rendered
    assert "改用日周期基线" in rendered
    assert "置信度：87%" in rendered
    assert "ID：46708" in rendered


def test_build_deep_analysis_card_formats_root_cause_json_report() -> None:
    from services.notifications.feishu import build_deep_analysis_card

    report = {
        "alert_identity": {
            "source": "volcengine",
            "project": "eve-cn",
            "region": "cn-shanghai",
            "namespace": "VCM_ESCloud",
            "service": "ESCloud",
            "resource_name": "eve-cn-prod-es",
            "resource_id": "o-00soqlw9sik6",
            "rule_name": "云搜索服务实例告警策略_自定义",
            "metric_name": "InstanceHealthState",
            "severity": "warning",
            "status": "firing",
        },
        "summary": "ES集群 eve-cn-prod-es 自 2026-06-03 23:38 CST 起处于 yellow 状态。",
        "root_cause": {
            "status": "confirmed",
            "description": "数据节点约 12.3 小时前发生重启，部分分片恢复失败。",
        },
        "impact_scope": "副本分片未完全恢复，读写正常但容错能力下降。",
        "recommendations": ["检查重启节点 JVM/磁盘水位", "手动重试失败分片"],
        "evidence": ["InstanceHealthState=warning"],
        "confidence": 0.91,
    }
    fenced_report = "```json\n" + json.dumps(report, ensure_ascii=False, indent=2) + "\n```"

    card = build_deep_analysis_card(
        {"analysis_result": {"root_cause": fenced_report}, "engine": "openclaw", "duration_seconds": 417.06},
        source="volcengine",
        webhook_event_id=47332,
    )

    rendered = str(card)
    assert "```json" not in rendered
    assert '"alert_identity"' not in rendered
    assert "ES集群 eve-cn-prod-es" in rendered
    assert "数据节点约 12.3 小时前发生重启" in rendered
    assert "副本分片未完全恢复" in rendered
    assert "检查重启节点 JVM/磁盘水位" in rendered
    assert "项目: eve-cn" in rendered
    assert "资源: eve-cn-prod-es" in rendered
    assert "指标: InstanceHealthState" in rendered
    assert "置信度：91%" in rendered


@pytest.mark.asyncio
async def test_send_to_feishu_uses_feishu_timeout_and_circuit_breaker(
    monkeypatch: pytest.MonkeyPatch, temp_config: Any
) -> None:
    from services.forwarding import remote
    from services.forwarding.circuit_breakers import RemoteForwardDependencies
    from services.notifications import feishu

    client = object()

    async def validate_url(url: str, **kwargs: Any) -> str:
        return url

    captured: dict[str, Any] = {}
    temp_config.notifications.FEISHU_WEBHOOK_TIMEOUT_SECONDS = 23

    monkeypatch.setattr(
        feishu,
        "build_remote_forward_dependencies",
        lambda: RemoteForwardDependencies(
            http_client=client,
            circuit_breaker=object(),
            validate_url=validate_url,
        ),
    )

    async def fake_post_json_to_remote(
        target_url: str,
        payload: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        captured.update({"target_url": target_url, "payload": payload, **kwargs})
        return {"status": "success", "status_code": 200}

    monkeypatch.setattr(remote, "post_json_to_remote", fake_post_json_to_remote)

    result = await feishu.send_to_feishu("https://open.feishu.cn/open-apis/bot/v2/hook/unit", {"msg": "ok"})

    assert result == {"status": "success", "status_code": 200}
    assert captured["target_url"] == "https://open.feishu.cn/open-apis/bot/v2/hook/unit"
    assert captured["payload"] == {"msg": "ok"}
    assert captured["validate_target"] is True
    assert captured["target_type_label"] == "feishu"
    assert captured["policy"].timeout_seconds == 23
    assert captured["dependencies"].http_client is client
    assert captured["dependencies"].circuit_breaker is feishu.feishu_cb
    assert captured["dependencies"].validate_url is validate_url


def test_card_strips_redundant_impact_and_summary_prefix() -> None:
    """The section header already says 影响范围/事件摘要 — drop a duplicate inline label."""
    from services.notifications.feishu_cards import build_feishu_card

    card = build_feishu_card(
        {"source": "volcengine", "timestamp": "2026-06-18T01:00:00Z", "parsed_data": {}},
        {
            "importance": "high",
            "summary": "摘要：桶错误率 36%",
            "impact_scope": "影响范围：存储桶 volc-flink-meta，可能影响 checkpoint 保存。",
        },
    )
    rendered = str(card)
    # The leading "影响范围：" / "摘要：" prefix is removed; the content remains.
    assert "影响范围：存储桶" not in rendered
    assert "摘要：桶错误率" not in rendered
    assert "存储桶 volc-flink-meta，可能影响 checkpoint 保存。" in rendered
    assert "桶错误率 36%" in rendered
    # The section header itself still reads 影响范围.
    assert "**🎯 影响范围**" in rendered
