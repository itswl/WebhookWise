"""AI 分析前的 Payload 清洗管道。"""

from __future__ import annotations

import asyncio
from typing import Any

from core import json
from core.logger import get_logger
from core.sensitive_data import redact_nested
from services.webhooks.policies import PayloadPolicy

logger = get_logger("payload_sanitizer")


def _should_offload(data: object, policy: PayloadPolicy, depth: int = 0) -> bool:
    if depth > 2:
        return False
    if data is None:
        return False
    if isinstance(data, dict):
        if len(data) > 2000:
            return True
        threshold = policy.offload_threshold_bytes
        for n, v in enumerate(data.values()):
            if isinstance(v, (str, bytes, bytearray)) and len(v) >= threshold:
                return True
            if isinstance(v, list) and len(v) > 5000:
                return True
            if isinstance(v, dict) and (len(v) > 2000 or _should_offload(v, policy, depth + 1)):
                return True
            if isinstance(v, list) and depth < 2:
                for item in v[:2000]:
                    if isinstance(item, (dict, list)) and _should_offload(item, policy, depth + 1):
                        return True
            if n >= 2000:
                break
        return False
    if isinstance(data, list):
        if len(data) > 5000:
            return True
        if depth < 2:
            for item in data[:2000]:
                if isinstance(item, (dict, list)) and _should_offload(item, policy, depth + 1):
                    return True
        return False
    return False


async def sanitize_for_ai_async(
    parsed_data: dict[str, Any], *, strip_configured_keys: bool = True, truncate: bool = True
) -> dict[str, Any]:
    if not parsed_data:
        return parsed_data
    policy = PayloadPolicy.from_config()
    if _should_offload(parsed_data, policy):
        res = await asyncio.to_thread(
            sanitize_for_ai,
            parsed_data,
            strip_configured_keys=strip_configured_keys,
            truncate=truncate,
            policy=policy,
        )
        return res
    return sanitize_for_ai(parsed_data, strip_configured_keys=strip_configured_keys, truncate=truncate, policy=policy)


def sanitize_for_ai(
    parsed_data: dict[str, Any],
    *,
    strip_configured_keys: bool = True,
    truncate: bool = True,
    policy: PayloadPolicy | None = None,
) -> dict[str, Any]:
    """清洗 parsed_data，移除噪音字段并截断过大内容。

    1. 递归移除 AI_PAYLOAD_STRIP_KEYS 指定的键
    2. 序列化后超过 AI_PAYLOAD_MAX_BYTES 则截断大值字段

    OpenClaw 深度分析可关闭 strip/truncate，仅保留敏感字段脱敏。
    """
    if not parsed_data:
        return parsed_data

    policy = policy or PayloadPolicy.from_config()
    strip_keys = set(policy.strip_keys) if strip_configured_keys else set()

    # Phase 1: 递归移除噪音字段（_strip_keys_recursive 本身非破坏性，无需 deepcopy）
    cleaned_obj = redact_nested(_strip_keys_recursive(parsed_data, strip_keys))
    cleaned: dict[str, Any] = cleaned_obj if isinstance(cleaned_obj, dict) else parsed_data

    # Phase 2: 检查大小，超限则截断
    if not truncate:
        return cleaned

    max_bytes = policy.max_bytes
    serialized = json.dumps_bytes(cleaned)
    if max_bytes > 0 and len(serialized) > max_bytes:
        logger.info(
            "Payload 超过 AI 输入限制 (%d > %d bytes)，执行截断",
            len(serialized),
            max_bytes,
        )
        truncated = _truncate_large_values(cleaned, max_bytes)
        if isinstance(truncated, dict):
            cleaned = truncated

    return cleaned


def _strip_keys_recursive(data: object, strip_keys: set[str], max_depth: int = 20, _depth: int = 0) -> object:
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


def _truncate_large_values(data: object, max_bytes: int, depth: int = 0) -> object:
    """按值大小降序截断，直到总大小低于限制。"""
    if depth > 5:
        # 超过递归深度直接返回摘要
        return {"_truncated": True, "_reason": "max depth exceeded"}

    if isinstance(data, dict):
        # 按值的序列化大小降序排列
        items_with_size = []
        for k, v in data.items():
            size = len(json.dumps_bytes(v))
            items_with_size.append((k, v, size))
        items_with_size.sort(key=lambda x: x[2], reverse=True)

        result: dict[str, object] = {}
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

    if isinstance(data, list) and len(json.dumps_bytes(data)) > max_bytes:
        # 截断列表到合理长度
        truncated = data[:10]
        truncated.append({"_truncated": True, "_original_length": len(data)})
        return truncated

    return data
