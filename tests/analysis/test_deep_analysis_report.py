from __future__ import annotations

import json


def test_normalizes_fenced_json_nested_in_root_cause() -> None:
    from contracts.deep_analysis_report import DEEP_ANALYSIS_REPORT_SCHEMA, normalize_deep_analysis_report

    upstream_report = {
        "alert_identity": {
            "source": "volcengine",
            "project": "eve-cn",
            "region": "cn-shanghai",
            "namespace": "VCM_ESCloud",
            "service": "ESCloud",
            "resource_name": "eve-cn-prod-es",
            "metric_name": "InstanceHealthState",
            "severity": "warning",
            "status": "firing",
        },
        "summary": "ES 集群处于 yellow 状态。",
        "root_cause": {
            "status": "confirmed",
            "description": "数据节点重启后部分副本分片恢复失败。",
        },
        "impact_scope": "读写正常，但副本不足导致容错能力下降。",
        "recommendations": [{"action": "重试失败分片", "reason": "恢复副本冗余"}],
        "evidence": ["InstanceHealthState=warning"],
        "next_checks": ["GET _cluster/allocation/explain"],
        "confidence": "91%",
    }
    raw = {"root_cause": "```json\n" + json.dumps(upstream_report, ensure_ascii=False, indent=2) + "\n```"}

    report = normalize_deep_analysis_report(raw).to_dict()

    assert report["schema"] == DEEP_ANALYSIS_REPORT_SCHEMA
    assert report["summary"] == "ES 集群处于 yellow 状态。"
    assert report["root_cause"] == "数据节点重启后部分副本分片恢复失败。"
    assert report["impact"] == "读写正常，但副本不足导致容错能力下降。"
    assert report["recommendations"] == ["重试失败分片（恢复副本冗余）"]
    assert report["evidence"] == ["InstanceHealthState=warning"]
    assert report["next_checks"] == ["GET _cluster/allocation/explain"]
    assert report["alert_identity"]["project"] == "eve-cn"
    assert report["alert_identity"]["resource_name"] == "eve-cn-prod-es"
    assert report["alert_identity"]["metric_name"] == "InstanceHealthState"
    assert report["confidence"] == 0.91
    assert [section["key"] for section in report["sections"]] == [
        "root_cause",
        "impact",
        "recommendations",
        "evidence",
        "next_checks",
        "alert_identity",
    ]


def test_normalizes_json_string_wrapped_by_upstream_response() -> None:
    from contracts.deep_analysis_report import normalize_deep_analysis_report

    inner = {
        "Summary": "队列积压来自下游 429。",
        "RootCause": {"Description": "Feishu webhook 被限流。"},
        "Actions": ["降低并发", "延长重试间隔"],
        "Confidence": 0.82,
    }
    payload = {
        "analysis_result": {
            "output": json.dumps(json.dumps(inner, ensure_ascii=False), ensure_ascii=False),
        }
    }

    report = normalize_deep_analysis_report(payload).to_dict()

    assert report["summary"] == "队列积压来自下游 429。"
    assert report["root_cause"] == "Feishu webhook 被限流。"
    assert report["recommendations"] == ["降低并发", "延长重试间隔"]
    assert report["confidence"] == 0.82


def test_plain_text_result_has_stable_empty_sections() -> None:
    from contracts.deep_analysis_report import normalize_deep_analysis_report

    report = normalize_deep_analysis_report("无法获取 OpenClaw 结构化结果").to_dict()

    assert report["summary"] == "无法获取 OpenClaw 结构化结果"
    assert report["primary_text"] == "无法获取 OpenClaw 结构化结果"
    assert report["source_format"] == "plain_text"
    assert report["sections"] == []


def test_top_level_json_text_string_is_classified_and_parsed() -> None:
    from contracts.deep_analysis_report import normalize_deep_analysis_report

    # A bare JSON string (no wrapping mapping) is the case where the top-level
    # parse is shared across source-format detection and candidate collection;
    # it must still classify as json_text and extract the fields.
    report = normalize_deep_analysis_report('{"summary": "disk full", "root_cause": "bad disk"}').to_dict()

    assert report["source_format"] == "json_text"
    assert report["summary"] == "disk full"
    assert report["root_cause"] == "bad disk"


def test_repairs_unescaped_quotes_inside_upstream_json_string() -> None:
    from contracts.deep_analysis_report import normalize_deep_analysis_report

    broken = """```json
{
  "summary": "ES 集群持续 yellow。",
  "root_cause": {
    "description": "副本分片恢复超时。"
  },
  "recommendations": [
    {
      "priority": "P3",
      "answer": "在 VCM 告警规则中增加"集群状态持续 yellow > 30 分钟"的独立告警项",
      "reason": "区分短暂抖动和持续异常"
    }
  ],
  "confidence": 0.85
}
```"""

    report = normalize_deep_analysis_report({"root_cause": broken}).to_dict()

    assert report["summary"] == "ES 集群持续 yellow。"
    assert report["root_cause"] == "副本分片恢复超时。"
    assert report["recommendations"] == [
        '在 VCM 告警规则中增加"集群状态持续 yellow > 30 分钟"的独立告警项（区分短暂抖动和持续异常）'
    ]
    assert report["confidence"] == 0.85
