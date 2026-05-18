import pytest


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


def test_prompt_templates_are_runtime_configurable() -> None:
    from core.config import Config

    expected = {
        "AI_USER_PROMPT",
        "AI_USER_PROMPT_FILE",
        "DEEP_ANALYSIS_PROMPT",
        "DEEP_ANALYSIS_PROMPT_FILE",
    }

    for key in expected:
        assert Config.RUNTIME_KEYS[key]["sub"] == "ai"


def test_connection_runtime_keys_require_restart_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import Config

    monkeypatch.setattr(Config.server, "ALLOW_RUNTIME_CONNECTION_CONFIG", False)

    assert Config.runtime_key_requires_restart("OPENAI_API_KEY") is True
    assert Config.runtime_key_requires_restart("OPENCLAW_GATEWAY_TOKEN") is True
    assert Config.runtime_key_requires_restart("OPENAI_MODEL") is False
    assert Config.runtime_key_requires_restart("OPENCLAW_TIMEOUT_SECONDS") is False
    Config._ensure_runtime_key_mutable("OPENAI_API_KEY", "")


def test_connection_runtime_keys_can_be_explicitly_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import Config

    monkeypatch.setattr(Config.server, "ALLOW_RUNTIME_CONNECTION_CONFIG", True)

    assert Config.runtime_key_requires_restart("OPENAI_API_KEY") is False
    Config._ensure_runtime_key_mutable("OPENAI_API_KEY", "sk-test")


def test_connection_runtime_key_update_is_rejected_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import Config

    monkeypatch.setattr(Config.server, "ALLOW_RUNTIME_CONNECTION_CONFIG", False)

    with pytest.raises(ValueError, match="默认禁止热更新"):
        Config._ensure_runtime_key_mutable("OPENCLAW_GATEWAY_URL", "http://openclaw.internal")


def test_config_sources_marks_restart_required_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import Config
    from services.runtime_config.config_service import get_config_sources

    monkeypatch.setattr(Config.server, "ALLOW_RUNTIME_CONNECTION_CONFIG", False)

    by_key = {item["key"]: item for item in get_config_sources()}

    assert by_key["OPENAI_API_KEY"]["requires_restart"] is True
    assert by_key["OPENCLAW_TIMEOUT_SECONDS"]["requires_restart"] is False
