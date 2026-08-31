"""Reversible masking: the model sees stable tokens, the operator sees reality."""

from unittest.mock import AsyncMock

import pytest

from services.analysis.pseudonymizer import (
    PseudonymPolicy,
    PseudonymSession,
    build_pseudonym_session,
    unmask_obj_with_map,
    unmask_text_with_map,
)


def _policy(**overrides) -> PseudonymPolicy:
    defaults = {"enabled": True, "mask_ips": True, "host_suffixes": (), "terms": ()}
    defaults.update(overrides)
    return PseudonymPolicy(**defaults)


# ── masking ───────────────────────────────────────────────────────────────────


def test_ips_mask_stably_and_round_trip() -> None:
    session = PseudonymSession(_policy())
    text = "db 10.20.30.40 timed out; retried 10.20.30.40 then failed over to 10.20.30.41"

    masked = session.mask_text(text)

    assert "10.20.30.40" not in masked and "10.20.30.41" not in masked
    assert masked.count("anon-ip-1") == 2  # same address, same token — referential integrity
    assert "anon-ip-2" in masked
    assert session.unmask_text(masked) == text


def test_loopback_and_zero_addresses_pass_through() -> None:
    session = PseudonymSession(_policy())
    text = "listening on 0.0.0.0, probe from 127.0.0.1"
    assert session.mask_text(text) == text


def test_host_suffix_masks_hosts_and_bare_domain() -> None:
    session = PseudonymSession(_policy(mask_ips=False, host_suffixes=("internal.example",)))
    text = "gw-3.pay.internal.example unreachable from internal.example edge"

    masked = session.mask_text(text)

    assert "internal.example" not in masked
    assert "anon-host-1" in masked and "anon-host-2" in masked
    assert session.unmask_text(masked) == text


def test_terms_mask_verbatim_including_cjk() -> None:
    session = PseudonymSession(_policy(mask_ips=False, terms=("清算核心集群", "AtlasPrime")))
    text = "清算核心集群 latency spike; AtlasPrime failover engaged (atlasprime untouched)"

    masked = session.mask_text(text)

    assert "清算核心集群" not in masked and "AtlasPrime" not in masked
    assert "atlasprime untouched" in masked  # terms are case-sensitive estate names
    assert session.unmask_text(masked) == text


def test_longer_term_wins_over_its_substring() -> None:
    policy = PseudonymPolicy(
        enabled=True, mask_ips=False, host_suffixes=(),
        terms=tuple(sorted({"pay-core", "pay-core-shanghai"}, key=len, reverse=True)),
    )
    session = PseudonymSession(policy)

    masked = session.mask_text("pay-core-shanghai and pay-core degraded")

    assert masked == "anon-term-1 and anon-term-2 degraded"
    assert session.unmask_text(masked) == "pay-core-shanghai and pay-core degraded"


# ── unmasking shapes ──────────────────────────────────────────────────────────


def test_unmask_obj_walks_nested_structures() -> None:
    mapping = {"anon-ip-1": "10.0.0.9", "anon-term-1": "AtlasPrime"}
    obj = {
        "summary": "anon-term-1 lost anon-ip-1",
        "actions": ["restart anon-term-1", {"target": "anon-ip-1"}],
        "count": 3,
    }
    assert unmask_obj_with_map(obj, mapping) == {
        "summary": "AtlasPrime lost 10.0.0.9",
        "actions": ["restart AtlasPrime", {"target": "10.0.0.9"}],
        "count": 3,
    }


def test_unknown_tokens_and_empty_maps_are_left_alone() -> None:
    assert unmask_text_with_map("anon-ip-42 stays", {"anon-ip-1": "x"}) == "anon-ip-42 stays"
    assert unmask_text_with_map("anon-ip-1 stays", None) == "anon-ip-1 stays"
    assert unmask_obj_with_map({"a": "anon-ip-1"}, {}) == {"a": "anon-ip-1"}


# ── policy gate ───────────────────────────────────────────────────────────────


def test_disabled_or_empty_policy_builds_no_session(monkeypatch, temp_config) -> None:
    monkeypatch.setattr(temp_config.ai, "AI_PSEUDONYMIZE_ENABLED", False, raising=False)
    assert build_pseudonym_session() is None

    monkeypatch.setattr(temp_config.ai, "AI_PSEUDONYMIZE_ENABLED", True, raising=False)
    monkeypatch.setattr(temp_config.ai, "AI_PSEUDONYMIZE_IPS", False, raising=False)
    monkeypatch.setattr(temp_config.ai, "AI_PSEUDONYMIZE_HOST_SUFFIXES", "", raising=False)
    monkeypatch.setattr(temp_config.ai, "AI_PSEUDONYMIZE_TERMS", "", raising=False)
    assert build_pseudonym_session() is None  # on, but with nothing to mask


