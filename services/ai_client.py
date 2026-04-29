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
from core.http_client import get_http_client
from core.metrics import AI_COST_USD_TOTAL, AI_TOKENS_TOTAL
from core.redis_client import get_redis
from core.utils import feishu_cb
from services.ai_parser import _parse_ai_analysis_response
from services.payload_sanitizer import sanitize_for_ai

logger = logging.getLogger("webhook_service.ai_client")

# 类型别名
WebhookData = dict[str, Any]
AnalysisResult = dict[str, Any]

# AsyncOpenAI 模块级单例，避免每次调用都新建客户端导致连接池泄漏
_openai_client: AsyncOpenAI | None = None


def _get_openai_client() -> AsyncOpenAI:
    """获取 AsyncOpenAI 客户端单例（懒加载）"""
    global _openai_client
    if _openai_client is None:
        _openai_client = AsyncOpenAI(
            api_key=Config.ai.OPENAI_API_KEY,
            base_url=Config.ai.OPENAI_API_URL,
            http_client=get_http_client(),  # 复用应用统一的连接池
            timeout=httpx.Timeout(60.0, connect=10.0),  # AI 分析允许更长超时
        )
        logger.info("[AI] 已初始化 AsyncOpenAI 客户端单例")
    return _openai_client


def reset_openai_client():
    """释放 OpenAI 单例引用，供 lifespan shutdown 调用"""
    global _openai_client
    _openai_client = None


async def _request_openai_completion(client: AsyncOpenAI, messages: list[dict[str, str]], max_tokens: int):
    return await client.chat.completions.create(
        model=Config.ai.OPENAI_MODEL, messages=messages, temperature=Config.ai.OPENAI_TEMPERATURE, max_tokens=max_tokens
    )


async def analyze_with_openai(data: dict[str, Any], source: str) -> AnalysisResult:
    """使用 OpenAI API 分析 webhook 数据"""
    from services.ai_prompts import load_user_prompt_template

    try:
        client = _get_openai_client()

        prompt_template = load_user_prompt_template()
        cleaned_data = sanitize_for_ai(data)
        data_yaml = yaml.dump(cleaned_data, allow_unicode=True, default_flow_style=False, sort_keys=False)
        user_prompt = prompt_template.format(source=source, data_json=data_yaml)
        messages = [{"role": "system", "content": Config.ai.AI_SYSTEM_PROMPT}, {"role": "user", "content": user_prompt}]

        logger.info(f"调用 OpenAI API 分析 webhook: {source}")
        try:
            prompt_hash = hashlib.sha256(user_prompt.encode("utf-8")).hexdigest()
        except Exception:
            prompt_hash = None
        logger.debug(f"[AI] prompt_size={len(user_prompt)}, prompt_sha256={prompt_hash}")
        response = await _request_openai_completion(client, messages, Config.ai.OPENAI_MAX_TOKENS)

        if not hasattr(response, "choices") or not response.choices:
            error_message = f"OpenAI API 返回无效响应: {response}"
            logger.error(error_message)
            raise TypeError(error_message)

        choice = response.choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        ai_response = (choice.message.content or "").strip()
        if not ai_response:
            raise ValueError("AI 返回空响应")

        if finish_reason == "length" and ai_response:
            logger.info("AI 响应被截断，使用续写模式继续生成")
            try:
                continuation_messages = messages + [
                    {"role": "assistant", "content": ai_response},
                    {"role": "user", "content": "请继续完成上面被截断的分析，直接从中断处继续，不要重复已有内容。"},
                ]
                continuation_response = await client.chat.completions.create(
                    model=Config.ai.OPENAI_MODEL,
                    messages=continuation_messages,
                    max_tokens=Config.ai.OPENAI_TRUNCATION_RETRY_MAX_TOKENS,
                    temperature=0.3,
                )
                if hasattr(continuation_response, "choices") and continuation_response.choices:
                    continuation_content = (continuation_response.choices[0].message.content or "").strip()
                    if continuation_content:
                        ai_response = ai_response + continuation_content
                        finish_reason = getattr(continuation_response.choices[0], "finish_reason", finish_reason)
            except Exception as cont_err:
                logger.warning(f"续写请求失败，使用截断内容作为最终结果: {cont_err!s}")

        try:
            resp_hash = hashlib.sha256(ai_response.encode("utf-8")).hexdigest()
        except Exception:
            resp_hash = None
        logger.debug(f"[AI] response_size={len(ai_response)}, response_sha256={resp_hash}")
        analysis_result = _parse_ai_analysis_response(ai_response, source)

        if finish_reason == "length":
            analysis_result["_truncated"] = True
            logger.warning("AI 最终响应仍为截断状态，已使用容错解析")

        return analysis_result

    except Exception as e:
        logger.error(f"OpenAI API 调用失败: {e!s}")
        raise


