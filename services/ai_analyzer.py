import requests
import json
import re
from datetime import datetime, timedelta
from typing import Any, Optional
from pathlib import Path

try:
    import json5
    HAS_JSON5 = True
except ImportError:
    HAS_JSON5 = False

from core.logger import logger
from core.config import Config
from core.utils import feishu_cb, openclaw_cb, forward_cb
from openai import OpenAI

# 类型别名
WebhookData = dict[str, Any]
AnalysisResult = dict[str, Any]
ForwardResult = dict[str, Any]

# 缓存 prompt 模板
_user_prompt_template: Optional[str] = None


def get_cache_key(alert_hash: str) -> str:
    """生成缓存 key"""
    return f"analysis_{alert_hash}"


def get_cached_analysis(alert_hash: str) -> Optional[dict]:
    """
    从缓存获取分析结果
    
    Args:
        alert_hash: 告警哈希值
        
    Returns:
        dict or None: 缓存的分析结果，未命中返回 None
    """
    if not Config.CACHE_ENABLED:
        return None
    
    try:
        from core.models import AnalysisCache, get_session
        
        session = get_session()
        try:
            cache_key = get_cache_key(alert_hash)
            cache_entry = session.query(AnalysisCache).filter(
                AnalysisCache.cache_key == cache_key
            ).first()
            
            if not cache_entry:
                logger.debug(f"缓存未命中: {cache_key[:20]}...")
                return None
            
            # 检查是否过期
            if cache_entry.is_expired():
                logger.info(f"缓存已过期: {cache_key[:20]}...")
                session.delete(cache_entry)
                session.commit()
                return None
            
            # 命中缓存，增加计数
            cache_entry.hit_count += 1
            session.commit()
            
            result = json.loads(cache_entry.analysis_result)
            result['_cache_hit'] = True
            result['_cache_hit_count'] = cache_entry.hit_count
            
            logger.info(f"缓存命中: {cache_key[:20]}..., 已命中 {cache_entry.hit_count} 次")
            return result
            
        finally:
            session.close()
            
    except Exception as e:
        logger.warning(f"读取缓存失败: {e}")
        return None


def save_to_cache(alert_hash: str, analysis_result: dict) -> bool:
    """
    将分析结果保存到缓存
    
    Args:
        alert_hash: 告警哈希值
        analysis_result: 分析结果
        
    Returns:
        bool: 是否保存成功
    """
    if not Config.CACHE_ENABLED:
        return False
    
    try:
        from core.models import AnalysisCache, get_session
        
        session = get_session()
        try:
            cache_key = get_cache_key(alert_hash)
            expires_at = datetime.now() + timedelta(seconds=Config.ANALYSIS_CACHE_TTL)
            
            # 清理内部字段
            result_to_cache = {k: v for k, v in analysis_result.items() 
                             if not k.startswith('_')}
            
            # 检查是否已存在
            existing = session.query(AnalysisCache).filter(
                AnalysisCache.cache_key == cache_key
            ).first()
            
            if existing:
                existing.analysis_result = json.dumps(result_to_cache, ensure_ascii=False)
                existing.expires_at = expires_at
                existing.created_at = datetime.now()
            else:
                cache_entry = AnalysisCache(
                    cache_key=cache_key,
                    analysis_result=json.dumps(result_to_cache, ensure_ascii=False),
                    expires_at=expires_at
                )
                session.add(cache_entry)
            
            session.commit()
            logger.info(f"分析结果已缓存: {cache_key[:20]}..., TTL={Config.ANALYSIS_CACHE_TTL}秒")
            return True
            
        finally:
            session.close()
            
    except Exception as e:
        logger.warning(f"保存缓存失败: {e}")
        return False


def log_ai_usage(
    route_type: str,
    alert_hash: str,
    source: str,
    model: Optional[str] = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cache_hit: bool = False
) -> None:
    """
    记录 AI 使用日志
    
    Args:
        route_type: 路由类型 ('ai', 'rule', 'cache')
        alert_hash: 告警哈希
        source: 告警来源
        model: 使用的模型名称
        tokens_in: 输入 token 数
        tokens_out: 输出 token 数
        cache_hit: 是否命中缓存
    """
    try:
        from core.models import AIUsageLog, get_session
        
        # 计算估算成本
        cost_estimate = 0.0
        if route_type == 'ai' and tokens_in > 0:
            cost_estimate = (
                (tokens_in / 1000) * Config.AI_COST_PER_1K_INPUT_TOKENS +
                (tokens_out / 1000) * Config.AI_COST_PER_1K_OUTPUT_TOKENS
            )
        
        session = get_session()
        try:
            usage_log = AIUsageLog(
                model=model or Config.OPENAI_MODEL,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_estimate=cost_estimate,
                cache_hit=cache_hit,
                route_type=route_type,
                alert_hash=alert_hash,
                source=source
            )
            session.add(usage_log)
            session.commit()
            
            logger.debug(f"AI 使用记录: type={route_type}, tokens={tokens_in}+{tokens_out}, cost=${cost_estimate:.6f}")
            
        finally:
            session.close()
            
    except Exception as e:
        logger.warning(f"记录 AI 使用日志失败: {e}")


