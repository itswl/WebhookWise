"""What operators decided about OTHER instances of this same alert rule.

`importance_overrides` closes the loop on one condition: correct an alert and
every later firing of that exact `alert_hash` inherits the correction. What it
cannot do is generalize. A rule fires on a new host, a new partition, a new
region — a different hash, so a fresh judgement, even when operators have
already corrected five siblings of it the same way.

This states that history to the model as a prior, and both docstrings that
rejected the idea of feeding corrections into the prompt named the reasons it
had to answer first:

    Deliberately NOT few-shot in the prompt: with a handful of samples that
    teaches nothing, and with many it would quietly move judgements on alerts
    nobody corrected, in a way no one could trace back.
        -- models/operations.py, ImportanceOverride

    Applied after analysis rather than fed into the prompt, so the decision is
    traceable to a person and scoped to the one condition they decided about.
        -- services/webhooks/pipeline_stages.py, _run_fresh_analysis

So this is not few-shot, and the difference is the point:

- It is an aggregate, not a sample. "Operators corrected 5 other instances of
  this rule to high" is a fact a model can use; five near-identical rule names
  are not.
- It is scoped by rule name and source, and it requires unanimity. A rule whose
  corrections disagree produces no prior at all — a split verdict must never be
  presented as consensus, and a correction can never reach an alert of a
  different rule.
- It is traceable. The prior travels on the analysis result and is persisted
  with it, including whether the model then followed it, so "which corrections
  steered this verdict" is a question with an answer.
- It is a prior, not a decision. The model may disagree with the payload in
  front of it, and the exact-hash override still outranks the whole thing
  afterwards in the pipeline.

Off by default. `MIN_CORRECTIONS` is what answers "a handful teaches nothing":
below the floor, the prompt says nothing at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select

from core.datetime_utils import utcnow
from core.logger import get_logger
from models import ImportanceOverride

logger = get_logger("analysis.correction_prior")

VALID_IMPORTANCE = ("high", "medium", "low")


@dataclass(frozen=True, slots=True)
class CorrectionPriorPolicy:
    enabled: bool
    min_corrections: int
    lookback_days: int

    @classmethod
    def from_config(cls) -> CorrectionPriorPolicy:
        from core.app_context import get_config_manager
        from services.operations import runtime_settings as rt

        cfg = get_config_manager().ai
        return cls(
            enabled=rt.override_or(
                "AI_CORRECTION_PRIOR_ENABLED", bool(getattr(cfg, "AI_CORRECTION_PRIOR_ENABLED", False))
            ),
            min_corrections=max(
                1,
                rt.override_or(
                    "AI_CORRECTION_PRIOR_MIN_CORRECTIONS", int(getattr(cfg, "AI_CORRECTION_PRIOR_MIN_CORRECTIONS", 2))
                ),
            ),
            lookback_days=max(
                1,
                rt.override_or(
                    "AI_CORRECTION_PRIOR_LOOKBACK_DAYS", int(getattr(cfg, "AI_CORRECTION_PRIOR_LOOKBACK_DAYS", 90))
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class CorrectionPrior:
    """A unanimous operator verdict on other instances of one alert rule."""

    alert_name: str
    source: str
    importance: str
    corrections: int
    total_hits: int
    since: str | None

    def prompt_block(self) -> str:
        """The Chinese prompt block. Steers the model's judgement, not display copy.

        Phrased as evidence plus permission to disagree. An instruction ("judge
        this high") would make the model's output unfalsifiable — it would agree
        with every prior, and the record of whether the prior helped would be
        worthless.
        """
        hits = f"，累计生效 {self.total_hits} 次" if self.total_hits else ""
        since = f"，最早于 {self.since}" if self.since else ""
        return (
            "\n**运维历史修正（同一告警规则的其他实例，仅供参考）**:\n"
            f"运维人员曾把来自 `{self.source}` 的告警规则 `{self.alert_name}` 的另外 "
            f"{self.corrections} 个实例的重要性一致修正为 `{self.importance}`{hits}{since}。\n"
            "若本次数据与那些实例情况相符，倾向于给出同样的 importance；"
            "若本次数据明显不同（例如已恢复、影响面更小、只是通知），仍按数据判断，"
            "并在 detailed_analysis 中说明为何与历史修正不同。\n"
        )

    def to_metadata(self, *, followed: bool) -> dict[str, Any]:
        """What gets persisted with the analysis, so the prior is auditable."""
        return {
            "alert_name": self.alert_name,
            "source": self.source,
            "importance": self.importance,
            "corrections": self.corrections,
            "total_hits": self.total_hits,
            "since": self.since,
            # The only question worth asking of a prior afterwards: did the
            # model take it? A prior nothing follows is noise in the prompt; a
            # prior everything follows is a hard override wearing a costume.
            "followed": followed,
        }


async def build_correction_prior(
    source: str,
    parsed_data: dict[str, Any],
    *,
    policy: CorrectionPriorPolicy | None = None,
) -> CorrectionPrior | None:
    """The prior for this alert, or None when there is nothing defensible to say.

    Best-effort by construction: any failure returns None and the analysis runs
    exactly as it does today. Opens no session while disabled.
    """
    policy = policy or CorrectionPriorPolicy.from_config()
    if not policy.enabled:
        return None

    from services.webhooks.inbound_rules import alert_rule_name

    alert_name = alert_rule_name(parsed_data)
    if not alert_name:
        # No rule identity means no scope to generalize within, and a prior
        # scoped to "everything from this source" is exactly the untraceable
        # drift the original rejection was about.
        return None

    try:
        from db.session import session_scope
        from services.dedup import generate_alert_hash

        # Excluded so the prior is only ever about OTHER instances: the exact
        # condition, if it has been corrected, is handled by the hard override.
        own_hash = generate_alert_hash(parsed_data, source)
        cutoff = utcnow() - timedelta(days=policy.lookback_days)

        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(
                        ImportanceOverride.importance,
                        func.count().label("corrections"),
                        func.coalesce(func.sum(ImportanceOverride.hit_count), 0).label("total_hits"),
                        func.min(ImportanceOverride.created_at).label("since"),
                    )
                    .where(
                        ImportanceOverride.alert_name == alert_name[:200],
                        ImportanceOverride.source == source,
                        ImportanceOverride.alert_hash != own_hash,
                        ImportanceOverride.updated_at >= cutoff,
                    )
                    .group_by(ImportanceOverride.importance)
                )
            ).all()
    except Exception as error:  # noqa: BLE001 - a prior is an improvement, not a dependency
        logger.warning("[AI] Correction prior lookup failed for rule=%s: %s", alert_name[:60], error)
        return None

    verdicts = [row for row in rows if str(row.importance).lower() in VALID_IMPORTANCE]
    if len(verdicts) != 1:
        # Zero: nothing to say. More than one: operators disagreed about this
        # rule, and averaging a disagreement into a single verdict would state
        # something no person ever said.
        if len(verdicts) > 1:
            logger.debug(
                "[AI] Correction prior withheld, corrections disagree rule=%s verdicts=%s",
                alert_name[:60],
                [str(row.importance) for row in verdicts],
            )
        return None

    verdict = verdicts[0]
    corrections = int(verdict.corrections or 0)
    if corrections < policy.min_corrections:
        return None

    return CorrectionPrior(
        alert_name=alert_name,
        source=source,
        importance=str(verdict.importance).lower(),
        corrections=corrections,
        total_hits=int(verdict.total_hits or 0),
        since=verdict.since.date().isoformat() if verdict.since else None,
    )
