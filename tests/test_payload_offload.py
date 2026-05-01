import pytest


@pytest.mark.asyncio
async def test_sanitize_for_ai_async_offloads_large_payload(monkeypatch):
    from services import payload_sanitizer

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
    from services import payload_sanitizer

    called = {"n": 0}

    async def fake_to_thread(fn, *args, **kwargs):
        called["n"] += 1
        return fn(*args, **kwargs)

    monkeypatch.setattr(payload_sanitizer.asyncio, "to_thread", fake_to_thread)

    small = {"k": "v"}
    out = await payload_sanitizer.sanitize_for_ai_async(small)
    assert out == small
    assert called["n"] == 0
