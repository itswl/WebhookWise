from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from core.app_context import get_config_manager
from core.collections_utils import scalar_text_or_empty
from core.datetime_utils import utcnow
from core.text import split_csv_lower
from services.webhooks.types import (
    AnalysisResult,
    NoiseReductionContext,
)

if TYPE_CHECKING:
    from models import WebhookEvent


@dataclass
class ForwardDecision:
    should_forward: bool
    skip_reason: str | None
    is_periodic_reminder: bool
    matched_rules: list[ForwardRuleSnapshot] = field(default_factory=list)
    skip_code: str = "none"
    # Set only when skip_code == "silenced": the active silence that muted this
    # alert, so the decision trace can link the skip to its silence.
    silence_id: int | None = None


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
    match_project: str = ""
    match_region: str = ""
    match_environment: str = ""


@dataclass(frozen=True)
class SilenceSnapshot:
    """An active silence's match criteria (the deny counterpart to a rule)."""

    id: int | None
    match_event_type: str = ""
    match_importance: str = ""
    match_source: str = ""
    match_payload: str = ""
    match_project: str = ""
    match_region: str = ""
    match_environment: str = ""
    comment: str = ""


@dataclass(frozen=True)
class ForwardingPolicy:
    notification_cooldown_seconds: int
    enable_periodic_reminder: bool
    reminder_interval_hours: int


def normalize_importance(value: Any) -> str:
    text = str(value or "").strip().lower()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text


def build_final_analysis(analysis_result: AnalysisResult, noise: NoiseReductionContext) -> AnalysisResult:
    final_analysis = analysis_result.copy()
    final_analysis["noise_reduction"] = asdict(noise)
    return final_analysis


_PROJECT_KEY_GROUPS = (("ProjectName", "project_name", "projectName"), ("Project", "project"))
_PROJECT_PLACEHOLDERS = {"", "default", "unknown", "none", "null", "-"}
_REGION_KEYS = ("Region", "region", "region_id", "regionId")
_ENVIRONMENT_KEYS = (
    "environment",
    "env",
    "stage",
    "deployment_environment",
    "deploymentEnvironment",
    "runtime_environment",
    "runtimeEnvironment",
)
_ENVIRONMENT_ALIASES = {
    "prod": "prod",
    "production": "prod",
    "prd": "prod",
    "live": "prod",
    "dev": "dev",
    "development": "dev",
    "test": "test",
    "testing": "test",
    "staging": "staging",
    "stage": "staging",
    "pre": "pre",
    "preprod": "pre",
    "preproduction": "pre",
    "uat": "uat",
    "qa": "qa",
    "gray": "gray",
    "grey": "gray",
}
_ENVIRONMENT_TOKEN_RE = "|".join(sorted((re.escape(k) for k in _ENVIRONMENT_ALIASES), key=len, reverse=True))
_PROJECT_FROM_ENV_RE = re.compile(
    rf"\b(?P<project>[a-z0-9]+(?:-[a-z0-9]+)*)-(?:{_ENVIRONMENT_TOKEN_RE})(?:-|$)",
    re.IGNORECASE,
)


def _find_in_payload_ci(payload: Any, keys: tuple[str, ...]) -> Any:
    if not keys:
        return None
    lowered = {key.lower() for key in keys}
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() in lowered:
                return value
        for value in payload.values():
            found = _find_in_payload_ci(value, keys)
            if found is not None:
                return found
        return None
    if isinstance(payload, list):
        for item in payload:
            found = _find_in_payload_ci(item, keys)
            if found is not None:
                return found
    return None


def _find_all_in_payload_ci(payload: Any, keys: tuple[str, ...]) -> list[Any]:
    if not keys:
        return []
    lowered = {key.lower() for key in keys}
    found: list[Any] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() in lowered:
                found.append(value)
        for value in payload.values():
            found.extend(_find_all_in_payload_ci(value, keys))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(_find_all_in_payload_ci(item, keys))
    return found


def _canonical_environment(value: Any, *, allow_unknown: bool = True) -> str:
    text = scalar_text_or_empty(value).lower()
    if not text:
        return ""
    if text in _ENVIRONMENT_ALIASES:
        return _ENVIRONMENT_ALIASES[text]
    for token in re.split(r"[^a-z0-9]+", text):
        if token in _ENVIRONMENT_ALIASES:
            return _ENVIRONMENT_ALIASES[token]
    return text if allow_unknown else ""


def _iter_payload_text(payload: Any) -> list[str]:
    if isinstance(payload, dict):
        values: list[str] = []
        for key, value in payload.items():
            key_text = scalar_text_or_empty(key)
            if key_text:
                values.append(key_text)
            values.extend(_iter_payload_text(value))
        return values
    if isinstance(payload, list):
        list_values: list[str] = []
        for item in payload:
            list_values.extend(_iter_payload_text(item))
        return list_values
    text = scalar_text_or_empty(payload)
    return [text] if text else []


def _infer_project_from_payload_text(payload: Any) -> str:
    for text in _iter_payload_text(payload):
        match = _PROJECT_FROM_ENV_RE.search(text.lower())
        if match:
            return match.group("project").strip("-")
    return ""


