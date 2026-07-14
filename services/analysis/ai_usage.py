"""AI usage audit logging."""

import asyncio

from sqlalchemy.exc import SQLAlchemyError

from core.logger import get_logger
from db.session import session_scope
from models import AIUsageLog
from services.analysis.analysis_policies import AIProviderPolicy

logger = get_logger("analysis.ai_usage")

# Usage rows are analytics-grade (they feed the /ai-usage dashboard), so they
# are buffered and flushed in batches instead of paying a dedicated
# INSERT+COMMIT per analyzed alert — otherwise even a cache hit costs an extra
# DB transaction on the hot path. Trade-off: up to _FLUSH_AFTER_SECONDS (or
# _BUFFER_MAX rows) of usage rows can be lost on a hard crash; process
# shutdown flushes via flush_ai_usage(). Alert persistence itself is not
# affected — this table is derived accounting, not source of truth.
_BUFFER_MAX = 50
_FLUSH_AFTER_SECONDS = 2.0

_buffer: list[AIUsageLog] = []
_buffer_lock = asyncio.Lock()
# A TimerHandle (not a sleeping Task) so an idle buffer never keeps a pending
# coroutine alive; the flush task is only created at fire time and retained in
# _active_flushes until done so it cannot be garbage-collected mid-write.
_flush_timer: asyncio.TimerHandle | None = None
_active_flushes: set[asyncio.Task[None]] = set()


async def log_ai_usage(
    route_type: str,
    alert_hash: str,
    source: str,
    model: str | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cache_hit: bool = False,
    policy: AIProviderPolicy | None = None,
) -> None:
    global _flush_timer
    try:
        policy = policy or AIProviderPolicy.from_config()
        cost = 0.0
        if tokens_in > 0:
            cost = policy.cost_for_tokens(tokens_in, tokens_out)
        row = AIUsageLog(
            model=model or policy.model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_estimate=cost,
            cache_hit=cache_hit,
            route_type=route_type,
            alert_hash=alert_hash,
            source=source,
        )
    except RuntimeError as e:
        logger.warning("Failed to build AI usage log row: %s", e)
        return

    rows_to_write: list[AIUsageLog] | None = None
    async with _buffer_lock:
        _buffer.append(row)
        if len(_buffer) >= _BUFFER_MAX:
            rows_to_write = list(_buffer)
            _buffer.clear()
        elif _flush_timer is None:
            _flush_timer = asyncio.get_running_loop().call_later(_FLUSH_AFTER_SECONDS, _run_scheduled_flush)
    if rows_to_write is not None:
        await _write_rows(rows_to_write)


async def flush_ai_usage() -> None:
    """Write out any buffered usage rows now (timer fire or process shutdown)."""
    global _flush_timer
    async with _buffer_lock:
        rows = list(_buffer)
        _buffer.clear()
        if _flush_timer is not None:
            _flush_timer.cancel()
            _flush_timer = None
    if rows:
        await _write_rows(rows)


def _run_scheduled_flush() -> None:
    global _flush_timer
    _flush_timer = None
    task = asyncio.ensure_future(flush_ai_usage())
    _active_flushes.add(task)
    task.add_done_callback(_active_flushes.discard)


async def _write_rows(rows: list[AIUsageLog]) -> None:
    try:
        async with session_scope() as session:
            session.add_all(rows)
    except (SQLAlchemyError, RuntimeError) as e:
        logger.warning("Failed to record %d AI usage log row(s): %s", len(rows), e)
