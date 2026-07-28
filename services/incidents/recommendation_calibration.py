"""Conservative recommendation-score calibration from existing operator outcomes."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Incident, IncidentIntelligenceFeedback, RunbookExecution

_RECOMMENDATION_TYPES = ("similar_incident", "change", "runbook")
_POSITIVE_VERDICTS = frozenset({"relevant", "used"})
_NEGATIVE_VERDICTS = frozenset({"irrelevant", "not_used"})
_MIN_SAMPLE_SIZE = 5
_PRIOR_SUCCESS = 8.0
_PRIOR_FAILURE = 8.0
_MAX_ABS_ADJUSTMENT = 0.10
_MAX_FEEDBACK_ROWS = 1_000
_MAX_EXECUTION_ROWS = 1_000


def _normalize(value: object, *, limit: int = 200) -> str:
    return str(value or "").strip().lower()[:limit]


def _dimensions(value: object) -> dict[str, str]:
    raw = value if isinstance(value, dict) else {}
    return {str(key): normalized for key, item in raw.items() if (normalized := _normalize(item))}


def _matches_scope(value: object, *, service: str, environment: str) -> bool:
    dimensions = _dimensions(value)
    if not service or dimensions.get("service") != service:
        return False
    return not environment or dimensions.get("environment") == environment


@dataclass(frozen=True, slots=True)
class RecommendationCalibration:
    """One service/environment/type posterior with a hard adjustment bound."""

    recommendation_type: str
    service: str
    environment: str
    positive_samples: int = 0
    negative_samples: int = 0
    feedback_samples: int = 0
    execution_samples: int = 0

    @property
    def sample_size(self) -> int:
        return self.positive_samples + self.negative_samples

    @property
    def posterior_success_rate(self) -> float:
        denominator = self.sample_size + _PRIOR_SUCCESS + _PRIOR_FAILURE
        return (self.positive_samples + _PRIOR_SUCCESS) / denominator

    @property
    def adjustment(self) -> float:
        if self.sample_size < _MIN_SAMPLE_SIZE:
            return 0.0
        centered_posterior = (self.posterior_success_rate - 0.5) * 2.0
        return max(
            -_MAX_ABS_ADJUSTMENT,
            min(_MAX_ABS_ADJUSTMENT, centered_posterior * _MAX_ABS_ADJUSTMENT),
        )

    def apply(self, raw_score: float) -> float:
        return max(0.0, min(1.0, raw_score + self.adjustment))

    def as_dict(self) -> dict[str, object]:
        if not self.service:
            reason = "missing_service_scope"
        elif self.sample_size < _MIN_SAMPLE_SIZE:
            reason = "insufficient_sample"
        else:
            reason = "bounded_bayesian_adjustment"
        return {
            "strategy": "bounded_beta_shrinkage_v1",
            "scope": {
                "service": self.service or None,
                "environment": self.environment or None,
                "recommendation_type": self.recommendation_type,
            },
            "sample_size": self.sample_size,
            "positive_samples": self.positive_samples,
            "negative_samples": self.negative_samples,
            "feedback_samples": self.feedback_samples,
            "execution_samples": self.execution_samples,
            "minimum_sample_size": _MIN_SAMPLE_SIZE,
            "prior_strength": _PRIOR_SUCCESS + _PRIOR_FAILURE,
            "posterior_success_rate": round(self.posterior_success_rate, 4),
            "max_abs_adjustment": _MAX_ABS_ADJUSTMENT,
            "adjustment": round(self.adjustment, 4),
            "applied": bool(self.service and self.sample_size >= _MIN_SAMPLE_SIZE),
            "reason": reason,
        }


def _neutral_calibrations(service: str, environment: str) -> dict[str, RecommendationCalibration]:
    return {
        recommendation_type: RecommendationCalibration(
            recommendation_type=recommendation_type,
            service=service,
            environment=environment,
        )
        for recommendation_type in _RECOMMENDATION_TYPES
    }


async def get_recommendation_calibrations(
    session: AsyncSession,
    *,
    service: str,
    environment: str,
) -> dict[str, RecommendationCalibration]:
    """Build bounded calibration posteriors for one incident identity scope.

    A rated terminal runbook execution supersedes the synthetic ``used``
    feedback recorded when that same runbook was started. This avoids counting
    one operator action twice while still learning from unrated executions via
    their persisted feedback.
    """
    service = _normalize(service)
    environment = _normalize(environment)
    if not service:
        return _neutral_calibrations(service, environment)

    scope_filters = [
        Incident.correlation_dimensions["service"].as_string() == service,
    ]
    if environment:
        scope_filters.append(Incident.correlation_dimensions["environment"].as_string() == environment)

    feedback_rows = (
        await session.execute(
            select(
                IncidentIntelligenceFeedback.incident_id,
                IncidentIntelligenceFeedback.recommendation_type,
                IncidentIntelligenceFeedback.candidate_ref,
                IncidentIntelligenceFeedback.verdict,
                Incident.correlation_dimensions,
            )
            .join(Incident, Incident.id == IncidentIntelligenceFeedback.incident_id)
            .where(*scope_filters)
            .order_by(
                IncidentIntelligenceFeedback.updated_at.desc(),
                IncidentIntelligenceFeedback.id.desc(),
            )
            .limit(_MAX_FEEDBACK_ROWS)
        )
    ).all()
    execution_rows = (
        await session.execute(
            select(
                RunbookExecution.incident_id,
                RunbookExecution.candidate_ref,
                RunbookExecution.status,
                RunbookExecution.effectiveness,
                Incident.correlation_dimensions,
            )
            .join(Incident, Incident.id == RunbookExecution.incident_id)
            .where(*scope_filters)
            .order_by(RunbookExecution.updated_at.desc(), RunbookExecution.id.desc())
            .limit(_MAX_EXECUTION_ROWS)
        )
    ).all()

    positive = dict.fromkeys(_RECOMMENDATION_TYPES, 0)
    negative = dict.fromkeys(_RECOMMENDATION_TYPES, 0)
    feedback_samples = dict.fromkeys(_RECOMMENDATION_TYPES, 0)
    execution_samples = dict.fromkeys(_RECOMMENDATION_TYPES, 0)

    terminal_runbook_outcomes: dict[tuple[int, str], bool] = {}
    for execution_row in execution_rows:
        if not _matches_scope(
            execution_row.correlation_dimensions,
            service=service,
            environment=environment,
        ):
            continue
        status = _normalize(execution_row.status)
        effectiveness = _normalize(execution_row.effectiveness)
        outcome: bool | None = None
        if status == "completed" and effectiveness == "effective":
            outcome = True
        elif status in {"completed", "failed", "abandoned"} and (
            effectiveness == "ineffective" or status in {"failed", "abandoned"}
        ):
            outcome = False
        if outcome is not None:
            terminal_runbook_outcomes[(int(execution_row.incident_id), str(execution_row.candidate_ref))] = outcome

    for feedback_row in feedback_rows:
        recommendation_type = str(feedback_row.recommendation_type)
        if recommendation_type not in positive or not _matches_scope(
            feedback_row.correlation_dimensions,
            service=service,
            environment=environment,
        ):
            continue
        if (
            recommendation_type == "runbook"
            and (
                int(feedback_row.incident_id),
                str(feedback_row.candidate_ref),
            )
            in terminal_runbook_outcomes
        ):
            continue
        verdict = _normalize(feedback_row.verdict)
        if verdict in _POSITIVE_VERDICTS:
            positive[recommendation_type] += 1
        elif verdict in _NEGATIVE_VERDICTS:
            negative[recommendation_type] += 1
        else:
            continue
        feedback_samples[recommendation_type] += 1

    for outcome in terminal_runbook_outcomes.values():
        if outcome:
            positive["runbook"] += 1
        else:
            negative["runbook"] += 1
        execution_samples["runbook"] += 1

    return {
        recommendation_type: RecommendationCalibration(
            recommendation_type=recommendation_type,
            service=service,
            environment=environment,
            positive_samples=positive[recommendation_type],
            negative_samples=negative[recommendation_type],
            feedback_samples=feedback_samples[recommendation_type],
            execution_samples=execution_samples[recommendation_type],
        )
        for recommendation_type in _RECOMMENDATION_TYPES
    }
