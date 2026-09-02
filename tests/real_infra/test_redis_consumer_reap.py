"""Consumer reaping against a real Redis stream group.

The mocked client in the default suite returns whatever a test hands it, so it
can prove the reaper's decisions but not that XINFO CONSUMERS and XGROUP
DELCONSUMER behave the way those decisions assume — in particular that a
consumer with pending entries is still listed after its worker is gone, and
that deleting a consumer really does discard its pending-entry list.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
import redis.asyncio as aioredis

from core.redis_streams import reap_idle_stream_consumers

pytestmark = [pytest.mark.real_services, pytest.mark.real_redis]

_STREAM = "reap-probe:queue"
_GROUP = "reap-probe-processors"


@pytest.fixture(autouse=True)
async def _clean_probe_stream():
    client = aioredis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    try:
        await client.delete(_STREAM)
        yield
        await client.delete(_STREAM)
    finally:
        await client.aclose()


async def _seed(client: Any) -> None:
    await client.xadd(_STREAM, {"payload": "one"})
    await client.xadd(_STREAM, {"payload": "two"})
    await client.xgroup_create(_STREAM, _GROUP, id="0")
    # `holder` reads one entry and never acks it, so it keeps a pending entry.
    await client.xreadgroup(_GROUP, "holder", {_STREAM: ">"}, count=1)
    # `empty` reads and acks, so it is registered with nothing pending.
    read = await client.xreadgroup(_GROUP, "empty", {_STREAM: ">"}, count=1)
    for _stream, entries in read:
        for entry_id, _fields in entries:
            await client.xack(_STREAM, _GROUP, entry_id)
    # `live` is this process; it must survive whatever its idle time says.
    await client.xreadgroup(_GROUP, "live", {_STREAM: ">"}, count=1)


async def test_only_the_acked_stranger_is_reaped(monkeypatch: pytest.MonkeyPatch) -> None:
    client = aioredis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    try:
        await _seed(client)
        # Every consumer is milliseconds old, so drop the threshold to 0 rather
        # than waiting a day; the guards under test are `pending` and `keep`.
        monkeypatch.setattr("core.redis_streams._CONSUMER_IDLE_REAP_MS", 0)

        removed = await reap_idle_stream_consumers(_STREAM, _GROUP, keep="live")

        assert removed == 1
        names = {str(c["name"]) for c in await client.xinfo_consumers(_STREAM, _GROUP)}
        assert names == {"holder", "live"}, "a consumer with pending entries, and this worker, both survive"
        # The reaped consumer had nothing pending, so nothing needs reclaiming.
        pending = await client.xpending(_STREAM, _GROUP)
        assert int(pending["pending"]) == 1
    finally:
        await client.aclose()


async def test_a_missing_group_is_not_an_error() -> None:
    """A fresh deployment reaps before the broker has created the group."""
    assert await reap_idle_stream_consumers("reap-probe:absent", _GROUP, keep="live") == 0
