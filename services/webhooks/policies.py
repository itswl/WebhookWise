"""Webhook service policies built from static process configuration."""

from dataclasses import dataclass
from typing import Any

from core.app_context import get_default_config
from services.analysis.config_models import NoiseScoringConfig
from services.webhooks.decisioning import ForwardingPolicy


@dataclass(frozen=True, slots=True)
class AnalysisResolutionPolicy:
    duplicate_window_hours: int
    recent_beyond_window_reuse_seconds: int
    reanalyze_after_time_window: bool

    @classmethod
    def from_config(cls, config: Any | None = None) -> "AnalysisResolutionPolicy":
        config = config or get_default_config()
        return cls(
            duplicate_window_hours=int(config.retry.DUPLICATE_ALERT_TIME_WINDOW),
            recent_beyond_window_reuse_seconds=int(config.retry.RECENT_BEYOND_WINDOW_REUSE_SECONDS),
            reanalyze_after_time_window=bool(config.retry.REANALYZE_AFTER_TIME_WINDOW),
        )


@dataclass(frozen=True, slots=True)
class NoiseReductionPolicy:
    enabled: bool
    window_minutes: int
    root_cause_min_confidence: float
    suppress_derived_forward: bool
    scoring_config: NoiseScoringConfig

    @classmethod
    def from_config(cls, config: Any | None = None) -> "NoiseReductionPolicy":
        config = config or get_default_config().ai
        return cls(
            enabled=bool(config.ENABLE_ALERT_NOISE_REDUCTION),
            window_minutes=max(1, int(config.NOISE_REDUCTION_WINDOW_MINUTES)),
            root_cause_min_confidence=float(config.ROOT_CAUSE_MIN_CONFIDENCE),
            suppress_derived_forward=bool(config.SUPPRESS_DERIVED_ALERT_FORWARD),
            scoring_config=NoiseScoringConfig.from_config(config),
        )


@dataclass(frozen=True, slots=True)
class WebhookFailurePolicy:
    max_retries: int
    initial_delay: int
    max_delay: int
    backoff_multiplier: float

    @classmethod
    def from_config(cls, config: Any | None = None) -> "WebhookFailurePolicy":
        config = config or get_default_config()
        return cls(
            max_retries=max(0, int(config.retry.WEBHOOK_RETRY_MAX_RETRIES)),
            initial_delay=int(config.retry.WEBHOOK_RETRY_INITIAL_DELAY),
            max_delay=int(config.retry.WEBHOOK_RETRY_MAX_DELAY),
            backoff_multiplier=float(config.retry.WEBHOOK_RETRY_BACKOFF_MULTIPLIER),
        )


@dataclass(frozen=True, slots=True)
class PayloadSanitizerPolicy:
    offload_threshold_bytes: int
    strip_keys: frozenset[str]
    max_bytes: int

    @classmethod
    def from_config(cls, config: Any | None = None) -> "PayloadSanitizerPolicy":
        config = config or get_default_config()
        threshold = int(config.server.PAYLOAD_OFFLOAD_THRESHOLD_BYTES or 0)
        strip_keys = (
            frozenset(k.strip().lower() for k in config.ai.AI_PAYLOAD_STRIP_KEYS.split(",") if k.strip())
            if config.ai.AI_PAYLOAD_STRIP_KEYS
            else frozenset()
        )
        return cls(
            offload_threshold_bytes=threshold if threshold > 0 else 512 * 1024,
            strip_keys=strip_keys,
            max_bytes=int(config.ai.AI_PAYLOAD_MAX_BYTES),
        )


@dataclass(frozen=True, slots=True)
class WebhookReceivePolicy:
    max_body_bytes: int
    ingress_backpressure_threshold: int
    ingress_backpressure_window_seconds: int

    @classmethod
    def from_config(cls, config: Any | None = None) -> "WebhookReceivePolicy":
        config = config or get_default_config()
        return cls(
            max_body_bytes=max(0, int(config.security.MAX_WEBHOOK_BODY_BYTES or 0)),
            ingress_backpressure_threshold=max(0, int(config.retry.PROCESSING_LOCK_FAILFAST_THRESHOLD or 0)),
            ingress_backpressure_window_seconds=max(1, int(config.retry.PROCESSING_LOCK_FAILFAST_WINDOW_SECONDS or 1)),
        )


def forwarding_policy_from_config(config: Any | None = None) -> ForwardingPolicy:
    config = config or get_default_config()
    return ForwardingPolicy(
        notification_cooldown_seconds=config.retry.NOTIFICATION_COOLDOWN_SECONDS,
        enable_periodic_reminder=config.retry.ENABLE_PERIODIC_REMINDER,
        reminder_interval_hours=config.retry.REMINDER_INTERVAL_HOURS,
        forward_duplicate_alerts=config.retry.FORWARD_DUPLICATE_ALERTS,
        forward_after_time_window=config.retry.FORWARD_AFTER_TIME_WINDOW,
    )
