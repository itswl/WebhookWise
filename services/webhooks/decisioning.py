from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Protocol

from core.app_context import get_config_manager
from core.observability.metrics import FORWARD_RULE_MATCH_TOTAL
from core.text import split_csv_lower
from services.webhooks.types import (
    AnalysisResult,
    NoiseReductionContext,
)


@dataclass
class ForwardDecision:
    should_forward: bool
    skip_reason: str | None
    is_periodic_reminder: bool
    matched_rules: list[ForwardRuleSnapshot] = field(default_factory=list)


class NotifiedEvent(Protocol):
    last_notified_at: datetime | None


@dataclass(frozen=True)
class ForwardRuleSnapshot:
    id: int | None
    name: str
    match_event_type: str
    match_importance: str
    match_source: str
    match_duplicate: str
    match_payload: str
    target_type: str
    target_url: str
    stop_on_match: bool
    target_name: str = ""


@dataclass(frozen=True)
class ForwardingPolicy:
    notification_cooldown_seconds: int
    enable_periodic_reminder: bool
    reminder_interval_hours: int
    forward_duplicate_alerts: bool
    default_target_url: str = ""


def normalize_importance(value: Any) -> str:
    text = str(value or "").strip().lower()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text


def build_final_analysis(analysis_result: AnalysisResult, noise: NoiseReductionContext) -> AnalysisResult:
    final_analysis = analysis_result.copy()
    final_analysis["noise_reduction"] = asdict(noise)
    return final_analysis


def _rule_matches(
    rule: ForwardRuleSnapshot,
    *,
    event_type: str = "",
    importance: str = "",
    source: str = "",
    is_duplicate: bool = False,
    parsed_data: dict[str, Any] | None = None,
) -> bool:
    if rule.match_event_type and event_type not in split_csv_lower(rule.match_event_type):
        return False
    if rule.match_importance and importance not in split_csv_lower(rule.match_importance):
        return False
    if rule.match_source and source.lower() not in split_csv_lower(rule.match_source):
        return False
    if rule.match_duplicate and rule.match_duplicate != "all":
        if rule.match_duplicate == "new" and is_duplicate:
            return False
        if rule.match_duplicate == "duplicate" and not is_duplicate:
            return False
    match_payload = getattr(rule, "match_payload", "") or ""
    return not (match_payload and not _payload_matches(match_payload, parsed_data or {}))


def _find_in_payload(payload: Any, key: str) -> Any:
    if not key:
        return None
    if isinstance(payload, dict):
        if key in payload:
            return payload[key]
        for value in payload.values():
            found = _find_in_payload(value, key)
            if found is not None:
                return found
        return None
    if isinstance(payload, list):
        for item in payload:
            found = _find_in_payload(item, key)
            if found is not None:
                return found
    return None


def _get_by_path(payload: Any, path: str) -> Any:
    if not path:
        return None
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _payload_matches(match_payload: str, parsed_data: dict[str, Any]) -> bool:
    if not match_payload:
        return True
    for pair in match_payload.split(","):
        raw = pair.strip()
        if not raw:
            continue
        key, sep, expected = raw.partition("=")
        if not sep:
            return False
        key = key.strip()
        expected = expected.strip()
        if not key:
            return False
        found = _get_by_path(parsed_data, key) if "." in key else _find_in_payload(parsed_data, key)
        if found is None:
            return False
        if str(found).strip() != expected:
            return False
    return True


def select_forward_rules(
    rules: list[ForwardRuleSnapshot],
    *,
    event_type: str = "",
    importance: str = "",
    source: str = "",
    is_duplicate: bool = False,
    parsed_data: dict[str, Any] | None = None,
) -> list[ForwardRuleSnapshot]:
    matched_rules: list[ForwardRuleSnapshot] = []
    for rule in rules:
        if not _rule_matches(
            rule,
            event_type=event_type,
            importance=importance,
            source=source,
            is_duplicate=is_duplicate,
            parsed_data=parsed_data,
        ):
            continue
        matched_rules.append(rule)
        FORWARD_RULE_MATCH_TOTAL.labels(rule.name, rule.target_type).inc()
        if rule.stop_on_match:
            break
    return matched_rules


def _decide_duplicate_alert(
    *,
    base_should_forward: bool,
    seconds_since_notify: float | None,
    policy: ForwardingPolicy,
    matched_rules: list[ForwardRuleSnapshot],
) -> ForwardDecision:
    if seconds_since_notify is not None and seconds_since_notify < policy.notification_cooldown_seconds:
        return ForwardDecision(False, "窗口内重复告警，刚刚已转发", False)

    if (
        policy.enable_periodic_reminder
        and seconds_since_notify is not None
        and seconds_since_notify / 3600 >= policy.reminder_interval_hours
    ):
        return ForwardDecision(
            base_should_forward,
            None if base_should_forward else "定期提醒：未匹配转发规则",
            True,
            matched_rules=matched_rules,
        )

    if not policy.forward_duplicate_alerts:
        return ForwardDecision(False, "窗口内重复告警，配置跳过转发", False)

    return ForwardDecision(
        base_should_forward,
        None if base_should_forward else "窗口内重复告警，未匹配转发规则",
        False,
        matched_rules=matched_rules,
    )


def decide_forwarding(
    *,
    event_type: str = "",
    importance: str = "",
    is_duplicate: bool = False,
    noise: NoiseReductionContext | None = None,
    original_event: NotifiedEvent | None = None,
    source: str = "",
    rules: list[ForwardRuleSnapshot],
    policy: ForwardingPolicy,
    parsed_data: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> ForwardDecision:
    if noise and noise.suppress_forward:
        return ForwardDecision(False, f"智能降噪抑制转发: {noise.reason}", False)

    matched_rules = select_forward_rules(
        rules,
        event_type=event_type,
        importance=importance,
        source=source,
        is_duplicate=is_duplicate,
        parsed_data=parsed_data,
    )
    current_time = now or datetime.now()
    base_should_fwd = bool(matched_rules)

    if is_duplicate:
        last_notified_at = original_event.last_notified_at if original_event else None
        seconds_since_notify = (current_time - last_notified_at).total_seconds() if last_notified_at is not None else None
        return _decide_duplicate_alert(
            base_should_forward=base_should_fwd,
            seconds_since_notify=seconds_since_notify,
            policy=policy,
            matched_rules=matched_rules,
        )

    return ForwardDecision(
        base_should_fwd,
        None if base_should_fwd else "未匹配转发规则",
        False,
        matched_rules=matched_rules,
    )


def forwarding_policy_from_config(config: Any | None = None) -> ForwardingPolicy:
    config = config or get_config_manager()
    return ForwardingPolicy(
        notification_cooldown_seconds=config.retry.NOTIFICATION_COOLDOWN_SECONDS,
        enable_periodic_reminder=config.retry.ENABLE_PERIODIC_REMINDER,
        reminder_interval_hours=config.retry.REMINDER_INTERVAL_HOURS,
        forward_duplicate_alerts=config.retry.FORWARD_DUPLICATE_ALERTS,
        default_target_url=str(config.forwarding.DEFAULT_FORWARD_TARGET_URL),
    )
