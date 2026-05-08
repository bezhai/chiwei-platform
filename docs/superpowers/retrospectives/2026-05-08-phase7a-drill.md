# Phase 7a dev-phase7a Drill 复盘

**Date:** 2026-05-08
**Branch:** `refactor/dataflow-parse-7`
**Lane:** `dev-phase7a`
**Final image tag:** `1.0.1.8`

## 目标

在 dev-phase7a 泳道端到端验证 Phase 7a transport-layer primitive
(Gap 7.1 / 7.2 / 9 / 11)。drill 的核心价值：单测无法覆盖的「真实
broker / RabbitMQ x-delayed-message 插件 / 进程边界 / contextvar
传播」组合行为。

## 5 个 drill 全部通过

### Drill 1 — runtime_inflight state machine
真实 chat 链路触发 `ChatRequest::chat_node` + `PostSafetyRequest::run_post_safety`
两条 inflight 行。验证：
- per-edge isolation：两条 edge 独立 PK，互不 dedup
- claim → mark_succeeded → lease 字段清空（locked_until / worker_id）
- adoption mode (`Meta.existing_table='agent_responses'`) 跳过 insert_idempotent

### Drill 2 — retry transport (Gap 7.2)
临时 wire `DrillFailingRequest -> drill_failing_node`（`.durable().retry(n=3, base=200ms, max=2000ms)`）。
验证：
- attempts 1 → 2 → 3 序列正确
- exponential backoff 时序 (200ms → 400ms 实测)
- attempts==n 时进 DLQ 路径（state=failed 终态）
- trace_id 跨 retry 透传

### Drill 3 — lease take-over (Gap 7.1)
SQL 注入 stale processing 行 (`worker_id=fake-worker-A`, `locked_until=过去`)
模拟 worker 死亡。验证：
- claim_inflight 检测 lease 过期 → take-over 分支
- attempts 累加 (3 → 4 → 5 → 6, 因为 retry chain 也走 take-over)
- worker_id 在 take-over 时被替换，mark_failed 时清空
- spec 设计意图：inflight.attempts 反映「被 claim 总次数」，
  retry policy 用 message header `x-delivery-count`，两者解耦

### Drill 4 — history backfill (Gap 7.1.1)
SQL pre-INSERT data_drill_failing_request 行（无 inflight），
trigger 同 dedup_hash。验证：
- claim_inflight: row missing → INSERT processing fresh=True
- insert_idempotent ON CONFLICT(dedup_hash) → n=0
- mark_history_backfill: state=succeeded, attempts=0, trace_id='backfill'
- consumer **不被调用**（关键：避免 pre-7a Data 重复执行）

### Drill 5 — emit_delayed durable + best_effort (Gap 9)
临时 wire `DrillEchoRequest -> drill_echo_node` (.durable())，
admin endpoint 调 `emit_delayed(..., delay_ms=N, durability=...)`。验证：
- durable 路径：DelayedTriggerEnvelope 入 `runtime_delayed_trigger_agent-service_dev-phase7a`
  queue 带 `x-delay=5000` → broker 真实 hold 5s → trigger consumer
  unwrap → bind_context → emit() → fan-out
- best_effort 路径：`asyncio.sleep + emit`，3s 准时 fire
- **trace_id 跨 5s 异步边界透传**：HTTP header → emit_delayed
  contextvar → envelope.trace_id → trigger consumer bind_context
  → publish_durable inject_context → handler claim_inflight 写
  trace_id 字段。全程不断
- **关键**：单测里 `mq.publish_with_confirm` 被 mock，**这是首次
  真实跑通 RabbitMQ x-delayed-message 插件 + envelope round-trip**

## Drill 暴露并修复的 3 个 P0 真 bug

每个 bug 都是单测全过、生产会出问题的类型。

### Bug 1: main.py lifespan 漏注册 trigger consumer
**Commit:** `47f6bf3` `fix(runtime): register delayed-trigger wire in main.py lifespan`

agent-service HTTP 主进程经 FastAPI lifespan 启动，**不走** `Runtime.run()`。
Phase 7a Task 8 把 `register_runtime_trigger_wire(app)` 放在
`Runtime.run()` 里，导致 agent-service 进程**完全没注册 trigger
consumer** → emit_delayed durable 投出去的 envelope 没人消费。
vectorize-worker 走 `workers/runtime_entry.py → Runtime.run()`
未受影响。

drill 启动日志缺 `runtime_delayed_trigger wire registered` 直接暴露。

### Bug 2: mq source 缺 trace_id fallback (Gap 11 漏覆盖)
**Commit:** `262ed22` `feat(runtime): mq source auto-generates trace_id when header missing`

Phase 7a Task 3 给 cron / interval source 加了 auto-trace_id
(`cron:expr:uuid8`)；mq source 这个**外部消息入口**漏了。
lark-server publish CHAT_REQUEST 时不注入 trace_id header
（apps/lark-server/src/core/services/ai/reply.ts），agent-service
mq source `extract_context` 拿到 `trace_id=None` → 整条 chat 链路
runtime_inflight.trace_id = NULL → langfuse trace 断链。

drill 1 SELECT 看到 trace_id=null 暴露。Fix: mq source 也走
auto-generate fallback `mq:<queue_base>:<uuid8>`。

### Bug 3: retry 路径双 ack
**Commit:** `49995a8` `fix(runtime): retry handler must not double-ack via process() context`

`async with message.process(requeue=False)` context manager 在
clean exit 时 __aexit__ 自动 ack。retry transport 实现里多了一行
显式 `await message.ack()`，导致每次 retry 抛 `MessageProcessError:
Message already processed` traceback。功能上 retry 仍正确（消息
已 ack + republish），但每次 retry 抛 2 条 traceback，生产几百
qps retry 会把日志流冲爆。

drill 2 日志看到 MessageProcessError 暴露。Fix: 删 `await
message.ack()`，让 context manager 接管。同时给单测加
`caplog.at_level(logging.ERROR, logger="asyncio")` 抓 aio-pika
后台 task 异常的 regression assertion。

## 经验沉淀（已写入 memory）

1. `feedback_main_vs_runtime_run_dual_entry.md` — runtime
   bootstrap hook 必须同步加 main.py lifespan + Runtime.run()
2. `feedback_aio_pika_process_context_double_ack.md` —
   `async with message.process()` 内禁止手动 ack/nack；retry
   写法 publish 后直接 return

## 13 commits on branch (含 drill commits)

- d616e52 .. 9f4fcbe — Tasks 1-10 + spec/plan/CI gate
- 47f6bf3 — fix Bug 1 (main.py trigger register)
- 262ed22 — feat Bug 2 (mq source trace_id fallback)
- 0b5feea — DRILL drill_phase7a wiring (will revert)
- 49995a8 — fix Bug 3 (retry double-ack)
- 01de011 — DRILL emit_delayed surface (will revert)

## ship 前 cleanup

1. `git revert` drill commits 0b5feea + 01de011
2. mutation submit:
   - `DROP TABLE data_drill_failing_request`
   - `DROP TABLE data_drill_echo_request`
   - `DELETE FROM runtime_inflight WHERE edge_id LIKE '%Drill%'`
3. 重新 build + deploy dev-phase7a 验证 cleanup 后启动 OK
4. `/ship` (项目自有 skill，**不走** `superpowers:finishing-a-development-branch`)
5. unbind dev bot + undeploy lane
