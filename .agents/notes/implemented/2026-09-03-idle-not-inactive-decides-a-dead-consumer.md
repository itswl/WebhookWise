---
title: idle, not inactive, decides whether a stream consumer is dead
status: implemented
date: 2026-09-03
scope: core
---

## Decision

Worker startup reaps consumers from the webhook stream group: any consumer
other than this worker's own, idle for more than 24 hours, with zero pending
entries, is removed with `XGROUP DELCONSUMER`. The count is logged. Every Redis
error is swallowed — `reap_idle_stream_consumers()` returns 0 and the worker
starts.

Two guards decide who lives, and both are easy to get backwards:

- **`pending > 0` is skipped.** Deleting such a consumer discards its
  pending-entry list; those messages then need `XAUTOCLAIM` to come back. A
  reaper that deletes them is dropping work, not cleaning up.
- **`idle` is the liveness signal, not `inactive`.** `idle` counts from the last
  *attempted* read, so a live worker blocking on an empty stream keeps it near
  zero. `inactive` counts from the last *successful* read, and on a quiet queue
  it grows without bound — reaping on `inactive` would delete exactly the
  workers that are running. Redis before 7.2 does not report `inactive` at all.

The threshold is a module constant, not a setting.

## Why

TaskIQ's `RedisStreamBroker` registers one consumer per worker, named after
`WORKER_ID`, and never removes it. `WORKER_ID` changes across restarts, so every
deploy leaves a corpse. Production measured **127 consumers in
`webhook-processors`, one of them alive** — 126 ghosts of earlier deploys.
`XINFO CONSUMERS` is O(that number) and sits on the queue-health path.

Startup is the right moment: Redis is up, the group certainly exists, and the
work is bounded and infrequent. A periodic task would run this on a schedule
nobody needs.

The threshold is a constant because the only way this feature can cause harm is
by being set too LOW, and an operator has no reason to want that. A day is far
longer than any deploy gap; making it configurable would add a knob whose only
interesting setting is the dangerous one.

## Consequences

- Verified against a real Redis 7.4 (`tests/real_infra/test_redis_consumer_reap.py`,
  which lowers the threshold to 0 rather than waiting a day): the consumer
  holding an unacked entry survives, this worker survives, the acked stranger is
  removed, and the group's pending count is unchanged afterwards.
- A worker that restarts under its OLD `WORKER_ID` inherits that consumer's idle
  time. `keep=` is what stops it from deleting the registration it just made,
  and `tests/operations/test_stream_consumer_reap.py` pins that.
- The reaper adds no new metric. `record_redis_operation` already labels both
  calls, and a new instrument with no dashboard, alert or automated decision
  consuming it is exactly what AGENTS.md forbids. The count goes to the log.
- A group that has genuinely leaked consumers with pending entries — a worker
  killed mid-batch and never replaced — is NOT cleaned up here. That backlog is
  `XAUTOCLAIM`'s job, and conflating the two would hide it.
