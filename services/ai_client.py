"""OpenAI / LLM 调用模块

封装与大语言模型的交互：API 调用、token 追踪、截断重试，
以及 AI 降级通知逻辑。
"""

import hashlib
import logging
from typing import Any

import httpx
import yaml
from openai import AsyncOpenAI

from core.config import Config
from core.config_provider import policies
from core.http_client import get_http_client
from core.metrics import AI_COST_USD_TOTAL, AI_TOKENS_TOTAL, OPENAI_ERRORS_TOTAL
from core.redis_client import get_redis
from core.utils import feishu_cb
from services.ai_parser import _parse_ai_analysis_response
from services.ai_response_repair import repair_concatenated_response
from services.payload_sanitizer import sanitize_for_ai_async

logger = logging.getLogger("webhook_service.ai_client")

# 类型别名
WebhookData = dict[str, Any]
AnalysisResult = dict[str, Any]

# AsyncOpenAI 模块级单例，避免每次调用都新建客户端导致连接池泄漏
_openai_client: AsyncOpenAI | None = None


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


def _get_openai_client() -> AsyncOpenAI:
    """获取 AsyncOpenAI 客户端单例（懒加载）"""
    global _openai_client
    if _openai_client is None:
        _openai_client = AsyncOpenAI(
            api_key=policies.ai.OPENAI_API_KEY,
            base_url=policies.ai.OPENAI_API_URL,
            http_client=get_http_client(),  # 复用应用统一的连接池
            timeout=httpx.Timeout(60.0, connect=10.0),  # AI 分析允许更长超时
        )
        logger.info("[AI] 已初始化 AsyncOpenAI 客户端单例")
    return _openai_client


def get_openai_client() -> AsyncOpenAI:
    return _get_openai_client()


def reset_openai_client():
    """释放 OpenAI 单例引用，供 lifespan shutdown 调用"""
    global _openai_client
    _openai_client = None


