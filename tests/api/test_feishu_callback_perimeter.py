"""Behavioral negative tests for the HTTP verification perimeter.

These exercise the real FastAPI dependency stack (routing, middleware, auth
dependencies) over ASGI instead of calling the dependency functions directly;
tests/runtime/test_runtime_core.py covers the direct-call branches.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

CARD_ACTIONS_PATH = "/v1/integrations/feishu/card-actions"
CHANGES_PATH = "/v1/changes"
VERIFICATION_TOKEN = "feishu-verification-token"


def _client() -> httpx.AsyncClient:
    from api.app import app

    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def _challenge_payload(token: str, challenge: str = "echo-me-back") -> dict[str, Any]:
    return {"header": {"token": token}, "type": "url_verification", "challenge": challenge}


@pytest.fixture
def card_actions_enabled(monkeypatch: pytest.MonkeyPatch, temp_config: Any) -> Any:
    """Feature on with a configured verification token; admin rate limit off."""
    monkeypatch.setattr(temp_config.security, "ADMIN_API_RATE_LIMIT_PER_MINUTE", 0)
    monkeypatch.setattr(temp_config.notifications, "FEISHU_CARD_ACTIONS_ENABLED", True)
    monkeypatch.setattr(temp_config.security, "FEISHU_CARD_VERIFICATION_TOKEN", VERIFICATION_TOKEN)
    return temp_config


@pytest.mark.asyncio
async def test_card_actions_return_404_while_feature_disabled(
    monkeypatch: pytest.MonkeyPatch, card_actions_enabled: Any
) -> None:
    monkeypatch.setattr(card_actions_enabled.notifications, "FEISHU_CARD_ACTIONS_ENABLED", False)

    async with _client() as client:
        disabled = await client.post(CARD_ACTIONS_PATH, json=_challenge_payload(VERIFICATION_TOKEN))
        monkeypatch.setattr(card_actions_enabled.notifications, "FEISHU_CARD_ACTIONS_ENABLED", True)
        enabled = await client.post(CARD_ACTIONS_PATH, json=_challenge_payload(VERIFICATION_TOKEN))

    # The same request flips between 404 and 200 on the feature flag alone,
    # proving the 404 is the disabled feature rather than a missing route.
    assert disabled.status_code == 404
    assert enabled.status_code == 200


@pytest.mark.asyncio
async def test_card_actions_return_503_while_verification_token_unconfigured(
    monkeypatch: pytest.MonkeyPatch, card_actions_enabled: Any
) -> None:
    monkeypatch.setattr(card_actions_enabled.security, "FEISHU_CARD_VERIFICATION_TOKEN", "")

    async with _client() as client:
        unconfigured = await client.post(CARD_ACTIONS_PATH, json=_challenge_payload(VERIFICATION_TOKEN))
        monkeypatch.setattr(card_actions_enabled.security, "FEISHU_CARD_VERIFICATION_TOKEN", VERIFICATION_TOKEN)
        configured = await client.post(CARD_ACTIONS_PATH, json=_challenge_payload(VERIFICATION_TOKEN))

    assert unconfigured.status_code == 503
    assert configured.status_code == 200


@pytest.mark.asyncio
async def test_card_actions_reject_wrong_verification_token(card_actions_enabled: Any) -> None:
    async with _client() as client:
        wrong_header_token = await client.post(CARD_ACTIONS_PATH, json=_challenge_payload("wrong-token"))
        wrong_legacy_token = await client.post(
            CARD_ACTIONS_PATH, json={"token": "wrong-token", "challenge": "echo-me-back"}
        )

    # Both token positions carry a challenge that would echo as 200 if the
    # perimeter let a bad token through.
    assert wrong_header_token.status_code == 401
    assert wrong_legacy_token.status_code == 401


@pytest.mark.asyncio
async def test_card_actions_echo_challenge_for_verified_handshake(card_actions_enabled: Any) -> None:
    async with _client() as client:
        response = await client.post(CARD_ACTIONS_PATH, json=_challenge_payload(VERIFICATION_TOKEN))

    assert response.status_code == 200
    assert response.json() == {"challenge": "echo-me-back"}


@pytest.mark.asyncio
async def test_card_actions_reject_oversized_and_empty_bodies(card_actions_enabled: Any) -> None:
    oversized = b'{"pad":"' + b"a" * 131_073 + b'"}'
    headers = {"Content-Type": "application/json"}

    async with _client() as client:
        too_big = await client.post(CARD_ACTIONS_PATH, content=oversized, headers=headers)
        empty = await client.post(CARD_ACTIONS_PATH, content=b"", headers=headers)

    assert too_big.status_code == 400
    assert empty.status_code == 400


@pytest.mark.asyncio
async def test_change_ingest_rejects_wrong_bearer_and_plain_api_key(
    monkeypatch: pytest.MonkeyPatch, temp_config: Any
) -> None:
    monkeypatch.setattr(temp_config.security, "ADMIN_API_RATE_LIMIT_PER_MINUTE", 0)
    monkeypatch.setattr(temp_config.security, "API_KEY", "plain-api-key")
    monkeypatch.setattr(temp_config.security, "ADMIN_WRITE_KEY", "admin-write-key")
    monkeypatch.setattr(temp_config.security, "CHANGE_INGEST_TOKEN", "change-ingest-token")

    async with _client() as client:
        wrong_token = await client.post(CHANGES_PATH, json={}, headers={"Authorization": "Bearer wrong-token"})
        plain_api_key = await client.post(CHANGES_PATH, json={}, headers={"Authorization": "Bearer plain-api-key"})
        correct_token = await client.post(
            CHANGES_PATH, json={}, headers={"Authorization": "Bearer change-ingest-token"}
        )

    assert wrong_token.status_code == 401
    # The read-scoped API key must not clear the ingest perimeter.
    assert plain_api_key.status_code == 401
    # The dedicated token clears auth; the empty body then fails schema validation.
    assert correct_token.status_code == 422
