"""Forwarding policies built from static configuration or explicit injection."""

from dataclasses import dataclass

from core.app_context import get_config_manager


@dataclass(frozen=True, slots=True)
class ForwardDeliveryPolicy:
    """投递行为配置：超时、重试、过期。所有外发路径共用。"""

    timeout_seconds: int
    max_attempts: int
    retry_initial_delay: int
    retry_max_delay: int
    retry_backoff_multiplier: float
    stale_processing_threshold_seconds: int
    max_delivery_age_seconds: int

    @classmethod
    def from_config(cls) -> "ForwardDeliveryPolicy":
        cfg = get_config_manager()
        return cls(
            timeout_seconds=int(cfg.retry.FORWARD_TIMEOUT),
            max_attempts=max(1, int(cfg.retry.FORWARD_RETRY_MAX_RETRIES) + 1),
            retry_initial_delay=int(cfg.retry.FORWARD_RETRY_INITIAL_DELAY),
            retry_max_delay=int(cfg.retry.FORWARD_RETRY_MAX_DELAY),
            retry_backoff_multiplier=float(cfg.retry.FORWARD_RETRY_BACKOFF_MULTIPLIER),
            stale_processing_threshold_seconds=int(cfg.tasks.FORWARD_OUTBOX_STALE_SECONDS),
            max_delivery_age_seconds=max(0, int(cfg.retry.FORWARD_MAX_DELIVERY_AGE_SECONDS)),
        )

    def delay_for_attempt(self, attempts: int) -> int:
        from services.operations.taskiq_retry_scheduler import compute_backoff_delay

        return compute_backoff_delay(
            attempts,
            initial_delay=self.retry_initial_delay,
            max_delay=self.retry_max_delay,
            multiplier=self.retry_backoff_multiplier,
        )


@dataclass(frozen=True, slots=True)
class OpenClawTriggerPolicy:
    enabled: bool
    timeout_seconds: int
    platform: str
    gateway_url: str
    hooks_token: str
    connect_timeout: float
    enable_degradation: bool
    http_api_url: str = ""
    max_retries: int = 3
    retry_sleep_seconds: float = 2.0

    @classmethod
    def from_config(cls) -> "OpenClawTriggerPolicy":
        cfg = get_config_manager()
        return cls(
            enabled=bool(cfg.openclaw.OPENCLAW_ENABLED),
            timeout_seconds=int(cfg.openclaw.OPENCLAW_TIMEOUT_SECONDS),
            platform=str(cfg.ai.DEEP_ANALYSIS_PLATFORM).lower(),
            gateway_url=str(cfg.openclaw.OPENCLAW_GATEWAY_URL),
            hooks_token=str(cfg.openclaw.OPENCLAW_HOOKS_TOKEN or cfg.openclaw.OPENCLAW_GATEWAY_TOKEN),
            connect_timeout=max(1.0, float(cfg.openclaw.OPENCLAW_CONNECT_TIMEOUT)),
            enable_degradation=bool(cfg.ai.ENABLE_AI_DEGRADATION),
            http_api_url=str(cfg.openclaw.OPENCLAW_HTTP_API_URL),
        )