async def _request_openai_completion(client: AsyncOpenAI, messages: list[dict[str, str]], max_tokens: int):
    try:
        resp = await client.chat.completions.create(
            model=policies.ai.OPENAI_MODEL,
            messages=messages,
            temperature=Config.ai.OPENAI_TEMPERATURE,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        resp._used_json_mode = True
        return resp
    except Exception as e:
        if not isinstance(e, TypeError) and "response_format" not in str(e):
            raise
        logger.warning("[AI] response_format 参数不支持，回退到普通输出: %s", e)
        resp = await client.chat.completions.create(
            model=policies.ai.OPENAI_MODEL,
            messages=messages,
            temperature=Config.ai.OPENAI_TEMPERATURE,
            max_tokens=max_tokens,
        )
        resp._used_json_mode = False
        return resp


# 续写指令：不携带完整 user_prompt，节省 Input Token
_CONTINUATION_INSTRUCTION = "Please continue from where you left off. " "Complete the remaining JSON/YAML content."


async def _call_openai_completion(
    system_prompt: str,
    user_prompt: str,
    source: str,
) -> tuple[str, str | None, int, int]:
    """底层公共函数：拼装 messages → 调用 OpenAI → 处理截断续写。

    Returns:
        tuple: (ai_response, finish_reason, tokens_in, tokens_out)
    """
    client = _get_openai_client()
    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]

    logger.info(f"调用 OpenAI API 分析 webhook: {source}")
    try:
        prompt_hash = hashlib.sha256(user_prompt.encode("utf-8")).hexdigest()
    except Exception as e:
        prompt_hash = None
        logger.warning("[AI] 哈希计算失败: %s", e)
    logger.debug(f"[AI] prompt_size={len(user_prompt)}, prompt_sha256={prompt_hash}")
    response = await _request_openai_completion(client, messages, Config.ai.OPENAI_MAX_TOKENS)
    used_json_mode = bool(getattr(response, "_used_json_mode", False))

    # 提取 token 使用量
    tokens_in = 0
    tokens_out = 0
    if hasattr(response, "usage") and response.usage:
        tokens_in = getattr(response.usage, "prompt_tokens", 0) or 0
        tokens_out = getattr(response.usage, "completion_tokens", 0) or 0

    if not hasattr(response, "choices") or not response.choices:
        error_message = f"OpenAI API 返回无效响应: {response}"
        logger.error(error_message)
        raise TypeError(error_message)

    choice = response.choices[0]
    finish_reason = getattr(choice, "finish_reason", None)
    raw_content = getattr(choice.message, "content", None)
    if raw_content is None or not str(raw_content).strip():
        tool_calls = getattr(choice.message, "tool_calls", None)
        if tool_calls:
            first_call = tool_calls[0]
            fn = getattr(first_call, "function", None)
            args = getattr(fn, "arguments", None) if fn else None
            if args:
                raw_content = args
    ai_response = (raw_content or "").strip()
    if not ai_response:
        logger.error(
            "AI 返回空响应 | finish_reason=%s | content=%r | model=%s | tokens_in=%d | tokens_out=%d | choice=%r",
            finish_reason,
            raw_content,
            policies.ai.OPENAI_MODEL,
            tokens_in,
            tokens_out,
            choice,
        )
        if finish_reason == "content_filter":
            raise ValueError(f"AI 返回空响应（内容被过滤，finish_reason={finish_reason}）")
        if raw_content is None:
            raise ValueError(
                f"AI 返回 None 内容（finish_reason={finish_reason}），" "请检查 API Key 余额、模型名称及 API 提供商状态"
            )
        raise ValueError(f"AI 返回空响应（finish_reason={finish_reason}）")

    # 截断续写：只保留 system + 截断回复 + 简短续写指令，不重复发送完整 user_prompt
    if finish_reason == "length" and ai_response:
        if not Config.ai.AI_CONTINUATION_ENABLED:
            logger.warning("AI 响应被截断，但续写功能已通过配置关闭 (AI_CONTINUATION_ENABLED=False)，直接使用截断内容")
        else:
            if used_json_mode:
                logger.info("AI 响应被截断，使用更高 max_tokens 重新生成（JSON Mode）")
                try:
                    retry_response = await _request_openai_completion(
                        client, messages, Config.ai.OPENAI_TRUNCATION_RETRY_MAX_TOKENS
                    )
                    retry_used_json_mode = bool(getattr(retry_response, "_used_json_mode", False))
                    if hasattr(retry_response, "usage") and retry_response.usage:
                        tokens_in += getattr(retry_response.usage, "prompt_tokens", 0) or 0
                        tokens_out += getattr(retry_response.usage, "completion_tokens", 0) or 0

                    if hasattr(retry_response, "choices") and retry_response.choices:
                        retry_choice = retry_response.choices[0]
                        retry_finish_reason = getattr(retry_choice, "finish_reason", None)
                        retry_content = getattr(retry_choice.message, "content", None)
                        if retry_content is None or not str(retry_content).strip():
                            tool_calls = getattr(retry_choice.message, "tool_calls", None)
                            if tool_calls:
                                first_call = tool_calls[0]
                                fn = getattr(first_call, "function", None)
                                args = getattr(fn, "arguments", None) if fn else None
                                if args:
                                    retry_content = args
                        retry_text = (retry_content or "").strip()
                        if retry_text:
                            ai_response = retry_text
                            finish_reason = retry_finish_reason
                            used_json_mode = retry_used_json_mode
                except Exception as cont_err:
                    logger.warning("JSON Mode 截断重试失败，回退到续写拼接: %s", cont_err)

            if finish_reason == "length" and ai_response:
                logger.info("AI 响应被截断，使用续写模式继续生成")
                try:
                    continuation_messages = [
                        {"role": "system", "content": system_prompt},
                        {"role": "assistant", "content": ai_response},
                        {"role": "user", "content": _CONTINUATION_INSTRUCTION},
                    ]
                    continuation_response = await client.chat.completions.create(
                        model=policies.ai.OPENAI_MODEL,
                        messages=continuation_messages,
                        max_tokens=Config.ai.OPENAI_TRUNCATION_RETRY_MAX_TOKENS,
                        temperature=0.3,
                    )

                    if hasattr(continuation_response, "usage") and continuation_response.usage:
                        tokens_in += getattr(continuation_response.usage, "prompt_tokens", 0) or 0
                        tokens_out += getattr(continuation_response.usage, "completion_tokens", 0) or 0

                    if hasattr(continuation_response, "choices") and continuation_response.choices:
                        continuation_content = (continuation_response.choices[0].message.content or "").strip()
                        if continuation_content:
                            ai_response = repair_concatenated_response(ai_response, continuation_content)
                            finish_reason = getattr(continuation_response.choices[0], "finish_reason", finish_reason)
                except Exception as cont_err:
                    logger.warning(f"续写请求失败，使用截断内容作为最终结果: {cont_err!s}")

    try:
        resp_hash = hashlib.sha256(ai_response.encode("utf-8")).hexdigest()
    except Exception as e:
        resp_hash = None
        logger.warning("[AI] 哈希计算失败: %s", e)
    logger.debug(f"[AI] response_size={len(ai_response)}, response_sha256={resp_hash}")

    return ai_response, finish_reason, tokens_in, tokens_out


