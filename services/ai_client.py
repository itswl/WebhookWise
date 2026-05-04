"""OpenAI / LLM 调用模块

封装与大语言模型的交互：使用 Instructor + Pydantic 实现结构化输出。
"""

import hashlib
import logging
from typing import Any

import httpx
import instructor
import yaml
from openai import AsyncOpenAI

from core.config import Config
from core.config import policies
from core.http_client import get_http_client
from core.metrics import AI_COST_USD_TOTAL, AI_TOKENS_TOTAL, OPENAI_ERRORS_TOTAL
from core.redis_client import get_redis
from core.circuit_breaker import feishu_cb
from schemas.analysis import WebhookAnalysisResult
from services.payload_sanitizer import sanitize_for_ai_async

logger = logging.getLogger("webhook_service.ai_client")

# 类型别名
WebhookData = dict[str, Any]
AnalysisResult = dict[str, Any]

# AsyncOpenAI 模块级单例
_openai_client: AsyncOpenAI | None = None
# Instructor 增强客户端
_instructor_client: instructor.Instructor | None = None


def _get_openai_client() -> AsyncOpenAI:
    """获取 AsyncOpenAI 客户端单例（懒加载）"""
    global _openai_client
    if _openai_client is None:
        _openai_client = AsyncOpenAI(
            api_key=policies.ai.OPENAI_API_KEY,
            base_url=policies.ai.OPENAI_API_URL,
            http_client=get_http_client(),
            timeout=httpx.Timeout(60.0, connect=10.0),
        )
    return _openai_client


def _get_instructor_client() -> instructor.Instructor:
    """获取 Instructor 增强客户端单例"""
    global _instructor_client
    if _instructor_client is None:
        client = _get_openai_client()
        # 使用 JSON 模式进行结构化输出
        _instructor_client = instructor.from_openai(client, mode=instructor.Mode.JSON)
        logger.info("[AI] 已初始化 Instructor 增强客户端")
    return _instructor_client


def get_openai_client() -> AsyncOpenAI:
    return _get_openai_client()


def reset_openai_client():
    """释放单例引用"""
    global _openai_client, _instructor_client
    _openai_client = None
    _instructor_client = None