def load_user_prompt_template() -> str:
    """
    加载 User Prompt 模板

    优先级：
    1. 环境变量 AI_USER_PROMPT（直接内容）
    2. 文件 AI_USER_PROMPT_FILE
    3. 默认硬编码模板

    Returns:
        str: Prompt 模板字符串，支持 {source} 和 {data_json} 占位符
    """
    global _user_prompt_template

    # 如果已缓存，直接返回
    if _user_prompt_template is not None:
        return _user_prompt_template

    # 1. 优先使用环境变量中的直接内容
    if Config.AI_USER_PROMPT:
        logger.info("使用环境变量 AI_USER_PROMPT 中的 prompt 模板")
        _user_prompt_template = Config.AI_USER_PROMPT
        return _user_prompt_template

    # 2. 尝试从文件加载
    prompt_file = Config.AI_USER_PROMPT_FILE
    if prompt_file:
        # 支持相对路径和绝对路径
        file_path = Path(prompt_file)
        if not file_path.is_absolute():
            # 相对于项目根目录（services的父目录）
            file_path = Path(__file__).parent.parent / file_path

        if file_path.exists():
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    _user_prompt_template = f.read()
                logger.info(f"成功从文件加载 prompt 模板: {file_path}")
                return _user_prompt_template
            except Exception as e:
                logger.warning(f"从文件加载 prompt 模板失败: {e}，使用默认模板")
        else:
            logger.warning(f"Prompt 模板文件不存在: {file_path}，使用默认模板")

    # 3. 使用默认模板
    logger.info("使用默认硬编码 prompt 模板")
    _user_prompt_template = """请分析以下 webhook 事件：

**来源**: {source}
**数据内容**:
```json
{data_json}
```

请按照以下 JSON 格式返回分析结果：

```json
{{
  "source": "来源系统",
  "event_type": "事件类型",
  "importance": "high/medium/low",
  "summary": "事件摘要（中文，50字内）",
  "actions": ["建议操作1", "建议操作2"],
  "risks": ["潜在风险1", "潜在风险2"],
  "impact_scope": "影响范围评估",
  "monitoring_suggestions": ["监控建议1", "监控建议2"]
}}
```

**重要性判断标准**:
- high:
  * 告警级别为 critical/error/严重/P0
  * 4xx/5xx 状态码 QPS 大幅超过阈值（超过4倍）
  * 服务不可用/故障/错误
  * 安全事件/攻击检测
  * 资金/支付相关异常
  * 数据库相关的异常
  * 对于 CPU 内存 磁盘空间 使用率超过 90% 的

- medium:
  * 告警级别为 warning/警告
  * 4xx/5xx 状态码 QPS 略微超过阈值（2-4倍）
  * 性能问题/慢查询
  * 一般业务警告

- low:
  * 告警级别为 info/information
  * 成功事件/正常操作
  * 常规通知

**特殊识别规则**:
- 如果是云监控告警（包含 Type、RuleName、Level 等字段），重点关注：
  * Level 字段（warning/critical/error/严重/P0）
  * 4xxQPS/5xxQPS 等状态码指标
  * CurrentValue 与 Threshold 的对比
  * Resources 中受影响的资源信息

**重要提示**:
1. 必须返回严格的 JSON 格式
2. 不要在 JSON 中使用注释
3. 数组中最后一个元素后不要有逗号
4. 所有字符串必须用双引号
5. 直接返回 JSON，不要包含其他文本和解释"""

    return _user_prompt_template


def reload_user_prompt_template() -> str:
    """
    重新加载 User Prompt 模板（清除缓存后重新加载）

    用于运行时动态更新 prompt 模板

    Returns:
        str: 新加载的 Prompt 模板字符串
    """
    global _user_prompt_template
    _user_prompt_template = None
    logger.info("清除 prompt 模板缓存，重新加载")
    return load_user_prompt_template()



def fix_json_format(json_str: str) -> str:
    """修复常见的 JSON 格式错误"""
    json_str = json_str.replace('\ufeff', '').strip()
    if not json_str:
        return json_str

    try:
        json.loads(json_str)
        return json_str
    except json.JSONDecodeError:
        pass

    if HAS_JSON5:
        try:
            parsed = json5.loads(json_str)
            return json.dumps(parsed, ensure_ascii=False)
        except Exception as e:
            logger.debug(f"json5 解析失败: {e}")

    fixed = json_str
    fixed = re.sub(r'//.*?$', '', fixed, flags=re.MULTILINE)
    fixed = re.sub(r'/\*.*?\*/', '', fixed, flags=re.DOTALL)
    fixed = re.sub(r',\s*([}\]])', r'\1', fixed)
    fixed = re.sub(r'([\[{])\s*,', r'\1', fixed)
    return fixed.strip()


def _extract_json_payload(text: str) -> str:
    """从响应文本中提取 JSON 片段。"""
    text = text.strip()
    fenced = re.search(r'```(?:json)?\s*([\s\S]*?)```', text, re.IGNORECASE)
    if fenced:
        return fenced.group(1).strip()
    return text


