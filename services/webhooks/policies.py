"""Webhook service policies built from static process configuration."""

from dataclasses import dataclass
from functools import lru_cache

from core.app_context import get_config_manager
from services.analysis.analysis_policies import NoiseScoringConfig


@lru_cache(maxsize=16)
def _parse_strip_keys(raw: str) -> frozenset[str]:
    """Parse the AI_PAYLOAD_STRIP_KEYS CSV into a frozenset, memoized on the raw
    string value so the per-analysis hot path skips the re-parse; a config change
    is a new key and is picked up immediately."""
    if not raw:
        return frozenset()
    return frozenset(k.strip().lower() for k in raw.split(",") if k.strip())


@dataclass(frozen=True, slots=True)
class NoiseReductionPolicy:
    enabled: bool
    window_minutes: int
    root_cause_min_confidence: float
    suppress_derived_forward: bool
    scoring_config: NoiseScoringConfig

    @classmethod
    def from_config(cls) -> "NoiseReductionPolicy":
        cfg = get_config_manager().noise
        return cls(
            enabled=bool(cfg.ENABLE_ALERT_NOISE_REDUCTION),
            window_minutes=max(1, int(cfg.NOISE_REDUCTION_WINDOW_MINUTES)),
            root_cause_min_confidence=float(cfg.ROOT_CAUSE_MIN_CONFIDENCE),
            suppress_derived_forward=bool(cfg.SUPPRESS_DERIVED_ALERT_FORWARD),
            scoring_config=NoiseScoringConfig.from_config(cfg),
        )


@dataclass(frozen=True, slots=True)
class IngressPolicy:
    """Ingress policy: body size limit + backpressure parameters."""

    max_body_bytes: int
    ingress_backpressure_threshold: int
    ingress_backpressure_window_seconds: int
    ingress_backpressure_fail_open_on_redis_error: bool = False

    @classmethod
    def from_config(cls) -> "IngressPolicy":
        cfg = get_config_manager()
        return cls(
            max_body_bytes=max(0, int(cfg.security.MAX_WEBHOOK_BODY_BYTES or 0)),
            ingress_backpressure_threshold=max(0, int(cfg.retry.PROCESSING_LOCK_FAILFAST_THRESHOLD or 0)),
            ingress_backpressure_window_seconds=max(1, int(cfg.retry.PROCESSING_LOCK_FAILFAST_WINDOW_SECONDS or 1)),
            ingress_backpressure_fail_open_on_redis_error=bool(cfg.retry.INGRESS_BACKPRESSURE_FAIL_OPEN_ON_REDIS_ERROR),
        )


@dataclass(frozen=True, slots=True)
class PayloadPolicy:
    """Payload processing policy: offload threshold + AI strip keys + AI max bytes."""

    offload_threshold_bytes: int = 512 * 1024
    strip_keys: frozenset[str] = frozenset()
    max_bytes: int = 0

    @classmethod
    def from_config(cls) -> "PayloadPolicy":
        cfg = get_config_manager()
        threshold = int(cfg.server.PAYLOAD_OFFLOAD_THRESHOLD_BYTES or 0)
        return cls(
            offload_threshold_bytes=threshold if threshold > 0 else 512 * 1024,
            strip_keys=_parse_strip_keys(str(cfg.ai.AI_PAYLOAD_STRIP_KEYS or "")),
            max_bytes=int(cfg.ai.AI_PAYLOAD_MAX_BYTES),
        )


@dataclass(frozen=True, slots=True)
class WebhookRetryPolicy:
    """Webhook retry policy."""

    max_retries: int = 0
    initial_delay: int = 5
    max_delay: int = 300
    backoff_multiplier: float = 2.0

    @classmethod
    def from_config(cls) -> "WebhookRetryPolicy":
        cfg = get_config_manager()
        return cls(
            max_retries=max(0, int(cfg.retry.WEBHOOK_RETRY_MAX_RETRIES)),
            initial_delay=int(cfg.retry.WEBHOOK_RETRY_INITIAL_DELAY),
            max_delay=int(cfg.retry.WEBHOOK_RETRY_MAX_DELAY),
            backoff_multiplier=float(cfg.retry.WEBHOOK_RETRY_BACKOFF_MULTIPLIER),
        )
