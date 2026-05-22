from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from core.text import split_csv_lower
from services.webhooks.types import (
    AnalysisResult,
    ForwardDecision,
    ForwardRuleTarget,
    NoiseReductionContext,
    NoiseReductionSnapshot,
)


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
    target_name: str = ""

    def to_dict(self) -> ForwardRuleTarget:
        data: ForwardRuleTarget = {
            "id": self.id,
            "name": self.name,
            "target_type": self.target_type,
            "target_url": self.target_url,
            "stop_on_match": self.stop_on_match,
        }
        if self.target_name:
            data["target_name"] = self.target_name
        return data


@dataclass(frozen=True)
class ForwardingPolicy:
    notification_cooldown_seconds: int
    enable_periodic_reminder: bool
    reminder_interval_hours: int
    forward_duplicate_alerts: bool
    forward_after_time_window: bool


@dataclass(frozen=True)
class _ForwardDecisionState:
    should_forward: bool = False
    skip_reason: str | None = None
    is_periodic_reminder: bool = False
    suppressed: bool = False


def normalize_importance(value: Any) -> str:
    text = str(value or "").strip().lower()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text


def build_final_analysis(analysis_result: AnalysisResult, noise: NoiseReductionContext) -> AnalysisResult:
    final_analysis = analysis_result.copy()
    noise_snapshot: NoiseReductionSnapshot = {
        "relation": noise.relation,
        "root_cause_event_id": noise.root_cause_event_id,
        "confidence": noise.confidence,
        "suppress_forward": noise.suppress_forward,
        "reason": noise.reason,
        "related_alert_count": noise.related_alert_count,
        "related_alert_ids": noise.related_alert_ids,
    }
    final_analysis["noise_reduction"] = noise_snapshot
    return final_analysis


def _rule_matches(
    rule: ForwardRuleSnapshot,
    *,
    importance: str,
    source: str,
    is_duplicate: bool,
    beyond_window: bool,
) -> bool:
    if rule.match_importance and importance not in split_csv_lower(rule.match_importance):
        return False
    if rule.match_source and source.lower() not in split_csv_lower(rule.match_source):
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
) -> list[ForwardRuleTarget]:
    matched_rules: list[ForwardRuleTarget] = []
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


def _low_importance_reason(prefix: str, importance: str) -> str:
    return (
        f"{prefix}：重要性为 {importance}，非高风险事件不自动转发"
        if prefix
        else f"重要性为 {importance}，非高风险事件不自动转发"
    )


def _decide_new_alert(*, base_should_forward: bool, importance: str) -> _ForwardDecisionState:
    return _ForwardDecisionState(
        should_forward=base_should_forward,
        skip_reason=None if base_should_forward else _low_importance_reason("", importance),
    )


def _decide_duplicate_alert(
    *,
    base_should_forward: bool,
    importance: str,
    seconds_since_notify: float | None,
    policy: ForwardingPolicy,
) -> _ForwardDecisionState:
    if seconds_since_notify is not None and seconds_since_notify < policy.notification_cooldown_seconds:
        return _ForwardDecisionState(suppressed=True, skip_reason="窗口内重复告警，刚刚已转发")

    if (
        policy.enable_periodic_reminder
        and seconds_since_notify is not None
        and seconds_since_notify / 3600 >= policy.reminder_interval_hours
    ):
        return _ForwardDecisionState(
            should_forward=base_should_forward,
            is_periodic_reminder=True,
            skip_reason=None if base_should_forward else _low_importance_reason("定期提醒", importance),
        )

    if not policy.forward_duplicate_alerts:
        return _ForwardDecisionState(suppressed=True, skip_reason="窗口内重复告警，配置跳过转发")

    return _ForwardDecisionState(
        should_forward=base_should_forward,
        skip_reason=None if base_should_forward else _low_importance_reason("窗口内重复告警", importance),
    )


def _decide_beyond_window_alert(
    *,
    base_should_forward: bool,
    importance: str,
    seconds_since_notify: float | None,
    policy: ForwardingPolicy,
) -> _ForwardDecisionState:
    if not policy.forward_after_time_window:
        return _ForwardDecisionState(suppressed=True, skip_reason="窗口外重复告警，配置不转发")
    if seconds_since_notify is not None and seconds_since_notify < policy.notification_cooldown_seconds:
        return _ForwardDecisionState(suppressed=True, skip_reason="窗口外重复告警，刚刚已转发")
    return _ForwardDecisionState(
        should_forward=base_should_forward,
        skip_reason=None if base_should_forward else _low_importance_reason("窗口外重复告警", importance),
    )


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
    base_should_fwd = importance == "high" or bool(matched_rules)

    last_notified_at = original_event.last_notified_at if original_event else None
    seconds_since_notify = (current_time - last_notified_at).total_seconds() if last_notified_at is not None else None

    if is_duplicate:
        state = _decide_duplicate_alert(
            base_should_forward=base_should_fwd,
            importance=importance,
            seconds_since_notify=seconds_since_notify,
            policy=policy,
        )
    elif beyond_window:
        state = _decide_beyond_window_alert(
            base_should_forward=base_should_fwd,
            importance=importance,
            seconds_since_notify=seconds_since_notify,
            policy=policy,
        )
    else:
        state = _decide_new_alert(base_should_forward=base_should_fwd, importance=importance)

    final_forward = False if state.suppressed else (state.should_forward or bool(matched_rules))
    return ForwardDecision(
        should_forward=final_forward,
        skip_reason=state.skip_reason if not final_forward else None,
        is_periodic_reminder=state.is_periodic_reminder,
        matched_rules=matched_rules if not state.suppressed else [],
    )
