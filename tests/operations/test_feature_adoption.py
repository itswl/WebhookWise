"""Feature-adoption ledger: recording, month bucketing, and fail-silence."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.exceptions import RedisError

from services.operations import feature_adoption as fa


@pytest.mark.asyncio
async def test_record_increments_monthly_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.hincrby = AsyncMock()
    client.expire = AsyncMock()
    monkeypatch.setattr(fa, "get_redis", lambda: client)

    await fa.record_feature_use("action:kb_draft_published", now=datetime(2026, 7, 16))
    client.hincrby.assert_awaited_once_with("feature_adoption:2026-07", "action:kb_draft_published", 1)
    client.expire.assert_awaited_once()


@pytest.mark.asyncio
async def test_record_is_fail_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> Any:
        raise RedisError("down")

    monkeypatch.setattr(fa, "get_redis", boom)
    await fa.record_feature_use("view:silence_debt")  # must not raise


@pytest.mark.asyncio
async def test_get_adoption_splits_actions_views_and_prev_month(monkeypatch: pytest.MonkeyPatch) -> None:
    hashes = {
        "feature_adoption:2026-01": {b"action:kb_draft_published": b"3", b"view:silence_debt": b"12"},
        "feature_adoption:2025-12": {b"action:silence_backtest_run": b"1"},
    }
    client = MagicMock()

    async def hgetall(key: str) -> dict[bytes, bytes]:
        return hashes.get(key, {})

    client.hgetall = hgetall
    monkeypatch.setattr(fa, "get_redis", lambda: client)

    # January exercises the year-boundary path for "previous month".
    data = await fa.get_feature_adoption(now=datetime(2026, 1, 15))
    assert data["months"]["2026-01"] == {
        "actions": {"kb_draft_published": 3},
        "views": {"silence_debt": 12},
    }
    assert data["months"]["2025-12"] == {"actions": {"silence_backtest_run": 1}, "views": {}}
    assert "auto-polling" in data["note"]


@pytest.mark.asyncio
async def test_get_adoption_degrades_per_month_on_redis_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()

    async def hgetall(key: str) -> dict[bytes, bytes]:
        raise RedisError("down")

    client.hgetall = hgetall
    monkeypatch.setattr(fa, "get_redis", lambda: client)
    data = await fa.get_feature_adoption(now=datetime(2026, 7, 16))
    assert data["months"]["2026-07"] == {"actions": {}, "views": {}}
