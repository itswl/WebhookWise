from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NoiseScoringConfig:
    source_weight: float
    resource_weight: float
    semantic_weight: float
    severity_weight: float
    time_weight: float
    severity_downgrade_score: float
    related_min_confidence: float

    @classmethod
    def from_config(cls, config: Any) -> "NoiseScoringConfig":
        return cls(
            source_weight=float(config.NOISE_SOURCE_WEIGHT),
            resource_weight=float(config.NOISE_RESOURCE_WEIGHT),
            semantic_weight=float(config.NOISE_SEMANTIC_WEIGHT),
            severity_weight=float(config.NOISE_SEVERITY_WEIGHT),
            time_weight=float(config.NOISE_TIME_WEIGHT),
            severity_downgrade_score=float(config.NOISE_SEVERITY_DOWNGRADE_SCORE),
            related_min_confidence=float(config.NOISE_RELATED_MIN_CONFIDENCE),
        )

