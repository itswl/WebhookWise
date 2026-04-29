"""OpenClaw 深度分析引擎。"""

from typing import Any

from core.config import Config
from core.logger import logger


class OpenClawAnalysisEngine:
    """基于 OpenClaw Agent 的深度分析引擎。"""

    name = "openclaw"

    async def analyze(
        self,
        alert_data: dict[str, Any],
        *,
        source: str = "unknown",
        headers: dict[str, Any] | None = None,
        user_question: str = "",
    ) -> dict[str, Any]:
        """调用现有的 analyze_with_openclaw 执行深度分析。

        当 OpenClaw 返回降级标记时，自动回退到本地 AI 分析。
        """
        from services.ai_analyzer import analyze_webhook_with_ai
        from services.forward import analyze_with_openclaw

        webhook_data: dict[str, Any] = {
            "source": source,
            "headers": headers or {},
            "parsed_data": alert_data,
        }

        result = await analyze_with_openclaw(webhook_data, user_question)

        # OpenClaw 调用失败降级到本地 AI
        if result.get("_degraded"):
            logger.warning(f"[OpenClawEngine] OpenClaw 降级，回退本地 AI: {result.get('_degraded_reason')}")
            result = await analyze_webhook_with_ai(webhook_data)

        return result

    def is_available(self) -> bool:
        return Config.openclaw.OPENCLAW_ENABLED
