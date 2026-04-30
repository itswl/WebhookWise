"""AI 响应解析与结果规范化模块

负责将 AI 返回的文本解析为结构化 JSON 结果，
包含多层容错策略：JSON 修复、截断补全、文本兜底提取。
"""

import logging
import re
from typing import Any

import orjson

from services.ai_prompts import (
    _close_truncated_json,
    _extract_first_json_object,
    _extract_json_payload,
    _safe_json_string,
)

try:
    import json5

    HAS_JSON5 = True
except ImportError:
    HAS_JSON5 = False

logger = logging.getLogger("webhook_service.ai_parser")

# 类型别名
AnalysisResult = dict[str, Any]


def fix_json_format(json_str: str) -> str:
    """修复常见的 JSON 格式错误"""
    json_str = json_str.replace("\ufeff", "").strip()
    if not json_str:
        return json_str

    try:
        orjson.loads(json_str)
        return json_str
    except (orjson.JSONDecodeError, ValueError):
        pass

    if HAS_JSON5:
        try:
            parsed = json5.loads(json_str)
            return orjson.dumps(parsed).decode()
        except Exception as e:
            logger.debug(f"json5 解析失败: {e}")

    fixed = json_str
    fixed = re.sub(r"//.*?$", "", fixed, flags=re.MULTILINE)
    fixed = re.sub(r"/\*.*?\*/", "", fixed, flags=re.DOTALL)
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
    fixed = re.sub(r"([\[{])\s*,", r"\1", fixed)
    fixed = re.sub(r'(")\s+("([^"\\]|\\.)*"\s*:)', r"\1, \2", fixed)
    return fixed.strip()


def _extract_flexible_field(text: str, key: str) -> str | None:
    """多格式字段提取：依次尝试严格JSON、截断JSON、宽松KV格式。"""
    # 1. 严格 JSON 格式: "key": "value"
    strict = re.search(rf'"{ re.escape(key)}"\s*:\s*"((?:\\.|[^"\\])*)"', text, re.DOTALL)
    if strict:
        return _safe_json_string(strict.group(1)).strip()

    # 2. 截断 JSON 格式: "key": "value（未闭合）
    truncated = re.search(rf'"{ re.escape(key)}"\s*:\s*"([^\n]*)', text)
    if truncated:
        val = truncated.group(1).strip().strip(",").strip()
        if val:
            return val

    # 3. 宽松 KV 格式: key: value 或 key=value（YAML / plain text）
    kv = re.search(
        rf'(?:^|\n)\s*{re.escape(key)}\s*[:=]\s*"?([^",\n\}}\]]+)"?',
        text,
        re.IGNORECASE,
    )
    if kv:
        val = kv.group(1).strip()
        if val:
            return val

    return None


def _extract_json_string_field(text: str, key: str) -> str | None:
    """向后兼容包装：委托给 _extract_flexible_field。"""
    return _extract_flexible_field(text, key)


def _extract_json_array_field(text: str, key: str) -> list[str]:
    key_match = re.search(rf'"{re.escape(key)}"\s*:\s*\[', text)
    if not key_match:
        return []

    start = key_match.end() - 1
    arr_part = text[start:]
    depth = 0
    in_string = False
    escape = False
    end = len(arr_part)

    for i, ch in enumerate(arr_part):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    block = arr_part[:end]
    items: list[str] = []
    for raw in re.findall(r'"((?:\\.|[^"\\])*)"', block, re.DOTALL):
        value = _safe_json_string(raw).strip()
        if value:
            items.append(value)

    return items


def _clean_string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []

    cleaned: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        item = value.strip().strip("\"'`").strip().strip(",").strip()
        item = item.strip("[]{}").strip()
        if not item:
            continue
        if item in {"[", "]", "{", "}"}:
            continue
        cleaned.append(item)

    return cleaned


