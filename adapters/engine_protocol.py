"""深度分析引擎协议定义。"""

from typing import Any, Protocol


class DeepAnalysisEngine(Protocol):
    """深度分析引擎协议。

    所有引擎实现必须符合此协议，并通过 registry 注册。
    """

    name: str

    async def analyze(
        self,
        alert_data: dict[str, Any],
        *,
        source: str = "unknown",
        headers: dict[str, Any] | None = None,
        user_question: str = "",
    ) -> dict[str, Any]:
        """执行深度分析。

        Args:
            alert_data: 解析后的告警数据。
            source: 告警来源。
            headers: 原始请求头。
            user_question: 用户补充问题。

        Returns:
            分析结果字典。
        """
        ...

    def is_available(self) -> bool:
        """引擎是否可用（配置已启用且依赖就绪）。"""
        ...
