"""本地 AI 深度分析引擎。"""

from typing import Any

from core.config import policies
from core.logger import logger


class LocalAnalysisEngine:
    """基于本地 AI（OpenAI 兼容 API）的深度分析引擎。"""

    name = "local"

    async def analyze(
        self,
        alert_data: dict[str, Any],
        *,
        source: str = "unknown",
        headers: dict[str, Any] | None = None,
        user_question: str = "",
    ) -> dict[str, Any]:
        """调用现有的 analyze_webhook_with_ai 执行本地 AI 分析。"""
        from services.ai_analyzer import analyze_webhook_with_ai

        webhook_data: dict[str, Any] = {
            "source": source,
            "headers": headers or {},
            "parsed_data": alert_data,
        }
        result = await analyze_webhook_with_ai(webhook_data)
        logger.info(f"[LocalEngine] 本地 AI 分析完成: source={source}")
        return result

    def is_available(self) -> bool:
        return policies.ai.ENABLE_AI_ANALYSIS
