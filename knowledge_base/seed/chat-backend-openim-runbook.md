---
title: chat-backend 发送 OpenIM 消息失败告警处置预案
service: chat-backend
tags: [chat-backend, openim, im, message, runbook]
owner: 业务后端组 @backend-chat
source_ref: 内部 Wiki / chat-backend 运维
---

# chat-backend 发送 OpenIM 消息失败告警处置预案

## 适用告警
- 文案：`chat-backend 出现 Send msg to openim failed`（或类似 OpenIM 投递失败日志告警）
- 来源：应用自身日志/事件（经飞书卡片或日志告警上报）

## 这是什么
chat-backend 依赖 OpenIM 做即时消息投递。`Send msg to openim failed` 表示 chat-backend 调用 OpenIM 接口下发消息失败。可能是 OpenIM 服务侧异常、网络抖动、鉴权 token 过期，或单条消息体异常。**偶发单条失败**通常可忽略（多有重试）；**短时间内大量失败**意味着 IM 链路出问题，用户会感知到消息发不出/收不到。

## 处置步骤
1. 看量级与趋势：是偶发一两条，还是持续报。偶发可观察，持续报立即介入。
2. 查 OpenIM 服务健康：OpenIM 自身是否存活、是否在重启/发版、依赖的存储（Redis/Mongo）是否异常。
3. 查链路：chat-backend → OpenIM 的网络连通性、调用方 token/鉴权是否失效。
4. 查消息体：若只有特定会话/消息类型失败，多为消息格式或大小问题，定位具体 caller。
5. 恢复后确认：失败期间的消息是否有补偿/重投机制，必要时人工触发补发。

## 负责人与升级路径
- 一线：业务后端组 @backend-chat
- OpenIM 平台侧：转 IM 平台/中间件负责人
- 升级：大面积消息收发失败影响在线用户 → P0，电话值班。

## 备注
这是应用级业务告警（非云监控基础设施指标），上下文以 chat-backend 的服务日志为准。与对象存储/带宽这类 volcengine 基础设施告警分属不同处置路径，不要混淆负责人。
