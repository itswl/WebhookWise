# WebhookWise

[English](README.md) | **中文**

[![CI](https://github.com/itswl/WebhookWise/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/itswl/WebhookWise/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Latest release](https://img.shields.io/github/v/release/itswl/WebhookWise)](https://github.com/itswl/WebhookWise/releases)

*部署在监控系统与聊天工具之间的自托管告警智能层——去重、降噪、AI 分诊,以及一条能解释每次通知(或未通知)的决策轨迹。*

> 本文是英文 [README](README.md) 的精简中文版;完整文档(架构、运维、参考)以英文版为准。

WebhookWise 站在你的监控系统和聊天工具之间。它把 Prometheus、Grafana、
Alertmanager、飞书、或任何能 POST JSON 的东西归一化,逐条判断,决定告诉谁——
并记录为什么,所以「我怎么没被叫到」有一个不靠猜的答案。

自托管、MIT、一条 `docker compose up`。

## 它自己暴露出来的那个问题

每个告警工具都说自己降噪。这个能拿出证据说明降没降。

一个 agent 化的调查员会去读那些值得读的告警——检索、关联、推理数分钟——
然后给出一份带自己判定的严重度报告。这就构成了一份**没人标注过的标注集**,
而第一次拿它打分,结果并不好看:

| | |
| --- | --- |
| WebhookWise 判为 `high` 的告警 | **一周 330 / 367(90%)** |
| 其中被真正调查过、调查员也认同的 | **21 / 80(26%)** |

`high` 已经退化成「有条告警」的意思。而调查员是对的、廉价的关键词判定是错的——
它把 SES 退信规则判为 critical(AWS 真会停发),把业务信号类金额告警判为 medium。

于是闭环形成:[`scripts/ops/severity_calibration.py`](scripts/ops/severity_calibration.py)
按告警规则给廉价判定打分并提出上限建议,由人决定是否采纳;结果每周 **59% 的告警量**
不再是 `high`。**护栏比机制更重要**——如果调查员有超过三分之一的次数说它确实是
high,脚本就拒绝建议降级,因为那种噪音必须靠把告警做得更具体来解决,而不是靠静音。

这就是整个项目的形状:一个决定、一份「为什么」的记录、以及一条事后发现这个决定
错了的路径。

## 五分钟上手

1. 准备配置:

```bash
cp .env.example .env
```

至少替换 `API_KEY`(管理 API 只读令牌)、`ADMIN_WRITE_KEY`(管理写操作令牌)、`CHANGE_INGEST_TOKEN`(CI/CD 变更事件最小权限令牌)、`WEBHOOK_SECRET`(Webhook HMAC-SHA256 签名密钥兼入口令牌);启用 AI 分析时填写 `OPENAI_API_KEY`。完整配置见 [.env.example.all](.env.example.all);配置只在进程启动时读取,修改后需重启。

2. 启动完整本地栈:

```bash
docker compose up -d --build
curl http://localhost:8000/ready
```

3. 发送测试事件:

```bash
curl -X POST http://localhost:8000/v1/webhook \
  -H "Content-Type: application/json" \
  -d '{"alertname":"TestAlert","severity":"critical","host":"prod-01"}'
```

或经真实入口灌入五分钟演示数据(去重风暴、恢复、振荡身份、多厂商载荷):

```bash
python scripts/seed_demo_data.py --base-url http://localhost:8000
```

开箱即用的源格式:volcengine、Grafana、Prometheus Alertmanager、Datadog、PagerDuty、飞书卡片(代码适配器),以及 Zabbix、Uptime-Kuma、阿里云云监控、腾讯云监控、Jenkins、Sentry 的声明式 YAML 规格(`adapters/specs/`)——写一个 YAML 文件即可接入自己的简单源,见 [adapters/specs/README.md](adapters/specs/README.md)(英文)。

4. 打开入口:

| 入口 | 地址 |
| --- | --- |
| 仪表盘 | `http://localhost:8000/` 或 `http://localhost:8000/dashboard` |
| Swagger UI | `http://localhost:8000/docs` |
| ReDoc | `http://localhost:8000/redoc` |
| 健康检查 | `http://localhost:8000/live` / `http://localhost:8000/ready` |

## 接下来看哪里

完整文档以英文为准。

| | |
| --- | --- |
| 用一个容器试试这个想法 | [WebhookWise Lite](lite/README.md) — SQLite、无 Redis、约 800 行 |
| 它能做什么、有哪些旋钮 | [docs/capabilities.md](docs/capabilities.md) |
| 内部怎么运转 | [docs/architecture/system-overview.md](docs/architecture/system-overview.md) |
| 模型周围跑着什么 | [docs/architecture/ai-engineering.md](docs/architecture/ai-engineering.md) |
| 为什么这样设计——**包括被否掉的方案** | [.agents/notes/](.agents/notes/) |
| 其余全部 | [docs/README.md](docs/README.md) |
| 看 API | 启动后访问 `http://localhost:8000/docs`;导出说明见 [docs/reference/api.md](docs/reference/api.md) |
| 部署 | [Compose](deploy/compose/README.md) · [Kubernetes](deploy/k8s/README.md) |
| 参与开发 | [CONTRIBUTING.md](CONTRIBUTING.md) · [CHANGELOG.md](CHANGELOG.md) |

## 社区

- 贡献指南:[CONTRIBUTING.md](CONTRIBUTING.md)(英文;完整质量门禁一条命令 `bash scripts/gate.sh`)。
- 安全漏洞:请经 [GitHub Security Advisories](https://github.com/itswl/WebhookWise/security/advisories/new) **私密**报告,不要开公开 issue,见 [SECURITY.md](SECURITY.md)。
- Bug 与功能需求:[GitHub Issues](https://github.com/itswl/WebhookWise/issues)。
- 行为准则:[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
- 文档入口:[docs/README.md](docs/README.md)(英文)。

## 许可证

MIT License——见 [LICENSE](LICENSE)。
