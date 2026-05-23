from core.redis_health import (
    ai_error_alert_lock,
    openclaw_poller_stability,
    rate_limit_burst,
    rate_limit_global,
    rate_limit_sustained,
    scheduled_task_lock,
    webhook_dedupe,
    webhook_global_task_slots,
    webhook_processing_lock,
    webhook_processing_queue,
)


def test_redis_key_builders_preserve_current_schema() -> None:
    assert webhook_dedupe("abc") == "webhook:dedupe:abc"
    assert webhook_processing_queue("abc") == "queue:webhook:abc"
    assert webhook_processing_lock("abc") == "lock:webhook:alert:abc"
    assert rate_limit_burst("1.2.3.4") == "rl:b:1.2.3.4"
    assert rate_limit_sustained("1.2.3.4") == "rl:s:1.2.3.4"
    assert rate_limit_global() == "rl:g"
    assert ai_error_alert_lock("hash") == "ai_error_alert_lock:hash"
    assert scheduled_task_lock("job") == "scheduled-task-lock:job"
    assert webhook_global_task_slots() == "webhook:global-task-slots"
    assert openclaw_poller_stability(42) == "openclaw:poller:stability:42"