def _extract_first_json_object(text: str) -> Optional[str]:
    """提取第一个 JSON 对象（允许末尾不完整）。"""
    start = text.find('{')
    if start < 0:
        return None

    stack: list[str] = ['}']
    in_string = False
    escape = False

    for i in range(start + 1, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == '{':
            stack.append('}')
        elif ch == '[':
            stack.append(']')
        elif ch in '}]':
            if not stack or ch != stack[-1]:
                return text[start:i + 1].strip()
            stack.pop()
            if not stack:
                return text[start:i + 1].strip()

    return text[start:].strip()


def _close_truncated_json(candidate: str) -> str:
    """尝试补全被截断的 JSON。"""
    text = candidate.strip()
    if not text:
        return text

    start = text.find('{')
    if start > 0:
        text = text[start:]

    stack: list[str] = []
    in_string = False
    escape = False

    for ch in text:
        if in_string:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == '{':
            stack.append('}')
        elif ch == '[':
            stack.append(']')
        elif ch in '}]' and stack and ch == stack[-1]:
            stack.pop()

    text = re.sub(r',\s*$', '', text)
    if in_string:
        text += '"'

    while stack:
        text = re.sub(r',\s*$', '', text)
        text += stack.pop()

    return text


def _safe_json_string(raw: str) -> str:
    try:
        return json.loads(f'"{raw}"')
    except Exception:
        return raw.replace('\\n', ' ').replace('\\"', '"').strip()


def _extract_json_string_field(text: str, key: str) -> Optional[str]:
    strict = re.search(rf'"{re.escape(key)}"\s*:\s*"((?:\\.|[^"\\])*)"', text, re.DOTALL)
    if strict:
        return _safe_json_string(strict.group(1)).strip()

    truncated = re.search(rf'"{re.escape(key)}"\s*:\s*"([^\n]*)', text)
    if truncated:
        return truncated.group(1).strip().strip(',').strip()

    return None


def _extract_json_array_field(text: str, key: str) -> list[str]:
    key_match = re.search(rf'"{re.escape(key)}"\s*:\s*\[', text)
    if not key_match:
        return []

    start = key_match.end() - 1
    arr_part = text[start:]
    depth = 0
    in_string = False
    escape = False
    end = len(arr_part)

    for i, ch in enumerate(arr_part):
        if in_string:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == '[':
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    block = arr_part[:end]
    items: list[str] = []
    for raw in re.findall(r'"((?:\\.|[^"\\])*)"', block, re.DOTALL):
        value = _safe_json_string(raw).strip()
        if value:
            items.append(value)

    return items


def _clean_string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []

    cleaned: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        item = value.strip().strip('"\'`').strip().strip(',').strip()
        item = item.strip('[]{}').strip()
        if not item:
            continue
        if item in {'[', ']', '{', '}'}:
            continue
        cleaned.append(item)

    return cleaned


def _normalize_analysis_result(result: AnalysisResult, source: str) -> AnalysisResult:
    if not isinstance(result, dict):
        result = {}

    normalized: AnalysisResult = dict(result)
    normalized['source'] = str(normalized.get('source') or source)

    event_type = str(normalized.get('event_type') or 'unknown').strip()
    normalized['event_type'] = event_type or 'unknown'

    importance = str(normalized.get('importance') or 'medium').lower().strip()
    if importance not in {'high', 'medium', 'low'}:
        importance = 'medium'
    normalized['importance'] = importance

    summary = str(normalized.get('summary') or '').strip()
    normalized['summary'] = summary or 'AI分析未生成摘要'

    if 'impact_scope' in normalized and normalized['impact_scope'] is not None:
        normalized['impact_scope'] = str(normalized['impact_scope']).strip()
        if not normalized['impact_scope']:
            normalized.pop('impact_scope', None)

    normalized['actions'] = _clean_string_list(normalized.get('actions', []))
    normalized['risks'] = _clean_string_list(normalized.get('risks', []))

    if 'monitoring_suggestions' in normalized:
        normalized['monitoring_suggestions'] = _clean_string_list(normalized.get('monitoring_suggestions', []))

    return normalized


def _try_parse_json_analysis(candidate: str) -> Optional[AnalysisResult]:
    attempts = [
        candidate,
        fix_json_format(candidate),
    ]
    closed = _close_truncated_json(candidate)
    attempts.extend([closed, fix_json_format(closed)])

    for text in attempts:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue

        if isinstance(parsed, dict):
            return parsed

    return None


def extract_from_text(text: str, source: str) -> AnalysisResult:
    """从 AI 响应文本中提取关键信息（兜底策略）。"""
    logger.info("使用文本提取策略解析 AI 响应")

    result: AnalysisResult = {
        'source': source,
        'event_type': 'unknown',
        'importance': 'medium',
        'summary': '',
        'actions': [],
        'risks': []
    }

    try:
        importance = _extract_json_string_field(text, 'importance')
        if importance:
            importance = importance.lower()
            if importance in {'high', 'medium', 'low'}:
                result['importance'] = importance

        summary = _extract_json_string_field(text, 'summary')
        if summary:
            result['summary'] = summary
        elif re.search(r'(告警|错误|异常|故障)', text):
            result['summary'] = '检测到系统告警或异常，需要关注'
        else:
            result['summary'] = 'Webhook 事件已接收，AI 分析结果解析不完整'

        event_type = _extract_json_string_field(text, 'event_type')
        if event_type:
            result['event_type'] = event_type

        impact_scope = _extract_json_string_field(text, 'impact_scope')
        if impact_scope:
            result['impact_scope'] = impact_scope

        actions = _extract_json_array_field(text, 'actions')
        risks = _extract_json_array_field(text, 'risks')
        monitoring = _extract_json_array_field(text, 'monitoring_suggestions')

        if actions:
            result['actions'] = actions
        if risks:
            result['risks'] = risks
        if monitoring:
            result['monitoring_suggestions'] = monitoring

        normalized = _normalize_analysis_result(result, source)
        logger.info(f"文本提取完成: {normalized}")
        return normalized

    except Exception as e:
        logger.error(f"文本提取失败: {str(e)}")
        result['summary'] = 'AI 分析响应格式错误，已降级处理'
        return _normalize_analysis_result(result, source)


def _parse_ai_analysis_response(ai_response: str, source: str) -> AnalysisResult:
    payload = _extract_json_payload(ai_response)

    candidates: list[str] = []
    if payload:
        candidates.append(payload)

    payload_obj = _extract_first_json_object(payload)
    if payload_obj:
        candidates.append(payload_obj)

    raw_obj = _extract_first_json_object(ai_response)
    if raw_obj:
        candidates.append(raw_obj)

    for candidate in candidates:
        parsed = _try_parse_json_analysis(candidate)
        if parsed is not None:
            return _normalize_analysis_result(parsed, source)

    logger.warning("JSON 解析失败，回退到文本提取策略")
    return _normalize_analysis_result(extract_from_text(payload or ai_response, source), source)


def analyze_webhook_with_ai(webhook_data: WebhookData, alert_hash: Optional[str] = None, skip_cache: bool = False) -> AnalysisResult:
    """
    使用 AI 分析 webhook 数据
    
    分析流程：
    1. 检查缓存（如果启用且 skip_cache=False）
    2. 智能路由判断（如果启用且 skip_cache=False）
    3. 调用 AI 分析（如果需要）
    4. 记录使用日志
    
    Args:
        webhook_data: Webhook 数据
        alert_hash: 告警哈希值（可选，未提供时自动生成）
        skip_cache: 是否跳过缓存，强制重新分析（默认 False）
    """
    source = webhook_data.get('source', 'unknown')
    parsed_data = webhook_data.get('parsed_data', {})
    
    # 生成 alert_hash（如果未提供）
    if not alert_hash:
        from core.utils import generate_alert_hash
        alert_hash = generate_alert_hash(parsed_data, source)
    
    # Step 1: 检查缓存（skip_cache=True 时跳过）
    if Config.CACHE_ENABLED and not skip_cache:
        cached_result = get_cached_analysis(alert_hash)
        if cached_result:
            logger.info(f"使用缓存的分析结果: source={source}")
            cached_result['_route_type'] = 'cache'
            # 记录缓存命中
            log_ai_usage(
                route_type='cache',
                alert_hash=alert_hash,
                source=source,
                cache_hit=True
            )
            # 返回缓存结果
            return cached_result
    elif skip_cache:
        logger.info(f"跳过缓存: 用户请求重新分析, source={source}")
    
    # Step 2: 检查是否启用 AI 分析
    if not Config.ENABLE_AI_ANALYSIS:
        logger.info("AI 分析功能已禁用，使用基础规则分析")
        result = analyze_with_rules(parsed_data, source)
        result['_degraded'] = True
        result['_degraded_reason'] = 'AI 分析功能已禁用'
        result['_route_type'] = 'rule'
        log_ai_usage(route_type='rule', alert_hash=alert_hash, source=source)
        # 返回结果
        return result

    # Step 3: 检查 API Key
    if not Config.OPENAI_API_KEY:
        logger.warning("OpenAI API Key 未配置，降级为规则分析")
        result = analyze_with_rules(parsed_data, source)
        result['_degraded'] = True
        result['_degraded_reason'] = 'OpenAI API Key 未配置'
        result['_route_type'] = 'rule'
        # 发送降级通知
        _send_degradation_alert(webhook_data, 'OpenAI API Key 未配置')
        log_ai_usage(route_type='rule', alert_hash=alert_hash, source=source)
        # 返回结果
        return result

    # Step 4: 调用 AI 分析
    try:
        analysis, tokens_in, tokens_out = analyze_with_openai_tracked(parsed_data, source)

        logger.info(f"AI 分析完成: {source}")
        analysis['_degraded'] = False
        analysis['_route_type'] = 'ai'
        
        # 保存到缓存
        save_to_cache(alert_hash, analysis)
        
        # 记录 AI 使用
        log_ai_usage(
            route_type='ai',
            alert_hash=alert_hash,
            source=source,
            model=Config.OPENAI_MODEL,
            tokens_in=tokens_in,
            tokens_out=tokens_out
        )
        
        return analysis

    except Exception as e:
        logger.error(f"AI 分析失败: {str(e)}", exc_info=True)
        # 根据配置决定是否降级
        if Config.ENABLE_AI_DEGRADATION:
            logger.warning("启用 AI 降级策略，使用本地规则分析")
            result = analyze_with_rules(parsed_data, source)
            result['_degraded'] = True
            result['_degraded_reason'] = f'AI 分析失败: {str(e)}'
            result['_route_type'] = 'rule'
            _send_degradation_alert(webhook_data, str(e))
            log_ai_usage(route_type='rule', alert_hash=alert_hash, source=source)
            return result
        else:
            # 不降级，直接返回错误
            logger.error("AI 分析失败且未启用降级策略，返回错误")
            _send_degradation_alert(webhook_data, str(e))
            return {
                'summary': f'AI 分析失败: {str(e)}',
                'root_cause': '分析失败，请检查 AI 服务配置',
                'impact': '未知',
                'recommendations': ['检查 AI 服务连接', '查看日志获取详细信息'],
                'severity': 'critical',
                '_degraded': True,
                '_degraded_reason': f'AI 分析失败: {str(e)}',
                '_route_type': 'error'
            }


def analyze_with_openai_tracked(data: dict[str, Any], source: str) -> tuple[AnalysisResult, int, int]:
    """
    使用 OpenAI API 分析 webhook 数据，并返回 token 使用量
    
    Returns:
        tuple: (分析结果, 输入 tokens, 输出 tokens)
    """
    try:
        client = OpenAI(
            api_key=Config.OPENAI_API_KEY,
            base_url=Config.OPENAI_API_URL
        )

        prompt_template = load_user_prompt_template()
        data_json = json.dumps(data, ensure_ascii=False, indent=2)
        user_prompt = prompt_template.format(source=source, data_json=data_json)
        messages = [
            {"role": "system", "content": Config.AI_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt}
        ]

        logger.info(f"调用 OpenAI API 分析 webhook: {source}")
        response = _request_openai_completion(client, messages, Config.OPENAI_MAX_TOKENS)

        # 提取 token 使用量
        tokens_in = 0
        tokens_out = 0
        if hasattr(response, 'usage') and response.usage:
            tokens_in = getattr(response.usage, 'prompt_tokens', 0) or 0
            tokens_out = getattr(response.usage, 'completion_tokens', 0) or 0

        if not hasattr(response, 'choices') or not response.choices:
            error_message = f"OpenAI API 返回无效响应: {response}"
            logger.error(error_message)
            raise TypeError(error_message)

        choice = response.choices[0]
        finish_reason = getattr(choice, 'finish_reason', None)
        raw_content = getattr(choice.message, 'content', None)
        ai_response = (raw_content or '').strip()
        if not ai_response:
            # 记录详细诊断信息，方便排查原因
            logger.error(
                "AI 返回空响应 | finish_reason=%s | content=%r | model=%s | "
                "tokens_in=%d | tokens_out=%d | choice=%r",
                finish_reason,
                raw_content,
                Config.OPENAI_MODEL,
                tokens_in,
                tokens_out,
                choice,
            )
            # finish_reason=content_filter 表示内容被过滤
            if finish_reason == 'content_filter':
                raise ValueError(f"AI 返回空响应（内容被过滤，finish_reason={finish_reason}）")
            # raw_content 为 None 通常是 API 账户/配额/模型名称问题
            if raw_content is None:
                raise ValueError(
                    f"AI 返回 None 内容（finish_reason={finish_reason}），"
                    "请检查 API Key 余额、模型名称及 API 提供商状态"
                )
            raise ValueError(f"AI 返回空响应（finish_reason={finish_reason}）")

        if finish_reason == 'length':
            retry_max_tokens = max(Config.OPENAI_TRUNCATION_RETRY_MAX_TOKENS, Config.OPENAI_MAX_TOKENS)
            if retry_max_tokens > Config.OPENAI_MAX_TOKENS:
                logger.warning(
                    "AI 响应可能被截断(finish_reason=length)，使用更大 max_tokens 重试: %s",
                    retry_max_tokens
                )
                retry_response = _request_openai_completion(client, messages, retry_max_tokens)
                
                # 更新 token 使用量
                if hasattr(retry_response, 'usage') and retry_response.usage:
                    tokens_in += getattr(retry_response.usage, 'prompt_tokens', 0) or 0
                    tokens_out += getattr(retry_response.usage, 'completion_tokens', 0) or 0
                
                if hasattr(retry_response, 'choices') and retry_response.choices:
                    retry_choice = retry_response.choices[0]
                    retry_text = (retry_choice.message.content or '').strip()
                    if retry_text:
                        ai_response = retry_text
                        finish_reason = getattr(retry_choice, 'finish_reason', finish_reason)

        logger.debug(f"AI 原始响应: {ai_response}")
        logger.info(f"Token 使用: input={tokens_in}, output={tokens_out}")
        
        analysis_result = _parse_ai_analysis_response(ai_response, source)

        if finish_reason == 'length':
            analysis_result['_truncated'] = True
            logger.warning("AI 最终响应仍为截断状态，已使用容错解析")

        return analysis_result, tokens_in, tokens_out

    except Exception as e:
        logger.error(f"OpenAI API 调用失败: {str(e)}")
        raise


def _request_openai_completion(client: OpenAI, messages: list[dict[str, str]], max_tokens: int):
    return client.chat.completions.create(
        model=Config.OPENAI_MODEL,
        messages=messages,
        temperature=Config.OPENAI_TEMPERATURE,
        max_tokens=max_tokens
    )


def analyze_with_openai(data: dict[str, Any], source: str) -> AnalysisResult:
    """使用 OpenAI API 分析 webhook 数据"""
    try:
        client = OpenAI(
            api_key=Config.OPENAI_API_KEY,
            base_url=Config.OPENAI_API_URL
        )

        prompt_template = load_user_prompt_template()
        data_json = json.dumps(data, ensure_ascii=False, indent=2)
        user_prompt = prompt_template.format(source=source, data_json=data_json)
        messages = [
            {"role": "system", "content": Config.AI_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt}
        ]

        logger.info(f"调用 OpenAI API 分析 webhook: {source}")
        response = _request_openai_completion(client, messages, Config.OPENAI_MAX_TOKENS)

        if not hasattr(response, 'choices') or not response.choices:
            error_message = f"OpenAI API 返回无效响应: {response}"
            logger.error(error_message)
            raise TypeError(error_message)

        choice = response.choices[0]
        finish_reason = getattr(choice, 'finish_reason', None)
        ai_response = (choice.message.content or '').strip()
        if not ai_response:
            raise ValueError("AI 返回空响应")

        if finish_reason == 'length':
            retry_max_tokens = max(Config.OPENAI_TRUNCATION_RETRY_MAX_TOKENS, Config.OPENAI_MAX_TOKENS)
            if retry_max_tokens > Config.OPENAI_MAX_TOKENS:
                logger.warning(
                    "AI 响应可能被截断(finish_reason=length)，使用更大 max_tokens 重试: %s",
                    retry_max_tokens
                )
                retry_response = _request_openai_completion(client, messages, retry_max_tokens)
                if hasattr(retry_response, 'choices') and retry_response.choices:
                    retry_choice = retry_response.choices[0]
                    retry_text = (retry_choice.message.content or '').strip()
                    if retry_text:
                        ai_response = retry_text
                        finish_reason = getattr(retry_choice, 'finish_reason', finish_reason)

        logger.debug(f"AI 原始响应: {ai_response}")
        analysis_result = _parse_ai_analysis_response(ai_response, source)

        if finish_reason == 'length':
            analysis_result['_truncated'] = True
            logger.warning("AI 最终响应仍为截断状态，已使用容错解析")

        return analysis_result

    except Exception as e:
        logger.error(f"OpenAI API 调用失败: {str(e)}")
        raise


def _should_send_degradation_alert() -> bool:
    """
    检查是否应该发送降级通知（24小时限流）

    使用文件记录上次通知时间，避免频繁通知

    Returns:
        bool: True - 应该发送，False - 跳过（24小时内已通知过）
    """
    from datetime import datetime, timedelta
    from pathlib import Path

    # 使用数据目录记录上次通知时间
    marker_file = Path(Config.DATA_DIR) / '.ai_degradation_last_alert'

    try:
        # 读取上次通知时间
        if marker_file.exists():
            with open(marker_file, 'r') as f:
                last_alert_time_str = f.read().strip()
                last_alert_time = datetime.fromisoformat(last_alert_time_str)

            # 检查是否在24小时内
            time_since_last = datetime.now() - last_alert_time
            if time_since_last < timedelta(hours=24):
                hours_remaining = 24 - (time_since_last.total_seconds() / 3600)
                logger.info(f"跳过降级通知：距离上次通知仅 {time_since_last.total_seconds() / 3600:.1f} 小时，还需等待 {hours_remaining:.1f} 小时")
                return False

        # 记录本次通知时间
        with open(marker_file, 'w') as f:
            f.write(datetime.now().isoformat())

        return True

    except Exception as e:
        logger.error(f"检查降级通知限流失败: {e}，默认允许发送")
        return True


def _send_degradation_alert(webhook_data: WebhookData, error_reason: str) -> None:
    """发送 AI 降级通知（带24小时限流）"""
    try:
        # 检查是否在限流期内
        if not _should_send_degradation_alert():
            return

        # 只有启用转发且配置了转发地址才发送
        if not Config.ENABLE_FORWARD or not Config.FORWARD_URL:
            logger.info("转发未启用，跳过降级通知")
            return

        # 检查是否是飞书 webhook
        is_feishu = 'feishu.cn' in Config.FORWARD_URL or 'lark' in Config.FORWARD_URL

        if is_feishu:
            # 构建飞书告警消息
            timestamp = webhook_data.get('timestamp', '')
            source = webhook_data.get('source', 'unknown')

            card_content = {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": "⚠️ AI 分析降级通知"
                    },
                    "template": "orange"  # 橙色警告
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**告警来源**: {source}\n**时间**: {timestamp[:19] if timestamp else '-'}"
                        }
                    },
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**⚠️ 降级原因**\n{error_reason}"
                        }
                    },
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": "**处理方式**\n已自动降级为基于规则的分析，告警仍会正常处理，但分析结果可能不够准确。请检查 AI 服务配置。"
                        }
                    },
                    {
                        "tag": "note",
                        "elements": [
                            {
                                "tag": "plain_text",
                                "content": "💡 此通知24小时内仅发送一次，避免频繁打扰。请尽快修复 AI 服务以恢复智能分析功能。"
                            }
                        ]
                    }
                ]
            }

            forward_data = {
                "msg_type": "interactive",
                "card": card_content
            }

            # 发送通知（熔断保护）
            response = feishu_cb.call(
                requests.post,
                Config.FORWARD_URL,
                json=forward_data,
                headers={'Content-Type': 'application/json'},
                timeout=Config.FEISHU_WEBHOOK_TIMEOUT
            )

            if response is not None and 200 <= response.status_code < 300:
                logger.info(f"AI 降级通知已发送到飞书")
            else:
                logger.warning(f"AI 降级通知发送失败或被熔断拦截")

    except Exception as e:
        # 降级通知失败不应影响主流程
        logger.error(f"发送 AI 降级通知失败: {str(e)}")


