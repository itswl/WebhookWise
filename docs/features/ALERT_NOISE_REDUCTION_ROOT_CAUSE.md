# 告警智能降噪 + 根因分析

## 概述

系统新增了「告警智能降噪 + 根因分析」能力，用于识别告警风暴中的根因与衍生关系。

目标：

- 降低高频告警风暴中的重复通知噪音
- 尽可能把通知焦点放在根因告警上
- 给出可解释的关联理由和置信度

## 工作机制

1. 在接收告警后，系统会读取最近 `NOISE_REDUCTION_WINDOW_MINUTES` 分钟内的历史告警（默认 5 分钟）。
2. 对当前告警与候选告警进行特征匹配，计算相关性分数：
   - 来源一致性
   - 资源 ID 相似度（实例/主机/Pod/服务）
   - 文本 token 相似度（RuleName、event_type、summary 等）
   - 严重级别倾向
   - 时间接近度
3. 当最高相关分数达到 `ROOT_CAUSE_MIN_CONFIDENCE`（默认 0.65）时：
   - 判定当前告警为 `derived`（衍生）
   - 记录关联根因候选告警 ID
4. 若 `SUPPRESS_DERIVED_ALERT_FORWARD=true`（默认开启），则自动抑制衍生告警的自动转发。

## 输出字段

AI 分析结果 `ai_analysis` 会新增 `noise_reduction`：

```json
{
  "noise_reduction": {
    "relation": "derived|root_cause|standalone",
    "root_cause_event_id": 123,
    "confidence": 0.78,
    "suppress_forward": true,
    "reason": "与告警#123 高相关（置信度 0.78）",
    "related_alert_count": 4,
    "related_alert_ids": [123, 120, 118, 117]
  }
}
```

## 配置项

```bash
ENABLE_ALERT_NOISE_REDUCTION=true
NOISE_REDUCTION_WINDOW_MINUTES=5
ROOT_CAUSE_MIN_CONFIDENCE=0.65
SUPPRESS_DERIVED_ALERT_FORWARD=true
```

## API / UI 影响

- 自动转发逻辑会优先检查 `noise_reduction.suppress_forward`。
- Dashboard 的 AI 分析面板新增：
  - 降噪判定（根因/衍生/独立）
  - 关联根因 ID
  - 关联说明

## 测试

```bash
PYTHONPATH=. pytest -q tests/test_noise_reduction.py
```

