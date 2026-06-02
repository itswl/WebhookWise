"""Analysis policies (AI + noise reduction) built from static configuration or explicit injection."""

from dataclasses import dataclass

from core.app_context import get_config_manager
from core.config.defaults import NoiseConfig

DEFAULT_USER_PROMPT_TEMPLATE = """请分析以下 webhook 事件：
**来源**: {source}
**数据内容**:
```yaml
{data_json}
```
请识别事件的类型、严重程度，并提供摘要、影响评估和处理建议。"""

DEFAULT_DEEP_ANALYSIS_PROMPT_TEMPLATE = (
    "你是 WebhookWise 的无人值守 SRE 深度分析 Agent。这个任务来自 webhook 自动触发，"
    "通常没有人会继续对话。必须基于当前告警字段、payload 和可用工具查询结果直接输出最终 JSON 报告。"
    "禁止向用户索要补充信息，禁止等待用户响应，禁止输出“用户未在 10 分钟内响应”。"
    "如果信息不足，在 unknowns、assumptions、next_checks 中说明缺口和后续验证步骤，仍然给出当前最佳判断。"
)


@dataclass(frozen=True, slots=True)
class RuleAnalysisPolicy:
    high_keywords: tuple[str, ...]
    warning_keywords: tuple[str, ...]
    metric_keywords: tuple[str, ...]
    threshold_multiplier: float

    @classmethod
    def from_config(cls) -> "RuleAnalysisPolicy":
        from core.text import split_csv_lower

        cfg = get_config_manager().ai
        return cls(
            high_keywords=tuple(split_csv_lower(cfg.RULE_HIGH_KEYWORDS)),
            warning_keywords=tuple(split_csv_lower(cfg.RULE_WARN_KEYWORDS)),
            metric_keywords=tuple(split_csv_lower(cfg.RULE_METRIC_KEYWORDS)),
            threshold_multiplier=float(cfg.RULE_THRESHOLD_MULTIPLIER or 4.0),
        )


@dataclass(frozen=True, slots=True)
class AIErrorNotificationPolicy:
    cooldown_seconds: int = 3600

    @classmethod
    def from_config(cls) -> "AIErrorNotificationPolicy":
        cfg = get_config_manager()
        return cls(
            cooldown_seconds=max(1, int(cfg.notifications.AI_ERROR_NOTIFICATION_COOLDOWN_SECONDS)),
        )


@dataclass(frozen=True, slots=True)
class AIProviderPolicy:
    enabled: bool
    api_key: str
    api_url: str
    model: str
    system_prompt: str
    temperature: float
    input_cost_per_1k_tokens: float
    output_cost_per_1k_tokens: float
    degradation_enabled: bool
    http_timeout_seconds: float
    http_connect_timeout_seconds: float

    @classmethod
    def from_config(cls) -> "AIProviderPolicy":
        cfg = get_config_manager().ai
        timeout_seconds = max(1.0, float(cfg.AI_HTTP_TIMEOUT_SECONDS))
        return cls(
            enabled=bool(cfg.ENABLE_AI_ANALYSIS),
            api_key=str(cfg.OPENAI_API_KEY),
            api_url=str(cfg.OPENAI_API_URL),
            model=str(cfg.OPENAI_MODEL),
            system_prompt=str(cfg.AI_SYSTEM_PROMPT),
            temperature=float(cfg.OPENAI_TEMPERATURE),
            input_cost_per_1k_tokens=float(cfg.AI_COST_PER_1K_INPUT_TOKENS),
            output_cost_per_1k_tokens=float(cfg.AI_COST_PER_1K_OUTPUT_TOKENS),
            degradation_enabled=bool(cfg.ENABLE_AI_DEGRADATION),
            http_timeout_seconds=timeout_seconds,
            http_connect_timeout_seconds=max(1.0, min(float(cfg.AI_HTTP_CONNECT_TIMEOUT_SECONDS), timeout_seconds)),
        )

    @property
    def available(self) -> bool:
        return self.enabled and bool(self.api_key)

    def cost_for_tokens(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in / 1000) * self.input_cost_per_1k_tokens + (tokens_out / 1000) * self.output_cost_per_1k_tokens


@dataclass(frozen=True, slots=True)
class PromptPolicy:
    inline_prompt: str
    prompt_file: str
    builtin_prompt: str
    inline_source: str
    builtin_source: str

    @classmethod
    def user(cls) -> "PromptPolicy":
        cfg = get_config_manager().ai
        return cls(
            inline_prompt=str(cfg.AI_USER_PROMPT),
            prompt_file=str(cfg.AI_USER_PROMPT_FILE),
            builtin_prompt=DEFAULT_USER_PROMPT_TEMPLATE,
            inline_source="env:AI_USER_PROMPT",
            builtin_source="builtin:user",
        )

    @classmethod
    def deep_analysis(cls) -> "PromptPolicy":
        cfg = get_config_manager().ai
        return cls(
            inline_prompt=str(cfg.DEEP_ANALYSIS_PROMPT),
            prompt_file=str(cfg.DEEP_ANALYSIS_PROMPT_FILE),
            builtin_prompt=DEFAULT_DEEP_ANALYSIS_PROMPT_TEMPLATE,
            inline_source="env:DEEP_ANALYSIS_PROMPT",
            builtin_source="builtin:deep_analysis",
        )


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
    def from_config(cls, noise: NoiseConfig) -> "NoiseScoringConfig":
        return cls(
            source_weight=float(noise.NOISE_SOURCE_WEIGHT),
            resource_weight=float(noise.NOISE_RESOURCE_WEIGHT),
            semantic_weight=float(noise.NOISE_SEMANTIC_WEIGHT),
            severity_weight=float(noise.NOISE_SEVERITY_WEIGHT),
            time_weight=float(noise.NOISE_TIME_WEIGHT),
            severity_downgrade_score=float(noise.NOISE_SEVERITY_DOWNGRADE_SCORE),
            related_min_confidence=float(noise.NOISE_RELATED_MIN_CONFIDENCE),
        )