def _extract_project(payload: Any) -> str:
    placeholder = ""
    for keys in _PROJECT_KEY_GROUPS:
        for value in _find_all_in_payload_ci(payload, keys):
            text = scalar_text_or_empty(value)
            lowered = text.lower()
            if lowered in _PROJECT_PLACEHOLDERS:
                placeholder = placeholder or text
                continue
            return text
    inferred = _infer_project_from_payload_text(payload)
    if inferred:
        return inferred
    return placeholder


def extract_forward_match_fields(parsed_data: dict[str, Any] | None) -> dict[str, str]:
    payload = parsed_data or {}
    project = _extract_project(payload)
    region = scalar_text_or_empty(_find_in_payload_ci(payload, _REGION_KEYS))
    environment = _canonical_environment(_find_in_payload_ci(payload, _ENVIRONMENT_KEYS))
    if not environment:
        for text in _iter_payload_text(payload):
            environment = _canonical_environment(text, allow_unknown=False)
            if environment:
                break
    return {"project": project, "region": region, "environment": environment}


def _csv_value_matches(expected_csv: str, actual: str) -> bool:
    if not expected_csv:
        return True
    actual_normalized = actual.lower()
    positives: set[str] = set()
    negatives: set[str] = set()
    for item in split_csv_lower(expected_csv):
        if item.startswith("!") and len(item) > 1:
            negatives.add(item[1:])
        else:
            positives.add(item)
    if actual_normalized in negatives:
        return False
    if positives:
        return actual_normalized in positives
    return True


def _csv_environment_matches(expected_csv: str, actual: str) -> bool:
    if not expected_csv:
        return True
    actual_env = _canonical_environment(actual)
    positives: set[str] = set()
    negatives: set[str] = set()
    for item in split_csv_lower(expected_csv):
        is_negative = item.startswith("!") and len(item) > 1
        value = item[1:] if is_negative else item
        normalized = _canonical_environment(value) or value.lower()
        if is_negative:
            negatives.add(normalized)
        else:
            positives.add(normalized)
    if actual_env in negatives:
        return False
    if positives:
        return actual_env in positives
    return True


def _rule_matches(
    rule: ForwardRuleSnapshot,
    *,
    event_type: str = "",
    importance: str = "",
    source: str = "",
    is_duplicate: bool = False,
    parsed_data: dict[str, Any] | None = None,
    identity: dict[str, str] | None = None,
) -> bool:
    if rule.match_event_type and event_type not in split_csv_lower(rule.match_event_type):
        return False
    if rule.match_importance and importance not in split_csv_lower(rule.match_importance):
        return False
    if rule.match_source and source.lower() not in split_csv_lower(rule.match_source):
        return False
    # identity (project/region/environment) depends only on parsed_data, not the
    # rule, so select_forward_rules computes it once and passes it in; fall back
    # to computing it here for direct callers that don't.
    if identity is None:
        identity = extract_forward_match_fields(parsed_data)
    if not _csv_value_matches(getattr(rule, "match_project", ""), identity["project"]):
        return False
    if not _csv_value_matches(getattr(rule, "match_region", ""), identity["region"]):
        return False
    if not _csv_environment_matches(getattr(rule, "match_environment", ""), identity["environment"]):
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
    identity: dict[str, str] | None = None,
) -> list[ForwardRuleSnapshot]:
    matched_rules: list[ForwardRuleSnapshot] = []
    # Compute the payload-derived identity once instead of re-traversing the
    # payload inside _rule_matches for every rule (callers that already computed
    # it, e.g. decide_forwarding, pass it in to avoid a repeat traversal).
    if identity is None:
        identity = extract_forward_match_fields(parsed_data)
    for rule in rules:
        if not _rule_matches(
            rule,
            event_type=event_type,
            importance=importance,
            source=source,
            is_duplicate=is_duplicate,
            parsed_data=parsed_data,
            identity=identity,
        ):
            continue
        matched_rules.append(rule)
        if rule.stop_on_match:
            break
    return matched_rules


def _first_matching_silence(
    silences: list[SilenceSnapshot],
    *,
    event_type: str,
    importance: str,
    source: str,
    is_duplicate: bool,
    parsed_data: dict[str, Any] | None,
    identity: dict[str, str],
) -> SilenceSnapshot | None:
    """Return the first silence whose criteria match, reusing rule-match logic.

    A silence mutes both new and duplicate occurrences, so match_duplicate is
    fixed to "all"; the rest of the criteria run through the same _rule_matches
    path as forward rules (CSV/negation/environment canonicalization included).
    """
    for silence in silences:
        probe = ForwardRuleSnapshot(
            id=silence.id,
            name="silence",
            match_event_type=silence.match_event_type,
            match_importance=silence.match_importance,
            match_source=silence.match_source,
            match_duplicate="all",
            match_payload=silence.match_payload,
            match_project=silence.match_project,
            match_region=silence.match_region,
            match_environment=silence.match_environment,
            target_type="silence",
            target_url="",
            stop_on_match=False,
        )
        if _rule_matches(
            probe,
            event_type=event_type,
            importance=importance,
            source=source,
            is_duplicate=is_duplicate,
            parsed_data=parsed_data,
            identity=identity,
        ):
            return silence
    return None


