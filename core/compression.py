"""Payload 压缩/解压工具。"""

from __future__ import annotations

import gzip

# 低于此阈值的 payload 不压缩，节省 CPU
COMPRESS_THRESHOLD_BYTES = 4096


def compress_payload(text: str | None) -> bytes | None:
    """将文本 payload gzip 压缩为 bytes。小于阈值时直接返回 UTF-8 bytes。"""
    if not text:
        return None
    raw = text.encode("utf-8")
    if len(raw) < COMPRESS_THRESHOLD_BYTES:
        return raw
    return gzip.compress(raw, compresslevel=6)


def decompress_payload(data: bytes | None) -> str | None:
    """将 gzip 压缩的 bytes 解压为文本。兼容迁移前的未压缩数据。"""
    if not data:
        return None
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data).decode("utf-8")
    # 迁移前的旧数据：直接 decode
    return data.decode("utf-8")
