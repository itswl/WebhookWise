"""Scheduled periodic-report task definitions (daily / weekly / monthly).

Split from tasks.py purely for file size; task names are unchanged, so queued
schedules and metrics labels are unaffected. Registration happens because
taskiq_wiring imports this module next to tasks.
"""

from core.taskiq_broker import broker
from services.operations.tasks import _REPORT_CRON_OFFSET, _run_scheduled


def _daily_report_cron() -> str:
    from core.app_context import get_config_manager

    return str(get_config_manager().notifications.DAILY_REPORT_CRON)


def _weekly_report_cron() -> str:
    from core.app_context import get_config_manager

    return str(get_config_manager().notifications.WEEKLY_REPORT_CRON)


def _monthly_report_cron() -> str:
    from core.app_context import get_config_manager

    return str(get_config_manager().notifications.MONTHLY_REPORT_CRON)


@broker.task(
    task_name="scheduled_daily_report",
    schedule=[{"cron": _daily_report_cron(), "cron_offset": _REPORT_CRON_OFFSET}],
)
async def scheduled_daily_report() -> None:
    from services.operations.periodic_report import check_ai_cost_budget, generate_and_send_report

    # Internally a no-op unless DAILY_REPORT_ENABLED; the leader lock prevents
    # duplicate sends if more than one scheduler is ever running.
    await _run_scheduled("daily_report", 86400, generate_and_send_report("daily"))
    # Piggyback the AI cost budget check on the daily cadence: no-op unless a
    # budget is set, and self-limits to one alert per month via a Redis NX claim.
    await _run_scheduled("ai_cost_budget_check", 86400, check_ai_cost_budget())


@broker.task(
    task_name="scheduled_weekly_report",
    schedule=[{"cron": _weekly_report_cron(), "cron_offset": _REPORT_CRON_OFFSET}],
)
async def scheduled_weekly_report() -> None:
    from services.operations.periodic_report import generate_and_send_report

    # Internally a no-op unless WEEKLY_REPORT_ENABLED; the leader lock prevents
    # duplicate sends if more than one scheduler is ever running.
    await _run_scheduled("weekly_report", 86400, generate_and_send_report("weekly"))


@broker.task(
    task_name="scheduled_monthly_report",
    schedule=[{"cron": _monthly_report_cron(), "cron_offset": _REPORT_CRON_OFFSET}],
)
async def scheduled_monthly_report() -> None:
    from services.operations.periodic_report import generate_and_send_report

    # Internally a no-op unless MONTHLY_REPORT_ENABLED; the leader lock prevents
    # duplicate sends if more than one scheduler is ever running.
    await _run_scheduled("monthly_report", 86400, generate_and_send_report("monthly"))