def _normalize_analysis_result(result: AnalysisResult, source: str) -> AnalysisResult:
    if not isinstance(result, dict):
        result = {}

    normalized: AnalysisResult = dict(result)
    normalized["source"] = str(normalized.get("source") or source)

    event_type = str(normalized.get("event_type") or "unknown").strip()
    normalized["event_type"] = event_type or "unknown"

    importance = str(normalized.get("importance") or "medium").lower().strip()
    if importance not in {"high", "medium", "low"}:
        importance = "medium"
    normalized["importance"] = importance

    summary = str(normalized.get("summary") or "").strip()
    normalized["summary"] = summary or "AI分析未生成摘要"

    if "impact_scope" in normalized and normalized["impact_scope"] is not None:
        normalized["impact_scope"] = str(normalized["impact_scope"]).strip()
        if not normalized["impact_scope"]:
            normalized.pop("impact_scope", None)

    normalized["actions"] = _clean_string_list(normalized.get("actions", []))
    normalized["risks"] = _clean_string_list(normalized.get("risks", []))

    if "monitoring_suggestions" in normalized:
        normalized["monitoring_suggestions"] = _clean_string_list(normalized.get("monitoring_suggestions", []))

    return normalized


def _try_parse_json_analysis(candidate: str) -> AnalysisResult | None:
    attempts = [
        candidate,
        fix_json_format(candidate),
    ]
    closed = _close_truncated_json(candidate)
    attempts.extend([closed, fix_json_format(closed)])

    for text in attempts:
        try:
            parsed = orjson.loads(text)
        except (orjson.JSONDecodeError, ValueError):
            continue

        if isinstance(parsed, dict):
            return parsed

    return None


def extract_from_text(text: str, source: str) -> AnalysisResult:
    """从 AI 响应文本中提取关键信息（兜底策略）。"""
    logger.info("使用文本提取策略解析 AI 响应")

    result: AnalysisResult = {
        "source": source,
        "event_type": "unknown",
        "importance": "medium",
        "summary": "",
        "actions": [],
        "risks": [],
    }

    try:
        importance = _extract_json_string_field(text, "importance")
        if importance:
            importance = importance.lower()
            if importance in {"high", "medium", "low"}:
                result["importance"] = importance

        summary = _extract_json_string_field(text, "summary")
        if summary:
            result["summary"] = summary
        else:
            # 截取原始文本的前 200 字符作为降级 summary
            cleaned = text.strip()[:200]
            if cleaned:
                result["summary"] = f"AI 分析结果（格式非预期）: {cleaned}"
            else:
                result["summary"] = "Webhook 事件已接收，AI 分析结果为空"

        event_type = _extract_json_string_field(text, "event_type")
        if event_type:
            result["event_type"] = event_type

        impact_scope = _extract_json_string_field(text, "impact_scope")
        if impact_scope:
            result["impact_scope"] = impact_scope

        actions = _extract_json_array_field(text, "actions")
        risks = _extract_json_array_field(text, "risks")
        monitoring = _extract_json_array_field(text, "monitoring_suggestions")

        if actions:
            result["actions"] = actions
        if risks:
            result["risks"] = risks
        if monitoring:
            result["monitoring_suggestions"] = monitoring

        normalized = _normalize_analysis_result(result, source)
        logger.info(f"文本提取完成: {normalized}")
        return normalized

    except Exception as e:
        logger.error(f"文本提取失败: {e!s}")
        result["summary"] = "AI 分析响应格式错误，已降级处理"
        return _normalize_analysis_result(result, source)


def _parse_ai_analysis_response(ai_response: str, source: str) -> AnalysisResult:
    payload = _extract_json_payload(ai_response)

    candidates: list[str] = []
    if payload:
        candidates.append(payload)

    payload_obj = _extract_first_json_object(payload)
    if payload_obj:
        candidates.append(payload_obj)

    raw_obj = _extract_first_json_object(ai_response)
    if raw_obj:
        candidates.append(raw_obj)

    for candidate in candidates:
        parsed = _try_parse_json_analysis(candidate)
        if parsed is not None:
            return _normalize_analysis_result(parsed, source)

    logger.warning("JSON 解析失败，回退到文本提取策略。原始响应片段: %s", ai_response[:300])
    return _normalize_analysis_result(extract_from_text(payload or ai_response, source), source)
