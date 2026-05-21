"""Redis Lua script registry."""

from importlib.resources import files


def _read_script(name: str) -> str:
    return files(__package__).joinpath(name).read_text(encoding="utf-8")


ALERT_REFRESH_LOCK_IF_OWNER = _read_script("alert_refresh_lock_if_owner.lua")
ALERT_RELEASE_LOCK_IF_OWNER = _read_script("alert_release_lock_if_owner.lua")
ALERT_RELEASE_QUEUE_SLOT = _read_script("alert_release_queue_slot.lua")
ALERT_RESERVE_QUEUE_SLOT = _read_script("alert_reserve_queue_slot.lua")
CIRCUIT_BREAKER_CHECK_STATE = _read_script("circuit_breaker_check_state.lua")
CIRCUIT_BREAKER_RECORD_FAILURE = _read_script("circuit_breaker_record_failure.lua")
CIRCUIT_BREAKER_RECORD_SUCCESS = _read_script("circuit_breaker_record_success.lua")
SLIDING_WINDOW_RATE_LIMIT = _read_script("sliding_window_rate_limit.lua")
TASK_SLOT_ACQUIRE = _read_script("task_slot_acquire.lua")
TASK_SLOT_RELEASE = _read_script("task_slot_release.lua")
TASK_SLOT_RENEW = _read_script("task_slot_renew.lua")

__all__ = [
    "ALERT_REFRESH_LOCK_IF_OWNER",
    "ALERT_RELEASE_LOCK_IF_OWNER",
    "ALERT_RELEASE_QUEUE_SLOT",
    "ALERT_RESERVE_QUEUE_SLOT",
    "CIRCUIT_BREAKER_CHECK_STATE",
    "CIRCUIT_BREAKER_RECORD_FAILURE",
    "CIRCUIT_BREAKER_RECORD_SUCCESS",
    "SLIDING_WINDOW_RATE_LIMIT",
    "TASK_SLOT_ACQUIRE",
    "TASK_SLOT_RELEASE",
    "TASK_SLOT_RENEW",
]
