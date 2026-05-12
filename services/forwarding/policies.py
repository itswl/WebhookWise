"""Forwarding policies built from runtime configuration or explicit injection."""

from dataclasses import dataclass
from typing import Any

from core.config import Config


@dataclass(frozen=True, slots=True)
class ForwardRetryPolicy:
    enabled: bool
    max_retries: int
    initial_delay: int
    max_delay: int
    backoff_multiplier: float

    @classmethod
    def from_config(cls, config: Any = Config) -> "ForwardRetryPolicy":
        return cls(
            enabled=bool(config.retry.ENABLE_FORWARD_RETRY),
            max_retries=int(config.retry.FORWARD_RETRY_MAX_RETRIES),
            initial_delay=int(config.retry.FORWARD_RETRY_INITIAL_DELAY),
            max_delay=int(config.retry.FORWARD_RETRY_MAX_DELAY),
            backoff_multiplier=float(config.retry.FORWARD_RETRY_BACKOFF_MULTIPLIER),
        )

    def delay_for_attempt(self, retry_count: int) -> int:
        delay = self.initial_delay * self.backoff_multiplier ** (max(1, retry_count) - 1)
        return int(min(delay, self.max_delay))


@dataclass(frozen=True, slots=True)
class RemoteForwardPolicy:
    forward_url: str
    timeout_seconds: int

    @classmethod
    def from_config(cls, config: Any = Config) -> "RemoteForwardPolicy":
        return cls(forward_url=str(config.ai.FORWARD_URL), timeout_seconds=int(config.ai.FORWARD_TIMEOUT))


@dataclass(frozen=True, slots=True)
class ForwardOutboxPolicy:
    default_target_url: str
    max_attempts: int
    retry_initial_delay: int
    retry_max_delay: int
    retry_backoff_multiplier: float
    stale_processing_threshold_seconds: int

    @classmethod
    def from_config(cls, config: Any = Config) -> "ForwardOutboxPolicy":
        return cls(
            default_target_url=str(config.ai.FORWARD_URL),
            max_attempts=max(1, int(config.retry.FORWARD_RETRY_MAX_RETRIES) + 1),
            retry_initial_delay=int(config.retry.FORWARD_RETRY_INITIAL_DELAY),
            retry_max_delay=int(config.retry.FORWARD_RETRY_MAX_DELAY),
            retry_backoff_multiplier=float(config.retry.FORWARD_RETRY_BACKOFF_MULTIPLIER),
            stale_processing_threshold_seconds=int(config.server.RECOVERY_POLLER_STUCK_THRESHOLD_SECONDS),
        )

    def delay_for_attempt(self, attempts: int) -> int:
        from services.operations.taskiq_retry_scheduler import compute_backoff_delay

        return compute_backoff_delay(
            attempts,
            initial_delay=self.retry_initial_delay,
            max_delay=self.retry_max_delay,
            multiplier=self.retry_backoff_multiplier,
        )

    def default_rule(self) -> dict[str, Any]:
        return {"name": "default", "target_url": self.default_target_url, "target_type": "webhook"}


@dataclass(frozen=True, slots=True)
class OpenClawTriggerPolicy:
    enabled: bool
    data_dir: str
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
    def from_config(cls, config: Any = Config) -> "OpenClawTriggerPolicy":
        return cls(
            enabled=bool(config.openclaw.OPENCLAW_ENABLED),
            data_dir=str(config.server.DATA_DIR),
            timeout_seconds=int(config.openclaw.OPENCLAW_TIMEOUT_SECONDS),
            platform=str(getattr(config.ai, "DEEP_ANALYSIS_PLATFORM", "openclaw")).lower(),
            gateway_url=str(config.openclaw.OPENCLAW_GATEWAY_URL),
            hooks_token=str(config.openclaw.OPENCLAW_HOOKS_TOKEN or config.openclaw.OPENCLAW_GATEWAY_TOKEN),
            connect_timeout=max(1.0, float(config.openclaw.OPENCLAW_CONNECT_TIMEOUT)),
            enable_degradation=bool(config.ai.ENABLE_AI_DEGRADATION),
            http_api_url=str(config.openclaw.OPENCLAW_HTTP_API_URL),
        )
