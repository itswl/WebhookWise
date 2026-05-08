from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from services.webhooks.types import ForwardDecision, NoiseReductionContext


class NotifiedEvent(Protocol):
    last_notified_at: datetime | None


@dataclass(frozen=True)
class ForwardRuleSnapshot:
    id: int | None
    name: str
    match_importance: str
    match_source: str
    match_duplicate: str
    target_type: str
    target_url: str
    stop_on_match: bool
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.extra)
        data.update(
            {
                "id": self.id,
                "name": self.name,
                "target_type": self.target_type,
                "target_url": self.target_url,
                "stop_on_match": self.stop_on_match,
            }
        )
        return data


@dataclass(frozen=True)
class ForwardingPolicy:
    notification_cooldown_seconds: int
    enable_periodic_reminder: bool
    reminder_interval_hours: int
    forward_duplicate_alerts: bool
    forward_after_time_window: bool


def normalize_importance(value: Any) -> str:
    text = str(value or "").strip().lower()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text


def build_final_analysis(analysis_result: dict[str, Any], noise: NoiseReductionContext) -> dict[str, Any]:
    final_analysis = dict(analysis_result)
    final_analysis["noise_reduction"] = noise.__dict__
    return final_analysis


def _split_csv(value: str) -> list[str]:
    return [item.strip().lower() for item in value.split(",") if item.strip()]


def _rule_matches(
    rule: ForwardRuleSnapshot,
    *,
    importance: str,
    source: str,
    is_duplicate: bool,
    beyond_window: bool,
) -> bool:
    if rule.match_importance and importance not in _split_csv(rule.match_importance):
        return False
    if rule.match_source and source.lower() not in _split_csv(rule.match_source):
        return False
    if rule.match_duplicate and rule.match_duplicate != "all":
        if rule.match_duplicate == "new" and (is_duplicate or beyond_window):
            return False
        if rule.match_duplicate == "duplicate" and (not is_duplicate or beyond_window):
            return False
        if rule.match_duplicate == "beyond_window" and not beyond_window:
            return False
    return True


def select_forward_rules(
    rules: list[ForwardRuleSnapshot],
    *,
    importance: str,
    source: str,
    is_duplicate: bool,
    beyond_window: bool,
) -> list[dict[str, Any]]:
    matched_rules: list[dict[str, Any]] = []
    for rule in rules:
        if not _rule_matches(
            rule,
            importance=importance,
            source=source,
            is_duplicate=is_duplicate,
            beyond_window=beyond_window,
        ):
            continue
        matched_rules.append(rule.to_dict())
        if rule.stop_on_match:
            break
    return matched_rules


def decide_forwarding(
    *,
    importance: str,
    is_duplicate: bool,
    beyond_window: bool,
    noise: NoiseReductionContext | None,
    original_event: NotifiedEvent | None,
    source: str,
    rules: list[ForwardRuleSnapshot],
    policy: ForwardingPolicy,
    now: datetime | None = None,
) -> ForwardDecision:
    if noise and noise.suppress_forward:
        return ForwardDecision(False, f"智能降噪抑制转发: {noise.reason}", False)

    matched_rules = select_forward_rules(
        rules,
        importance=importance,
        source=source,
        is_duplicate=is_duplicate,
        beyond_window=beyond_window,
    )
    current_time = now or datetime.now()
    should_fwd, is_periodic, skip_reason = False, False, None
    suppressed = False
    base_should_fwd = importance == "high" or bool(matched_rules)

    last_notified_at = original_event.last_notified_at if original_event else None
    seconds_since_notify = (current_time - last_notified_at).total_seconds() if last_notified_at is not None else None

    if is_duplicate:
        if seconds_since_notify is not None and seconds_since_notify < policy.notification_cooldown_seconds:
            suppressed, skip_reason = True, "窗口内重复告警，刚刚已转发"
        elif (
            policy.enable_periodic_reminder
            and seconds_since_notify is not None
            and seconds_since_notify / 3600 >= policy.reminder_interval_hours
        ):
            should_fwd, is_periodic = base_should_fwd, True
            if not should_fwd:
                skip_reason = f"定期提醒：重要性为 {importance}，非高风险事件不自动转发"
        elif not policy.forward_duplicate_alerts:
            suppressed, skip_reason = True, "窗口内重复告警，配置跳过转发"
        else:
            should_fwd = base_should_fwd
            if not should_fwd:
                skip_reason = f"窗口内重复告警：重要性为 {importance}，非高风险事件不自动转发"
    elif beyond_window:
        if not policy.forward_after_time_window:
            suppressed, skip_reason = True, "窗口外重复告警，配置不转发"
        elif seconds_since_notify is not None and seconds_since_notify < policy.notification_cooldown_seconds:
            suppressed, skip_reason = True, "窗口外重复告警，刚刚已转发"
        else:
            should_fwd = base_should_fwd
            if not should_fwd:
                skip_reason = f"窗口外重复告警：重要性为 {importance}，非高风险事件不自动转发"
    else:
        should_fwd = base_should_fwd
        skip_reason = f"重要性为 {importance}，非高风险事件不自动转发" if not should_fwd else None

    final_forward = False if suppressed else (should_fwd or bool(matched_rules))
    return ForwardDecision(
        should_forward=final_forward,
        skip_reason=skip_reason if not final_forward else None,
        is_periodic_reminder=is_periodic,
        matched_rules=matched_rules if not suppressed else [],
    )
