# DLQ 排查与 Replay Runbook

**触发时机**：`DeadLettersBacklog` 告警（`dead_letters` 队列 messages_ready > 0 持续 1 分钟）。

## 背景

dataflow runtime 是 **fail-to-DLQ** 设计，消费者抛异常时走 `process(requeue=False)` → DLX `post_processing_dlx` → DLQ `dead_letters`。**没有自动 retry**。一旦消息进入 DLQ，必须由 operator 决定 replay / 丢弃 / 修代码后再 replay。

相关文件：
- `apps/agent-service/app/infra/rabbitmq.py` — DLX/DLQ 拓扑
- `apps/agent-service/app/runtime/durable.py` — durable 边的失败语义
- `apps/agent-service/app/runtime/engine.py` `_source_loop_mq` — MQ source 的异常→DLX 路径

## 第一步：定位是哪条管线挂的

DLQ 里每条消息的 `x-death` header（aio-pika `IncomingMessage.headers["x-death"]`）记录原 queue + routing key + 异常时刻。同时 Loki 里有对应日志：

```bash
make logs APP=agent-service KEYWORD="DLX'd"  SINCE=1h
make logs APP=vectorize-worker KEYWORD="DLX'd" SINCE=1h
```

关键日志 pattern（`engine.py:510`）：

```
mq source <queue>: target <node> message DLX'd ...
```

或者 durable consumer 抛错（`durable.py`）。日志里会带 message_id / fragment_id 等业务键，足够还原是哪条业务记录。

如果日志拿不到原始 body，需登 RabbitMQ Management UI（仅在 K8s 集群内可达，开发机走 `make logs` 看不到队列内容；如果没有 UI 通路，**这是后续要补的 admin endpoint：见末尾**）查看 `dead_letters` 队列里每条消息的 payload。

## 第二步：判断该不该 replay

| 场景 | 操作 |
|---|---|
| 临时性故障（DB / Qdrant / LLM API 超时） | replay |
| 已知 bug 已修复并 ship | replay |
| Payload 永久无效（消息引用的 row 被删 / id 不存在） | **丢弃** |
| Idempotency 已通过其他路径生效（eg. qdrant point_id 已存在） | **丢弃**（重复 replay 仅在浪费配额） |
| 同一类错误在 DLQ 大量堆积 | **先停 replay**，回到代码修根因，否则 replay 后再次进 DLQ |

## 第三步：Replay

### 当前可用的方式

**RabbitMQ Management UI（运维侧）**：进 `dead_letters` 队列页面 → "Move messages" → 目标 exchange `post_processing` + routing key 写原 routing key（从 `x-death` header 拿）。

### 暂未自动化的部分（待补）

agent-service 没有直接的 `POST /admin/dlq/replay` endpoint。如果 DLQ 频繁堆积、UI 操作繁琐，应当加一个 admin endpoint：

```
POST /internal/admin/dlq/replay
  body: { "limit": N, "filter": { "original_queue": "..." } }
```

实现要点：从 `dead_letters` consume，按 `x-death` header 重新 `mq.publish` 到原 queue + routing key，acked 后从 DLQ 移除。错误的消息（payload 损坏、原 queue 不存在）单独丢到 `dead_letters_archive` 留证。

## 第四步：复盘 + 关闭告警

1. 在飞书 `#chiwei-incidents` 或对应频道贴出：触发原因、影响范围、replay/丢弃决策、根因修复 PR
2. 等 `dead_letters` 队列回到 0，告警自动 resolve
3. 如果根因是 dataflow @node 抛异常（而非基础设施抖动），考虑写一个回归 test 在 `apps/agent-service/tests/nodes/` 覆盖

## 已知 DLQ 触发模式

PR #198 dataflow runtime 上线后，DLQ 主要来源：

- `Source.mq` 解码失败：JSONDecodeError / ValidationError / TypeError / UnicodeDecodeError → `engine.py` re-raise 出 `process(requeue=False)`，body 直接进 DLQ
- durable consumer 抛异常：`durable.py` 上的 wrapper 不 swallow 业务异常，原样进 DLQ
- @node 内部异常：`emit` 链上任何 raise 都会冒泡到 source loop 进 DLQ

排查时优先看 source / consumer / @node 三层日志的最近一条 ERROR。