async def analyze_with_openai_tracked(data: dict[str, Any], source: str) -> tuple[AnalysisResult, int, int]:
    """
    使用 OpenAI API 分析 webhook 数据，并返回 token 使用量

    Returns:
        tuple: (分析结果, 输入 tokens, 输出 tokens)
    """
    from services.ai_prompts import load_user_prompt_template

    try:
        client = _get_openai_client()

        prompt_template = load_user_prompt_template()
        cleaned_data = sanitize_for_ai(data)
        data_yaml = yaml.dump(cleaned_data, allow_unicode=True, default_flow_style=False, sort_keys=False)
        user_prompt = prompt_template.format(source=source, data_json=data_yaml)
        messages = [{"role": "system", "content": Config.ai.AI_SYSTEM_PROMPT}, {"role": "user", "content": user_prompt}]

        logger.info(f"调用 OpenAI API 分析 webhook: {source}")
        try:
            prompt_hash = hashlib.sha256(user_prompt.encode("utf-8")).hexdigest()
        except Exception:
            prompt_hash = None
        logger.debug(f"[AI] prompt_size={len(user_prompt)}, prompt_sha256={prompt_hash}")
        response = await _request_openai_completion(client, messages, Config.ai.OPENAI_MAX_TOKENS)

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
        ai_response = (raw_content or "").strip()
        if not ai_response:
            # 记录详细诊断信息，方便排查原因
            logger.error(
                "AI 返回空响应 | finish_reason=%s | content=%r | model=%s | tokens_in=%d | tokens_out=%d | choice=%r",
                finish_reason,
                raw_content,
                Config.ai.OPENAI_MODEL,
                tokens_in,
                tokens_out,
                choice,
            )
            # finish_reason=content_filter 表示内容被过滤
            if finish_reason == "content_filter":
                raise ValueError(f"AI 返回空响应（内容被过滤，finish_reason={finish_reason}）")
            # raw_content 为 None 通常是 API 账户/配额/模型名称问题
            if raw_content is None:
                raise ValueError(
                    f"AI 返回 None 内容（finish_reason={finish_reason}），"
                    "请检查 API Key 余额、模型名称及 API 提供商状态"
                )
            raise ValueError(f"AI 返回空响应（finish_reason={finish_reason}）")

        if finish_reason == "length" and ai_response:
            logger.info("AI 响应被截断，使用续写模式继续生成")
            try:
                continuation_messages = messages + [
                    {"role": "assistant", "content": ai_response},
                    {"role": "user", "content": "请继续完成上面被截断的分析，直接从中断处继续，不要重复已有内容。"},
                ]
                continuation_response = await client.chat.completions.create(
                    model=Config.ai.OPENAI_MODEL,
                    messages=continuation_messages,
                    max_tokens=Config.ai.OPENAI_TRUNCATION_RETRY_MAX_TOKENS,
                    temperature=0.3,
                )

                # 更新 token 使用量
                if hasattr(continuation_response, "usage") and continuation_response.usage:
                    tokens_in += getattr(continuation_response.usage, "prompt_tokens", 0) or 0
                    tokens_out += getattr(continuation_response.usage, "completion_tokens", 0) or 0

                if hasattr(continuation_response, "choices") and continuation_response.choices:
                    continuation_content = (continuation_response.choices[0].message.content or "").strip()
                    if continuation_content:
                        ai_response = ai_response + continuation_content
                        finish_reason = getattr(continuation_response.choices[0], "finish_reason", finish_reason)
            except Exception as cont_err:
                logger.warning(f"续写请求失败，使用截断内容作为最终结果: {cont_err!s}")

        try:
            resp_hash = hashlib.sha256(ai_response.encode("utf-8")).hexdigest()
        except Exception:
            resp_hash = None
        logger.debug(f"[AI] response_size={len(ai_response)}, response_sha256={resp_hash}")
        input_cost = (tokens_in / 1000) * Config.ai.AI_COST_PER_1K_INPUT_TOKENS
        output_cost = (tokens_out / 1000) * Config.ai.AI_COST_PER_1K_OUTPUT_TOKENS
        total_cost = input_cost + output_cost
        AI_TOKENS_TOTAL.labels(model=Config.ai.OPENAI_MODEL, token_type="input").inc(tokens_in)  # nosec B106
        AI_TOKENS_TOTAL.labels(model=Config.ai.OPENAI_MODEL, token_type="output").inc(tokens_out)  # nosec B106
        AI_COST_USD_TOTAL.labels(model=Config.ai.OPENAI_MODEL).inc(total_cost)
        logger.info(f"[AI] Token 使用: in={tokens_in}, out={tokens_out}, cost=${total_cost:.4f}")

        analysis_result = _parse_ai_analysis_response(ai_response, source)

        if finish_reason == "length":
            analysis_result["_truncated"] = True
            logger.warning("AI 最终响应仍为截断状态，已使用容错解析")

        return analysis_result, tokens_in, tokens_out

    except Exception as e:
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
        if not Config.ai.ENABLE_FORWARD or not Config.ai.FORWARD_URL:
            logger.info("转发未启用，跳过降级通知")
            return

        # 检查是否是飞书 webhook
        is_feishu = "feishu.cn" in Config.ai.FORWARD_URL or "lark" in Config.ai.FORWARD_URL

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
                Config.ai.FORWARD_URL,
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
