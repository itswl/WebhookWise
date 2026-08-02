"""Importance triage: LLM first, deterministic rules as the floor.

The rule path is not a degraded stub — it is the guarantee. An alert pipeline
whose judgement disappears when a third-party API is slow has traded a real
dependency for an imaginary feature, so every LLM failure mode (no key, error,
timeout, unparseable answer) falls through to rules and says so in `route`.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

# Matched against incoming Chinese alert text: these are behavioural keywords,
# not display copy, and translating them breaks classification.
_HIGH = ("critical", "fatal", "down", "outage", "严重", "宕机", "故障", "不可用", "失败率")
_MEDIUM = ("error", "warn", "high", "错误", "告警", "超时", "异常")
_LOW = ("info", "notice", "recovered", "恢复", "通知")

_PROMPT = """你是一个运维告警分诊助手。请判断这条告警的重要程度并给出一句话摘要。
%(resolved_note)s

重要程度只能是 high / medium / low 之一:
- high: 影响用户或核心业务、需要立即介入
- medium: 需要关注但可以排期处理
- low: 信息性通知、已恢复、无需人工介入

只输出 JSON,格式:{"importance": "high|medium|low", "summary": "一句话中文摘要"}

告警来源:%(source)s
标题:%(title)s
内容:%(body)s"""


def rule_triage(source: str, title: str, body: str, resolved: bool) -> dict[str, str]:
    # A recovery keeps the importance its firing alert would get, deliberately.
    # Downgrading it to "low" would route it away from whoever was paged, so
    # they would be told about the problem and never told it was over. The card
    # carries the recovery marker instead; urgency is a display concern here.
    text = f"{title} {body}".lower()
    for keyword in _HIGH:
        if keyword in text:
            return {"importance": "high", "summary": title, "route": "rule"}
    for keyword in _MEDIUM:
        if keyword in text:
            return {"importance": "medium", "summary": title, "route": "rule"}
    for keyword in _LOW:
        if keyword in text:
            return {"importance": "low", "summary": title, "route": "rule"}
    return {"importance": "medium", "summary": title, "route": "rule"}


async def triage(
    client: httpx.AsyncClient,
    settings: Any,
    source: str,
    title: str,
    body: str,
    resolved: bool,
) -> dict[str, str]:
    fallback = rule_triage(source, title, body, resolved)
    if not settings.openai_api_key:
        return fallback

    try:
        response = await client.post(
            f"{settings.openai_api_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json={
                "model": settings.openai_model,
                "temperature": 0.2,
                "messages": [
                    {
                        "role": "user",
                        "content": _PROMPT
                        % {
                            "source": source,
                            "title": title,
                            "body": body[:2000],
                            "resolved_note": (
                                "注意:这是一条恢复通知(告警已解除),摘要请写明已恢复。\n" if resolved else ""
                            ),
                        },
                    }
                ],
            },
            timeout=settings.ai_timeout_seconds,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        parsed = json.loads(_strip_fence(content))
        importance = str(parsed.get("importance", "")).lower()
        if importance not in ("high", "medium", "low"):
            return {**fallback, "route": "rule", "degraded": "llm returned an unknown importance"}
        return {
            "importance": importance,
            "summary": str(parsed.get("summary") or title)[:300],
            "route": "ai",
        }
    except Exception as e:  # noqa: BLE001 - any LLM failure must land on the rule floor
        return {**fallback, "degraded": f"{type(e).__name__}: {e}"[:200]}


def _strip_fence(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0]
    return text.strip()
