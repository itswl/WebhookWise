"""Payload 压缩/解压工具。"""

from __future__ import annotations

import asyncio

import zstandard as zstd

_DEFAULT_COMPRESS_THRESHOLD_BYTES = 4096

# Zstandard 编解码器（模块级单例，线程安全）
_compressor = zstd.ZstdCompressor(level=3)
_decompressor = zstd.ZstdDecompressor()

# Magic headers
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def _compress_threshold_bytes() -> int:
    try:
        from core.app_context import get_config_manager

        configured = int(get_config_manager().server.PAYLOAD_COMPRESS_THRESHOLD_BYTES or 0)
        return configured if configured > 0 else _DEFAULT_COMPRESS_THRESHOLD_BYTES
    except Exception:
        return _DEFAULT_COMPRESS_THRESHOLD_BYTES


def _decompress_async_threshold_bytes() -> int:
    try:
        from core.app_context import get_config_manager

        configured = int(get_config_manager().server.PAYLOAD_DECOMPRESS_ASYNC_THRESHOLD_BYTES or 0)
        return configured if configured > 0 else _DEFAULT_COMPRESS_THRESHOLD_BYTES
    except Exception:
        return _DEFAULT_COMPRESS_THRESHOLD_BYTES



def compress_payload(text: str | bytes | None) -> bytes | None:
    """将文本 payload 使用 Zstandard 压缩为 bytes。小于阈值时直接返回 UTF-8 bytes。"""
    if not text:
        return None
    raw = text.encode("utf-8") if isinstance(text, str) else text
    if len(raw) < _compress_threshold_bytes():
        return raw
    return _compressor.compress(raw)


def decompress_payload(data: bytes | str | None) -> str | None:
    """解压 bytes 为文本。自动识别 Zstd/原始 UTF-8 数据。"""
    if data is None:
        return None
    if isinstance(data, str):
        if data.startswith("\\x"):
            # PostgreSQL BYTEA hex format → decode to bytes, then fall through
            data = bytes.fromhex(data[2:])
        else:
            return data
    if data[:4] == _ZSTD_MAGIC:
        return _decompressor.decompress(data).decode("utf-8")
    # 无 magic header：当作原始 UTF-8 文本处理
    return data.decode("utf-8")


async def decompress_payload_async(data: bytes | str | None) -> str | None:
    """异步解压：大 payload 卸载到线程池，与 compress_payload 对称处理。"""
    if data is None:
        return None
    if isinstance(data, str):
        if data.startswith("\\x"):
            # PostgreSQL BYTEA hex format → decode to bytes, then fall through
            data = bytes.fromhex(data[2:])
        else:
            return data
    # 超过阈值时卸载到线程池，避免阻塞事件循环
    if len(data) > _decompress_async_threshold_bytes():
        return await asyncio.to_thread(decompress_payload, data)
    return decompress_payload(data)