async def analyze_with_openai_tracked(data: dict[str, Any], source: str) -> tuple[AnalysisResult, int, int]:
    """
    使用 Instructor + Pydantic 进行结构化 AI 分析，并追踪 Token 使用。
    """
    from services.ai_prompts import get_prompt_source, load_user_prompt_template

    client = _get_instructor_client()
    try:
        prompt_template = load_user_prompt_template()
        cleaned_data = await sanitize_for_ai_async(data)
        data_yaml = yaml.dump(cleaned_data, allow_unicode=True, default_flow_style=False, sort_keys=False)
        user_prompt = prompt_template.format(source=source, data_json=data_yaml)

        logger.info(
            "[AI] 调用结构化分析 | 来源: %s | Prompt 长度: %d | Source: %s",
            get_prompt_source(),
            len(user_prompt),
            source,
        )

        # 调用 Instructor
        # response_model 强制要求 LLM 返回符合 WebhookAnalysisResult 的结构
        # max_retries 处理校验失败时的自动重试
        response, completion = await client.chat.completions.create_with_completion(
            model=policies.ai.OPENAI_MODEL,
            response_model=WebhookAnalysisResult,
            messages=[
                {"role": "system", "content": policies.ai.AI_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=Config.ai.OPENAI_TEMPERATURE,
            max_retries=2,
        )

        # 提取 Token 使用情况
        tokens_in = completion.usage.prompt_tokens if completion.usage else 0
        tokens_out = completion.usage.completion_tokens if completion.usage else 0

        # 打点与成本计算
        input_cost = (tokens_in / 1000) * Config.ai.AI_COST_PER_1K_INPUT_TOKENS
        output_cost = (tokens_out / 1000) * Config.ai.AI_COST_PER_1K_OUTPUT_TOKENS
        total_cost = input_cost + output_cost
        AI_TOKENS_TOTAL.labels(model=policies.ai.OPENAI_MODEL, token_type="input").inc(tokens_in)
        AI_TOKENS_TOTAL.labels(model=policies.ai.OPENAI_MODEL, token_type="output").inc(tokens_out)
        AI_COST_USD_TOTAL.labels(model=policies.ai.OPENAI_MODEL).inc(total_cost)
        
        logger.info(f"[AI] 结构化解析成功: in={tokens_in}, out={tokens_out}, cost=${total_cost:.4f}")

        # 转换为字典并返回
        result = response.to_dict()
        return result, tokens_in, tokens_out

    except Exception as e:
        OPENAI_ERRORS_TOTAL.labels(type=_classify_openai_error(e)).inc()
        logger.error(f"OpenAI 结构化分析失败: {e!s}", exc_info=True)
        raise


async def analyze_with_openai(data: dict[str, Any], source: str) -> AnalysisResult:
    """兼容性包装函数"""
    result, _, _ = await analyze_with_openai_tracked(data, source)
    return result


def _classify_openai_error(err: Exception) -> str:
    name = type(err).__name__
    if isinstance(err, httpx.TimeoutException):
        return "timeout"
    if name in {"RateLimitError", "APIRateLimitError"}:
        return "rate_limit"
    if name in {"APITimeoutError", "TimeoutError"}:
        return "timeout"
    if name in {"BadRequestError", "UnprocessableEntityError"}:
        msg = str(err).lower()
        if "context_length" in msg or "context length" in msg or "maximum context" in msg:
            return "context_length"
        if "content_policy" in msg or "content policy" in msg or "content filter" in msg:
            return "content_filter"
        return "bad_request"
    if name in {"PermissionDeniedError", "AuthenticationError"}:
        return "auth"
    if isinstance(err, ValueError):
        if "内容被过滤" in str(err) or "content_filter" in str(err):
            return "content_filter"
        if "空响应" in str(err) or "None 内容" in str(err):
            return "empty_response"
    return "other"


async def _should_send_degradation_alert() -> bool:
    """检查是否应发送降级告警（24小时内最多一次）"""
    try:
        r = get_redis()
        success = await r.set("ai_degradation_alert_lock", "1", nx=True, ex=86400)
        return bool(success)
    except Exception as e:
        logger.warning(f"Redis 限流检查失败，允许发送: {e}")
        return True


async def _send_degradation_alert(webhook_data: WebhookData, error_reason: str) -> None:
    """发送 AI 降级通知（带24小时限流）"""
    try:
        if not await _should_send_degradation_alert():
            return
        if not policies.ai.ENABLE_FORWARD or not policies.ai.FORWARD_URL:
            return

        is_feishu = "feishu.cn" in policies.ai.FORWARD_URL or "lark" in policies.ai.FORWARD_URL
        if is_feishu:
            timestamp = webhook_data.get("timestamp", "")
            source = webhook_data.get("source", "unknown")

            card_content = {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text", "content": "⚠️ AI 分析降级通知"},
                    "template": "orange",
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**告警来源**: {source}\n**时间**: {timestamp[:19] if timestamp else '-'}",
                        },
                    },
                    {"tag": "div", "text": {"tag": "lark_md", "content": f"**⚠️ 降级原因**\n{error_reason}"}},
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": "**处理方式**\n已自动降级为基于规则的分析，告警仍会正常处理，但分析结果可能不够准确。请检查 AI 服务配置。",
                        },
                    },
                    {
                        "tag": "note",
                        "elements": [
                            {
                                "tag": "plain_text",
                                "content": "💡 此通知24小时内仅发送一次，避免频繁打扰。请尽快修复 AI 服务以恢复智能分析功能。",
                            }
                        ],
                    },
                ],
            }

            forward_data = {"msg_type": "interactive", "card": card_content}
            client = get_http_client()
            await feishu_cb.call_async(
                client.post,
                policies.ai.FORWARD_URL,
                json=forward_data,
                headers={"Content-Type": "application/json"},
                timeout=Config.ai.FEISHU_WEBHOOK_TIMEOUT,
            )
            logger.info("AI 降级通知已发送到飞书")

    except Exception as e:
        logger.error(f"发送 AI 降级通知失败: {e!s}")
