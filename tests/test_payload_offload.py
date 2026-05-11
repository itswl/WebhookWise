import pytest


@pytest.mark.asyncio
async def test_sanitize_for_ai_async_offloads_large_payload(monkeypatch):
    from services.webhooks import payload_sanitizer

    called = {"n": 0}

    async def fake_to_thread(fn, *args, **kwargs):
        called["n"] += 1
        return fn(*args, **kwargs)

    monkeypatch.setattr(payload_sanitizer.asyncio, "to_thread", fake_to_thread)

    threshold = payload_sanitizer._get_offload_threshold_bytes()
    big = {"raw": "x" * threshold}
    out = await payload_sanitizer.sanitize_for_ai_async(big)
    assert out
    assert called["n"] == 1


@pytest.mark.asyncio
async def test_sanitize_for_ai_async_does_not_offload_small_payload(monkeypatch):
    from services.webhooks import payload_sanitizer

    called = {"n": 0}

    async def fake_to_thread(fn, *args, **kwargs):
        called["n"] += 1
        return fn(*args, **kwargs)

    monkeypatch.setattr(payload_sanitizer.asyncio, "to_thread", fake_to_thread)

    small = {"k": "v"}
    out = await payload_sanitizer.sanitize_for_ai_async(small)
    assert out == small
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_sanitize_for_ai_async_offloads_deep_nested_large_string(monkeypatch):
    from services.webhooks import payload_sanitizer

    called = {"n": 0}

    async def fake_to_thread(fn, *args, **kwargs):
        called["n"] += 1
        return fn(*args, **kwargs)

    monkeypatch.setattr(payload_sanitizer.asyncio, "to_thread", fake_to_thread)

    threshold = payload_sanitizer._get_offload_threshold_bytes()
    nested = {"event": "pod_crash", "detail": {"raw_log": "x" * threshold}}
    out = await payload_sanitizer.sanitize_for_ai_async(nested)
    assert out
    assert called["n"] == 1


@pytest.mark.asyncio
async def test_sanitize_for_ai_async_can_disable_strip_and_truncate(monkeypatch):
    from core.config import Config
    from core.sensitive_data import REDACTED
    from services.webhooks import payload_sanitizer

    monkeypatch.setattr(Config.ai, "AI_PAYLOAD_MAX_BYTES", 32)
    monkeypatch.setattr(Config.ai, "AI_PAYLOAD_STRIP_KEYS", "raw_trace")

    payload = {"raw_trace": "x" * 1000, "token": "secret-token"}
    out = await payload_sanitizer.sanitize_for_ai_async(payload, strip_configured_keys=False, truncate=False)

    assert out["raw_trace"] == "x" * 1000
    assert out["token"] == REDACTED