def analyze_with_rules(data: dict[str, Any], source: str) -> AnalysisResult:
    """基于规则的简单分析（AI 降级方案）"""
    # 基础分析结果
    analysis = {
        'source': source,
        'event_type': 'unknown',
        'importance': 'medium',
        'summary': '规则分析（AI 降级）',
        'actions': ['查看告警详情', '检查 AI 服务状态'],
        'risks': ['使用规则分析，可能不够准确']
    }

    # 检测告警格式
    is_prometheus = 'alerts' in data and isinstance(data.get('alerts'), list) and len(data.get('alerts', [])) > 0

    if is_prometheus:
        # Prometheus Alertmanager 格式
        first_alert = data['alerts'][0]
        labels = first_alert.get('labels', {})

        # 获取告警名称
        alert_name = labels.get('alertname', labels.get('alertingRuleName', 'unknown'))
        analysis['event_type'] = alert_name

        # 获取告警级别
        alert_level = labels.get('internal_label_alert_level', labels.get('severity', '')).lower()

        # 判断重要性
        if alert_level in ['critical', 'p0', '严重', 'error']:
            analysis['importance'] = 'high'
            analysis['summary'] = f'🔴 严重告警: {alert_name}'
            analysis['actions'] = ['立即处理', '检查服务状态', '查看日志']
        elif alert_level in ['warning', 'warn', 'p1']:
            analysis['importance'] = 'medium'
            analysis['summary'] = f'🟡 警告告警: {alert_name}'
            analysis['actions'] = ['关注趋势', '准备应对措施']
        else:
            analysis['summary'] = f'📊 告警: {alert_name}'

    else:
        # 华为云/通用格式
        # 获取告警名称
        rule_name = data.get('RuleName') or data.get('alert_name') or data.get('MetricName', 'unknown')
        analysis['event_type'] = rule_name

        # 获取告警级别
        level = str(data.get('Level', '')).lower()

        # 判断重要性
        if level in ['critical', 'error', '严重', 'p0']:
            analysis['importance'] = 'high'
            analysis['summary'] = f'🔴 严重告警: {rule_name}'
            analysis['actions'] = ['立即处理', '检查资源状态', '查看监控指标']
        elif level in ['warn', 'warning', 'p1']:
            analysis['importance'] = 'medium'
            analysis['summary'] = f'🟡 警告告警: {rule_name}'
            analysis['actions'] = ['关注趋势', '评估影响范围']
        else:
            # 检查指标名称中的关键词
            metric_name = str(data.get('MetricName', '')).lower()
            if any(keyword in metric_name for keyword in ['4xxqps', '5xxqps', 'error', 'cpu', 'memory', 'disk']):
                analysis['importance'] = 'medium'
                analysis['summary'] = f'📊 监控告警: {rule_name}'
            else:
                analysis['summary'] = f'ℹ️ 通知: {rule_name}'

        # 检查阈值超标情况
        current_value = data.get('CurrentValue')
        threshold = data.get('Threshold')
        if current_value is not None and threshold is not None:
            try:
                current_num = float(current_value)
                threshold_num = float(threshold)
                if current_num > threshold_num * 4:
                    # 超过4倍阈值，提升重要性
                    analysis['importance'] = 'high'
                    analysis['summary'] = f'🔴 严重超标: {rule_name} (当前值 {current_value} >> 阈值 {threshold})'
            except (ValueError, TypeError):
                pass

        # 检查资源信息
        resources = data.get('Resources', [])
        if resources and isinstance(resources, list):
            resource_count = len(resources)
            if resource_count > 1:
                analysis['impact_scope'] = f'影响 {resource_count} 个资源'

    # 通用事件类型检查（兜底）
    if analysis['event_type'] == 'unknown':
        event = str(data.get('event', data.get('event_type', ''))).lower()
        if event:
            analysis['event_type'] = event

            # 基于关键词判断
            if any(keyword in event for keyword in ['error', 'failure', 'critical', 'alert', '错误', '失败', '故障']):
                analysis['importance'] = 'high'
                analysis['summary'] = f'🔴 严重事件: {event}'
            elif any(keyword in event for keyword in ['warning', 'warn', '警告']):
                analysis['importance'] = 'medium'
                analysis['summary'] = f'🟡 警告事件: {event}'

    return analysis


