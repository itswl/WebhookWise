"""AI 响应结构自愈 — 修复续写拼接后的格式问题。"""

from __future__ import annotations

import logging
import re

import orjson

logger = logging.getLogger(__name__)


def repair_concatenated_response(original: str, continuation: str) -> str:
    """拼接续写内容并修复格式问题。

    1. 清理续写内容中的 markdown 代码块包裹
    2. 拼接后尝试 JSON 解析
    3. 失败则尝试修复常见格式问题
    4. 仍失败则 fallback 到原始截断内容
    """
    # Step 1: 清理续写内容的 markdown 包裹
    cleaned_continuation = _strip_markdown_fences(continuation)

    # Step 2: 尝试直接拼接
    combined = original + cleaned_continuation

    # Step 3: 尝试解析
    parsed = _try_parse(combined)
    if parsed is not None:
        return combined

    # Step 4: 尝试修复常见问题
    repaired = _repair_json_structure(combined)
    parsed = _try_parse(repaired)
    if parsed is not None:
        logger.info("AI 续写拼接后经结构修复成功解析")
        return repaired

    # Step 5: Fallback — 使用原始截断内容
    logger.warning("AI 续写拼接修复失败，fallback 到截断内容")
    return original


def _strip_markdown_fences(text: str) -> str:
    """移除 markdown 代码块包裹 (```json ... ``` 或 ```yaml ... ```)。"""
    # 去除开头的 ```json 或 ```yaml 或 ```
    text = re.sub(r"^```(?:json|yaml|JSON|YAML)?\s*\n?", "", text.strip())
    # 去除结尾的 ```
    text = re.sub(r"\n?```\s*$", "", text)
    return text


def _try_parse(text: str) -> dict | None:
    """尝试解析为 JSON。"""
    try:
        return orjson.loads(text.encode("utf-8") if isinstance(text, str) else text)
    except Exception:
        return None


def _repair_json_structure(text: str) -> str:
    """修复常见的 JSON 结构问题。"""
    # 1. 去除尾部多余逗号
    text = re.sub(r",\s*([}\]])", r"\1", text)

    # 2. 补全缺失的右括号
    open_braces = text.count("{") - text.count("}")
    open_brackets = text.count("[") - text.count("]")

    if open_braces > 0:
        # 检查是否需要先关闭未完成的字符串
        # 简单策略：如果最后一个非空白字符不是 } ] , " 或数字，尝试补引号
        stripped = text.rstrip()
        if stripped and stripped[-1] not in "{}[],\"'0123456789tfnul":
            text = text + '"'
        text = text + "}" * open_braces

    if open_brackets > 0:
        text = text + "]" * open_brackets

    return text
