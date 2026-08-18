"""Corrections generalize to sibling instances of a rule — scoped, unanimous, recorded.

The override docstring rejected feeding corrections into the prompt for two
reasons: it would move judgements on alerts nobody corrected, and nobody could
trace it back. These tests are those two objections, as assertions.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.datetime_utils import utcnow
from services.analysis.correction_prior import (
    CorrectionPrior,
    CorrectionPriorPolicy,
    build_correction_prior,
)
from services.analysis.importance_overrides import remember_override

ON = CorrectionPriorPolicy(enabled=True, min_corrections=2, lookback_days=90)

# The payload every test scores: one instance of the "PaymentGateway5xx" rule.
PAYLOAD: dict[str, Any] = {"RuleName": "PaymentGateway5xx", "Level": "warning", "message": "5xx rate 3%"}


async def _correct(
    factory: async_sessionmaker[AsyncSession],
    *,
    alert_hash: str,
    importance: str = "high",
    alert_name: str = "PaymentGateway5xx",
    source: str = "volcengine",
    hit_count: int = 0,
    age_days: int = 0,
) -> None:
    """Record one operator correction, as the dashboard would."""
    from sqlalchemy import update

    from models import ImportanceOverride

    async with factory() as session:
        await remember_override(
            session,
            alert_hash=alert_hash,
            importance=importance,
            source=source,
            alert_name=alert_name,
            actor="alice",
        )
        if hit_count or age_days:
            stamp = utcnow() - timedelta(days=age_days)
            await session.execute(
                update(ImportanceOverride)
                .where(ImportanceOverride.alert_hash == alert_hash)
                .values(hit_count=hit_count, created_at=stamp, updated_at=stamp)
            )
        await session.commit()


async def _prior(policy: CorrectionPriorPolicy = ON, payload: dict[str, Any] | None = None) -> CorrectionPrior | None:
    return await build_correction_prior("volcengine", payload if payload is not None else PAYLOAD, policy=policy)


# ── Scope: a correction must not reach an alert it was not about ──────────────


class TestScope:
    async def test_two_agreeing_corrections_on_the_same_rule_produce_a_prior(
        self, db_app_context_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _correct(db_app_context_session_factory, alert_hash="sibling-1", hit_count=4)
        await _correct(db_app_context_session_factory, alert_hash="sibling-2", hit_count=1)

        prior = await _prior()

        assert prior is not None
        assert (prior.alert_name, prior.importance, prior.corrections) == ("PaymentGateway5xx", "high", 2)
        assert prior.total_hits == 5

    async def test_a_correction_on_another_rule_is_invisible(
        self, db_app_context_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        for index in range(3):
            await _correct(db_app_context_session_factory, alert_hash=f"other-{index}", alert_name="DiskUsageHigh")

        assert await _prior() is None

    async def test_a_correction_from_another_source_is_invisible(
        self, db_app_context_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        for index in range(3):
            await _correct(db_app_context_session_factory, alert_hash=f"other-{index}", source="grafana")

        assert await _prior() is None

    async def test_the_alerts_own_correction_is_excluded(
        self, db_app_context_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The exact condition is the hard override's job; the prior is about siblings."""
        from services.dedup import generate_alert_hash

        own_hash = generate_alert_hash(PAYLOAD, "volcengine")
        await _correct(db_app_context_session_factory, alert_hash=own_hash)
        await _correct(db_app_context_session_factory, alert_hash="sibling-1")

        # One sibling remains, which is below the floor of two.
        assert await _prior() is None

    async def test_a_payload_without_a_rule_name_gets_no_prior(
        self, db_app_context_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Without a rule there is no scope, and a source-wide prior is the drift
        the original rejection was about."""
        await _correct(db_app_context_session_factory, alert_hash="sibling-1")
        await _correct(db_app_context_session_factory, alert_hash="sibling-2")

        assert await _prior(payload={"message": "no rule name here"}) is None


# ── Unanimity and floors ─────────────────────────────────────────────────────


class TestWhenToSayNothing:
    async def test_disagreeing_corrections_produce_no_prior(
        self, db_app_context_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Averaging a disagreement would state something no person ever said."""
        await _correct(db_app_context_session_factory, alert_hash="sibling-1", importance="high")
        await _correct(db_app_context_session_factory, alert_hash="sibling-2", importance="high")
        await _correct(db_app_context_session_factory, alert_hash="sibling-3", importance="low")

        assert await _prior() is None

    async def test_one_correction_is_not_a_pattern(
        self, db_app_context_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _correct(db_app_context_session_factory, alert_hash="sibling-1")

        assert await _prior() is None

    async def test_the_floor_is_configurable_down_to_one(
        self, db_app_context_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _correct(db_app_context_session_factory, alert_hash="sibling-1")

        prior = await _prior(CorrectionPriorPolicy(enabled=True, min_corrections=1, lookback_days=90))

        assert prior is not None and prior.corrections == 1

    async def test_corrections_older_than_the_lookback_stop_counting(
        self, db_app_context_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _correct(db_app_context_session_factory, alert_hash="sibling-1", age_days=400)
        await _correct(db_app_context_session_factory, alert_hash="sibling-2", age_days=400)
        await _correct(db_app_context_session_factory, alert_hash="sibling-3")

        assert await _prior() is None

    async def test_nothing_corrected_means_no_prior(
        self, db_app_context_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        assert await _prior() is None

    async def test_disabled_asks_the_database_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Off by default has to mean free, not merely quiet."""
        import db.session

        def explode(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("a disabled prior must not open a session")

        monkeypatch.setattr(db.session, "session_scope", explode)

        assert await _prior(CorrectionPriorPolicy(enabled=False, min_corrections=1, lookback_days=90)) is None

    async def test_a_lookup_failure_degrades_to_no_prior(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A prior is an improvement, not a dependency: analysis must still run."""
        import db.session

        def explode(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("database is down")

        monkeypatch.setattr(db.session, "session_scope", explode)

        assert await _prior() is None


# ── Traceability ──────────────────────────────────────────────────────────────


class TestPromptBlock:
    def _prior(self, **overrides: Any) -> CorrectionPrior:
        fields: dict[str, Any] = {
            "alert_name": "PaymentGateway5xx",
            "source": "volcengine",
            "importance": "high",
            "corrections": 5,
            "total_hits": 12,
            "since": "2026-06-01",
        }
        fields.update(overrides)
        return CorrectionPrior(**fields)

    def test_the_block_names_the_rule_the_verdict_and_the_evidence(self) -> None:
        block = self._prior().prompt_block()

        assert "PaymentGateway5xx" in block
        assert "volcengine" in block
        assert "`high`" in block
        assert "5 个实例" in block
        assert "12 次" in block
        assert "2026-06-01" in block

    def test_the_block_permits_disagreement(self) -> None:
        """An instruction would make the model agree with every prior, and the
        record of whether the prior helped would be worthless."""
        block = self._prior().prompt_block()

        assert "仍按数据判断" in block
        assert "说明为何与历史修正不同" in block

    def test_the_importance_stays_a_schema_literal(self) -> None:
        """The model has to echo the enum, so this value is never translated."""
        for importance in ("high", "medium", "low"):
            assert f"`{importance}`" in self._prior(importance=importance).prompt_block()

    def test_missing_evidence_is_left_out_rather_than_faked(self) -> None:
        block = self._prior(total_hits=0, since=None).prompt_block()

        assert "累计生效" not in block
        assert "最早于" not in block

    def test_metadata_records_whether_the_model_took_it(self) -> None:
        assert self._prior().to_metadata(followed=True)["followed"] is True
        assert self._prior().to_metadata(followed=False)["followed"] is False

    def test_metadata_carries_the_whole_claim(self) -> None:
        metadata = self._prior().to_metadata(followed=True)

        assert metadata == {
            "alert_name": "PaymentGateway5xx",
            "source": "volcengine",
            "importance": "high",
            "corrections": 5,
            "total_hits": 12,
            "since": "2026-06-01",
            "followed": True,
        }


# ── Wiring into the analysis path ─────────────────────────────────────────────


class TestPromptAndResultWiring:
    async def _run(
        self, monkeypatch: pytest.MonkeyPatch, *, prior: CorrectionPrior | None, importance: str
    ) -> tuple[str, dict[str, Any]]:
        from types import SimpleNamespace

        from services.analysis import ai_llm_client

        prompts: list[str] = []

        async def template(*_a: object, **_k: object) -> str:
            return "src={source}\nDATA:{data_json}\n{kb_context}{correction_prior}"

        async def no_kb(*_a: object, **_k: object) -> str:
            return ""

        async def build_prior(*_a: object, **_k: object) -> CorrectionPrior | None:
            return prior

        async def invoke(user_prompt: str, *_a: object, **_k: object) -> tuple[dict[str, Any], int, int]:
            prompts.append(user_prompt)
            return {"importance": importance, "summary": "ok"}, 1, 2

        monkeypatch.setattr(ai_llm_client, "load_user_prompt_template", template)
        monkeypatch.setattr(ai_llm_client, "_retrieve_kb_context", no_kb)
        monkeypatch.setattr(ai_llm_client, "get_prompt_source", lambda: "test")
        monkeypatch.setattr(ai_llm_client, "build_correction_prior", build_prior)
        monkeypatch.setattr(ai_llm_client, "_invoke_ai_with_retry", invoke)
        monkeypatch.setattr(
            ai_llm_client.AIProviderPolicy,
            "from_config",
            classmethod(lambda _cls: SimpleNamespace(model="test-model")),
        )

        result, _, _ = await ai_llm_client._call_ai_with_retry(PAYLOAD, "volcengine")
        return prompts[0], dict(result)

    async def test_the_prior_reaches_the_prompt_and_the_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        prior = CorrectionPrior(
            alert_name="PaymentGateway5xx",
            source="volcengine",
            importance="high",
            corrections=3,
            total_hits=7,
            since="2026-06-01",
        )

        prompt, result = await self._run(monkeypatch, prior=prior, importance="high")

        assert "运维历史修正" in prompt
        assert result["_correction_prior"]["corrections"] == 3
        assert result["_correction_prior"]["followed"] is True

    async def test_a_disagreeing_verdict_is_recorded_as_not_followed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The number that decides whether this feature earns its tokens."""
        prior = CorrectionPrior(
            alert_name="PaymentGateway5xx",
            source="volcengine",
            importance="high",
            corrections=3,
            total_hits=0,
            since=None,
        )

        _, result = await self._run(monkeypatch, prior=prior, importance="low")

        assert result["_correction_prior"]["followed"] is False

    async def test_no_prior_leaves_the_prompt_and_the_result_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        prompt, result = await self._run(monkeypatch, prior=None, importance="medium")

        assert "运维历史修正" not in prompt
        assert "_correction_prior" not in result

    async def test_a_prior_is_never_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A cache hit must not claim a prior computed for an earlier call."""
        import core.redis_client
        from services.analysis import ai_cache

        written: list[bytes] = []

        async def redis_eval_int(_script: str, _numkeys: int, *args: object) -> int:
            written.append(bytes(args[-1]))  # type: ignore[arg-type]
            return 1

        monkeypatch.setattr(core.redis_client, "redis_eval_int", redis_eval_int)
        saved = await ai_cache.save_to_cache(
            "hash-1",
            {"importance": "high", "summary": "ok", "_correction_prior": {"followed": True}},
            enabled=True,
            ttl_seconds=60,
        )

        assert saved and written, "nothing was written to the cache"
        assert b"_correction_prior" not in written[0]
        assert b"summary" in written[0]
