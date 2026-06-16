from __future__ import annotations

from pydantic import BaseModel

from .base import APIResponse


class TokenStats(BaseModel):
    total: int
    input: int
    output: int


class CostStats(BaseModel):
    total: float
    saved_estimate: float


class CacheStatistics(BaseModel):
    total_cache_entries: int
    total_hits: int
    avg_hits_per_entry: float
    cache_hit_rate: float
    saved_calls: int


class TrendPoint(BaseModel):
    time: str
    total_calls: int
    ai_calls: int
    rule_calls: int
    tokens: int
    cost: float


class AIUsageData(BaseModel):
    """AI usage statistics data"""

    total_calls: int
    route_breakdown: dict[str, int]
    percentages: dict[str, float]
    tokens: TokenStats
    cost: CostStats
    cache_statistics: CacheStatistics
    trend: list[TrendPoint]


class AIUsageResponse(APIResponse[AIUsageData]):
    """AI usage statistics response"""
