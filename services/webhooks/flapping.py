"""Per-identity status-flapping detection (firing↔recovered oscillation).

An alert identity (source + upstream rule name) that keeps flipping between
firing and recovered produces a notification storm that dedup cannot absorb:
each recovery "resolves" the thread and the next firing starts a fresh one.
This module tracks status flips per identity in a Redis window and reports an
identity as *flapping* once the flip count crosses the threshold.

Detection is always on and fail-open (Redis trouble → not flapping). Whether a
flapping identity's notifications are actually withheld is a separate opt-in
(`FLAPPING_SUPPRESS_ENABLED`), applied in the forward decision with skip_code
"flapping" so every suppressed card stays visible in the decision trace.

Distinct from the rule-audit "flapping" flag (a chronic fires-most-days
heuristic over the DB): this is realtime oscillation within minutes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from redis.exceptions import RedisError

from core.datetime_utils import utcnow
from core.logger import get_logger
from core.redis_client import get_redis, redis_eval_int

logger = get_logger("webhooks.flapping")

_LAST_KEY = "flap:last:{digest}"
_FLIPS_KEY = "flap:flips:{digest}"
ACTIVE_FLAPPING_KEY = "flap:active"

# Atomically record a status observation and return the flip count in-window.
# KEYS[1]=last-status key, KEYS[2]=flips zset, KEYS[3]=active zset
# ARGV[1]=now_ms, ARGV[2]=status, ARGV[3]=window_ms, ARGV[4]=min_transitions,
# ARGV[5]=identity label
_OBSERVE_LUA = """
local last = redis.call('GET', KEYS[1])
if last and last ~= ARGV[2] then
  redis.call('ZADD', KEYS[2], ARGV[1], ARGV[1])
end
redis.call('SET', KEYS[1], ARGV[2], 'PX', ARGV[3] * 4)
redis.call('ZREMRANGEBYSCORE', KEYS[2], 0, ARGV[1] - ARGV[3])
redis.call('PEXPIRE', KEYS[2], ARGV[3] * 4)
local flips = redis.call('ZCARD', KEYS[2])
if flips >= tonumber(ARGV[4]) then
  redis.call('ZADD', KEYS[3], ARGV[1] + ARGV[3], ARGV[5])
  redis.call('PEXPIRE', KEYS[3], ARGV[3] * 8)
end
return flips
"""

_FLAPPING_ERRORS = (RedisError, RuntimeError, OSError, TypeError, ValueError)


@dataclass(frozen=True, slots=True)
class FlappingPolicy:
    window_minutes: int = 10
    min_transitions: int = 6
    suppress_enabled: bool = False

    @classmethod
    def from_config(cls) -> FlappingPolicy:
        from core.app_context import get_config_manager

        noise = get_config_manager().noise
        return cls(
            window_minutes=int(noise.FLAPPING_WINDOW_MINUTES),
            min_transitions=int(noise.FLAPPING_MIN_TRANSITIONS),
            suppress_enabled=bool(noise.FLAPPING_SUPPRESS_ENABLED),
        )


@dataclass(frozen=True, slots=True)
class FlappingStatus:
    identity: str
    flips: int
    flapping: bool


def flap_identity(source: str, parsed_data: dict[str, Any] | None) -> str:
    """The oscillating unit: source + upstream rule/alert name.

    Same identity a firing alert shares with its recovery counterpart (their
    dedup keys differ by status, so dedup_key is NOT usable here).
    """
    parsed = parsed_data if isinstance(parsed_data, dict) else {}
    rule = str(parsed.get("RuleName") or parsed.get("AlertName") or parsed.get("alert_name") or "").strip()
    return f"{source or 'unknown'}::{rule or 'unknown'}"


def _digest(identity: str) -> str:
    return hashlib.sha1(identity.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


async def observe_flapping(
    source: str,
    parsed_data: dict[str, Any] | None,
    ai_analysis: dict[str, Any] | None,
    *,
    policy: FlappingPolicy | None = None,
    now: datetime | None = None,
) -> FlappingStatus:
    """Record this event's status for its identity and report flap state.

    Fail-open: any Redis problem reports "not flapping" — a probe gap must
    never suppress (or delay) a notification.
    """
    from services.incidents.grouping import is_recovery_payload

    policy = policy or FlappingPolicy.from_config()
    identity = flap_identity(source, parsed_data)
    status = "recovery" if is_recovery_payload(parsed_data, ai_analysis) else "firing"
    now_ms = int((now or utcnow()).timestamp() * 1000)
    window_ms = policy.window_minutes * 60 * 1000
    digest = _digest(identity)
    try:
        flips = await redis_eval_int(
            _OBSERVE_LUA,
            3,
            _LAST_KEY.format(digest=digest),
            _FLIPS_KEY.format(digest=digest),
            ACTIVE_FLAPPING_KEY,
            now_ms,
            status,
            window_ms,
            policy.min_transitions,
            identity,
        )
    except _FLAPPING_ERRORS as e:
        logger.warning("[Flapping] Observation failed (fail-open) identity=%s error=%s", identity, e)
        return FlappingStatus(identity=identity, flips=0, flapping=False)
    flip_count = int(flips or 0)
    is_flapping = flip_count >= policy.min_transitions
    if is_flapping:
        logger.info(
            "[Flapping] Identity is flapping identity=%s flips=%d window_minutes=%d suppress=%s",
            identity,
            flip_count,
            policy.window_minutes,
            policy.suppress_enabled,
        )
    return FlappingStatus(identity=identity, flips=flip_count, flapping=is_flapping)


async def list_active_flapping(limit: int = 20, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Identities currently marked flapping (advisory, for the Action Center)."""
    now_ms = int((now or utcnow()).timestamp() * 1000)
    try:
        client = get_redis()
        await client.zremrangebyscore(ACTIVE_FLAPPING_KEY, "-inf", now_ms)
        rows = await client.zrevrange(ACTIVE_FLAPPING_KEY, 0, max(0, limit - 1), withscores=True)
    except _FLAPPING_ERRORS as e:
        logger.warning("[Flapping] Active listing failed (fail-open): %s", e)
        return []
    items: list[dict[str, Any]] = []
    for member, score in rows:
        label = member.decode("utf-8", "replace") if isinstance(member, (bytes, bytearray)) else str(member)
        items.append({"identity": label, "quiet_at_ms": int(score)})
    return items
