"""AI 分析前的 Payload 清洗管道。"""

from __future__ import annotations

import orjson

from core.config import Config
from core.logger import get_logger

logger = get_logger("payload_sanitizer")


def sanitize_for_ai(parsed_data: dict) -> dict:
    """清洗 parsed_data，移除噪音字段并截断过大内容。

    1. 递归移除 AI_PAYLOAD_STRIP_KEYS 指定的键
    2. 序列化后超过 AI_PAYLOAD_MAX_BYTES 则截断大值字段
    """
    if not parsed_data:
        return parsed_data

    strip_keys = (
        {k.strip().lower() for k in Config.ai.AI_PAYLOAD_STRIP_KEYS.split(",")}
        if Config.ai.AI_PAYLOAD_STRIP_KEYS
        else set()
    )
    max_bytes = Config.ai.AI_PAYLOAD_MAX_BYTES

    # Phase 1: 递归移除噪音字段（_strip_keys_recursive 本身非破坏性，无需 deepcopy）
    cleaned = _strip_keys_recursive(parsed_data, strip_keys)

    # Phase 2: 检查大小，超限则截断
    serialized = orjson.dumps(cleaned)
    if len(serialized) > max_bytes:
        logger.info(
            "Payload 超过 AI 输入限制 (%d > %d bytes)，执行截断",
            len(serialized),
            max_bytes,
        )
        cleaned = _truncate_large_values(cleaned, max_bytes)

    return cleaned


def _strip_keys_recursive(data, strip_keys: set, max_depth: int = 20, _depth: int = 0):
    """递归移除指定的键。"""
    if _depth >= max_depth:
        # 超过最大深度，直接截断返回
        if isinstance(data, (dict, list)):
            return {"_truncated": True, "_reason": f"max recursion depth {max_depth}"}
        return data
    if isinstance(data, dict):
        return {
            k: _strip_keys_recursive(v, strip_keys, max_depth, _depth + 1)
            for k, v in data.items()
            if k.lower() not in strip_keys
        }
    if isinstance(data, list):
        return [_strip_keys_recursive(item, strip_keys, max_depth, _depth + 1) for item in data]
    return data


def _truncate_large_values(data, max_bytes: int, depth: int = 0) -> dict | list | str:
    """按值大小降序截断，直到总大小低于限制。"""
    if depth > 5:
        # 超过递归深度直接返回摘要
        return {"_truncated": True, "_reason": "max depth exceeded"}

    if isinstance(data, dict):
        # 按值的序列化大小降序排列
        items_with_size = []
        for k, v in data.items():
            size = len(orjson.dumps(v))
            items_with_size.append((k, v, size))
        items_with_size.sort(key=lambda x: x[2], reverse=True)

        result = {}
        current_size = 2  # {}
        for k, v, size in items_with_size:
            if current_size + size + len(k) + 4 > max_bytes and result:
                # 截断此字段
                if isinstance(v, str) and len(v) > 200:
                    result[k] = v[:200] + f"...[truncated, original {len(v)} chars]"
                elif isinstance(v, (dict, list)):
                    result[k] = {"_truncated": True, "_original_size": size}
                else:
                    result[k] = v
            else:
                result[k] = v
                current_size += size + len(k) + 4
        return result

    if isinstance(data, list) and len(orjson.dumps(data)) > max_bytes:
        # 截断列表到合理长度
        truncated = data[:10]
        truncated.append({"_truncated": True, "_original_length": len(data)})
        return truncated

    return data
