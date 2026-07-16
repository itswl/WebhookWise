"""Feature-adoption counters: is anyone actually using what we shipped?

Lightweight monthly counters in a Redis hash (`feature_adoption:{YYYY-MM}`),
incremented from feature endpoints and read back by the admin endpoint. This
exists to answer one product question after a release: which of the recently
shipped operator features get used, so the next iteration can double down or
delete. It is deliberately not OpenTelemetry — it is a product ledger consumed
by a human decision, not a service-health signal.

Interpretation guide (encoded in key naming):
- ``action:*`` — explicit operator actions (publish, create, export). Strong
  adoption signals.
- ``view:*`` — GET endpoints. Weak signals: the dashboard may auto-poll some
  of them, so treat as an upper bound.

Recording is fail-silent: adoption accounting must never break a feature.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from redis.exceptions import RedisError

from core.datetime_utils import utcnow
from core.logger import get_logger
from core.redis_client import get_redis

logger = get_logger("operations.feature_adoption")

_KEY_PREFIX = "feature_adoption:"
_RETENTION_SECONDS = 400 * 24 * 3600  # keep ~13 months of monthly hashes
_ADOPTION_ERRORS = (RedisError, RuntimeError, OSError, TypeError, ValueError)


def _month_key(now: datetime | None = None) -> str:
    stamp = now or utcnow()
    return f"{_KEY_PREFIX}{stamp.strftime('%Y-%m')}"


async def record_feature_use(feature: str, *, now: datetime | None = None) -> None:
    """Increment this month's counter for `feature`; never raises."""
    try:
        client = get_redis()
        key = _month_key(now)
        await client.hincrby(key, feature, 1)
        await client.expire(key, _RETENTION_SECONDS)
    except _ADOPTION_ERRORS as e:
        logger.debug("[FeatureAdoption] Recording %s failed (ignored): %s", feature, e)


async def get_feature_adoption(*, now: datetime | None = None) -> dict[str, Any]:
    """Current + previous month counters, split into actions and views."""
    stamp = now or utcnow()
    first_of_month = stamp.replace(day=1)
    prev_month_stamp = first_of_month.replace(
        year=first_of_month.year - 1 if first_of_month.month == 1 else first_of_month.year,
        month=12 if first_of_month.month == 1 else first_of_month.month - 1,
    )
    months: dict[str, dict[str, dict[str, int]]] = {}
    for month_stamp in (stamp, prev_month_stamp):
        label = month_stamp.strftime("%Y-%m")
        counters: dict[str, int] = {}
        try:
            raw = await get_redis().hgetall(_month_key(month_stamp))
        except _ADOPTION_ERRORS as e:
            logger.warning("[FeatureAdoption] Read failed for %s: %s", label, e)
            raw = {}
        for field, value in (raw or {}).items():
            name = field.decode("utf-8", "replace") if isinstance(field, (bytes, bytearray)) else str(field)
            try:
                counters[name] = int(value)
            except (TypeError, ValueError):
                continue
        months[label] = {
            "actions": {k.removeprefix("action:"): v for k, v in sorted(counters.items()) if k.startswith("action:")},
            "views": {k.removeprefix("view:"): v for k, v in sorted(counters.items()) if k.startswith("view:")},
        }
    return {
        "months": months,
        "note": (
            "actions are explicit operator actions (strong adoption signals); "
            "views are GET hits and may include dashboard auto-polling (upper bound)"
        ),
    }