def test_policy_from_config_normalizes_suffixes_and_orders_terms(monkeypatch, temp_config) -> None:
    monkeypatch.setattr(temp_config.ai, "AI_PSEUDONYMIZE_ENABLED", True, raising=False)
    monkeypatch.setattr(temp_config.ai, "AI_PSEUDONYMIZE_HOST_SUFFIXES", ".Corp.Example, internal.example", raising=False)
    monkeypatch.setattr(temp_config.ai, "AI_PSEUDONYMIZE_TERMS", "pay, pay-core-shanghai, pay-core", raising=False)

    policy = PseudonymPolicy.from_config()

    assert policy.host_suffixes == ("corp.example", "internal.example")
    assert policy.terms == ("pay-core-shanghai", "pay-core", "pay")


# ── sync AI path integration ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prompt_is_masked_and_result_unmasked_end_to_end(monkeypatch, temp_config) -> None:
    from services.analysis import ai_llm_client

    monkeypatch.setattr(temp_config.ai, "AI_PSEUDONYMIZE_ENABLED", True, raising=False)
    monkeypatch.setattr(temp_config.ai, "AI_PSEUDONYMIZE_TERMS", "AtlasPrime", raising=False)
    monkeypatch.setattr(ai_llm_client, "build_correction_prior", AsyncMock(return_value=None))
    monkeypatch.setattr(
        ai_llm_client,
        "_build_user_prompt",
        AsyncMock(return_value="alert from AtlasPrime at 10.9.8.7"),
    )

    seen: dict[str, str] = {}

    async def fake_invoke(user_prompt, source, *, policy=None, http_client=None):
        seen["prompt"] = user_prompt
        return {"importance": "high", "summary": f"{user_prompt.split()[2]} degraded"}, 10, 5

    monkeypatch.setattr(ai_llm_client, "_invoke_ai_with_retry", fake_invoke)

    result, _, _ = await ai_llm_client._call_ai_with_retry({"msg": "x"}, "grafana")

    assert "AtlasPrime" not in seen["prompt"] and "10.9.8.7" not in seen["prompt"]
    assert "anon-term-1" in seen["prompt"] and "anon-ip-1" in seen["prompt"]
    # The provider answered in tokens; the caller reads reality.
    assert result["summary"] == "AtlasPrime degraded"


@pytest.mark.asyncio
async def test_masking_off_leaves_the_path_byte_identical(monkeypatch, temp_config) -> None:
    from services.analysis import ai_llm_client

    monkeypatch.setattr(temp_config.ai, "AI_PSEUDONYMIZE_ENABLED", False, raising=False)
    monkeypatch.setattr(ai_llm_client, "build_correction_prior", AsyncMock(return_value=None))
    monkeypatch.setattr(
        ai_llm_client, "_build_user_prompt", AsyncMock(return_value="alert from AtlasPrime")
    )
    invoke = AsyncMock(return_value=({"importance": "low", "summary": "s"}, 1, 1))
    monkeypatch.setattr(ai_llm_client, "_invoke_ai_with_retry", invoke)

    await ai_llm_client._call_ai_with_retry({"msg": "x"}, "grafana")

    assert invoke.await_args.args[0] == "alert from AtlasPrime"


# ── gateway round-trip unmasking ──────────────────────────────────────────────


def test_completed_poll_unmasks_report_and_clears_the_map() -> None:
    from services.analysis.deep_analysis_poll import _completed_update

    rec = {
        "id": 7,
        "gateway_run_id": "run-1",
        "engine": "gateway",
        "pseudonym_map": {"anon-term-1": "AtlasPrime", "anon-ip-1": "10.9.8.7"},
    }

    update = _completed_update(rec, "root cause: anon-term-1 lost anon-ip-1", None)

    result_text = str(update["analysis_result"])
    assert "AtlasPrime" in result_text and "10.9.8.7" in result_text
    assert "anon-term-1" not in result_text
    assert update["pseudonym_map"] is None  # consumed exactly once


def test_completed_poll_without_map_is_unchanged() -> None:
    from services.analysis.deep_analysis_poll import _completed_update

    rec = {"id": 7, "gateway_run_id": "run-1", "engine": "gateway", "pseudonym_map": None}
    update = _completed_update(rec, "plain report", None)
    assert "plain report" in str(update["analysis_result"])
