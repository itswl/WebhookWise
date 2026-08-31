"""Regression tests for review group C (security/AI/config fixes).

- AI cache key incorporates a model+prompt fingerprint
- secret minimum-length enforcement in production
- webhook replay protection (timestamp + one-time nonce)
"""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest

# --- AI cache key fingerprint ---------------------------------------------------


def test_cache_key_changes_with_model(monkeypatch: pytest.MonkeyPatch, temp_config) -> None:
    from services.analysis import ai_cache

    monkeypatch.setattr(temp_config.ai, "OPENAI_MODEL", "model-a")
    key_a = ai_cache.get_cache_key("alert-x")
    monkeypatch.setattr(temp_config.ai, "OPENAI_MODEL", "model-b")
    key_b = ai_cache.get_cache_key("alert-x")

    assert key_a != key_b
    assert key_a.endswith("_alert-x")
    assert key_b.endswith("_alert-x")


# --- secret min length ----------------------------------------------------------


def test_startup_warns_but_allows_short_api_key(temp_config, monkeypatch: pytest.MonkeyPatch) -> None:
    from core.web import startup_checks

    temp_config.security.API_KEY = "short"
    temp_config.security.ADMIN_WRITE_KEY = "a-sufficiently-long-admin-key"
    temp_config.security.REQUIRE_WEBHOOK_AUTH = True
    temp_config.security.WEBHOOK_SECRET = "a-sufficiently-long-webhook-secret"

    warnings: list[str] = []
    monkeypatch.setattr(
        startup_checks.logger, "warning", lambda msg, *args, **_k: warnings.append(msg % args if args else msg)
    )

    # Short secrets warn (rotate prompt) but must NOT block startup so an
    # existing deployment with legacy keys is not bricked.
    startup_checks.validate_startup_security(temp_config, app_env="production")
    assert any("API_KEY" in w for w in warnings)


def test_startup_rejects_empty_webhook_secret_when_auth_required(temp_config) -> None:
    from core.web.startup_checks import validate_startup_security

    temp_config.security.API_KEY = "a-sufficiently-long-api-key-value"
    temp_config.security.ADMIN_WRITE_KEY = "a-sufficiently-long-admin-key-value"
    temp_config.security.REQUIRE_WEBHOOK_AUTH = True
    temp_config.security.WEBHOOK_SECRET = ""
    with pytest.raises(RuntimeError, match="WEBHOOK_SECRET is empty"):
        validate_startup_security(temp_config, app_env="production")


def test_startup_accepts_strong_secrets(temp_config) -> None:
    from core.web.startup_checks import validate_startup_security

    temp_config.security.API_KEY = "a-sufficiently-long-api-key-value"
    temp_config.security.ADMIN_WRITE_KEY = "a-sufficiently-long-admin-key-value"
    temp_config.security.REQUIRE_WEBHOOK_AUTH = True
    temp_config.security.WEBHOOK_SECRET = "a-sufficiently-long-webhook-secret"
    validate_startup_security(temp_config, app_env="production")  # no raise


