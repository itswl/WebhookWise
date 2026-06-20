---
title: Flink 元数据存储桶告警与影响说明
service: Flink
project: common-infra
tags: [flink, checkpoint, oss, bucket, storage, runbook]
owner: 实时计算组 @realtime
source_ref: 内部 Wiki / Flink 作业运维
---

# Flink 元数据存储桶告警与影响说明

## 适用告警
- 存储桶（如 volc-flink-meta-*）错误率升高、访问异常
- 来源：对象存储/云监控，project=common-infra
- 典型：bucket 错误率 36% 等

## 这是什么
Flink 作业用对象存储桶保存 **checkpoint、状态快照和元数据**。该桶异常会影响：
- checkpoint 保存失败 → 作业无法记录进度
- 状态恢复失败 → 故障重启时丢状态或恢复慢
- 元数据查询异常 → 作业调度/管理受影响

**注意**：桶错误率高**不一定立即影响核心业务流量**，但会导致 Flink 任务频繁重试、性能下降、状态不一致，是需要关注但通常非 P0 的问题（除非 checkpoint 完全失败导致作业挂掉）。

## 处置步骤
1. 看错误类型：限流（降低 checkpoint 频率/并发）vs 权限（查 AK/SK、桶策略）vs 桶不可用（联系存储团队）。
2. 评估作业影响：作业是否还在正常 checkpoint？最近一次成功 checkpoint 时间？
3. 缓解：临时调大 checkpoint interval 降低对桶的压力；必要时切换备用桶。
4. 升级：桶持续不可用且作业开始失败 → 联系实时计算组 @realtime + 存储团队。

## 负责人与升级路径
- 一线：实时计算组 @realtime
- 存储侧：对象存储团队

## 备注
判定要点：区分"桶错误率高但作业仍在 checkpoint"（关注级）与"checkpoint 持续失败/作业已挂"（高优）。摘要里应带上桶名、项目、错误率，便于定位。
