"""AI 响应解析与结果规范化模块

已重构：现在主要由 Instructor + Pydantic 在 ai_client 层处理结构化输出。
本模块仅保留基础的规范化逻辑和极简的兜底提取。
"""

import logging
from typing import Any

from schemas.analysis import Importance

logger = logging.getLogger("webhook_service.ai_parser")

# 类型别名
AnalysisResult = dict[str, Any]


def _normalize_analysis_result(result: dict[str, Any], source: str) -> AnalysisResult:
    """确保 AI 返回的结果符合业务要求的默认值。"""
    if not isinstance(result, dict):
        result = {}

    normalized: AnalysisResult = dict(result)
    normalized["source"] = str(normalized.get("source") or source)

    event_type = str(normalized.get("event_type") or "unknown").strip()
    normalized["event_type"] = event_type or "unknown"

    # 校验重要性枚举
    importance = str(normalized.get("importance") or "medium").lower().strip()
    if importance not in [e.value for e in Importance]:
        importance = "medium"
    normalized["importance"] = importance

    summary = str(normalized.get("summary") or "").strip()
    normalized["summary"] = summary or "AI分析未生成摘要"

    # 清理列表字段
    for list_field in ["actions", "risks", "monitoring_suggestions"]:
        items = normalized.get(list_field, [])
        if not isinstance(items, list):
            normalized[list_field] = []
        else:
            normalized[list_field] = [str(i).strip() for i in items if i]

    return normalized


# ── 以下为向后兼容保留的极简逻辑（通常不再使用） ──

def fix_json_format(json_str: str) -> str:
    """[已废弃] Instructor 已处理 JSON 修复"""
    return json_str.strip()


def extract_from_text(text: str, source: str) -> AnalysisResult:
    """从文本中提取的极简兜底（当结构化输出彻底失败时）"""
    logger.warning("触发解析兜底：尝试从非结构化文本生成摘要")
    return _normalize_analysis_result({
        "summary": text[:200] if text else "AI 响应格式解析失败",
        "importance": "medium",
        "event_type": "text_fallback"
    }, source)


def _parse_ai_analysis_response(ai_response: str, source: str) -> AnalysisResult:
    """[兼容性接口] 供旧测试使用"""
    import orjson
    try:
        parsed = orjson.loads(ai_response)
        return _normalize_analysis_result(parsed, source)
    except Exception:
        return extract_from_text(ai_response, source)