def forward_to_remote(
    webhook_data: WebhookData,
    analysis_result: AnalysisResult,
    target_url: Optional[str] = None,
    is_periodic_reminder: bool = False
) -> ForwardResult:
    """将分析后的数据转发到远程服务器

    Args:
        webhook_data: Webhook 数据
        analysis_result: AI 分析结果
        target_url: 目标 URL
        is_periodic_reminder: 是否为周期性提醒
    """
    # 检查是否启用转发
    if not Config.ENABLE_FORWARD:
        logger.info("转发功能已禁用")
        return {
            'status': 'disabled',
            'message': '转发功能已禁用'
        }

    if target_url is None:
        target_url = Config.FORWARD_URL

    try:
        # 检查是否是飞书 webhook
        is_feishu = 'feishu.cn' in target_url or 'lark' in target_url

        if is_feishu:
            # 构建飞书消息格式
            forward_data = build_feishu_message(webhook_data, analysis_result, is_periodic_reminder=is_periodic_reminder)
        else:
            # 构建普通转发数据
            forward_data = {
                'original_data': webhook_data.get('parsed_data', {}),
                'original_source': webhook_data.get('source', 'unknown'),
                'original_timestamp': webhook_data.get('timestamp'),
                'ai_analysis': analysis_result,
                'processed_by': 'webhook-analyzer',
                'client_ip': webhook_data.get('client_ip')
            }
        
        # 发送到远程服务器
        headers = {
            'Content-Type': 'application/json'
        }
        
        if not is_feishu:
            headers['X-Webhook-Source'] = f"analyzed-{webhook_data.get('source', 'unknown')}"
            headers['X-Analysis-Importance'] = analysis_result.get('importance', 'unknown')
        
        logger.info(f"转发数据到 {target_url}")
        response = forward_cb.call(
            requests.post,
            target_url,
            json=forward_data,
            headers=headers,
            timeout=Config.FORWARD_TIMEOUT
        )

        if response is None:
            return {'status': 'failed', 'message': '转发请求被熔断拦截'}

        if 200 <= response.status_code < 300:
            logger.info(f"成功转发到远程服务器: {target_url} (状态码: {response.status_code})")
            return {
                'status': 'success',
                'response': response.json() if response.content else {},
                'status_code': response.status_code
            }
        else:
            logger.warning(f"转发失败,状态码: {response.status_code}")
            return {
                'status': 'failed',
                'status_code': response.status_code,
                'response': response.text
            }
            
    except requests.exceptions.Timeout:
        logger.error(f"转发超时: {target_url}")
        return {
            'status': 'timeout',
            'message': '请求超时'
        }
    except requests.exceptions.ConnectionError:
        logger.error(f"无法连接到远程服务器: {target_url}")
        return {
            'status': 'connection_error',
            'message': '无法连接到远程服务器'
        }
    except Exception as e:
        logger.error(f"转发失败: {str(e)}", exc_info=True)
        return {
            'status': 'error',
            'message': str(e)
        }


