"""飞书卡片（火山引擎日志服务等）生态适配器插件。"""

from __future__ import annotations

from adapters.ecosystem_adapters import _normalize_level
from adapters.registry import registry

_IMPORTANCE_TEMPLATE = {"high": "red", "critical": "red", "medium": "orange", "low": "green"}
_IMPORTANCE_EMOJI = {"high": "🔴", "critical": "🚨", "medium": "🟡", "low": "🟢"}


def build_feishu_card(
    webhook_data: dict,
    analysis_result: dict,
    *,
    is_periodic_reminder: bool = False,
) -> dict:
    """将 webhook 事件和 AI 分析结果构建为飞书交互卡片 payload。"""
    importance = str(analysis_result.get("importance", "medium")).lower()
    template = _IMPORTANCE_TEMPLATE.get(importance, "orange")
    emoji = _IMPORTANCE_EMOJI.get(importance, "🟡")

    source = webhook_data.get("source", "") or webhook_data.get("body", {}).get("source", "")
    summary = analysis_result.get("summary", "")
    root_cause = analysis_result.get("root_cause", "")
    suggestion = analysis_result.get("suggestion", "") or analysis_result.get("action", "")
    event_type = (webhook_data.get("parsed_data") or {}).get("event_type", "") or webhook_data.get("event_type", "")

    prefix = "🔁 [周期提醒] " if is_periodic_reminder else ""
    title = f"{prefix}{emoji} [{source}] {event_type or '告警通知'}" if source else f"{prefix}{emoji} 告警通知"

    elements = []
    if summary:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**📋 摘要：**\n{summary[:600]}"}})
        elements.append({"tag": "hr"})
    if root_cause:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**🔍 根因：**\n{root_cause[:400]}"}})
        elements.append({"tag": "hr"})
    if suggestion:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**💡 建议：**\n{suggestion[:400]}"}})
        elements.append({"tag": "hr"})

    noise = analysis_result.get("noise_reduction") or {}
    noise_reason = noise.get("reason", "")
    if noise_reason:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**🔕 降噪：**\n{noise_reason[:200]}"}})
        elements.append({"tag": "hr"})

    if not elements:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "（暂无详情）"}})

    return {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
            "elements": elements,
        },
    }


@registry.register_detector("feishu_card")
def detect(data: dict) -> bool:
    """检测飞书卡片格式（火山引擎日志服务告警等）"""
    if not isinstance(data, dict):
        return False
    return data.get("msg_type") == "interactive" and isinstance(data.get("card"), dict)


@registry.register("feishu_card", aliases={"feishu_card", "volcengine_log"})
def normalize(data: dict) -> dict:
    """
    解析飞书卡片格式，提取关键字段。
    典型来源：火山引擎日志服务告警通知
    """
    card = data.get("card", {})
    header = card.get("header", {})
    elements = card.get("elements", [])

    # 从 header 提取标题
    header_title = ""
    if isinstance(header, dict):
        title_obj = header.get("title", {})
        if isinstance(title_obj, dict):
            header_title = str(title_obj.get("content", "") or "").strip()
        elif isinstance(title_obj, str):
            header_title = title_obj.strip()

    # 从 elements[0].content 解析关键信息
    content_text = ""
    for elem in elements:
        if isinstance(elem, dict) and elem.get("tag") == "markdown":
            content_text = str(elem.get("content", "") or "").strip()
            break

    # 解析 content 中的键值对
    alert_strategy = ""
    log_topic = ""
    alert_level = ""
    first_trigger_time = ""
    trigger_condition = ""
    query_result = ""

    lines = content_text.split("\n")
    for line in lines:
        line = line.strip()
        if "告警策略：" in line:
            alert_strategy = line.split("告警策略：", 1)[1].strip()
        elif "告警日志主题：" in line:
            log_topic = line.split("告警日志主题：", 1)[1].strip()
        elif "告警级别：" in line:
            alert_level = line.split("告警级别：", 1)[1].strip()
        elif "首次触发时间：" in line:
            first_trigger_time = line.split("首次触发时间：", 1)[1].strip()
        elif "触发条件：" in line:
            trigger_condition = line.split("触发条件：", 1)[1].strip()
        elif "当前查询结果：" in line:
            query_result = line.split("当前查询结果：", 1)[1].strip()

    normalized = dict(data)
    normalized.update(
        {
            "Type": "FeishuCard",
            "RuleName": alert_strategy or header_title or "feishu_alert",
            "alert_name": alert_strategy or header_title or "feishu_alert",
            "Level": _normalize_level(alert_level),
            "MetricName": log_topic or "feishu_log_alert",
            "event": "alert",
            "event_type": "feishu_card_alert",
            "alert_id": alert_strategy,
        }
    )

    if first_trigger_time:
        normalized["first_trigger_time"] = first_trigger_time
    if trigger_condition:
        normalized["trigger_condition"] = trigger_condition
    if query_result:
        normalized["query_result"] = query_result
    if log_topic:
        normalized["Resources"] = [{"InstanceId": log_topic}]
    if content_text:
        normalized["summary"] = content_text

    return normalized
