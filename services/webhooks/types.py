"""Webhook workflow value objects and business result types."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, Literal, NotRequired, Protocol, Required, TypedDict, cast

from contracts.webhook_payload import JsonObject, WebhookData


class CorrectionPriorLike(Protocol):
    """The shape set_analysis_correction_prior needs.

    A Protocol rather than the class itself: this module is the low-level
    contract every layer imports, and importing an analysis service back into
    it would invert the dependency (and cycle through models).
    """

    def to_metadata(self, *, followed: bool) -> JsonObject: ...


class AnalysisMetaKey(StrEnum):
    ROUTE_TYPE = "_route_type"
    DEGRADED = "_degraded"
    DEGRADED_REASON = "_degraded_reason"
    CACHE_HIT = "_cache_hit"
    CACHE_HIT_COUNT = "_cache_hit_count"
    PENDING = "_pending"
    EMBEDDING = "_embedding"
    USAGE = "_usage"
    PROMPT_KIND = "_prompt_kind"
    PROMPT_VERSION = "_prompt_version"
    CORRECTION_PRIOR = "_correction_prior"


class ForwardMetaKey(StrEnum):
    PENDING = "_pending"
    GATEWAY_RUN_ID = "_gateway_run_id"
    GATEWAY_SESSION_KEY = "_gateway_session_key"
    DEGRADED = "_degraded"
    DEGRADED_REASON = "_degraded_reason"


class GatewayMetaKey(StrEnum):
    TEXT = "_gateway_text"
    NEED_SUCCESS_NOTIFY = "_need_success_notify"
    MANUAL_RETRY_STARTED_AT = "_manual_retry_started_at"


ANALYSIS_ROUTE_TYPE: Final = AnalysisMetaKey.ROUTE_TYPE.value
ANALYSIS_DEGRADED: Final = AnalysisMetaKey.DEGRADED.value
ANALYSIS_DEGRADED_REASON: Final = AnalysisMetaKey.DEGRADED_REASON.value
ANALYSIS_CACHE_HIT: Final = AnalysisMetaKey.CACHE_HIT.value
ANALYSIS_CACHE_HIT_COUNT: Final = AnalysisMetaKey.CACHE_HIT_COUNT.value
ANALYSIS_PENDING: Final = AnalysisMetaKey.PENDING.value
ANALYSIS_EMBEDDING: Final = AnalysisMetaKey.EMBEDDING.value
ANALYSIS_USAGE: Final = AnalysisMetaKey.USAGE.value
ANALYSIS_PROMPT_KIND: Final = AnalysisMetaKey.PROMPT_KIND.value
ANALYSIS_PROMPT_VERSION: Final = AnalysisMetaKey.PROMPT_VERSION.value
ANALYSIS_CORRECTION_PRIOR: Final = AnalysisMetaKey.CORRECTION_PRIOR.value
FORWARD_PENDING: Final = ForwardMetaKey.PENDING.value
GATEWAY_RUN_ID: Final = ForwardMetaKey.GATEWAY_RUN_ID.value
GATEWAY_SESSION_KEY: Final = ForwardMetaKey.GATEWAY_SESSION_KEY.value
FORWARD_DEGRADED: Final = ForwardMetaKey.DEGRADED.value
FORWARD_DEGRADED_REASON: Final = ForwardMetaKey.DEGRADED_REASON.value
GATEWAY_TEXT: Final = GatewayMetaKey.TEXT.value
GATEWAY_NEED_SUCCESS_NOTIFY: Final = GatewayMetaKey.NEED_SUCCESS_NOTIFY.value
MANUAL_RETRY_STARTED_AT: Final = GatewayMetaKey.MANUAL_RETRY_STARTED_AT.value

# ── Inbound actions ──────────────────────────────────────────────────────────
# The verbs an inbound rule can carry. Declared here rather than in
# inbound_rules because the MATCHER lives in decisioning, which inbound_rules
# imports from — putting the vocabulary in either would make a cycle, and
# spelling it in both would break the one-declaration contract.
SKIP_AI: Final = "skip_ai"
SKIP_DEEP_ANALYSIS: Final = "skip_deep_analysis"
# Ceiling an operator sets for a named alert rule, applied AFTER judgement to
# whatever route answered. Needs InboundRule.action_value to say "at what".
CAP_IMPORTANCE: Final = "cap_importance"
# Deliberately short. "drop" would make an alert unfindable afterwards, and
# "mute" already exists as a silence, with expiry semantics this table lacks.
INBOUND_ACTIONS: Final = frozenset({SKIP_AI, SKIP_DEEP_ANALYSIS, CAP_IMPORTANCE})
# Only these verbs take a value; the others must leave action_value empty.
INBOUND_ACTIONS_WITH_VALUE: Final = frozenset({CAP_IMPORTANCE})

# A capped severity must never look like the judgement that produced it. Same
# doctrine as _importance_override: an importance nobody can trace back to a
# decision is how you end up arguing with a model that never said it.
ANALYSIS_IMPORTANCE_CAP: Final = "_importance_cap"
# Ordered so a cap can be compared against a judgement. Unknown values are
# treated as `high` when capping, so a severity this table does not recognise
# is never left ABOVE the ceiling an operator set.
_IMPORTANCE_RANK: Final = {"low": 0, "medium": 1, "high": 2}


# "rule" = degraded to rules (AI unavailable/failed). "rule_routed" = tiered
# routing intentionally skipped the LLM for a low-value alert (not a degradation).
AnalysisRouteType = Literal[
    "ai",
    "cache",
    "rule",
    "rule_routed",
    # Never analysed because policy names this alert rule, not because the
    # rule pass judged it cheap. Distinct from rule_routed so an operator can
    # tell "we decided this one is not worth a model" from "we decided never
    # to analyse this rule at all".
    "rule_excluded",
    "redis_reuse",
    "db_reuse",
    "rechain",
    "silenced_skip",
]
ALLOWED_ANALYSIS_ROUTE_TYPES: Final = frozenset(
    {
        "ai",
        "cache",
        "rule",
        "rule_routed",
        "rule_excluded",
        "redis_reuse",
        "db_reuse",
        "rechain",
        "silenced_skip",
    }
)

# The routes that answered WITHOUT calling the LLM, i.e. what "cache hit" means
# for the AI-cost view. One definition, because it was previously spelled out
# by hand in the aggregate query AND again in the dashboard renderer: adding a
# future reuse route to one list and not the other would silently under-report
# the hit rate, with nothing failing. "rule" and "rule_routed" are excluded on
# purpose — those skipped the LLM by policy or degradation, not by reuse.
NO_LLM_REUSE_ROUTE_TYPES: Final = frozenset({"cache", "reuse", "redis_reuse", "db_reuse", "rechain"})


class AnalysisResult(TypedDict):
    """AI/rule analysis contract shared by cache, noise reduction and persistence."""

    importance: Required[str]
    summary: Required[str]
    source: NotRequired[str]
    event_type: NotRequired[str]
    alert_identity: NotRequired[JsonObject]
    impact_scope: NotRequired[str | None]
    actions: NotRequired[list[str]]
    risks: NotRequired[list[str]]
    monitoring_suggestions: NotRequired[list[str]]
    noise_reduction: NotRequired[JsonObject]
    root_cause: NotRequired[str]
    impact: NotRequired[str]
    confidence: NotRequired[float]
    # Act-now-vs-defer triage: stable machine values ("act_now"|"monitor"|
    # "defer") + 0-1 confidence. Display-only for now — decisioning does not
    # read them; wiring them into routing is a separate decision.
    triage_verdict: NotRequired[str]
    triage_confidence: NotRequired[float]
    _route_type: NotRequired[AnalysisRouteType]
    _degraded: NotRequired[bool]
    _degraded_reason: NotRequired[str]
    _cache_hit: NotRequired[bool]
    _cache_hit_count: NotRequired[int]
    _pending: NotRequired[bool]
    _embedding: NotRequired[list[float]]
    _prompt_kind: NotRequired[str]
    _prompt_version: NotRequired[str]
    _usage: NotRequired[JsonObject]
    _importance_override: NotRequired[str]
    _importance_override_reason: NotRequired[str]
    _correction_prior: NotRequired[JsonObject]
    _importance_cap: NotRequired[JsonObject]


class ForwardResult(TypedDict):
    """Result shape returned by forwarding integrations."""

    status: Required[str]
    reason: NotRequired[str]
    message: NotRequired[str]
    status_code: NotRequired[int]
    retryable: NotRequired[bool]
    disable_rule: NotRequired[bool]
    error_code: NotRequired[str]
    outbox_id: NotRequired[int]
    outbox_ids: NotRequired[list[int]]
    _pending: NotRequired[bool]
    _gateway_run_id: NotRequired[str]
    _gateway_session_key: NotRequired[str]
    _degraded: NotRequired[bool]
    _degraded_reason: NotRequired[str]


def is_analysis_degraded(result: Mapping[str, Any] | None) -> bool:
    return bool(result and result.get(ANALYSIS_DEGRADED))


def analysis_degraded_reason(result: Mapping[str, Any] | None) -> str:
    return str((result or {}).get(ANALYSIS_DEGRADED_REASON, ""))


def analysis_route(result: Mapping[str, Any] | None, default: AnalysisRouteType = "ai") -> str:
    return str((result or {}).get(ANALYSIS_ROUTE_TYPE, default))


def set_analysis_route(result: AnalysisResult, route: str) -> AnalysisResult:
    if route not in ALLOWED_ANALYSIS_ROUTE_TYPES:
        raise ValueError(f"unsupported analysis route: {route}")
    result[ANALYSIS_ROUTE_TYPE] = cast(AnalysisRouteType, route)
    return result


def set_analysis_prompt(result: AnalysisResult, *, kind: str, version: str) -> AnalysisResult:
    """Which prompt text produced this analysis.

    The fingerprint already existed — ai_cache keys on it so that editing a
    prompt invalidates stale results — but it was computed and thrown away, so
    nothing could say afterwards which instructions an analysis came from. That
    is the question asked when a report reads wrong, and the prompts here are
    unusually easy to move: three kinds, overridable by env, and reloadable at
    runtime.

    Unlike _usage this is set BEFORE the result is cached. A cached analysis is
    still the output of the prompt that produced it, and a reuse should say so;
    what a reuse must not claim is a second purchase.
    """
    result["_prompt_kind"] = kind
    result["_prompt_version"] = version
    return result


def set_analysis_usage(
    result: AnalysisResult, *, model: str, tokens_in: int, tokens_out: int, cost_usd: float
) -> AnalysisResult:
    """Record what this analysis actually spent, on the analysis itself.

    The numbers already existed in metrics and in the AI usage table, but both
    aggregate: neither can answer "what did THIS alert cost", which is the
    question asked when reading one alert. Attached here it travels with the
    analysis into persistence and onto the card.

    Only set on the route that genuinely called the model. Reuse routes must
    not inherit it — save_to_cache strips underscore-prefixed keys, so a cache
    hit carries no usage and cannot be mistaken for a second purchase.
    """
    result[ANALYSIS_USAGE] = {
        "model": model,
        "tokens_in": int(tokens_in),
        "tokens_out": int(tokens_out),
        "cost_usd": round(float(cost_usd), 6),
    }
    return result


def set_analysis_correction_prior(
    result: AnalysisResult, *, prior: CorrectionPriorLike, followed: bool
) -> AnalysisResult:
    """Record which operator history was put in front of the model, and whether it took it.

    A prior that steers a verdict without leaving a record is the thing the
    override docstring refused to build: a judgement nobody can trace back to a
    person. Attached here it travels into persistence with the analysis, so the
    stored answer to "why was this high" can name the corrections behind it.

    `followed` is the honest half. A prior nothing ever follows is noise in the
    prompt and should be turned off; a prior everything follows is a hard
    override in disguise and should be one. Neither is visible without it.

    Underscore-prefixed, so save_to_cache strips it: a cache hit must not claim
    a prior that was computed for an earlier call.
    """
    result[ANALYSIS_CORRECTION_PRIOR] = prior.to_metadata(followed=followed)
    return result


def mark_analysis_degraded(
    result: AnalysisResult, reason: str, *, route: AnalysisRouteType | None = None
) -> AnalysisResult:
    result[ANALYSIS_DEGRADED] = True
    result[ANALYSIS_DEGRADED_REASON] = reason
    if route is not None:
        result[ANALYSIS_ROUTE_TYPE] = route
    return result


def cache_hit_count(result: Mapping[str, Any] | None, default: int = 1) -> int:
    raw = (result or {}).get(ANALYSIS_CACHE_HIT_COUNT, default)
    return raw if isinstance(raw, int) else default


def mark_cache_hit(result: AnalysisResult, count: int) -> AnalysisResult:
    result[ANALYSIS_CACHE_HIT] = True
    result[ANALYSIS_CACHE_HIT_COUNT] = count
    return result


def is_pending_result(result: Mapping[str, Any] | None) -> bool:
    return bool(result and result.get(FORWARD_PENDING))


def gateway_run_id(result: Mapping[str, Any] | None) -> str:
    return str((result or {}).get(GATEWAY_RUN_ID, ""))


def gateway_session_key(result: Mapping[str, Any] | None) -> str:
    return str((result or {}).get(GATEWAY_SESSION_KEY, ""))


def pending_forward_result(run_id: str, session_key: str) -> ForwardResult:
    return {
        "status": "pending",
        FORWARD_PENDING: True,
        GATEWAY_RUN_ID: run_id,
        GATEWAY_SESSION_KEY: session_key,
    }


def degraded_forward_result(reason: str) -> ForwardResult:
    return {"status": "degraded", FORWARD_DEGRADED: True, FORWARD_DEGRADED_REASON: reason}


def pending_dedup_placeholder() -> JsonObject:
    return {ANALYSIS_DEGRADED: True, ANALYSIS_PENDING: True}


def unknown_analysis_result() -> AnalysisResult:
    return {"importance": "unknown", "summary": ""}


class WebhookProcessingStatus(StrEnum):
    RECEIVED = "received"
    ANALYZING = "analyzing"
    RETRY = "retry"
    FAILED = "failed"
    COMPLETED = "completed"
    DEAD_LETTER = "dead_letter"


class ForwardOutboxStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    RETRYING = "retrying"
    SENT = "sent"
    EXPIRED = "expired"
    EXHAUSTED = "exhausted"


class DeepAnalysisStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    DEGRADED = "degraded"
    ERROR = "error"


@dataclass(frozen=True)
class WebhookRequestContext:
    client_ip: str
    source: str
    payload: bytes
    parsed_data: WebhookData
    webhook_full_data: WebhookData
    headers: JsonObject = field(default_factory=dict)
    source_connection_id: int | None = None


@dataclass(frozen=True, slots=True)
class WebhookProcessContext:
    event_id: int | None
    request_id: str | None
    metric_source: str
    req_ctx: WebhookRequestContext
    alert_hash: str
    dedup_key: str
    # Lazily-computed forward-match identity (project/region/environment),
    # cached here so the payload-walk extraction runs once per event and is
    # shared by the analysis-skip silence check and the forward decision
    # (populated via decisioning.ensure_forward_match_identity).
    forward_match_identity: dict[str, str] | None = None


@dataclass(frozen=True)
class NoiseReductionContext:
    relation: str
    root_cause_event_id: int | None
    confidence: float
    suppress_forward: bool
    reason: str
    related_alert_count: int
    related_alert_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "related_alert_ids", tuple(int(item) for item in self.related_alert_ids))


def apply_importance_cap(result: AnalysisResult, *, cap: str, rule_name: str) -> AnalysisResult:
    """Lower this analysis's importance to `cap`, visibly, if it is above it.

    A ceiling, never a floor: an alert the judgement already called `low` is not
    raised to a `medium` cap. So the operator is saying "never MORE than this",
    which is the only direction that is safe to state once and forget.

    Marked like every other importance the model did not choose. Without the
    marker a capped severity is indistinguishable from a judged one, and the
    first question when a report reads wrong — "who decided this?" — has no
    answer.
    """
    if not cap or cap not in _IMPORTANCE_RANK:
        return result
    current = str(result.get("importance") or "")
    if _IMPORTANCE_RANK.get(current, _IMPORTANCE_RANK["high"]) <= _IMPORTANCE_RANK[cap]:
        return result
    result["importance"] = cap
    result[ANALYSIS_IMPORTANCE_CAP] = {"capped_to": cap, "judged": current, "rule": rule_name}
    return result