def build_feishu_message(webhook_data: WebhookData, analysis_result: AnalysisResult, is_periodic_reminder: bool = False) -> dict:
    """构建飞书机器人消息格式

    Args:
        webhook_data: Webhook 数据
        analysis_result: AI 分析结果
        is_periodic_reminder: 是否为周期性提醒
    """
    # 获取基本信息
    source = webhook_data.get('source', 'unknown')
    timestamp = webhook_data.get('timestamp', '')
    importance = analysis_result.get('importance', 'medium')
    summary = analysis_result.get('summary', '无摘要')
    event_type = analysis_result.get('event_type', '未知事件')
    duplicate_count = webhook_data.get('duplicate_count', 1)

    # 使用配置中的重要性配置
    imp_info = Config.IMPORTANCE_CONFIG.get(importance, Config.IMPORTANCE_CONFIG['medium'])

    # 标题：如果是周期性提醒，添加特殊标识
    if is_periodic_reminder:
        title = f"🔔 周期性提醒：告警持续中（已重复 {duplicate_count} 次）"
    else:
        title = "📡 Webhook 事件通知"

    # 构建卡片消息
    card_content = {
        "config": {
            "wide_screen_mode": True
        },
        "header": {
            "title": {
                "tag": "plain_text",
                "content": title
            },
            "template": imp_info['color']
        },
        "elements": [
            {
                "tag": "div",
                "fields": [
                    {
                        "is_short": True,
                        "text": {
                            "tag": "lark_md",
                            "content": f"**来源**\n{source}"
                        }
                    },
                    {
                        "is_short": True,
                        "text": {
                            "tag": "lark_md",
                            "content": f"**重要性**\n{imp_info['emoji']} {imp_info['text']}"
                        }
                    },
                    {
                        "is_short": True,
                        "text": {
                            "tag": "lark_md",
                            "content": f"**事件类型**\n{event_type}"
                        }
                    },
                    {
                        "is_short": True,
                        "text": {
                            "tag": "lark_md",
                            "content": f"**时间**\n{timestamp[:19] if timestamp else '-'}"
                        }
                    }
                ]
            },
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**📝 事件摘要**\n{summary}"
                }
            }
        ]
    }
    
    # 添加影响范围
    if analysis_result.get('impact_scope'):
        card_content['elements'].append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**🎯 影响范围**\n{analysis_result.get('impact_scope')}"
            }
        })
    
    # 添加建议操作
    if analysis_result.get('actions'):
        actions_text = '\n'.join([f"{i+1}. {action}" for i, action in enumerate(analysis_result.get('actions', []))])
        card_content['elements'].append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**✅ 建议操作**\n{actions_text}"
            }
        })
    
    return {
        "msg_type": "interactive",
        "card": card_content
    }


