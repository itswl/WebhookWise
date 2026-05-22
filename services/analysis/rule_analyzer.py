"""Rule-based fallback analysis."""

import math
import time
from typing import Any

from core.logger import get_logger
from core.observability.metrics import AI_ANALYSIS_DURATION_SECONDS, ALERT_NUMERIC_PARSE_FAILURE_TOTAL, sanitize_source
from services.analysis.ai_policies import RuleAnalysisPolicy
from services.webhooks.types import AnalysisResult

logger = get_logger("analysis.rule_analyzer")


def analyze_with_rules(
    data: dict[str, Any],
    source: str,
    *,
    policy: RuleAnalysisPolicy | None = None,
    numeric_parse_failure_counter: Any = None,
) -> AnalysisResult:
    policy = policy or RuleAnalysisPolicy.from_config()
    counter = numeric_parse_failure_counter or ALERT_NUMERIC_PARSE_FAILURE_TOTAL
    start_time = time.time()
    res: AnalysisResult = {
        "source": source,
        "event_type": "unknown",
        "importance": "medium",
        "summary": "规则分析（AI 降级）",
        "actions": ["查看告警详情"],
        "risks": ["分析可能不准"],
    }

    rule_name = str(data.get("RuleName") or data.get("alert_name") or data.get("AlertName") or "unknown")
    res["event_type"] = rule_name

    labels = data.get("labels")
    labels_sev = labels.get("severity") if isinstance(labels, dict) else None
    level_raw = (
        data.get("Level") or data.get("level") or data.get("Severity") or data.get("severity") or labels_sev or ""
    )
    level = str(level_raw).strip().lower()
    name_l = rule_name.lower()

    high_kw = policy.high_keywords
    warn_kw = policy.warning_keywords
    metric_kw = policy.metric_keywords

    importance = "medium"
    if level in high_kw or any(k in level for k in high_kw) or any(k in name_l for k in high_kw):
        importance = "high"
    elif level in warn_kw or any(k in level for k in warn_kw) or any(k in name_l for k in warn_kw):
        importance = "medium"
    elif any(k in level for k in ("info", "information", "notice", "ok", "resolved", "success", "normal", "恢复")):
        importance = "low"

    cur_val = data.get("CurrentValue") or data.get("current_value") or data.get("current") or data.get("value")
    thr_val = data.get("Threshold") or data.get("threshold") or data.get("limit")
    multiplier = policy.threshold_multiplier

    def _record_numeric_parse_failure(field: str, value: Any, reason: str) -> None:
        counter.labels(
            source=sanitize_source(source),
            field=field,
            reason=reason,
        ).inc()
        logger.debug(
            "[AI] 规则分析数值字段解析失败 source=%s field=%s reason=%s value=%r",
            source,
            field,
            reason,
            value,
        )

    def _to_float(v: Any, field: str) -> float | None:
        if v is None:
            return None
        if isinstance(v, (int, float)):
            numeric = float(v)
            if math.isfinite(numeric):
                return numeric
            _record_numeric_parse_failure(field, v, "non_finite")
            return None
        s = str(v).strip()
        if not s:
            return None
        try:
            numeric = float(s)
        except ValueError:
            _record_numeric_parse_failure(field, v, "non_numeric")
            return None
        if math.isfinite(numeric):
            return numeric
        _record_numeric_parse_failure(field, v, "non_finite")
        return None

    cur_f = _to_float(cur_val, "current")
    thr_f = _to_float(thr_val, "threshold")
    if cur_f is not None and thr_f is not None and thr_f > 0:
        data_l = str(data).lower()
        is_metric_related = any(k in name_l for k in metric_kw) or any(k in data_l for k in metric_kw)
        if is_metric_related:
            if cur_f >= thr_f * multiplier:
                importance = "high"
            elif cur_f >= thr_f and importance != "high":
                importance = "medium"

    res["importance"] = importance
    prefix = {"high": "🔴", "medium": "🟠", "low": "🟢"}.get(importance, "🟠")
    if cur_f is not None and thr_f is not None:
        res["summary"] = f"{prefix} {rule_name}: 当前值 {cur_f:g} / 阈值 {thr_f:g}"
    else:
        res["summary"] = f"{prefix} {rule_name}"

    if importance == "high":
        res["actions"] = ["立即确认影响范围", "检查近 5 分钟指标/日志", "按 Runbook 执行处置"]
        res["risks"] = ["可能导致服务不可用或核心能力下降", "可能影响用户或业务数据"]
    elif importance == "low":
        res["actions"] = ["确认是否为预期事件", "必要时补充告警规则"]
        res["risks"] = ["告警可能噪声偏多"]

    AI_ANALYSIS_DURATION_SECONDS.labels(source=sanitize_source(source), engine="rule").observe(time.time() - start_time)
    return res
