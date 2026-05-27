from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError


def test_forward_rule_request_models_validate_api_boundary() -> None:
    from schemas.forwarding import ForwardRuleCreateRequest, ForwardRuleUpdateRequest

    create = ForwardRuleCreateRequest.model_validate(
        {
            "name": "  pager  ",
            "target_type": "webhook",
            "target_url": " https://example.com/hook ",
            "match_duplicate": "new",
        }
    )
    assert create.name == "pager"
    assert create.target_url == "https://example.com/hook"
    assert create.to_service_kwargs()["match_duplicate"] == "new"

    with pytest.raises(ValidationError):
        ForwardRuleCreateRequest.model_validate({"name": "bad", "target_type": "smtp"})

    with pytest.raises(ValidationError):
        ForwardRuleCreateRequest.model_validate({"name": "bad", "target_type": "webhook", "surprise": True})

    with pytest.raises(ValidationError):
        ForwardRuleUpdateRequest.model_validate({})

    with pytest.raises(ValidationError):
        ForwardRuleUpdateRequest.model_validate({"name": None})

    update = ForwardRuleUpdateRequest.model_validate({"priority": 20, "target_type": "openclaw"})
    assert update.to_update_payload() == {"priority": 20, "target_type": "openclaw"}


@pytest.mark.asyncio
async def test_forward_rule_mutations_invalidate_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.forwarding import rules

    calls: list[str] = []
    monkeypatch.setattr(rules, "invalidate_forward_rules_cache", lambda: calls.append("invalidated"))

    class FakeSession:
        def __init__(self) -> None:
            self.added: list[Any] = []
            self.deleted: list[Any] = []

        def add(self, item: Any) -> None:
            self.added.append(item)

        async def flush(self) -> None:
            return None

        async def delete(self, item: Any) -> None:
            self.deleted.append(item)

    session = FakeSession()
    await rules.create_forward_rule(session, name="created", target_type="webhook")  # type: ignore[arg-type]
    assert calls == ["invalidated"]

    existing = SimpleNamespace(
        name="old",
        enabled=True,
        priority=1,
        match_event_type="",
        match_importance="",
        match_duplicate="all",
        match_source="",
        match_payload="",
        target_type="webhook",
        target_url="https://example.com/hook",
        target_name="",
        stop_on_match=False,
        updated_at=None,
    )

    async def fake_get_forward_rule(_session: Any, _rule_id: int) -> Any:
        return existing

    monkeypatch.setattr(rules, "get_forward_rule", fake_get_forward_rule)

    await rules.update_forward_rule(session, 1, {"name": "updated"})  # type: ignore[arg-type]
    assert existing.name == "updated"
    assert calls == ["invalidated", "invalidated"]

    assert await rules.delete_forward_rule(session, 1) is True  # type: ignore[arg-type]
    assert session.deleted == [existing]
    assert calls == ["invalidated", "invalidated", "invalidated"]
