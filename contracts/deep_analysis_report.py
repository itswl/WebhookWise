"""Stable deep-analysis report contract.

The upstream deep-analysis engines can return a dict, fenced JSON, escaped JSON
inside a string field, prose with an embedded JSON object, or a plain text
fallback. This module keeps that uncertainty at the contract boundary and
exposes one stable shape for API responses, dashboard rendering and
notifications.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Final, Literal

from json_repair import repair_json

from core.collections_utils import scalar_text_or_empty
from core.json import JSONDecodeError, dumps, extract_balanced_json_text, loads

DEEP_ANALYSIS_REPORT_SCHEMA = "deep_analysis_report.v1"
OPENCLAW_TEXT_KEY = "_openclaw_text"

ReportSectionKind = Literal["text", "list", "identity"]

_WRAPPER_REPORT_KEYS = (
    "analysis_result",
    "normalized_report",
    "result",
    "report",
    "data",
    "payload",
    "response",
    "output",
    "content",
    "message",
    "text",
    "details",
    "detail",
    OPENCLAW_TEXT_KEY,
)
_STRING_REPORT_KEYS = (
    "summary",
    "root_cause",
    "analysis",
    "impact",
    "recommendations",
    "evidence",
    "next_checks",
    "failure_reason",
    "error",
)
_TEXT_KEYS = (
    "answer",
    "description",
    "summary",
    "conclusion",
    "finding",
    "observation",
    "analysis",
    "reason",
    "root_cause",
    "impact",
    "impact_scope",
    "scope",
    "action",
    "recommendation",
    "solution",
    "message",
    "content",
    "text",
    "title",
    "name",
    "status",
)
_SUMMARY_KEYS = ("summary", "conclusion", "diagnosis_summary", "overview", "title")
_ROOT_CAUSE_KEYS = ("root_cause", "rootCause", "cause", "reason", "failure_reason", "analysis")
_IMPACT_KEYS = ("impact_scope", "impact", "business_impact", "scope", "blast_radius")
_RECOMMENDATION_KEYS = (
    "recommendations",
    "recommendation",
    "actions",
    "action_items",
    "next_steps",
    "solution",
    "solutions",
    "remediation",
    "repair_suggestions",
)
_EVIDENCE_KEYS = ("evidence", "supports", "supporting_evidence", "observations", "signals", "facts")
_NEXT_CHECK_KEYS = ("next_checks", "checks", "verification_steps", "diagnostic_steps", "runbooks")
_CONFIDENCE_KEYS = ("confidence", "confidence_score", "score", "probability")
_FAILURE_KEYS = ("analysis_failed", "failed", "is_failed")
_STATUS_KEYS = ("status", "state")
_ERROR_KEYS = ("error", "failure_reason", "exception", "message")
_IDENTITY_KEYS = {
    "source": ("source", "Source"),
    "project": ("project", "Project", "ProjectName"),
    "region": ("region", "Region"),
    "namespace": ("namespace", "product_namespace", "Namespace"),
    "service": ("service", "Service", "Product", "product"),
    "resource_name": ("resource_name", "resourceName", "ResourceName", "Name", "InstanceName"),
    "resource_id": ("resource_id", "resourceId", "ResourceId", "Id", "InstanceId"),
    "rule_name": ("rule_name", "ruleName", "RuleName", "alert_name", "AlertName"),
    "rule_id": ("rule_id", "ruleId", "RuleId"),
    "metric_name": ("metric_name", "metricName", "MetricName", "Name"),
    "severity": ("severity", "Severity", "Level", "level"),
    "status": ("status", "Status", "state"),
}
_SECTION_TITLES = {
    "root_cause": "根因定位",
    "impact": "影响评估",
    "recommendations": "修复建议",
    "evidence": "关键证据",
    "next_checks": "后续检查",
    "alert_identity": "告警身份",
}

# Sentinel distinguishing "no precomputed parse supplied" from a real ``None``
# parse result, so the top-level string parse can be computed once and threaded
# into the helpers below (see normalize_deep_analysis_report).
_UNSET: Final = object()


@dataclass(frozen=True)
class DeepAnalysisReportSection:
    key: str
    title: str
    kind: ReportSectionKind
    text: str = ""
    items: tuple[str, ...] = ()
    fields: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"key": self.key, "title": self.title, "kind": self.kind}
        if self.text:
            data["text"] = self.text
        if self.items:
            data["items"] = list(self.items)
        if self.fields:
            data["fields"] = dict(self.fields)
        return data


@dataclass(frozen=True)
class DeepAnalysisReport:
    summary: str = ""
    root_cause: str = ""
    impact: str = ""
    recommendations: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    next_checks: tuple[str, ...] = ()
    alert_identity: Mapping[str, str] = field(default_factory=dict)
    confidence: float | None = None
    analysis_failed: bool = False
    failure_reason: str = ""
    primary_text: str = ""
    source_format: str = "empty"
    raw_text: str = ""

    def sections(self) -> tuple[DeepAnalysisReportSection, ...]:
        sections: list[DeepAnalysisReportSection] = []
        if self.root_cause:
            sections.append(_text_section("root_cause", self.root_cause))
        if self.impact:
            sections.append(_text_section("impact", self.impact))
        if self.recommendations:
            sections.append(_list_section("recommendations", self.recommendations))
        if self.evidence:
            sections.append(_list_section("evidence", self.evidence))
        if self.next_checks:
            sections.append(_list_section("next_checks", self.next_checks))
        if self.alert_identity:
            sections.append(
                DeepAnalysisReportSection(
                    key="alert_identity",
                    title=_SECTION_TITLES["alert_identity"],
                    kind="identity",
                    fields=dict(self.alert_identity),
                )
            )
        return tuple(sections)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": DEEP_ANALYSIS_REPORT_SCHEMA,
            "summary": self.summary,
            "root_cause": self.root_cause,
            "impact": self.impact,
            "recommendations": list(self.recommendations),
            "evidence": list(self.evidence),
            "next_checks": list(self.next_checks),
            "alert_identity": dict(self.alert_identity),
            "confidence": self.confidence,
            "analysis_failed": self.analysis_failed,
            "failure_reason": self.failure_reason,
            "primary_text": self.primary_text,
            "source_format": self.source_format,
            "raw_text": self.raw_text,
            "sections": [section.to_dict() for section in self.sections()],
        }


def normalize_deep_analysis_report(value: Any) -> DeepAnalysisReport:
    """Normalize uncertain upstream deep-analysis output into a stable report."""
    # source-format detection and candidate collection both run the same
    # (json_repair + regex) parse on the same top-level string; parse it once
    # here and thread the result through instead of paying that cost per helper.
    top_parsed = _parse_json_like_text(_strip_markdown_json_fence(value)) if isinstance(value, str) else _UNSET
    source_format = _source_format(value, parsed=top_parsed)
    raw_text = _first_raw_text(value)
    candidates = _collect_candidate_mappings(value, parsed=top_parsed)
    data = _merge_candidates(candidates)

    if not data and raw_text:
        text = _single_line(raw_text)
        return DeepAnalysisReport(summary=text, primary_text=text, source_format="plain_text", raw_text=raw_text)

    summary = _first_text(data, _SUMMARY_KEYS)
    root_cause = _display_value(_pick(data, *_ROOT_CAUSE_KEYS))
    impact = _display_value(_pick(data, *_IMPACT_KEYS))
    recommendations = tuple(_unique_items(_list_texts(_pick(data, *_RECOMMENDATION_KEYS), style="recommendation")))
    evidence = tuple(_unique_items(_list_texts(_pick(data, *_EVIDENCE_KEYS), style="evidence")))
    next_checks = tuple(_unique_items(_list_texts(_pick(data, *_NEXT_CHECK_KEYS), style="evidence")))
    alert_identity = _extract_identity(data)
    confidence = _normalize_confidence(_pick(data, *_CONFIDENCE_KEYS))
    failure_reason = _display_value(_pick(data, *_ERROR_KEYS))
    analysis_failed = _boolish(_pick(data, *_FAILURE_KEYS)) or _is_failed_status(_pick(data, *_STATUS_KEYS))

    primary_text = _first_non_empty(summary, root_cause, impact, failure_reason, raw_text)
    if not summary:
        summary = _truncate(_single_line(primary_text), 260)

    return DeepAnalysisReport(
        summary=summary,
        root_cause=root_cause,
        impact=impact,
        recommendations=recommendations,
        evidence=evidence,
        next_checks=next_checks,
        alert_identity=alert_identity,
        confidence=confidence,
        analysis_failed=analysis_failed,
        failure_reason=failure_reason,
        primary_text=primary_text,
        source_format=source_format,
        raw_text=raw_text,
    )


def parse_openclaw_report_payload(text: Any) -> dict[str, Any] | None:
    """Recover the structured JSON report mapping from raw OpenClaw text.

    Applies the same robust parsing the normalizer uses (thinking-prefix prose,
    trailing text, markdown fences, escaped JSON and truncated JSON are all
    handled) and returns the parsed mapping, or ``None`` when nothing parses.

    This is the single entry point the poll layer should use so the persisted
    ``analysis_result`` is already structured rather than a raw blob collapsed
    into ``root_cause``.
    """
    parsed = _parse_json_like_text(text) if isinstance(text, str) else None
    if isinstance(parsed, Mapping):
        return dict(parsed)
    if isinstance(parsed, list):
        first = _first_mapping_from_list(parsed)
        if first:
            return dict(first)
    return None


def summarize_deep_analysis_preview(value: Any, *, limit: int = 260) -> str:
    """Cheaply extract a short preview line for list views.

    Avoids the full normalize_deep_analysis_report pipeline (json_repair + regex
    + section building) so a list of N rows does not pay N× that cost. When the
    stored result is already a mapping (the common case after the poll-layer fix
    persists structured JSON), it reads summary-like fields directly; otherwise
    it falls back to a trimmed single line of the raw text.
    """
    if isinstance(value, Mapping):
        text = _first_text(value, _SUMMARY_KEYS)
        if not text:
            text = _display_value(_pick(value, *_ROOT_CAUSE_KEYS))
        if not text:
            text = _display_value(_pick(value, *_IMPACT_KEYS))
        if not text:
            text = _display_value(_pick(value, *_ERROR_KEYS))
        return _truncate(_single_line(text), limit)
    if isinstance(value, str):
        return _truncate(_single_line(value), limit)
    return ""


def _text_section(key: str, text: str) -> DeepAnalysisReportSection:
    return DeepAnalysisReportSection(key=key, title=_SECTION_TITLES[key], kind="text", text=text)


def _list_section(key: str, items: Iterable[str]) -> DeepAnalysisReportSection:
    return DeepAnalysisReportSection(key=key, title=_SECTION_TITLES[key], kind="list", items=tuple(items))


def _source_format(value: Any, *, parsed: Any = _UNSET) -> str:
    if value in (None, ""):
        return "empty"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        if parsed is _UNSET:
            parsed = _parse_json_like_text(_strip_markdown_json_fence(value))
        return "json_text" if parsed is not None else "plain_text"
    return type(value).__name__


def _collect_candidate_mappings(value: Any, *, depth: int = 0, parsed: Any = _UNSET) -> list[dict[str, Any]]:
    if depth > 5 or value in (None, ""):
        return []

    if isinstance(value, Mapping):
        current = {str(key): item for key, item in value.items()}
        mappings = [current]
        for key in _WRAPPER_REPORT_KEYS:
            nested = _pick(current, key)
            if nested is not None and nested is not value:
                mappings.extend(_collect_candidate_mappings(nested, depth=depth + 1))
        for key in _STRING_REPORT_KEYS:
            nested = _pick(current, key)
            if not isinstance(nested, str):
                continue
            nested_parsed = _parse_json_like_text(nested)
            if nested_parsed is not None and nested_parsed is not nested:
                mappings.extend(_collect_candidate_mappings(nested_parsed, depth=depth + 1))
        return mappings

    if isinstance(value, list):
        list_mappings: list[dict[str, Any]] = []
        for item in value:
            list_mappings.extend(_collect_candidate_mappings(item, depth=depth + 1))
        return list_mappings

    # ``parsed`` is precomputed only for the top-level string (see
    # normalize_deep_analysis_report); recursive calls recompute it here.
    if parsed is _UNSET:
        parsed = _parse_json_like_text(value)
    if parsed is not None and parsed is not value:
        return _collect_candidate_mappings(parsed, depth=depth + 1)
    return []


def _merge_candidates(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for candidate in candidates:
        for key, value in candidate.items():
            if _is_empty(value):
                continue
            existing = merged.get(key)
            if _is_empty(existing) or _is_more_structured(value, existing):
                merged[key] = value
    return merged


def _is_more_structured(value: Any, existing: Any) -> bool:
    return isinstance(value, Mapping | list) and not isinstance(existing, Mapping | list)


def _parse_json_like_text(value: Any, *, depth: int = 0) -> Any | None:
    if not isinstance(value, str) or depth > 3:
        return None

    stripped = _strip_markdown_json_fence(value)
    if not stripped:
        return None

    json_block = extract_balanced_json_text(stripped, allow_arrays=True) or ""
    decoded_stripped = _decode_escaped_json_text(stripped)
    decoded_block = _decode_escaped_json_text(json_block)
    candidates = _unique_texts(
        [
            stripped,
            _sanitize_loose_json(stripped),
            decoded_stripped,
            _sanitize_loose_json(decoded_stripped),
            json_block,
            _sanitize_loose_json(json_block),
            decoded_block,
            _sanitize_loose_json(decoded_block),
        ]
    )
    for candidate in candidates:
        try:
            parsed = loads(candidate)
        except (TypeError, JSONDecodeError):
            parsed = _repair_json_like_text(candidate)
        if isinstance(parsed, str):
            nested = _parse_json_like_text(parsed, depth=depth + 1)
            return nested if nested is not None else parsed
        if isinstance(parsed, Mapping | list):
            return parsed

    if not json_block:
        truncated_json = _extract_truncated_json(stripped)
        if truncated_json:
            return truncated_json
    return None


def _extract_truncated_json(text: str) -> Any | None:
    """Attempt to repair a truncated JSON object embedded after free-text preamble."""
    opening_chars = "{["
    start = -1
    for idx, char in enumerate(text):
        if char in opening_chars:
            start = idx
            break
    if start < 0:
        return None
    fragment = text[start:]
    if len(fragment) < 20:
        return None
    return _repair_json_like_text(fragment)


def _repair_json_like_text(text: str) -> Any | None:
    if not _looks_like_json_container(text):
        return None
    try:
        repaired = repair_json(text, return_objects=True)
    except (IndexError, KeyError, TypeError, ValueError):
        return None
    return repaired if isinstance(repaired, Mapping | list) else None


def _looks_like_json_container(text: str) -> bool:
    stripped = _strip_markdown_json_fence(text)
    return stripped.startswith(("{", "["))


def _strip_markdown_json_fence(text: str) -> str:
    stripped = text.strip()
    stripped = re.sub(r"^```(?:[a-z0-9_-]+)?\s*", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\s*```$", "", stripped, flags=re.IGNORECASE)
    return stripped.strip()


def _sanitize_loose_json(text: str) -> str:
    return re.sub(r"\\(?![\"\\/bfnrtu])", r"\\\\", text) if text else ""


_JSON_ESCAPE_MAP = {"\\": "\\", '"': '"', "/": "/", "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}
_JSON_ESCAPE_RE = re.compile(r"\\(.)", re.DOTALL)


def _decode_escaped_json_text(text: str) -> str:
    stripped = text.strip() if isinstance(text, str) else ""
    if not stripped or not any(token in stripped for token in ('\\"', "\\n", "\\t")):
        return ""

    # Decode in a SINGLE left-to-right pass: a chained .replace() is
    # order-dependent and corrupts input (e.g. a literal "\\n" would first be
    # turned into a newline by the "\n" pass before the "\\" pass collapses the
    # backslash). Matching each "\<char>" once and consuming both characters
    # makes the result independent of escape ordering. Unknown escapes (e.g.
    # "\u" sequences we don't expand here) are left verbatim.
    def _replace(match: re.Match[str]) -> str:
        char = match.group(1)
        if char in _JSON_ESCAPE_MAP:
            return _JSON_ESCAPE_MAP[char]
        return match.group(0)

    return _JSON_ESCAPE_RE.sub(_replace, stripped).strip()


def _first_raw_text(value: Any, *, depth: int = 0) -> str:
    if depth > 4 or value in (None, ""):
        return ""
    if isinstance(value, str):
        # Raw-text extraction only needs the fence-stripped string; the parse
        # that used to run here was discarded (both branches returned the same
        # stripped text), so it is dropped rather than recomputed.
        return _strip_markdown_json_fence(value)
    if isinstance(value, Mapping):
        for key in (OPENCLAW_TEXT_KEY, "raw_text", "content", "text", "message", "root_cause", "summary"):
            text = _first_raw_text(_pick(value, key), depth=depth + 1)
            if text:
                return text
    if isinstance(value, list):
        for item in value:
            text = _first_raw_text(item, depth=depth + 1)
            if text:
                return text
    return ""


def _first_text(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        text = _display_value(_pick(mapping, key))
        if text:
            return text
    return ""


def _display_value(value: Any, *, depth: int = 0) -> str:
    if depth > 4 or value in (None, ""):
        return ""
    if isinstance(value, str):
        parsed = _parse_json_container_text(value)
        if parsed is not None and parsed is not value:
            return _display_value(parsed, depth=depth + 1)
        return value.strip()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        return "\n".join(text for text in (_display_value(item, depth=depth + 1) for item in value) if text)
    if isinstance(value, Mapping):
        action = _display_value(_pick(value, "action", "answer", "recommendation", "solution", "step"), depth=depth + 1)
        reason = _display_value(_pick(value, "reason", "why"), depth=depth + 1)
        if action and reason:
            return f"{action}（{reason}）"
        for key in _TEXT_KEYS:
            text = _display_value(_pick(value, key), depth=depth + 1)
            if text:
                return text
        try:
            return dumps(value, indent=False)
        except TypeError:
            return str(value)
    return str(value)


def _list_texts(value: Any, *, style: Literal["recommendation", "evidence"]) -> list[str]:
    parsed = _parse_json_container_text(value) if isinstance(value, str) else _parse_json_like_text(value)
    if parsed is not None:
        value = parsed
    if value in (None, ""):
        return []
    items = value if isinstance(value, list) else [value]
    texts: list[str] = []
    for item in items:
        if style == "recommendation" and isinstance(item, Mapping):
            action = _display_value(_pick(item, "action", "answer", "recommendation", "solution", "step"))
            reason = _display_value(_pick(item, "reason", "why"))
            text = f"{action}（{reason}）" if action and reason else action or _display_value(item)
        else:
            text = _display_value(item)
        if text:
            texts.append(text)
    return texts


def _parse_json_container_text(value: str) -> Any | None:
    return _parse_json_like_text(value) if _looks_like_json_container(value) else None


def _extract_identity(data: Mapping[str, Any]) -> dict[str, str]:
    identity_source = _pick(data, "alert_identity", "identity", "alertIdentity", "_alert_identity")
    identity = identity_source if isinstance(identity_source, Mapping) else {}
    resource = _first_mapping_from_list(_pick(data, "Resources", "resources"))
    metric = _first_mapping_from_list(resource.get("Metrics") if resource else None)

    values: dict[str, str] = {}
    for target_key, candidates in _IDENTITY_KEYS.items():
        raw = _pick(identity, *candidates)
        if raw is None:
            raw = _pick(data, *candidates)
        if raw is None and resource:
            raw = _pick(resource, *candidates)
        if raw is None and metric and target_key == "metric_name":
            raw = _pick(metric, *candidates)
        text = scalar_text_or_empty(raw)
        if text:
            values[target_key] = text
    return values


def _first_mapping_from_list(value: Any) -> Mapping[str, Any]:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, Mapping):
                return item
    return {}


def _normalize_confidence(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        normalized = value.strip().rstrip("%")
        try:
            numeric = float(normalized)
        except ValueError:
            return None
        return max(0.0, min(1.0, numeric / 100 if numeric > 1 else numeric))
    if isinstance(value, int | float) and not isinstance(value, bool):
        numeric = float(value)
        return max(0.0, min(1.0, numeric / 100 if numeric > 1 else numeric))
    return None


def _pick(mapping: Mapping[str, Any] | Any, *keys: str) -> Any | None:
    if not isinstance(mapping, Mapping):
        return None
    indexed = {_key_id(key): value for key, value in mapping.items() if isinstance(key, str)}
    for key in keys:
        value = indexed.get(_key_id(key))
        if not _is_empty(value):
            return value
    return None


@lru_cache(maxsize=4096)
def _key_id(key: str) -> str:
    # Pure normalization run once per (key, _pick call); memoized since the same
    # handful of report field names recur across every record's normalization.
    return re.sub(r"[^a-z0-9]+", "", key.lower())


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "failed", "error"}
    return False


def _is_failed_status(value: Any) -> bool:
    return str(value or "").strip().lower() in {"failed", "failure", "error", "timeout", "degraded"}


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = value if isinstance(value, str) else scalar_text_or_empty(value)
        if text:
            return text
    return ""


def _single_line(text: str) -> str:
    return " ".join(str(text or "").split()).strip()


def _truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def _unique_items(values: Iterable[str]) -> list[str]:
    return _unique_texts(_single_line(value) for value in values)


def _unique_texts(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    texts: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        texts.append(text)
    return texts


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}
