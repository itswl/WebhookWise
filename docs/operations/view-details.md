# 查看事件详情

Dashboard 列表接口只返回摘要字段，用于快速渲染事件列表；展开某条事件时，前端会按需调用详情接口加载脱敏后的原始数据和完整分析结果。

## Dashboard

1. 打开 `http://localhost:8000`。
2. 点击任意告警项展开详情。
3. 在 `概览`、`原始数据`、`AI 分析`、`深度分析` 标签页之间切换。

## API

列表摘要：

```bash
curl "http://localhost:8000/api/webhooks?page=1&page_size=100" \
  -H "Authorization: Bearer $API_KEY" | jq .
```

单条详情：

```bash
curl "http://localhost:8000/api/webhooks/8265" \
  -H "Authorization: Bearer $API_KEY" | jq .
```

详情响应包含 `raw_payload`、`headers`、`parsed_data`、`alert_hash`、`ai_analysis`、`processing_status` 等排查字段。常见敏感 header 和 secret/token/password 字段会被脱敏。