def forward_to_openclaw(webhook_data: dict, analysis_result: dict) -> dict:
    """将告警推送到 OpenClaw 触发深度分析（非阻塞触发，立即返回）"""
    from core.config import Config
    
    if not Config.OPENCLAW_ENABLED:
        return {'status': 'disabled', 'message': 'OpenClaw 未启用'}
    
    alert_data = webhook_data.get('parsed_data', {})
    source = webhook_data.get('source', 'unknown')
    importance = analysis_result.get('importance', 'medium') if analysis_result else 'medium'
    
    message = f"""收到新告警，请自主排查分析：

来源: {source}
重要性: {importance}

## 告警数据
```json
{json.dumps(alert_data, ensure_ascii=False, indent=2)}
```

## AI 初步分析
{json.dumps(analysis_result, ensure_ascii=False, indent=2) if analysis_result else '无'}

## 指令
你可以自主使用 MCP 工具和 Skills 进行排查：
- 根据告警内容，自行决定需要查询哪些数据、执行哪些排查命令
- 如果涉及 Kubernetes，可以使用 kubectl 相关能力查看 Pod/Node/Service 状态
- 如果涉及监控指标，可以查询 Prometheus/Grafana 获取历史数据
- 分析完成后，提供根因分析和可执行的修复建议"""
    
    import uuid
    session_key = f"hook:alert:{source}:{uuid.uuid4()}"
    payload = {
        "message": message,
        "name": f"alert-{source}",
        "sessionKey": session_key,
        "wakeMode": "now",
        "deliver": False,
        "thinking": "high",
        "timeoutSeconds": Config.OPENCLAW_TIMEOUT_SECONDS
    }
    
    # hooks 端点使用 hooks token 认证（Authorization: Bearer）
    hooks_token = Config.OPENCLAW_HOOKS_TOKEN or Config.OPENCLAW_GATEWAY_TOKEN
    headers = {
        "Authorization": f"Bearer {hooks_token}",
        "Content-Type": "application/json"
    }
    
    # 超时配置：(连接超时, 读取超时)
    # - 连接超时 10s: TCP 连接建立
    # - 读取超时 60s: 等待服务端 session 初始化并返回 202
    response = openclaw_cb.call(
        requests.post,
        f"{Config.OPENCLAW_GATEWAY_URL}/hooks/agent",
        json=payload,
        headers=headers,
        timeout=(10, 60)
    )

    if response is None:
        return {'status': 'error', 'message': 'OpenClaw 请求被熔断拦截'}

    try:
        response.raise_for_status()
        result = response.json()
        run_id = result.get('runId')
        logger.info(f"OpenClaw 转发成功: run_id={run_id}, session_key={session_key}")

        # 非阻塞触发：HTTP POST 成功后立即返回
        return {
            'status': 'success',
            'run_id': run_id,
            'session_key': session_key,
            '_pending': True
        }
    except Exception as e:
        logger.error(f"OpenClaw 转发失败: {e}")
        return {'status': 'error', 'message': str(e)}


