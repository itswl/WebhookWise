---
title: VikingDB 限流（429）告警处置预案
service: VikingDB
tags: [vikingdb, ratelimit, 429, vector-db, runbook]
owner: 数据平台组 @data-platform
source_ref: 内部 Wiki / VikingDB 接入规范
---

# VikingDB 限流（429）告警处置预案

## 适用告警
- 错误码 `429` / 文案含 "limit"、"rate limited"、"频率限制"
- 启动期出现 "VikingDB 429 limit during startup"
- 来源服务：依赖 VikingDB 向量检索/写入的业务（如 elys-backend）

## 这是什么
VikingDB 是向量数据库服务，按 QPS / 并发配额限流。429 表示调用方超过了分配的 `max_qps` 或并发上限。**启动期的 429 多为瞬时**（实例批量预热、连接池初始化），稳态持续 429 才说明配额真的不够或有异常放大调用。

## 处置步骤
1. 区分瞬时 vs 持续：
   - **启动期/发布期瞬时 429**：通常随预热结束自愈，观察 5-10 分钟。
   - **稳态持续 429**：进入下一步。
2. 查调用方是否有异常放大：是否新上线了高频查询、缓存击穿导致回源 VikingDB、或循环重试风暴。
3. 临时缓解：在配置中心把调用方 `max_qps` 调低做自我保护，或对非核心查询加本地缓存/降级。
4. 提配额：如确为正常增长，联系数据平台组 @data-platform 提升 VikingDB 配额。

## 负责人与升级路径
- 一线：数据平台组 @data-platform
- 调用方排查：对应业务 owner（如 elys-backend 团队）

## 备注
该类告警在 WebhookWise 侧已配置永久静默（实例 52964 类），即默认不转发飞书——因为启动期 429 噪声大且多自愈。如需恢复通知，在静默页解除。真正持续的 429 应通过其它稳态指标发现。
