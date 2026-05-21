"""AI analysis policies built from runtime configuration or explicit injection."""

from dataclasses import dataclass
from typing import Any

from core.config import Config

DEFAULT_USER_PROMPT_TEMPLATE = """请分析以下 webhook 事件：
**来源**: {source}
**数据内容**:
```yaml
{data_json}
```
请识别事件的类型、严重程度，并提供摘要、影响评估和处理建议。"""

DEFAULT_DEEP_ANALYSIS_PROMPT_TEMPLATE = "请对以下告警进行深度根因分析。"


def _split_keywords(value: str) -> tuple[str, ...]:
    return tuple(part.strip().lower() for part in str(value).split(",") if part.strip())


@dataclass(frozen=True, slots=True)
class RuleAnalysisPolicy:
    high_keywords: tuple[str, ...]
    warning_keywords: tuple[str, ...]
    metric_keywords: tuple[str, ...]
    threshold_multiplier: float

    @classmethod
    def from_config(cls, config: Any = Config.ai) -> "RuleAnalysisPolicy":
        return cls(
            high_keywords=_split_keywords(config.RULE_HIGH_KEYWORDS),
            warning_keywords=_split_keywords(config.RULE_WARN_KEYWORDS),
            metric_keywords=_split_keywords(config.RULE_METRIC_KEYWORDS),
            threshold_multiplier=float(config.RULE_THRESHOLD_MULTIPLIER or 4.0),
        )


@dataclass(frozen=True, slots=True)
class AIErrorNotificationPolicy:
    enabled: bool
    target_url: str
    cooldown_seconds: int = 3600
    timeout_seconds: int = 10

    @classmethod
    def from_config(cls, config: Any = Config) -> "AIErrorNotificationPolicy":
        return cls(
            enabled=bool(config.forwarding.ENABLE_FORWARD),
            target_url=str(config.forwarding.FORWARD_URL),
            cooldown_seconds=max(1, int(config.notifications.AI_ERROR_NOTIFICATION_COOLDOWN_SECONDS)),
            timeout_seconds=max(1, int(config.notifications.AI_ERROR_NOTIFICATION_TIMEOUT_SECONDS)),
        )


@dataclass(frozen=True, slots=True)
class AICachePolicy:
    enabled: bool
    ttl_seconds: int

    @classmethod
    def from_config(cls, config: Any = Config.ai) -> "AICachePolicy":
        return cls(enabled=bool(config.CACHE_ENABLED), ttl_seconds=int(config.ANALYSIS_CACHE_TTL))


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

    @classmethod
    def from_config(cls, config: Any = Config.ai) -> "AIProviderPolicy":
        return cls(
            enabled=bool(config.ENABLE_AI_ANALYSIS),
            api_key=str(config.OPENAI_API_KEY),
            api_url=str(config.OPENAI_API_URL),
            model=str(config.OPENAI_MODEL),
            system_prompt=str(config.AI_SYSTEM_PROMPT),
            temperature=float(config.OPENAI_TEMPERATURE),
            input_cost_per_1k_tokens=float(config.AI_COST_PER_1K_INPUT_TOKENS),
            output_cost_per_1k_tokens=float(config.AI_COST_PER_1K_OUTPUT_TOKENS),
            degradation_enabled=bool(config.ENABLE_AI_DEGRADATION),
        )

    @property
    def available(self) -> bool:
        return self.enabled and bool(self.api_key)

    def cost_for_tokens(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in / 1000) * self.input_cost_per_1k_tokens + (tokens_out / 1000) * self.output_cost_per_1k_tokens


@dataclass(frozen=True, slots=True)
class AIPromptPolicy:
    inline_prompt: str
    prompt_file: str
    builtin_prompt: str = DEFAULT_USER_PROMPT_TEMPLATE
    inline_source: str = "env:AI_USER_PROMPT"
    builtin_source: str = "builtin:user"

    @classmethod
    def from_config(cls, config: Any = Config.ai) -> "AIPromptPolicy":
        return cls(inline_prompt=str(config.AI_USER_PROMPT), prompt_file=str(config.AI_USER_PROMPT_FILE))


@dataclass(frozen=True, slots=True)
class DeepAnalysisPromptPolicy:
    inline_prompt: str
    prompt_file: str
    builtin_prompt: str = DEFAULT_DEEP_ANALYSIS_PROMPT_TEMPLATE
    inline_source: str = "env:DEEP_ANALYSIS_PROMPT"
    builtin_source: str = "builtin:deep_analysis"

    @classmethod
    def from_config(cls, config: Any = Config.ai) -> "DeepAnalysisPromptPolicy":
        return cls(
            inline_prompt=str(config.DEEP_ANALYSIS_PROMPT),
            prompt_file=str(config.DEEP_ANALYSIS_PROMPT_FILE),
        )
