"""Forwarding policies built from static configuration or explicit injection."""

from dataclasses import dataclass

from core.app_context import get_config_manager


@dataclass(frozen=True, slots=True)
class ForwardDeliveryPolicy:
    """Delivery behavior configuration: timeout, retry, expiration. Shared by all outbound paths."""

    timeout_seconds: int
    max_attempts: int
    retry_initial_delay: int
    retry_max_delay: int
    retry_backoff_multiplier: float
    stale_processing_threshold_seconds: int
    max_delivery_age_seconds: int

    @classmethod
    def from_config(cls) -> "ForwardDeliveryPolicy":
        from services.operations import runtime_settings as rt

        cfg = get_config_manager()
        return cls(
            timeout_seconds=rt.override_or("FORWARD_TIMEOUT_SECONDS", int(cfg.retry.FORWARD_TIMEOUT_SECONDS)),
            max_attempts=max(
                1, rt.override_or("FORWARD_RETRY_MAX_RETRIES", int(cfg.retry.FORWARD_RETRY_MAX_RETRIES)) + 1
            ),
            retry_initial_delay=int(
                rt.override_or(
                    "FORWARD_RETRY_INITIAL_DELAY_SECONDS", float(cfg.retry.FORWARD_RETRY_INITIAL_DELAY_SECONDS)
                )
            ),
            retry_max_delay=int(
                rt.override_or("FORWARD_RETRY_MAX_DELAY_SECONDS", float(cfg.retry.FORWARD_RETRY_MAX_DELAY_SECONDS))
            ),
            retry_backoff_multiplier=rt.override_or(
                "FORWARD_RETRY_BACKOFF_MULTIPLIER", float(cfg.retry.FORWARD_RETRY_BACKOFF_MULTIPLIER)
            ),
            stale_processing_threshold_seconds=int(cfg.tasks.FORWARD_OUTBOX_STALE_SECONDS),
            max_delivery_age_seconds=max(
                0, rt.override_or("FORWARD_MAX_DELIVERY_AGE_SECONDS", int(cfg.retry.FORWARD_MAX_DELIVERY_AGE_SECONDS))
            ),
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
class DeepAnalysisTriggerPolicy:
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

    # Which named gateway this policy addresses; recorded on the analysis row so
    # the poller collects from the same one.
    gateway_name: str = ""

    @classmethod
    def from_config(cls, gateway_name: str | None = None) -> "DeepAnalysisTriggerPolicy":
        """Build the policy for one gateway.

        Addressing and dialect come from the named instance; timeouts and
        degradation stay global, because they describe how patiently this
        service waits rather than a property of the thing it waits on.

        Raises UnknownGatewayError when a rule names a gateway that is not
        configured — never falls back to the default, which would send an
        investigation somewhere the rule did not ask for.
        """
        from services.analysis.deep_analysis_gateways import resolve_gateway
        from services.operations import runtime_settings as rt

        cfg = get_config_manager()
        gateway = resolve_gateway(gateway_name)
        return cls(
            enabled=rt.override_or("DEEP_ANALYSIS_ENABLED", bool(cfg.deep_analysis.DEEP_ANALYSIS_ENABLED)),
            timeout_seconds=rt.override_or(
                "DEEP_ANALYSIS_TIMEOUT_SECONDS", int(cfg.deep_analysis.DEEP_ANALYSIS_TIMEOUT_SECONDS)
            ),
            platform=gateway.platform,
            gateway_url=gateway.gateway_url,
            hooks_token=gateway.token,
            connect_timeout=max(1.0, float(cfg.deep_analysis.DEEP_ANALYSIS_CONNECT_TIMEOUT_SECONDS)),
            enable_degradation=rt.override_or("ENABLE_AI_DEGRADATION", bool(cfg.ai.ENABLE_AI_DEGRADATION)),
            http_api_url=gateway.http_api_url,
            gateway_name=gateway.name,
        )