def analyze_with_openclaw(webhook_data: dict, user_question: str = '', thinking_level: str = 'high') -> dict:
    """通过 OpenClaw Agent 进行深度分析（非阻塞触发，立即返回）"""
    from core.config import Config
    
    if not Config.OPENCLAW_ENABLED:
        logger.warning("OpenClaw 未启用")
        return {'_degraded': True, '_degraded_reason': 'OpenClaw 未启用'}
    
    alert_data = webhook_data.get('parsed_data', {})
    source = webhook_data.get('source', 'unknown')
    
    message = f"""请对以下告警进行深度根因分析：

告警来源: {source}

## 告警数据
```json
{json.dumps(alert_data, ensure_ascii=False, indent=2)}
```

## 可用能力
你可以自主决策并使用以下能力来排查和分析问题：
- **MCP 工具**: 你可以调用已连接的 MCP 服务（如 Kubernetes、Prometheus、日志系统等）获取实时数据
- **Skills**: 你可以调用已配置的 Skills 执行自动化排查操作
- **自主决策**: 根据告警内容，自行决定需要调用哪些工具、查询哪些数据、执行哪些排查步骤

## 分析要求
1. **根因分析**: 结合实际环境数据，深度挖掘问题根本原因
2. **影响评估**: 评估对系统的影响范围和紧急程度
3. **排查过程**: 说明你执行了哪些排查步骤、调用了哪些工具、获取了哪些数据
4. **修复建议**: 提供可执行的解决方案，优先给出可直接执行的命令或操作
5. **置信度**: 评估分析可信度 (0-1)

请返回 JSON 格式:
"root_cause": "...", "impact": "...", "investigation_steps": [...], "recommendations": [...], "confidence": 0.85"""
    
    if user_question:
        message += f"\n\n## 用户补充问题\n{user_question}"
    
    import uuid
    session_key = f"hook:deep-analysis:{source}:{uuid.uuid4()}"
    payload = {
        "message": message,
        "name": "deep-analysis",
        "sessionKey": session_key,
        "wakeMode": "now",
        "deliver": False,
        "thinking": thinking_level,
        "timeoutSeconds": Config.OPENCLAW_TIMEOUT_SECONDS
    }
    
    # hooks 端点使用 hooks token 认证（Authorization: Bearer）
    hooks_token = Config.OPENCLAW_HOOKS_TOKEN or Config.OPENCLAW_GATEWAY_TOKEN
    headers = {
        "Authorization": f"Bearer {hooks_token}",
        "Content-Type": "application/json"
    }
    
    response = openclaw_cb.call(
        requests.post,
        f"{Config.OPENCLAW_GATEWAY_URL}/hooks/agent",
        json=payload,
        headers=headers,
        timeout=(10, 60)
    )

    if response is None:
        # 根据配置决定是否降级
        if Config.ENABLE_AI_DEGRADATION:
            logger.warning("OpenClaw 请求失败，降级到本地 AI 分析")
            return {'_degraded': True, '_degraded_reason': 'OpenClaw 请求失败'}
        else:
            logger.error("OpenClaw 请求失败，未启用降级策略")
            raise Exception('OpenClaw 请求失败')

    try:
        response.raise_for_status()
        result = response.json()
        run_id = result.get('runId')
        logger.info(f"OpenClaw 分析已触发: run_id={run_id}, session_key={session_key}")

        # 非阻塞触发：HTTP POST 成功后立即返回
        return {
            '_pending': True,
            '_openclaw_run_id': run_id,
            '_openclaw_session_key': session_key
        }
    except requests.exceptions.RequestException as e:
        logger.error(f"OpenClaw 请求失败: {e}")
        # 根据配置决定是否降级
        if Config.ENABLE_AI_DEGRADATION:
            logger.warning("OpenClaw 请求失败，降级到本地 AI 分析")
            return {'_degraded': True, '_degraded_reason': f'OpenClaw 不可用: {str(e)}'}
        else:
            logger.error("OpenClaw 请求失败，未启用降级策略")
            raise
