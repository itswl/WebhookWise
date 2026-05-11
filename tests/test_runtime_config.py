def test_openclaw_operational_knobs_are_runtime_configurable() -> None:
    from core.config import Config

    expected = {
        "OPENCLAW_ENABLED",
        "OPENCLAW_GATEWAY_URL",
        "OPENCLAW_GATEWAY_TOKEN",
        "OPENCLAW_HOOKS_TOKEN",
        "OPENCLAW_HTTP_API_URL",
        "OPENCLAW_TIMEOUT_SECONDS",
        "OPENCLAW_STABILITY_REQUIRED_HITS",
        "OPENCLAW_POLL_INITIAL_DELAY_SECONDS",
        "OPENCLAW_POLL_MAX_DELAY_SECONDS",
        "OPENCLAW_POLL_BACKOFF_MULTIPLIER",
        "OPENCLAW_MAX_CONSECUTIVE_ERRORS",
        "OPENCLAW_ENABLE_DEGRADATION",
        "OPENCLAW_CONNECT_TIMEOUT",
        "OPENCLAW_HANDSHAKE_TIMEOUT",
        "OPENCLAW_NONCE_TIMEOUT",
        "OPENCLAW_POLL_TIMEOUT",
    }

    for key in expected:
        assert Config.RUNTIME_KEYS[key]["sub"] == "openclaw"
