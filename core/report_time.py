"""The one operator-facing timezone, and how a time is labelled in it.

``Asia/Shanghai`` used to be written into three unrelated places — the periodic
report's cron zone, the Feishu card timestamp suffix (as a hard-coded fixed
+08:00 offset AND the literal text " UTC+8"), and the maintenance-window
default. A deployment outside that zone had to patch source in three files, and
the card would have kept claiming UTC+8 regardless. They now all read
``REPORT_TIMEZONE``.

This is display and scheduling only. Everything stored is naive UTC
(core/datetime_utils.py) and nothing here changes that.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.app_context import get_config_manager
from core.logger import get_logger

_logger = get_logger("report_time")

UTC_ZONE = ZoneInfo("UTC")


def report_timezone_name() -> str:
    """The configured IANA zone name.

    Validated at config load, so a deployment learns about a typo at startup
    rather than when the first report is due.
    """
    return str(getattr(get_config_manager().notifications, "REPORT_TIMEZONE", "") or "UTC")


def report_timezone() -> ZoneInfo:
    """The configured zone. Resolved per call — a module constant would freeze
    whatever the config said at import time, which no test could then override."""
    return resolve_timezone(report_timezone_name())


def resolve_timezone(name: str | None, *, context: str = "") -> ZoneInfo:
    """``ZoneInfo`` for an IANA name, falling back to UTC for an unknown one.

    ``REPORT_TIMEZONE`` is validated at config load, but a maintenance window
    carries an operator-supplied zone per row that nothing else checks. A bad
    name must not take the sweep down.
    """
    if not name:
        return UTC_ZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        _logger.warning("[ReportTime] Unknown timezone %r%s, falling back to UTC", name, context)
        return UTC_ZONE


def format_utc_offset(moment: datetime) -> str:
    """Label an aware datetime's offset generically: UTC, UTC+8, UTC+5:30, UTC-7.

    Derived from the instant, not from a constant, so a zone that observes DST
    is labelled with the offset that actually applied. Sub-minute offsets (LMT,
    which only appears for pre-1900 timestamps) are truncated to the minute.
    """
    offset = moment.utcoffset()
    total_seconds = int(offset.total_seconds()) if offset is not None else 0
    if total_seconds == 0:
        return "UTC"
    sign = "+" if total_seconds > 0 else "-"
    hours, remainder = divmod(abs(total_seconds), 3600)
    minutes = remainder // 60
    if minutes == 0:
        return f"UTC{sign}{hours}"
    return f"UTC{sign}{hours}:{minutes:02d}"


__all__ = [
    "UTC_ZONE",
    "format_utc_offset",
    "report_timezone",
    "report_timezone_name",
    "resolve_timezone",
]
