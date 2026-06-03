"""Webhook service policies built from static process configuration."""

from dataclasses import dataclass

from core.app_context import get_config_manager
from services.analysis.analysis_policies import NoiseScoringConfig


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
    """入口策略：body 大小限制 + 背压参数。"""

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
    """Payload 处理策略：offload 阈值 + AI strip keys + AI max bytes。"""

    offload_threshold_bytes: int = 512 * 1024
    strip_keys: frozenset[str] = frozenset()
    max_bytes: int = 0

    @classmethod
    def from_config(cls) -> "PayloadPolicy":
        cfg = get_config_manager()
        threshold = int(cfg.server.PAYLOAD_OFFLOAD_THRESHOLD_BYTES or 0)
        strip_keys = (
            frozenset(k.strip().lower() for k in cfg.ai.AI_PAYLOAD_STRIP_KEYS.split(",") if k.strip())
            if cfg.ai.AI_PAYLOAD_STRIP_KEYS
            else frozenset()
        )
        return cls(
            offload_threshold_bytes=threshold if threshold > 0 else 512 * 1024,
            strip_keys=strip_keys,
            max_bytes=int(cfg.ai.AI_PAYLOAD_MAX_BYTES),
        )


@dataclass(frozen=True, slots=True)
class WebhookRetryPolicy:
    """Webhook 重试策略。"""

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