def _collect_startup_warnings(temp_config, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from core.web import startup_checks

    temp_config.security.API_KEY = "a-sufficiently-long-api-key-value"
    temp_config.security.ADMIN_WRITE_KEY = "a-sufficiently-long-admin-key-value"
    temp_config.security.REQUIRE_WEBHOOK_AUTH = True
    temp_config.security.WEBHOOK_SECRET = "a-sufficiently-long-webhook-secret"

    warnings: list[str] = []
    monkeypatch.setattr(
        startup_checks.logger, "warning", lambda msg, *args, **_k: warnings.append(msg % args if args else msg)
    )
    startup_checks.validate_startup_security(temp_config, app_env="production")
    return warnings


def test_startup_warns_when_ingress_ships_unlimited_and_replayable(
    temp_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Out of the box the UNAUTHENTICATED ingress had no per-IP ceiling while
    the authenticated admin API had one, and a captured signed request replayed
    forever — both documented defaults, neither surfaced at startup. Non-fatal:
    existing deployments must keep starting."""
    temp_config.security.WEBHOOK_RATE_LIMIT_PER_MINUTE = 0
    temp_config.security.WEBHOOK_RATE_LIMIT_BURST = 0
    temp_config.security.WEBHOOK_RATE_LIMIT_GLOBAL_PER_MINUTE = 0
    temp_config.security.WEBHOOK_REPLAY_PROTECTION_ENABLED = False

    warnings = _collect_startup_warnings(temp_config, monkeypatch)
    assert any("rate limiting is disabled" in w for w in warnings)
    assert any("replay protection is disabled" in w for w in warnings)


def test_startup_stays_quiet_when_ingress_protections_are_on(temp_config, monkeypatch: pytest.MonkeyPatch) -> None:
    temp_config.security.WEBHOOK_RATE_LIMIT_PER_MINUTE = 120
    temp_config.security.WEBHOOK_REPLAY_PROTECTION_ENABLED = True

    warnings = _collect_startup_warnings(temp_config, monkeypatch)
    assert not any("rate limiting is disabled" in w for w in warnings)
    assert not any("replay protection is disabled" in w for w in warnings)


def test_startup_warns_when_kb_runs_on_placeholder_embeddings(temp_config, monkeypatch: pytest.MonkeyPatch) -> None:
    """KB_ENABLED with no embeddings endpoint 'works' end to end while ranking
    on a deterministic hash vector — a feature that looks functional and is
    not. The dev/offline story is unchanged; production gets one log line."""
    temp_config.security.WEBHOOK_RATE_LIMIT_PER_MINUTE = 120
    temp_config.security.WEBHOOK_REPLAY_PROTECTION_ENABLED = True
    temp_config.kb.KB_ENABLED = True
    temp_config.kb.KB_EMBEDDING_API_URL = ""

    warnings = _collect_startup_warnings(temp_config, monkeypatch)
    assert any("placeholder embedding" in w for w in warnings)

    temp_config.kb.KB_EMBEDDING_API_URL = "https://embeddings.example.com/v1"
    warnings = _collect_startup_warnings(temp_config, monkeypatch)
    assert not any("placeholder embedding" in w for w in warnings)


# --- webhook replay protection --------------------------------------------------


def test_verify_timestamped_signature_roundtrip() -> None:
    from core.webhook_security import verify_timestamped_signature

    secret = "topsecret-value-1234"
    body = b'{"alertname":"X"}'
    ts = "1700000000"
    sig = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()

    assert verify_timestamped_signature(ts, body, sig, secret) is True
    assert verify_timestamped_signature("1700000001", body, sig, secret) is False  # ts changed
    assert verify_timestamped_signature(ts, b"tampered", sig, secret) is False


@pytest.mark.asyncio
async def test_replay_protection_rejects_stale_timestamp(monkeypatch: pytest.MonkeyPatch, temp_config) -> None:
    from core.webhook_security import ReplayError, enforce_replay_protection

    temp_config.security.WEBHOOK_SECRET = "topsecret-value-1234"
    temp_config.security.WEBHOOK_REPLAY_MAX_SKEW_SECONDS = 300

    stale_ts = str(int(time.time()) - 10_000)
    body = b'{"alertname":"X"}'
    sig = hmac.new(b"topsecret-value-1234", stale_ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    headers = {"x-webhook-signature": sig, "x-webhook-timestamp": stale_ts}

    with pytest.raises(ReplayError, match="skew"):
        await enforce_replay_protection(headers, body, security=temp_config.security)


@pytest.mark.asyncio
async def test_replay_protection_consumes_nonce_once(monkeypatch: pytest.MonkeyPatch, temp_config) -> None:
    import core.redis_client as redis_client
    from core.webhook_security import ReplayError, enforce_replay_protection

    temp_config.security.WEBHOOK_SECRET = "topsecret-value-1234"
    temp_config.security.WEBHOOK_REPLAY_MAX_SKEW_SECONDS = 300

    seen: set[str] = set()

    async def fake_set_nx_ex(key: str, value: str, ttl_seconds: int) -> bool:
        if key in seen:
            return False
        seen.add(key)
        return True

    monkeypatch.setattr(redis_client, "redis_set_nx_ex", fake_set_nx_ex)

    ts = str(int(time.time()))
    body = b'{"alertname":"X"}'
    sig = hmac.new(b"topsecret-value-1234", ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    headers = {"x-webhook-signature": sig, "x-webhook-timestamp": ts}

    # First request succeeds; identical replay is rejected.
    await enforce_replay_protection(headers, body, security=temp_config.security)
    with pytest.raises(ReplayError, match="replay"):
        await enforce_replay_protection(headers, body, security=temp_config.security)


@pytest.mark.asyncio
async def test_replay_protection_accepts_previous_secret_during_rotation(
    monkeypatch: pytest.MonkeyPatch, temp_config
) -> None:
    import core.redis_client as redis_client
    from core.webhook_security import ReplayError, enforce_replay_protection

    temp_config.security.WEBHOOK_SECRET = "new-secret-after-rotation"
    temp_config.security.WEBHOOK_SECRET_PREVIOUS = "topsecret-value-1234"
    temp_config.security.WEBHOOK_REPLAY_MAX_SKEW_SECONDS = 300

    async def fake_set_nx_ex(key: str, value: str, ttl_seconds: int) -> bool:
        return True

    monkeypatch.setattr(redis_client, "redis_set_nx_ex", fake_set_nx_ex)

    ts = str(int(time.time()))
    body = b'{"alertname":"X"}'
    old_sig = hmac.new(b"topsecret-value-1234", ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    await enforce_replay_protection(
        {"x-webhook-signature": old_sig, "x-webhook-timestamp": ts}, body, security=temp_config.security
    )

    forged = hmac.new(b"some-other-secret", ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    with pytest.raises(ReplayError, match="mismatch"):
        await enforce_replay_protection(
            {"x-webhook-signature": forged, "x-webhook-timestamp": ts}, body, security=temp_config.security
        )


@pytest.mark.asyncio
async def test_replay_protection_noop_without_signature(temp_config) -> None:
    from core.webhook_security import enforce_replay_protection

    temp_config.security.WEBHOOK_SECRET = "topsecret-value-1234"
    # Token-auth path (no signature header) is not replay-checked.
    await enforce_replay_protection({}, b"{}", security=temp_config.security)
