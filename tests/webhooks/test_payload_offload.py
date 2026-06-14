import pytest


@pytest.mark.asyncio
async def test_sanitize_for_ai_async_offloads_large_payload(monkeypatch):
    from services.webhooks import payload_sanitizer

    called = {"n": 0}

    async def fake_to_thread(fn, *args, **kwargs):
        called["n"] += 1
        return fn(*args, **kwargs)

    monkeypatch.setattr(payload_sanitizer.asyncio, "to_thread", fake_to_thread)

    threshold = payload_sanitizer.PayloadPolicy.from_config().offload_threshold_bytes
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

    threshold = payload_sanitizer.PayloadPolicy.from_config().offload_threshold_bytes
    nested = {"event": "pod_crash", "detail": {"raw_log": "x" * threshold}}
    out = await payload_sanitizer.sanitize_for_ai_async(nested)
    assert out
    assert called["n"] == 1


@pytest.mark.asyncio
async def test_sanitize_for_ai_async_can_disable_strip_and_truncate(monkeypatch, temp_config):
    from core.sensitive_data import REDACTED
    from services.webhooks import payload_sanitizer

    monkeypatch.setattr(temp_config.ai, "AI_PAYLOAD_MAX_BYTES", 32)
    monkeypatch.setattr(temp_config.ai, "AI_PAYLOAD_STRIP_KEYS", "raw_trace")

    payload = {"raw_trace": "x" * 1000, "token": "secret-token"}
    out = await payload_sanitizer.sanitize_for_ai_async(payload, strip_configured_keys=False, truncate=False)

    assert out["raw_trace"] == "x" * 1000
    assert out["token"] == REDACTED


def test_truncate_large_string_keeps_buried_error_line():
    """A panic line buried in the middle of a big log must survive truncation —
    a blind head-cut (old behavior) would have discarded it."""
    from services.webhooks import payload_sanitizer

    filler_head = "\n".join(f"INFO routine log line {i}" for i in range(400))
    needle = "FATAL panic: segfault in elys-backend at handler.go:42"
    filler_tail = "\n".join(f"DEBUG trailing line {i}" for i in range(400))
    big_log = f"{filler_head}\n{needle}\n{filler_tail}"

    payload = {"keep": "small", "log": big_log}
    out = payload_sanitizer._truncate_large_values(payload, max_bytes=1200)

    assert isinstance(out["log"], str)
    assert needle in out["log"], "buried error line was dropped by truncation"
    assert "truncated" in out["log"]
    assert out["keep"] == "small"  # small fields kept intact


def test_truncate_large_list_keeps_head_and_tail():
    """A large list keeps head + tail (recent events) with an elision marker,
    not just the head."""
    from services.webhooks import payload_sanitizer

    items = [{"i": i, "event": f"e{i}"} for i in range(100)]
    out = payload_sanitizer._summarize_large_list(items)

    assert out[0] == {"i": 0, "event": "e0"}  # head kept
    assert out[-1] == {"i": 99, "event": "e99"}  # tail (most recent) kept
    marker = next(x for x in out if isinstance(x, dict) and x.get("_truncated"))
    assert marker["_original_length"] == 100
    assert marker["_omitted_items"] == 100 - payload_sanitizer._LIST_HEAD - payload_sanitizer._LIST_TAIL
