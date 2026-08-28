---
title: WebhookWise
description: 部署在监控系统与聊天工具之间的自托管告警智能层 —— 以及一条能解释每次通知(或未通知)的决策轨迹。
---

[English](../) · **中文**

WebhookWise 站在你的监控系统和聊天工具之间。它把 Prometheus、Grafana、
Alertmanager、飞书、或任何能 POST JSON 的东西归一化,逐条判断,决定告诉谁 ——
并记录为什么,所以*「我怎么没被叫到」*有一个不靠猜的答案。

**已经先后在两家公司的生产环境跑了 8 个月**,累计处理上万条真实告警。下面的截图
和成本数字都取自当前在跑的这套环境,不是 benchmark,也不是 demo 种子数据。

自托管、MIT、一条 `docker compose up`。→
**[github.com/itswl/WebhookWise](https://github.com/itswl/WebhookWise)**

![总览:一屏看健康度](../img/01-overview.png)

---

## 每个告警工具都说自己降噪。这个能拿出证据说明降没降。

一个 agent 化的调查员会去读那些值得读的告警 —— 检索、关联、推理数分钟 —— 然后给出
一份带自己判定的严重度报告。这就构成了一份**没人标注过的标注集**,而第一次拿它
打分,结果并不好看:

| | |
| --- | --- |
| WebhookWise 判为 `high` 的告警 | **一周 330 / 367(90%)** |
| 其中被真正调查过、调查员也认同的 | **21 / 80(26%)** |

`high` 已经退化成「有条告警」的意思。于是闭环形成:校准脚本按告警规则给廉价判定
打分并提出上限建议,由人决定是否采纳;结果每周 **59% 的告警量**不再是 `high`。

**护栏比机制更重要** —— 如果调查员有超过三分之一的次数说它确实是 high,脚本就拒绝
建议降级,因为那种噪音必须靠把告警做得更具体来解决,而不是靠静音。

这就是整个项目的形状:**一个决定、一份「为什么」的记录、以及一条事后发现这个决定
错了的路径。**

![决策链:每一次「拦下」都有答案](../img/02-decision-trace.png)

---

## 可审计的抑制

八道闸门站在告警和人之间 —— 去重、静默、维护窗、风暴抑制、冷却、预算 —— 而每一次
「拦下」都有记录,所以一条静默规则可以*被打分*,而不是被信任。降噪中心把这些记录
读回来算成每条规则的 ROI:拦了多少、省了多少分钟、哪条是僵尸规则(90 天零匹配)。
新规则上线前先对历史数据回测。

线上环境上周的真实账单:**AI 花费 $5.55,靠缓存复用和「抑制已经答过的告警不再付费」
省下 $9.95**。

![降噪中心:每条静默规则都有 ROI](../img/03-noise-center.png)

---

## 可以直接问它问题

读侧通过 MCP 暴露 —— 20 个工具、一份使用指南资源和调查提示词 —— 任何 MCP 客户端都能
直接查询这个部署:

```bash
claude mcp add --transport http webhookwise \
  https://<your-host>/mcp/ \
  --header "Authorization: Bearer <API_KEY>"
```

接上之后可以直接问*「为什么 #923 没通知我?」*、*「这个班发生了什么?」*、
*「哪些规则可以删了?」*。仓库里带了四个现成技能:单条告警全链路调查、交接班简报、
降噪审计、可观测性排查。

**刻意只读** —— Agent 在这里调用的任何东西都不会改变这个部署的行为。唯一的写只是
记录一条待批提案,必须由持有写凭据的人批准。

![事故列表:相关告警自动聚合](../img/04-incidents.png)

---

## 可以从更小的开始

| | |
| --- | --- |
| **WebhookWise Lite** | 单容器、SQLite、无 Redis、约 800 行、四道抑制闸门。先感受一下再决定要不要上全量版。 |
| **全量版** | FastAPI + TaskIQ + PostgreSQL + Redis,全程 OpenTelemetry。`docker compose up -d`。 |

刻意不做:值班表和状态页。那是 Grafana OnCall 和状态页服务的地盘,把告警的「守门人」
这一件事做好就够了。

![内置教程页:一条告警的旅程](../img/06-guide.png)

---

## 继续读

- [仓库](https://github.com/itswl/WebhookWise) · [中文 README](https://github.com/itswl/WebhookWise/blob/main/README.zh.md)
- [它能做什么,有哪些旋钮](https://github.com/itswl/WebhookWise/blob/main/docs/capabilities.md)(英文) · [内部怎么工作](https://github.com/itswl/WebhookWise/blob/main/docs/architecture/system-overview.md)(英文)
- [为什么这么设计 —— 包括被否掉的方案](https://github.com/itswl/WebhookWise/tree/main/.agents/notes)(英文)
- [hookstack](https://itswl.github.io/hookstack/zh/) —— 小而专的那条路线,包括做深度调查的那个 agent runner
