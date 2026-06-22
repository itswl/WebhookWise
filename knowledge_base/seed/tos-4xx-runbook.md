---
title: 对象存储桶 4xx 错误占比告警处置预案
service: 对象存储 TOS
tags: [tos, oss, bucket, 4xx, storage, runbook]
owner: 存储与中间件组 @infra-storage
source_ref: 内部 Wiki / 对象存储运维预案
---

# 对象存储桶 4xx 错误占比告警处置预案

## 适用告警
- 指标：对象存储桶 4xx 状态码占比超过阈值（默认 5%）
- 常见资源桶：`volc-flink-meta-*`（Flink checkpoint 元数据）、`cyberclone-cn-prod-object` / `cyberclone-cn-dev-object`、`common-prod-object`、`eve-cn-prod-object`
- 典型文案：「对象存储桶 X 的 4xx 状态码占比达到 N%，远超 5% 阈值」

## 这是什么
4xx 是客户端侧错误，绝大多数不是存储服务故障，而是**调用方**的问题：403（鉴权/AK 失效/桶策略）、404（对象不存在/路径写错）、400（请求格式）。占比突增通常意味着某个调用方在反复请求不存在的对象，或凭证刚失效。**注意区分桶用途**：`*-dev-object`、`volc-flink-meta-*` 这类的 4xx 多为非关键路径噪声；`*-prod-object` 业务桶持续高 4xx 才需要重点关注。

## 处置步骤
1. 看是哪种 4xx：到 TOS 控制台或访问日志按状态码拆分（403 vs 404 vs 400）。
2. **403 为主**：检查调用方 AK/SK 是否过期、桶 Bucket Policy / 跨账号授权是否被改、是否新上线服务没配权限。
3. **404 为主**：多为调用方请求了已删除/未上传的对象，或路径前缀写错；定位来源 IP/UA，通知对应业务方修调用逻辑。通常**不需要存储侧动作**。
4. **flink-meta 桶**：4xx 往往来自 checkpoint 生命周期内的探测性请求，结合 [[Flink 元数据存储桶告警与影响说明]] 一起看；若 Flink 作业本身健康，多可观察。
5. 确认是否误报：dev 桶或低流量桶分母小，几次 4xx 就能把占比顶高，结合绝对请求量判断。

## 负责人与升级路径
- 一线：存储与中间件组 @infra-storage
- 调用方问题：转对应业务负责人（cyberclone → 业务后端组；flink-meta → 实时计算组 @realtime）
- 升级：prod 业务桶持续 100% 4xx 且影响在线读写 → P0；dev/元数据桶 → 工作时间内处理。

## 备注
该类告警来自 volcengine（火山引擎云监控），高频且多为噪声，建议对确认无影响的 dev/元数据桶配置静默规则收敛（参考静默收益面板确认拦截效果）。