def match_active_silence(
    silences: list[SilenceSnapshot],
    *,
    event_type: str = "",
    importance: str = "",
    source: str = "",
    is_duplicate: bool = False,
    parsed_data: dict[str, Any] | None = None,
) -> SilenceSnapshot | None:
    """Public wrapper: return the first matching silence (computes identity).

    Used by the analysis stage to decide whether to skip the (paid) AI call for a
    silenced alert; the forward stage uses the lower-level path with a shared
    precomputed identity.
    """
    if not silences:
        return None
    identity = extract_forward_match_fields(parsed_data)
    return _first_matching_silence(
        silences,
        event_type=event_type,
        importance=importance,
        source=source,
        is_duplicate=is_duplicate,
        parsed_data=parsed_data,
        identity=identity,
    )


def _decide_duplicate_alert(
    *,
    base_should_forward: bool,
    seconds_since_notify: float | None,
    policy: ForwardingPolicy,
    matched_rules: list[ForwardRuleSnapshot],
) -> ForwardDecision:
    # Cooldown period (60s): if we just notified, do not send again, to prevent a
    # short-term notification storm
    if seconds_since_notify is not None and seconds_since_notify < policy.notification_cooldown_seconds:
        return ForwardDecision(False, "Just notified, in cooldown", False, skip_code="cooldown")

    # Periodic reminder (6h): the same alert persists, re-notify on a schedule
    if (
        policy.enable_periodic_reminder
        and seconds_since_notify is not None
        and seconds_since_notify / 3600 >= policy.reminder_interval_hours
    ):
        return ForwardDecision(
            base_should_forward,
            None if base_should_forward else "Periodic reminder: no matching forwarding rule",
            True,
            matched_rules=matched_rules,
            skip_code="none" if base_should_forward else "periodic_no_rule",
        )

    # The rule has already decided whether to match duplicate alerts via
    # match_duplicate; no extra global switch is needed to override the rule
    # matching result.
    if base_should_forward:
        return ForwardDecision(True, None, False, matched_rules=matched_rules)

    return ForwardDecision(False, "Duplicate alert: no matching forwarding rule", False, skip_code="duplicate_no_rule")


def decide_forwarding(
    *,
    event_type: str = "",
    importance: str = "",
    is_duplicate: bool = False,
    noise: NoiseReductionContext | None = None,
    original_event: WebhookEvent | None = None,
    source: str = "",
    rules: list[ForwardRuleSnapshot],
    policy: ForwardingPolicy,
    parsed_data: dict[str, Any] | None = None,
    now: datetime | None = None,
    silences: list[SilenceSnapshot] | None = None,
) -> ForwardDecision:
    if noise and noise.suppress_forward:
        return ForwardDecision(False, f"Smart noise reduction suppressed forwarding: {noise.reason}", False, skip_code="noise_suppressed")

    # An active manual silence mutes forwarding for matching alerts. Checked
    # after noise (both are suppressors) and before rule routing. The identity is
    # computed once and reused by both the silence check and rule selection.
    identity = extract_forward_match_fields(parsed_data)
    if silences and (silenced := _first_matching_silence(
        silences,
        event_type=event_type,
        importance=importance,
        source=source,
        is_duplicate=is_duplicate,
        parsed_data=parsed_data,
        identity=identity,
    )):
        return ForwardDecision(
            False, f"Silenced (id={silenced.id})", False, skip_code="silenced", silence_id=silenced.id
        )

    matched_rules = select_forward_rules(
        rules,
        event_type=event_type,
        importance=importance,
        source=source,
        is_duplicate=is_duplicate,
        parsed_data=parsed_data,
        identity=identity,
    )
    current_time = now or utcnow()
    base_should_fwd = bool(matched_rules)

    if is_duplicate:
        last_notified_at = original_event.last_notified_at if original_event else None
        seconds_since_notify = (
            (current_time - last_notified_at).total_seconds() if last_notified_at is not None else None
        )
        return _decide_duplicate_alert(
            base_should_forward=base_should_fwd,
            seconds_since_notify=seconds_since_notify,
            policy=policy,
            matched_rules=matched_rules,
        )

    return ForwardDecision(
        base_should_fwd,
        None if base_should_fwd else "No matching forwarding rule",
        False,
        matched_rules=matched_rules,
        skip_code="none" if base_should_fwd else "no_match",
    )


def forwarding_policy_from_config() -> ForwardingPolicy:
    cfg = get_config_manager()
    return ForwardingPolicy(
        notification_cooldown_seconds=cfg.retry.NOTIFICATION_COOLDOWN_SECONDS,
        enable_periodic_reminder=cfg.retry.ENABLE_PERIODIC_REMINDER,
        reminder_interval_hours=cfg.retry.REMINDER_INTERVAL_HOURS,
    )
