"""REPORT_TIMEZONE: one setting behind the report cron, the card stamp, and the
maintenance-window default.

Asia/Shanghai used to be written into three unrelated files, and the Feishu card
additionally hard-coded both a fixed +08:00 offset and the literal text
" UTC+8" — so a deployment elsewhere got Beijing times under a label that
happened to be true only in Beijing. Every case here runs a NON-default zone;
the default is covered by the existing suites.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from core.config.defaults import NotificationConfig
from core.report_time import format_utc_offset, report_timezone, report_timezone_name
from models import MaintenanceWindow
from services.notifications.digest_cards import _format_window
from services.notifications.feishu_cards import _format_card_time
from services.operations.periodic_report import _most_recent_fire
from services.silences.maintenance_windows import _window_tz


def test_config_rejects_a_timezone_that_is_not_an_iana_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPORT_TIMEZONE", "Mars/Olympus_Mons")
    with pytest.raises(ValueError, match="REPORT_TIMEZONE"):
        NotificationConfig()

    monkeypatch.setenv("REPORT_TIMEZONE", "  ")
    with pytest.raises(ValueError, match="REPORT_TIMEZONE"):
        NotificationConfig()

    monkeypatch.setenv("REPORT_TIMEZONE", "Asia/Kolkata")
    assert NotificationConfig().REPORT_TIMEZONE == "Asia/Kolkata"


def test_the_resolved_zone_follows_the_setting(temp_config: Any) -> None:
    temp_config.notifications.REPORT_TIMEZONE = "America/New_York"
    assert report_timezone_name() == "America/New_York"
    # 2026-06-16 is inside US DST, so the zone resolves to a -4 offset, not -5:
    # the zone is looked up per call rather than frozen at import.
    moment = datetime(2026, 6, 16, 12, 0, tzinfo=UTC).astimezone(report_timezone())
    assert format_utc_offset(moment) == "UTC-4"


@pytest.mark.parametrize(
    ("zone", "instant", "expected"),
    [
        ("Asia/Shanghai", datetime(2026, 6, 16, 12, 0, tzinfo=UTC), "UTC+8"),
        # Half-hour offset: the old fixed timezone(timedelta(hours=8)) could not
        # express it and the literal suffix would have lied about it.
        ("Asia/Kolkata", datetime(2026, 6, 16, 12, 0, tzinfo=UTC), "UTC+5:30"),
        ("America/Los_Angeles", datetime(2026, 6, 16, 12, 0, tzinfo=UTC), "UTC-7"),
        # Same zone, winter: the label follows the instant, not the zone.
        ("America/Los_Angeles", datetime(2026, 1, 16, 12, 0, tzinfo=UTC), "UTC-8"),
        ("America/St_Johns", datetime(2026, 6, 16, 12, 0, tzinfo=UTC), "UTC-2:30"),
        ("UTC", datetime(2026, 6, 16, 12, 0, tzinfo=UTC), "UTC"),
    ],
)
def test_offset_labels_are_derived_not_hard_coded(
    temp_config: Any, zone: str, instant: datetime, expected: str
) -> None:
    temp_config.notifications.REPORT_TIMEZONE = zone
    assert format_utc_offset(instant.astimezone(report_timezone())) == expected


def test_report_cron_fires_in_the_configured_zone(temp_config: Any) -> None:
    # "0 9 * * *" in New York during DST (UTC-4) is 13:00 UTC, not the 01:00 UTC
    # that the same cron produces in Asia/Shanghai.
    temp_config.notifications.REPORT_TIMEZONE = "America/New_York"
    now = datetime(2026, 6, 16, 14, 30, tzinfo=UTC)
    assert _most_recent_fire("0 9 * * *", now, 24 * 60 + 60) == datetime(2026, 6, 16, 13, 0, tzinfo=UTC)

    temp_config.notifications.REPORT_TIMEZONE = "Asia/Shanghai"
    assert _most_recent_fire("0 9 * * *", now, 24 * 60 + 60) == datetime(2026, 6, 16, 1, 0, tzinfo=UTC)


def test_card_timestamp_renders_in_the_configured_zone(temp_config: Any) -> None:
    temp_config.notifications.REPORT_TIMEZONE = "Asia/Kolkata"
    # 2026-06-03 01:02:03 UTC + 05:30 = 06:32:03 local.
    assert _format_card_time("2026-06-03T01:02:03Z") == "2026-06-03 06:32:03 UTC+5:30"

    temp_config.notifications.REPORT_TIMEZONE = "UTC"
    assert _format_card_time("2026-06-03T01:02:03Z") == "2026-06-03 01:02:03 UTC"


def test_digest_window_label_follows_the_configured_zone(temp_config: Any) -> None:
    temp_config.notifications.REPORT_TIMEZONE = "Europe/Berlin"
    # 02:00–03:00 UTC in June (CEST, UTC+2) is 04:00–05:00 local.
    assert _format_window(datetime(2026, 9, 2, 2, 0), datetime(2026, 9, 2, 3, 0)) == ("2026-09-02 04:00 – 05:00 UTC+2")


def test_a_maintenance_window_without_its_own_zone_uses_the_setting(temp_config: Any) -> None:
    temp_config.notifications.REPORT_TIMEZONE = "Europe/Berlin"
    window = MaintenanceWindow(id=1, name="w", days_of_week="1", start_minute=0, duration_minutes=60, timezone="")
    assert str(_window_tz(window)) == "Europe/Berlin"

    # A window that names its own zone still wins over the default.
    window.timezone = "Asia/Tokyo"
    assert str(_window_tz(window)) == "Asia/Tokyo"

    # And an unusable one degrades to UTC instead of taking the sweep down.
    window.timezone = "Mars/Olympus_Mons"
    assert str(_window_tz(window)) == "UTC"
