"""Payload 压缩/解压工具。"""

from __future__ import annotations

import asyncio
import gzip

# 低于此阈值的 payload 不压缩，节省 CPU
COMPRESS_THRESHOLD_BYTES = 4096


def compress_payload(text: str | bytes | None) -> bytes | None:
    """将文本 payload gzip 压缩为 bytes。小于阈值时直接返回 UTF-8 bytes。"""
    if not text:
        return None
    raw = text.encode("utf-8") if isinstance(text, str) else text
    if len(raw) < COMPRESS_THRESHOLD_BYTES:
        return raw
    return gzip.compress(raw, compresslevel=6)


def decompress_payload(data: bytes | str | None) -> str | None:
    """将 gzip 压缩的 bytes 解压为文本。兼容迁移前的未压缩数据。"""
    if data is None:
        return None
    if isinstance(data, str):
        return data
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data).decode("utf-8")
    # 迁移前的旧数据：直接 decode
    return data.decode("utf-8")


async def decompress_payload_async(data: bytes | str | None) -> str | None:
    """异步解压：大 payload 卸载到线程池，与 compress_payload 对称处理。"""
    if data is None:
        return None
    if isinstance(data, str):
        return data
    # 超过阈值时卸载到线程池，避免阻塞事件循环
    if len(data) > COMPRESS_THRESHOLD_BYTES:
        return await asyncio.to_thread(decompress_payload, data)
    return decompress_payload(data)
