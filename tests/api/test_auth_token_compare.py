"""Non-ASCII credentials must be a clean 401, never a TypeError→500."""

from __future__ import annotations

from core.auth import _matches_any_configured_token


def test_non_ascii_credentials_fail_closed_without_raising() -> None:
    # Latin-1-decoded Authorization headers can carry non-ASCII; str-based
    # compare_digest raises TypeError on them (verified), which used to 500.
    assert _matches_any_configured_token("café-token", "secret") is False
    assert _matches_any_configured_token("café", "café") is True
    assert _matches_any_configured_token(None, "secret") is False
    assert _matches_any_configured_token("secret", "secret") is True