async def analyze_with_openai(data: dict[str, Any], source: str) -> AnalysisResult:
    """使用 OpenAI API 分析 webhook 数据"""
    from services.ai_prompts import get_prompt_source, load_user_prompt_template

    try:
        prompt_template = load_user_prompt_template()
        cleaned_data = await sanitize_for_ai_async(data)
        data_yaml = yaml.dump(cleaned_data, allow_unicode=True, default_flow_style=False, sort_keys=False)
        user_prompt = prompt_template.format(source=source, data_json=data_yaml)

        prompt_source = get_prompt_source()
        logger.info(
            "[AI] Prompt 来源: %s | user_prompt 长度: %d | data_yaml 长度: %d | source: %s",
            prompt_source,
            len(user_prompt),
            len(data_yaml),
            source,
        )
        if not user_prompt or not user_prompt.strip():
            raise ValueError(
                f"user_prompt 为空，无法调用 AI 分析。"
                f"prompt_source={prompt_source}, template_len={len(prompt_template)}, data_yaml_len={len(data_yaml)}"
            )

        ai_response, finish_reason, _tokens_in, _tokens_out = await _call_openai_completion(
            policies.ai.AI_SYSTEM_PROMPT, user_prompt, source
        )

        analysis_result = _parse_ai_analysis_response(ai_response, source)

        if finish_reason == "length":
            analysis_result["_truncated"] = True
            logger.warning("AI 最终响应仍为截断状态，已使用容错解析")

        return analysis_result

    except Exception as e:
        OPENAI_ERRORS_TOTAL.labels(type=_classify_openai_error(e)).inc()
        logger.error(f"OpenAI API 调用失败: {e!s}")
        raise


