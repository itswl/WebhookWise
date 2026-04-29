"""Payload 压缩/解压工具。"""

from __future__ import annotations

import gzip


def compress_payload(text: str | None) -> bytes | None:
    """将文本 payload gzip 压缩为 bytes。"""
    if not text:
        return None
    return gzip.compress(text.encode("utf-8"), compresslevel=6)


def decompress_payload(data: bytes | None) -> str | None:
    """将 gzip 压缩的 bytes 解压为文本。兼容迁移前的未压缩数据。"""
    if not data:
        return None
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data).decode("utf-8")
    # 迁移前的旧数据：直接 decode
    return data.decode("utf-8")
