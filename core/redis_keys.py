"""Central Redis key builders.

The returned strings preserve the current production key schema. Centralizing
them here makes future prefix migrations auditable instead of scattered.
"""


def webhook_dedupe(alert_hash: str) -> str:
    return f"webhook:dedupe:{alert_hash}"


def webhook_processing_queue(alert_hash: str) -> str:
    return f"queue:webhook:{alert_hash}"


def webhook_processing_lock(alert_hash: str) -> str:
    return f"lock:webhook:alert:{alert_hash}"


def rate_limit_burst(client_ip: str) -> str:
    return f"rl:b:{client_ip}"


def rate_limit_sustained(client_ip: str) -> str:
    return f"rl:s:{client_ip}"


def rate_limit_global() -> str:
    return "rl:g"


def ai_error_alert_lock(error_hash: str) -> str:
    return f"ai_error_alert_lock:{error_hash}"


def scheduled_task_lock(name: str) -> str:
    return f"scheduled-task-lock:{name}"


def webhook_global_task_slots() -> str:
    return "webhook:global-task-slots"


def openclaw_poller_stability(record_id: int) -> str:
    return f"openclaw:poller:stability:{record_id}"
