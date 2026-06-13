"""Payload 压缩/解压工具。"""

from __future__ import annotations

import asyncio

import zstandard as zstd

from core.logger import get_logger

logger = get_logger("compression")

_DEFAULT_THRESHOLD_BYTES = 4096
_compressor = zstd.ZstdCompressor(level=3)
_decompressor = zstd.ZstdDecompressor()
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
# Returned when a stored payload cannot be decoded. Keeping this a stable,
# JSON-safe string means a single corrupt row degrades to a placeholder instead
# of 500-ing every API call that serializes that event.
_UNDECODABLE_PLACEHOLDER = "<payload-decode-error>"


def _threshold(attr: str) -> int:
    try:
        from core.app_context import get_config_manager

        configured = int(getattr(get_config_manager().server, attr) or 0)
        return configured if configured > 0 else _DEFAULT_THRESHOLD_BYTES
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return _DEFAULT_THRESHOLD_BYTES


def compress_payload(text: str | bytes | None) -> bytes | None:
    """将文本 payload 使用 Zstandard 压缩为 bytes。小于阈值时直接返回 UTF-8 bytes。"""
    if not text:
        return None
    raw = text.encode("utf-8") if isinstance(text, str) else text
    if len(raw) < _threshold("PAYLOAD_COMPRESS_THRESHOLD_BYTES"):
        return raw
    return _compressor.compress(raw)


def decompress_payload(data: bytes | str | None) -> str | None:
    """解压 bytes 为文本。自动识别 Zstd/原始 UTF-8 数据。

    对损坏数据做降级处理而不是抛异常：一条损坏记录不应让所有序列化该事件的
    API 报 500。无法解压的 zstd 返回占位符，非法 UTF-8 用 replace 容错解码。
    """
    if data is None:
        return None
    if isinstance(data, str):
        if data.startswith("\\x"):
            try:
                data = bytes.fromhex(data[2:])
            except ValueError:
                logger.warning("[Compression] 十六进制 payload 解析失败，返回占位符")
                return _UNDECODABLE_PLACEHOLDER
        else:
            return data
    if data[:4] == _ZSTD_MAGIC:
        try:
            decompressed = _decompressor.decompress(data)
        except zstd.ZstdError:
            logger.warning("[Compression] Zstd 解压失败（数据可能损坏），返回占位符")
            return _UNDECODABLE_PLACEHOLDER
        return decompressed.decode("utf-8", errors="replace")
    return data.decode("utf-8", errors="replace")


async def decompress_payload_async(data: bytes | str | None) -> str | None:
    """异步解压：大 payload 卸载到线程池，与 compress_payload 对称处理。"""
    if data is None:
        return None
    if isinstance(data, str):
        if data.startswith("\\x"):
            data = bytes.fromhex(data[2:])
        else:
            return data
    if len(data) > _threshold("PAYLOAD_DECOMPRESS_ASYNC_THRESHOLD_BYTES"):
        return await asyncio.to_thread(decompress_payload, data)
    return decompress_payload(data)
