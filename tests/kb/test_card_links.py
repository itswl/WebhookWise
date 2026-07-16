"""KB → alert-card runbook block: matching, degradation, and rendering."""

from __future__ import annotations

from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.webhook_payload import WebhookData
from models import KBDocument
from services.kb.card_links import find_kb_snippets_for_alert
from services.notifications.feishu_cards import build_feishu_card
from services.webhooks.types import AnalysisResult


def _doc(**over: object) -> KBDocument:
    base: dict[str, object] = {
        "title": "Incident resolution: volcengine incident — gpu-mem-high",
        "content": "Restart the ComfyUI worker on the affected node, then clear the model cache.",
        "content_hash": f"hash-{over.get('title', 'default')}"[:64],
        "chunk_index": 0,
        "status": "published",
        "tags": {"kind": "incident_resolution", "source": "volcengine"},
    }
    base.update(over)
    return KBDocument(**base)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_matches_published_doc_by_rule_tokens(
    db_app_context_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_app_context_session_factory.begin() as session:
        session.add(_doc())
        session.add(
            _doc(
                title="Postgres connection pool exhaustion",
                content_hash="hash-unrelated",
                tags={"source": "prometheus"},
            )
        )

    links = await find_kb_snippets_for_alert(
        source="volcengine", rule_name="gpu-mem-high", summary="GPU memory above 95%", limit=2
    )
    assert len(links) == 1
    assert "gpu-mem-high" in links[0]["title"]
    assert links[0]["snippet"].startswith("Restart the ComfyUI worker")


@pytest.mark.asyncio
async def test_ignores_drafts_and_weak_matches(
    db_app_context_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_app_context_session_factory.begin() as session:
        session.add(_doc(status="draft", content_hash="hash-draft"))
        # Shares only one generic token ("volcengine" via tags) — below threshold
        # unless the doc source also matches, which contributes the bonus; use a
        # doc with a different source and no shared tokens.
        session.add(
            _doc(
                title="Disk usage cleanup playbook",
                content_hash="hash-disk",
                tags={"source": "zabbix"},
            )
        )

    links = await find_kb_snippets_for_alert(
        source="volcengine", rule_name="gpu-mem-high", summary="GPU memory above 95%", limit=2
    )
    assert links == []


@pytest.mark.asyncio
async def test_empty_alert_tokens_short_circuit() -> None:
    assert await find_kb_snippets_for_alert(source="", rule_name="", summary="", limit=2) == []


def _card_inputs() -> tuple[WebhookData, AnalysisResult]:
    webhook_data = cast(
        WebhookData,
        {"source": "volcengine", "timestamp": "2026-07-16T08:00:00Z", "parsed_data": {"RuleName": "gpu-mem-high"}},
    )
    analysis = cast(AnalysisResult, {"importance": "high", "summary": "GPU 显存超过 95%", "event_type": "alert"})
    return webhook_data, analysis


def _card_markdown(card: dict[str, Any]) -> str:
    import json

    return json.dumps(card, ensure_ascii=False)


def test_card_renders_kb_block_when_links_present() -> None:
    webhook_data, analysis = _card_inputs()
    card = build_feishu_card(
        webhook_data,
        analysis,
        kb_links=[{"title": "GPU OOM runbook", "snippet": "Restart the worker."}],
    )
    text = _card_markdown(dict(card))
    assert "相关知识库" in text
    assert "GPU OOM runbook" in text


def test_card_omits_kb_block_by_default() -> None:
    webhook_data, analysis = _card_inputs()
    card = build_feishu_card(webhook_data, analysis)
    assert "相关知识库" not in _card_markdown(dict(card))


@pytest.mark.asyncio
async def test_channel_skips_kb_for_preformatted_payloads() -> None:
    from models import ForwardOutbox
    from services.forwarding.channels import _kb_links_for_alert_card

    record = ForwardOutbox(
        idempotency_key="k1",
        target_type="feishu",
        formatted_payload={"msg_type": "interactive", "card": {}},
    )
    assert await _kb_links_for_alert_card(record) is None