async def analyze_with_openai_tracked(data: dict[str, Any], source: str) -> tuple[AnalysisResult, int, int]:
    """
    使用 OpenAI API 分析 webhook 数据，并返回 token 使用量

    Returns:
        tuple: (分析结果, 输入 tokens, 输出 tokens)
    """
    from services.ai_prompts import get_prompt_source, load_user_prompt_template

    try:
        prompt_template = load_user_prompt_template()
        cleaned_data = await sanitize_for_ai_async(data)
        data_yaml = yaml.dump(cleaned_data, allow_unicode=True, default_flow_style=False, sort_keys=False)
        user_prompt = prompt_template.format(source=source, data_json=data_yaml)

        prompt_source = get_prompt_source()
        logger.info(
            "[AI] Prompt 来源: %s | user_prompt 长度: %d | data_yaml 长度: %d | source: %s",
            prompt_source,
            len(user_prompt),
            len(data_yaml),
            source,
        )
        if not user_prompt or not user_prompt.strip():
            raise ValueError(
                f"user_prompt 为空，无法调用 AI 分析。"
                f"prompt_source={prompt_source}, template_len={len(prompt_template)}, data_yaml_len={len(data_yaml)}"
            )

        ai_response, finish_reason, tokens_in, tokens_out = await _call_openai_completion(
            policies.ai.AI_SYSTEM_PROMPT, user_prompt, source
        )

        # Prometheus 打点 & 成本计算
        input_cost = (tokens_in / 1000) * Config.ai.AI_COST_PER_1K_INPUT_TOKENS
        output_cost = (tokens_out / 1000) * Config.ai.AI_COST_PER_1K_OUTPUT_TOKENS
        total_cost = input_cost + output_cost
        AI_TOKENS_TOTAL.labels(model=policies.ai.OPENAI_MODEL, token_type="input").inc(tokens_in)  # nosec B106
        AI_TOKENS_TOTAL.labels(model=policies.ai.OPENAI_MODEL, token_type="output").inc(tokens_out)  # nosec B106
        AI_COST_USD_TOTAL.labels(model=policies.ai.OPENAI_MODEL).inc(total_cost)
        logger.info(f"[AI] Token 使用: in={tokens_in}, out={tokens_out}, cost=${total_cost:.4f}")

        analysis_result = _parse_ai_analysis_response(ai_response, source)

        if finish_reason == "length":
            analysis_result["_truncated"] = True
            logger.warning("AI 最终响应仍为截断状态，已使用容错解析")

        return analysis_result, tokens_in, tokens_out

    except Exception as e:
        OPENAI_ERRORS_TOTAL.labels(type=_classify_openai_error(e)).inc()
        logger.error(f"OpenAI API 调用失败: {e!s}")
        raise


async def _should_send_degradation_alert() -> bool:
    """检查是否应发送降级告警（24小时内最多一次）

    使用 Redis SET NX EX 原子操作实现分布式限流，
    多 worker / 多 Pod 下均可正确限流。

    Returns:
        bool: True - 应该发送，False - 跳过（24小时内已通知过）
    """
    try:
        r = get_redis()
        # NX: key 不存在才设置成功; EX: 24小时过期
        success = await r.set("ai_degradation_alert_lock", "1", nx=True, ex=86400)
        if not success:
            logger.info("跳过降级通知：Redis 限流锁仍有效（24小时内已通知过）")
        return bool(success)
    except Exception as e:
        logger.warning(f"Redis 限流检查失败，允许发送: {e}")
        return True


async def _send_degradation_alert(webhook_data: WebhookData, error_reason: str) -> None:
    """发送 AI 降级通知（带24小时限流）"""
    try:
        # 检查是否在限流期内
        if not await _should_send_degradation_alert():
            return

        # 只有启用转发且配置了转发地址才发送
        if not policies.ai.ENABLE_FORWARD or not policies.ai.FORWARD_URL:
            logger.info("转发未启用，跳过降级通知")
            return

        # 检查是否是飞书 webhook
        is_feishu = "feishu.cn" in policies.ai.FORWARD_URL or "lark" in policies.ai.FORWARD_URL

        if is_feishu:
            # 构建飞书告警消息
            timestamp = webhook_data.get("timestamp", "")
            source = webhook_data.get("source", "unknown")

            card_content = {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text", "content": "⚠️ AI 分析降级通知"},
                    "template": "orange",  # 橙色警告
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

            # 发送通知（熔断保护）
            client = get_http_client()
            response = await feishu_cb.call_async(
                client.post,
                policies.ai.FORWARD_URL,
                json=forward_data,
                headers={"Content-Type": "application/json"},
                timeout=Config.ai.FEISHU_WEBHOOK_TIMEOUT,
            )

            if response is not None and 200 <= response.status_code < 300:
                logger.info("AI 降级通知已发送到飞书")
            else:
                logger.warning("AI 降级通知发送失败或被熔断拦截")

    except Exception as e:
        # 降级通知失败不应影响主流程
        logger.error(f"发送 AI 降级通知失败: {e!s}")
