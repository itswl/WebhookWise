"""Payload compression/decompression utilities."""

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
    """Compress a text payload to bytes using Zstandard. Returns the raw UTF-8 bytes directly when below the threshold."""
    if not text:
        return None
    raw = text.encode("utf-8") if isinstance(text, str) else text
    if len(raw) < _threshold("PAYLOAD_COMPRESS_THRESHOLD_BYTES"):
        return raw
    return _compressor.compress(raw)


def decompress_payload(data: bytes | str | None) -> str | None:
    """Decompress bytes to text. Automatically detects Zstd vs. raw UTF-8 data.

    Corrupt data is degraded rather than raising: a single corrupt record should
    not make every API that serializes that event return 500. Zstd data that
    cannot be decompressed returns a placeholder, and invalid UTF-8 is decoded
    leniently using errors="replace".
    """
    if data is None:
        return None
    if isinstance(data, str):
        if data.startswith("\\x"):
            try:
                data = bytes.fromhex(data[2:])
            except ValueError:
                logger.warning("[Compression] Failed to parse hex payload; returning placeholder")
                return _UNDECODABLE_PLACEHOLDER
        else:
            return data
    if data[:4] == _ZSTD_MAGIC:
        try:
            decompressed = _decompressor.decompress(data)
        except zstd.ZstdError:
            logger.warning("[Compression] Zstd decompression failed (data may be corrupt); returning placeholder")
            return _UNDECODABLE_PLACEHOLDER
        return decompressed.decode("utf-8", errors="replace")
    return data.decode("utf-8", errors="replace")


async def compress_payload_async(text: str | bytes | None) -> bytes | None:
    """Async compression: offload above-threshold payloads to a thread pool.

    Mirrors decompress_payload_async — payloads large enough to compress are
    exactly the ones whose zstd pass would stall the event loop. A fresh
    compressor is created inside the thread because ZstdCompressor instances
    are not safe for concurrent use across threads.
    """
    if not text:
        return None
    raw = text.encode("utf-8") if isinstance(text, str) else text
    if len(raw) < _threshold("PAYLOAD_COMPRESS_THRESHOLD_BYTES"):
        return raw
    return await asyncio.to_thread(lambda: zstd.ZstdCompressor(level=3).compress(raw))


async def decompress_payload_async(data: bytes | str | None) -> str | None:
    """Async decompression: offload large payloads to a thread pool, mirroring compress_payload."""
    if data is None:
        return None
    if isinstance(data, str):
        if data.startswith("\\x"):
            try:
                data = bytes.fromhex(data[2:])
            except ValueError:
                logger.warning("[Compression] Failed to parse hex payload; returning placeholder")
                return _UNDECODABLE_PLACEHOLDER
        else:
            return data
    if len(data) > _threshold("PAYLOAD_DECOMPRESS_ASYNC_THRESHOLD_BYTES"):
        return await asyncio.to_thread(decompress_payload, data)
    return decompress_payload(data)
