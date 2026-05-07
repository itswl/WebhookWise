"""兼容导出层。

新代码请按职责直接依赖：
- services.webhook_command_service: 接收、保存、重放等命令操作
- services.webhook_query_service: 列表、分页、dead letter/stuck 视图查询
"""

from db.session import session_scope
from services.webhook_command_service import (
    SaveWebhookResult,
    _resolve_analysis_for_duplicate,
    get_client_ip,
    quick_receive_webhook,
    replay_dead_letter,
    requeue_stuck_event,
    save_webhook_data,
)
from services.webhook_query_service import (
    _row_to_summary_dict,
    count_dead_letters,
    list_dead_letters,
    list_stuck_events,
    list_webhook_summaries,
    list_webhook_summaries_cursor,
)


async def get_all_webhooks(
    page: int = 1, page_size: int = 20, cursor_id: int | None = None, fields: str = "summary"
) -> tuple[list[dict[str, object]], int, int | None]:
    """旧入口兼容：新代码请直接使用 services.webhook_query_service.get_all_webhooks。"""
    async with session_scope() as session:
        items, has_more, next_cursor = await list_webhook_summaries(session, cursor_id=cursor_id, page_size=page_size)
        return items, -1, next_cursor


__all__ = [
    "SaveWebhookResult",
    "_resolve_analysis_for_duplicate",
    "_row_to_summary_dict",
    "count_dead_letters",
    "get_all_webhooks",
    "get_client_ip",
    "list_dead_letters",
    "list_stuck_events",
    "list_webhook_summaries",
    "list_webhook_summaries_cursor",
    "quick_receive_webhook",
    "replay_dead_letter",
    "requeue_stuck_event",
    "save_webhook_data",
]
