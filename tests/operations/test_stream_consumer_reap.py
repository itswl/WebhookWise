"""Reaping dead consumers out of the webhook stream group.

TaskIQ registers one consumer per worker, named after WORKER_ID, and never
removes it. Production measured 127 consumers in `webhook-processors` with one
process alive — every one of the other 126 a restart from some earlier deploy.
XINFO CONSUMERS is O(that number) and sits on the queue-health path.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

import services.operations.taskiq_wiring as wiring
from core.redis_streams import _CONSUMER_IDLE_REAP_MS, reap_idle_stream_consumers

_DEAD_MS = _CONSUMER_IDLE_REAP_MS + 1
_STREAM = "webhook:queue"
_GROUP = "webhook-processors"


def _consumer(name: str, *, idle: int, pending: int = 0, inactive: int | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": name, "pending": pending, "idle": idle}
    if inactive is not None:
        entry["inactive"] = inactive
    return entry


def _stub(mock_redis: Any, consumers: object) -> AsyncMock:
    """Point the mocked client's XINFO CONSUMERS at ``consumers``."""
    if isinstance(consumers, Exception):
        mock_redis.xinfo_consumers = AsyncMock(side_effect=consumers)
    else:
        mock_redis.xinfo_consumers = AsyncMock(return_value=consumers)
    delconsumer = AsyncMock(return_value=1)
    mock_redis.xgroup_delconsumer = delconsumer
    return delconsumer


async def test_removes_only_the_long_idle_consumers_with_nothing_pending(mock_redis: Any) -> None:
    delconsumer = _stub(
        mock_redis,
        [
            _consumer("gone-1", idle=_DEAD_MS),
            _consumer("gone-2", idle=_DEAD_MS * 7),
            _consumer("busy-recently", idle=5_000),
        ],
    )

    assert await reap_idle_stream_consumers(_STREAM, _GROUP, keep="me") == 2
    assert sorted(call.args[2] for call in delconsumer.await_args_list) == ["gone-1", "gone-2"]


async def test_a_consumer_holding_pending_entries_is_left_alone(mock_redis: Any) -> None:
    """Deleting it discards its pending-entry list; those messages then need
    XAUTOCLAIM to come back. However dead the worker is, the work is not."""
    delconsumer = _stub(mock_redis, [_consumer("dead-but-owed", idle=_DEAD_MS * 30, pending=3)])

    assert await reap_idle_stream_consumers(_STREAM, _GROUP, keep="me") == 0
    delconsumer.assert_not_awaited()


async def test_this_worker_is_never_reaped(mock_redis: Any) -> None:
    """A worker restarting under its old WORKER_ID inherits the old consumer's
    idle time, and would otherwise delete the registration it just made."""
    delconsumer = _stub(mock_redis, [_consumer("me", idle=_DEAD_MS * 99)])

    assert await reap_idle_stream_consumers(_STREAM, _GROUP, keep="me") == 0
    delconsumer.assert_not_awaited()


async def test_inactive_is_not_the_signal(mock_redis: Any) -> None:
    """A live worker polling an empty queue has a tiny `idle` and an unbounded
    `inactive`. Reading `inactive` would delete exactly the running workers."""
    delconsumer = _stub(mock_redis, [_consumer("alive-but-unfed", idle=900, inactive=_DEAD_MS * 5)])

    assert await reap_idle_stream_consumers(_STREAM, _GROUP, keep="me") == 0
    delconsumer.assert_not_awaited()


@pytest.mark.parametrize(
    "reply",
    [
        [],
        None,
        "not-a-list",
        ["not-a-dict", 7],
        [{"pending": 0, "idle": _DEAD_MS}],  # no name
        [{"name": "no-idle-field", "pending": 0}],  # pre-7.0-shaped reply
        [{"name": "junk", "pending": "?", "idle": "?"}],
    ],
)
async def test_a_reply_we_cannot_read_removes_nothing(mock_redis: Any, reply: object) -> None:
    delconsumer = _stub(mock_redis, reply)

    assert await reap_idle_stream_consumers(_STREAM, _GROUP, keep="me") == 0
    delconsumer.assert_not_awaited()


async def test_xinfo_failing_fails_open(mock_redis: Any) -> None:
    """No group yet on a fresh deployment, or no Redis at all."""
    _stub(mock_redis, RuntimeError("NOGROUP No such key or consumer group"))

    assert await reap_idle_stream_consumers(_STREAM, _GROUP, keep="me") == 0


async def test_one_failing_delete_does_not_strand_the_rest(mock_redis: Any) -> None:
    _stub(mock_redis, [_consumer(f"gone-{i}", idle=_DEAD_MS) for i in range(3)])
    mock_redis.xgroup_delconsumer = AsyncMock(side_effect=[RuntimeError("boom"), 1, 1])

    assert await reap_idle_stream_consumers(_STREAM, _GROUP, keep="me") == 2


async def test_the_worker_startup_hook_reaps_with_its_own_name_kept(mock_redis: Any) -> None:
    """The hook must pass the running worker's consumer name as `keep`, or the
    first thing a restart does is delete the registration it just made."""
    me = wiring._settings.consumer_name
    delconsumer = _stub(mock_redis, [_consumer(me, idle=_DEAD_MS * 4), _consumer("gone", idle=_DEAD_MS)])

    assert (
        await reap_idle_stream_consumers(wiring._settings.queue_name, wiring._settings.consumer_group_name, keep=me)
        == 1
    )
    assert [call.args[2] for call in delconsumer.await_args_list] == ["gone"]
