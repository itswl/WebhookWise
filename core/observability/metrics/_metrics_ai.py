"""AI analysis, cache, cost, token, and deep-analysis metrics."""

from __future__ import annotations

from core.observability.metrics.base import Counter, Histogram

AI_TOKENS_TOTAL = Counter(
    "ai.tokens",
    "Total number of tokens consumed by AI analysis",
    ("ai.model", "ai.token_type"),
)
AI_COST_USD_TOTAL = Counter("ai.cost", "Total estimated cost of AI analysis in USD", ("ai.model",), unit="USD")
AI_CACHE_REQUESTS_TOTAL = Counter(
    "ai.cache.requests",
    "AI analysis cache request count",
    ("ai.cache.operation", "ai.cache.result"),
)
AI_CACHE_OPERATION_DURATION_SECONDS = Histogram(
    "ai.cache.operation.duration",
    "AI analysis cache operation duration",
    ("ai.cache.operation", "ai.cache.result"),
    unit="s",
)
AI_DEGRADATIONS_TOTAL = Counter(
    "ai.degradations",
    "AI analysis degradation count",
    ("ai.degradation.reason",),
)
AI_ANALYSIS_DURATION_SECONDS = Histogram(
    "ai.request.duration",
    "Time spent on AI analysis",
    ("webhook.source", "ai.engine"),
    unit="s",
)
OPENAI_ERRORS_TOTAL = Counter("ai.request.errors", "AI provider call errors total", ("error.type",))
DEEP_ANALYSIS_TOTAL = Counter("ai.deep_analysis", "Deep analysis task result count", ("webhook.status", "ai.engine"))
