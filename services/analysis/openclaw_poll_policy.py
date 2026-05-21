"""OpenClaw polling policy built from runtime configuration."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from core.config import Config


@dataclass(frozen=True, slots=True)
class OpenClawPollPolicy:
    timeout_seconds: int
    poll_timeout_seconds: int
    poll_initial_delay_seconds: int
    poll_max_delay_seconds: int
    poll_backoff_multiplier: float
    http_api_url: str
    gateway_url: str
    gateway_token: str
    hooks_token: str
    connect_timeout_seconds: float
    stability_required_hits: int
    max_consecutive_errors: int
    enable_degradation: bool
    notification_webhook_url: str

    @classmethod
    def from_config(cls, config: Any | None = None) -> "OpenClawPollPolicy":
        config = config or Config
        return cls(
            timeout_seconds=int(config.openclaw.OPENCLAW_TIMEOUT_SECONDS),
            poll_timeout_seconds=max(1, int(config.openclaw.OPENCLAW_POLL_TIMEOUT)),
            poll_initial_delay_seconds=max(1, int(config.openclaw.OPENCLAW_POLL_INITIAL_DELAY_SECONDS)),
            poll_max_delay_seconds=max(
                max(1, int(config.openclaw.OPENCLAW_POLL_INITIAL_DELAY_SECONDS)),
                int(config.openclaw.OPENCLAW_POLL_MAX_DELAY_SECONDS),
            ),
            poll_backoff_multiplier=max(1.0, float(config.openclaw.OPENCLAW_POLL_BACKOFF_MULTIPLIER)),
            http_api_url=str(config.openclaw.OPENCLAW_HTTP_API_URL).strip(),
            gateway_url=str(config.openclaw.OPENCLAW_GATEWAY_URL).strip(),
            gateway_token=str(config.openclaw.OPENCLAW_GATEWAY_TOKEN),
            hooks_token=str(config.openclaw.OPENCLAW_HOOKS_TOKEN or config.openclaw.OPENCLAW_GATEWAY_TOKEN),
            connect_timeout_seconds=max(1.0, float(config.openclaw.OPENCLAW_CONNECT_TIMEOUT)),
            stability_required_hits=max(1, int(config.openclaw.OPENCLAW_STABILITY_REQUIRED_HITS)),
            max_consecutive_errors=int(config.openclaw.OPENCLAW_MAX_CONSECUTIVE_ERRORS),
            enable_degradation=bool(config.openclaw.OPENCLAW_ENABLE_DEGRADATION),
            notification_webhook_url=str(config.notifications.DEEP_ANALYSIS_FEISHU_WEBHOOK),
        )

    @property
    def has_http_api(self) -> bool:
        return bool(self.http_api_url.strip())

    @property
    def http_poll_timeout(self) -> float:
        return float(self.poll_timeout_seconds)

    @property
    def http_connect_timeout(self) -> float:
        return max(1.0, min(float(self.connect_timeout_seconds), self.http_poll_timeout))

    @property
    def poll_claim_lease_seconds(self) -> int:
        return max(30, self.poll_timeout_seconds * 3, 90) + 30

    def clamp_delay_to_timeout(self, delay_seconds: int, created_at: datetime | None) -> int:
        if created_at is None:
            return delay_seconds
        elapsed = (datetime.now() - created_at).total_seconds()
        remaining = int(self.timeout_seconds - elapsed)
        if remaining <= 0:
            return 1
        return max(1, min(delay_seconds, remaining))

    def delay_for_attempt(self, poll_attempts: int) -> int:
        normalized_attempts = max(0, int(poll_attempts))
        delay = float(self.poll_initial_delay_seconds)
        for _ in range(normalized_attempts):
            delay *= self.poll_backoff_multiplier
            if delay >= self.poll_max_delay_seconds:
                return self.poll_max_delay_seconds
        return max(1, int(delay))

    def http_auth_headers(self, trace_id: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.hooks_token}"}
        if trace_id:
            headers["X-Trace-Id"] = trace_id
        return headers


@dataclass(frozen=True, slots=True)
class OpenClawWsPolicy:
    device_id: str
    device_private_key_b64: str
    device_token: str
    gateway_token: str
    nonce_timeout: float

    @classmethod
    def from_config(cls, config: Any | None = None) -> "OpenClawWsPolicy":
        config = config or Config
        return cls(
            device_id=str(config.openclaw.OPENCLAW_DEVICE_ID),
            device_private_key_b64=str(config.openclaw.OPENCLAW_DEVICE_PRIVATE_KEY_PEM),
            device_token=str(config.openclaw.OPENCLAW_DEVICE_TOKEN),
            gateway_token=str(config.openclaw.OPENCLAW_GATEWAY_TOKEN),
            nonce_timeout=float(config.openclaw.OPENCLAW_NONCE_TIMEOUT),
        )
