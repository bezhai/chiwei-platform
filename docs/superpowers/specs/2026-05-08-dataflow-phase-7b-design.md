# Dataflow Phase 7b — Reliability + Error Policy + Outbox 设计

**状态**: Draft v3（2026-05-09，吸收 reviewer 二轮 findings：dispatcher 改 emit() fan-out 一致化 / helper contract 明确 handled-return + dlq-raise / publish-confirm 失败 fall-through DLQ / 迁移分类表按"本函数是否持有事务并 commit 后 emit"重做 / DLQ requeue 6-step transaction-like 协议 / at-least-once 措辞）
**前置**: PR #212（Phase 7a transport primitives）已 ship 到 prod 1.0.1.10，Gap 7 / 9 / 11 闭合
**承接**: `docs/superpowers/specs/2026-05-08-dataflow-phase-7-gap-analysis.md` §2 中 Gap 8 / 12 / 18
**分支**: `refactor/flow-parse-7b`
**后续**: 7c (Gap 15 — arq 退场) / 7d (Gap 13/14/16 — DB/Redis/HTTP capability) / 7e (Gap 10/17/19 — streaming / async join / lifecycle)

---

## 0. 上下文承接

7a 已 ship 三件事（PR #212）：
- **Gap 7**：durable wire retry — `runtime_inflight` 状态机 + lease + history backfill；`publish_with_confirm` + `wire(...).durable().retry(n, backoff, lease_ms)` DSL
- **Gap 9**：`emit_delayed` / `emit_at` 顶层 API（durable + best_effort 双路径）+ `runtime_delayed_trigger_{app}` queue
- **Gap 11**：propagation primitive（trace_id / lane / origin_app extract / inject / bind），cron / interval / mq source 自动 trace_id

7b 在这之上加 **三层 reliability 表达力**：
- **Gap 18 — error policy DSL**：业务区分四种失败语义（dlq / ignore-duplicate / manual-review，retry 通过保留的 `.retry()` 控制）
- **Gap 12 — DLQ replay**：admin endpoint + audit + Makefile target + runbook
- **Gap 8 — outbox**：`transactional_emit` 把「DB 写 + emit」原子化，dispatcher 后台扫表

业务作者只动**节点和 wire 声明**；runtime 内部新增一张表（`runtime_outbox`）、一张审计表（`runtime_dlq_audit`）、一个 dispatcher loop、一类 manual-review queue、4 个 admin endpoint、3 个 Makefile target、1 篇 runbook。

---

## 1. 架构总览

```
业务节点                                        7a 已 ship                    7b 新增
─────────────                                   ─────────────                ─────────────
async def my_node(data, *, session):
    await session.execute(...)            ┐
    async with transactional_emit(s) as e: │  durable wire                ✅ runtime_outbox 表
        await e.append(MutationDone(...))  │ → publish_with_confirm        + transactional_emit
    raise DuplicateData(...)               │ → runtime_inflight 状态机     + dispatcher loop (调 emit)
    raise NeedsReview(...)                 │ → retry / nack                ✅ on_error policy
                                           │                               + DuplicateData / NeedsReview
                                           │                               + manual-review queue
                                           │
                                           │  DLQ                          ✅ 4 个 admin endpoint
                                           │ → 现在只能手工 SQL replay    + Makefile target
                                                                            + runtime_dlq_audit + runbook
```

**数据流（mutation case）**

```
node ──╮
       │ session.execute(UPDATE/INSERT)
       │ outbox.append(data) ───────► runtime_outbox row (state=pending)
       │ session.commit()
       ▼
  business commit OK ──► dispatcher loop scans pending rows (FOR UPDATE SKIP LOCKED)
                         ──► bind_propagation_from(row) + emit(data)  ◄ 走完整 wire fan-out
                         ──► outbox row state=dispatched
```

**数据流（error case）**

```
node raise DuplicateData → wire on_error="ignore-duplicate" → log warning, ack, no DLQ
node raise NeedsReview   → wire on_error="manual-review"   → publish to <queue>_review, ack
node raise Exception     → wire .retry() retries → on_error="dlq" (默认) → DLQ
```

---

## 2. Gap 18 — error policy DSL

### 2.1 DSL

```python
wire(SomeData).durable().on_error("dlq")              # 默认（兼容现状）
wire(SomeData).durable().on_error("ignore-duplicate") # raise DuplicateData → ack + log warning
wire(SomeData).durable().on_error("manual-review")    # raise NeedsReview → 进 review queue + ack
wire(SomeData).durable().retry(3).on_error("dlq")     # retry 3 次后进 DLQ（显式化）
```

`.on_error()` 默认值 `"dlq"`，存到 wire spec；handler 拿到 spec 后按 policy 分发。

**与 spec §7b 字面差异**：原 spec 列了 4 种 policy（dlq / retry / ignore-duplicate / manual-review），本设计**取消 `"retry"` 作为 on_error 值**，retry 完全由保留的 `.retry(n, backoff, lease_ms)` 旋钮控制。理由：retry 是过程，on_error 是结局；混在一起表达力反而下降。spec 母文档相应更新。

### 2.2 typed exception

`runtime/errors.py` 新增两个**业务可 raise** 的异常基类：

```python
class DuplicateData(Exception):
    """节点检测到本条 data 是业务级重复（idempotent dedup 之外的重复）。

    framework 行为：
      - on_error="ignore-duplicate" → ack + log warning + 不进 DLQ 不重试
      - on_error 其他值 → 走对应分支（不特殊待）
    """

class NeedsReview(Exception):
    """节点判定本条 data 需要人工审，不能自动 retry/DLQ。

    framework 行为：
      - on_error="manual-review" → 转发到 manual-review queue + ack
      - on_error 其他值 → 走对应分支（不特殊待）
    """
```

业务 raise 这两个异常**只在配套的 on_error 下生效**；mismatch 时按 on_error policy 走（不报错），保持组合自由。

### 2.3 manual-review queue

per-data per-consumer 一条独立 queue：`durable_<data_snake>_<consumer>_review`，**不绑定 DLX**（review queue 是终点，不会自动 retry）。

消息 body 包含原 data envelope + 失败上下文（trace_id / first_failed_at / last_error / attempts），保留运维裁决（手动重投 / 标记忽略 / 删）。`/admin/dlq/inspect` 同样支持查 review queue（参数 `queue_kind` 区分）。

### 2.4 handler 路由（durable.py 改造）

现状（`runtime/durable.py:217-271`）：

```python
try:
    await consumer(data, ...)
except Exception as exc:
    mark_failed(...)
    action = decide_retry(...)
    if action == "retry":
        publish_with_confirm(retry_envelope)
        return  # ack
    raise  # → message.process(requeue=False) → DLX → DLQ
```

7b 改造（单一 except + 显式 dispatch helper，Python 语法 except 之间不会级联）：

**Helper contract**（避免「不 raise」与现实矛盾）：
- **handled** 路径（已决议 ack，inflight 终态写入完成）→ helper `return`
- **DLQ** 路径（最终走 caller 的 `async with message.process(requeue=False)` nack→DLX）→ helper `raise original exc`
- **publish_with_confirm 失败**（retry envelope / review queue publish 不 confirmed）→ fall-through 到 DLQ raise，跟 7a `durable.py:248-271` 同语义
- helper **从不**手动 ack/nack message（项目 memory `feedback_aio_pika_process_context_double_ack`：`async with message.process()` 内禁止手动 ack/nack）

```python
try:
    await consumer(data, ...)
    await mark_succeeded(inflight_key=ik)        # 无异常 → succeeded
except Exception as exc:
    await _route_consumer_exception(
        exc, wire=w, inflight_key=ik,
        data=data, attempts=attempts,
    )
    # handled 路径：helper return，本 try 块清洁退出，caller 的 process(__aexit__) ack
    # DLQ 路径：helper 已 raise，caller 的 process(__aexit__) nack→DLX→DLQ

async def _route_consumer_exception(exc, *, wire, inflight_key, data, attempts):
    """所有路径必须明确更新 inflight 终态。
    return = handled (ack)；raise = 降级 DLQ (nack)。
    """
    # 1. typed exception 在配套 policy 下生效
    if isinstance(exc, DuplicateData) and wire.on_error == "ignore-duplicate":
        log.warning("duplicate ignored", trace_id=trace_id_var.get(), reason=str(exc))
        await mark_succeeded(inflight_key)        # 业务等价"已处理"
        return                                     # ack
    if isinstance(exc, NeedsReview) and wire.on_error == "manual-review":
        confirmed = await publish_to_review_queue(data, exc, attempts, last_error=str(exc))
        if not confirmed:
            log.warning("review queue publish-confirm failed, falling through to DLQ")
            await mark_failed(inflight_key, last_error=str(exc))
            raise exc                              # DLQ 兜底
        await mark_review(inflight_key)            # inflight state='review'（新 state，扩 enum）
        return                                     # ack

    # 2. 通用路径（含 typed exception 在 mismatch policy 下降级）
    await mark_failed(inflight_key, last_error=str(exc))
    action = decide_retry(attempts=attempts, exc=exc, retry_spec=wire.retry)
    if action == "retry":
        confirmed = await publish_with_confirm(retry_envelope_for(wire, data, attempts + 1))
        if not confirmed:
            log.warning("retry publish-confirm failed, falling through to DLQ")
            raise exc                              # DLQ 兜底（同 7a durable.py:265-271）
        return                                     # retry envelope 已确认入 broker → ack
    if wire.on_error == "manual-review":
        # 配 .retry(n).on_error("manual-review")：retry 耗尽进 review
        confirmed = await publish_to_review_queue(data, exc, attempts, last_error=str(exc))
        if not confirmed:
            log.warning("review queue publish-confirm failed, falling through to DLQ")
            raise exc                              # DLQ 兜底
        await mark_review(inflight_key)            # 终态 failed → review
        return                                     # ack

    # 3. 默认 on_error="dlq"
    raise exc                                      # → caller 的 message.process(requeue=False) → DLX → DLQ
```

**inflight 状态扩展**：`runtime_inflight.state` 现在 `{processing, succeeded, failed}`，7b 加 `review`。state 字段是 TEXT 不需要 schema 改动（inflight.py:55），只是 helper / mark_review 的实现增量。

> **mismatch 处理（语义补充）**：DuplicateData / NeedsReview 在 mismatch on_error 下**降级到通用 Exception 路径**——走 mark_failed + decide_retry + on_error 默认。这意味着 `raise DuplicateData(...)` 在 `on_error="dlq"` 配置下不会静默 ack，会跟普通异常一样进 DLQ。安全默认：业务作者错配获得"消息保留可观察"，而非隐式丢消息。

### 2.5 业务侧迁移

唯一已知泄漏点 `nodes/life_dataflow.py:305-306`（注释 `# 不要 try/except`）：删注释 + wire 声明加 `.on_error("dlq")` 显式化（语义不变）。新业务作者按需选 policy。

---

## 3. Gap 12 — DLQ replay

### 3.1 4 个 admin endpoint

`wiring/admin.py` 新增（节点实现 `nodes/dlq_admin.py`）：

| Endpoint | Method | 入参 | 出参 |
|---|---|---|---|
| `/admin/dlq/inspect` | POST | `{queue, limit=20, queue_kind="dlq"\|"review"}` | `[{message_id, trace_id, data_type, payload, first_failed_at, last_error, attempts}, ...]` |
| `/admin/dlq/clear-idempotent` | POST | `{by: "trace_id" \| "edge_idempotent", trace_id?, edge_id?, idempotent_key?}` | `{deleted: N}` |
| `/admin/dlq/dry-run` | POST | `{queue, limit=20, queue_kind}` | `{plan: [{message_id, will_clear_idempotent: bool, target_queue}, ...]}` |
| `/admin/dlq/requeue` | POST | `{queue, queue_kind, limit=20, clear_idempotent=false}` | `{requeued: N, audit_id}` |

`queue_kind` 区分 DLQ / manual-review（同一组接口复用）。

### 3.2 实现要点

**inspect**：调 RabbitMQ Management HTTP API `GET /api/queues/{vhost}/{queue}/get`，参数 `ackmode=ack_requeue_true`（peek 不消费）。解 envelope 拿 trace_id / data_type / attempts；`first_failed_at` 不在 envelope 里，按 trace_id JOIN `runtime_inflight` 取 `created_at` 当 first_failed_at（7a 状态机已 ship，inflight 行在 first attempt 时 INSERT）。如果 inflight 行已被 clear，first_failed_at 字段返回 null。

**clear-idempotent**：在 `runtime/inflight.py` 新增 `delete_inflight(*, by, trace_id=None, edge_id=None, idempotent_key=None) -> int`（7a 故意没做，留给 7b）。两种入参模式：
- `by="trace_id"`：按 trace_id 删该 trace 下所有 inflight 行（适合「整条业务流重投」）
- `by="edge_idempotent"`：按 `(edge_id, idempotent_key)` 复合 PK 精确删一行（适合「单条消息重投」）

endpoint 调它，互斥参数（同时给会报 400）。

**dry-run**：跑 inspect + 模拟 clear（不真改 DB / queue），给运维看 replay 计划。

**requeue（transaction-like 协议）**：管理 API 的 `/get` 模式不能做写操作（reviewer P0-3 — 消费后任何一步失败都丢消息）。改用 AMQP `basic_get(no_ack=False)`，按以下顺序操作，每步都有失败回滚路径：

```
1. msg = basic_get(no_ack=False)             # message 进 unack 状态，未 ack 不丢
2. INSERT runtime_dlq_audit (status=cleared, recovery_token=msg_id, ...)  # 必须先于改状态
3. delete_inflight(...)                      # 清 idempotent，让 consumer 不 dedup
4. publish_with_confirm(原 queue, envelope)  # 重投；从 envelope 重建 propagation
5. UPDATE runtime_dlq_audit SET status='requeued'
6. msg.ack()                                 # 真正消费走 DLQ 消息
```

失败处理：
- step 4 失败 → `msg.nack(requeue=True)` 还原 DLQ 消息；audit row 标 status='publish_failed' + recovery_hint。consumer 端不会 dedup（idempotent 已清 + 消息回 DLQ），下次 replay 直接走 step 4，等价幂等。
- step 5/6 失败 → broker unack 超时（默认 30 min）后自动 requeue；下游 consumer 会再次收到——consumer 端 inflight dedup 兜底（7a 状态机），不会重复处理。

**关键性质**：`clear_idempotent` 必须先于 `publish_with_confirm`（顺序反了，consumer 收到消息时 idempotent 还在，会被 dedup 跳过——退化回 7a 之前的"replay 默认 no-op" bug）。这是协议级硬性顺序，不可换。

### 3.3 audit 表

```sql
CREATE TABLE runtime_dlq_audit (
  id BIGSERIAL PRIMARY KEY,
  action TEXT NOT NULL,             -- 'requeue' / 'clear-idempotent'
  status TEXT NOT NULL,             -- 'cleared' / 'requeued' / 'publish_failed'（多 step 协议状态）
  queue TEXT,
  queue_kind TEXT,                  -- 'dlq' / 'review'
  message_ids JSONB,                -- ["..."]
  recovery_token TEXT,              -- DLQ 消息 id，publish 失败时让运维知道哪条 unack pending
  recovery_hint TEXT,               -- 失败原因 + 下一步建议
  cleared_inflight_count INT,
  requeued_count INT,
  operator TEXT,                    -- 来自 X-Operator header（运维姓名 / "system"）
  trace_id TEXT,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX runtime_dlq_audit_queue_idx ON runtime_dlq_audit (queue, created_at DESC);
CREATE INDEX runtime_dlq_audit_status_idx ON runtime_dlq_audit (status) WHERE status != 'requeued';
```

**写时机**：requeue + clear-idempotent 必写；inspect / dry-run 不写（只读不算审计目标，避免噪声）。

### 3.4 RabbitMQ Management 凭据

走 ConfigBundle（基础设施连接惯例）：
- `RABBITMQ_USER` / `RABBITMQ_PASSWORD`：复用现有 AMQP 账号（同账号同密码）
- `RABBITMQ_MANAGEMENT_PORT`：默认 15672

理由：基础设施连接归 ConfigBundle 是项目惯例（CLAUDE.md「基础设施连接（DB/Redis）走 ConfigBundle」），dynamic config 留给业务行为参数。

### 3.5 Makefile target（顶层 Makefile）

```makefile
dlq-inspect:  ## DLQ inspect: QUEUE=<name> [LIMIT=20] [KIND=dlq|review]
	@scripts/http.sh POST $(PAAS_API)/admin/dlq/inspect \
	  -d '{"queue":"$(QUEUE)","limit":$(or $(LIMIT),20),"queue_kind":"$(or $(KIND),dlq)"}'

dlq-replay:   ## DLQ replay: QUEUE=<name> [LIMIT=N] [CLEAR=true]
	@scripts/http.sh POST $(PAAS_API)/admin/dlq/requeue \
	  -H "X-Operator: $$(git config user.name)" \
	  -d '{"queue":"$(QUEUE)","queue_kind":"dlq","limit":$(or $(LIMIT),20),"clear_idempotent":$(or $(CLEAR),false)}'

dlq-dry-run:  ## DLQ dry-run: QUEUE=<name> [LIMIT=20] [KIND=dlq|review]
	@scripts/http.sh POST $(PAAS_API)/admin/dlq/dry-run \
	  -d '{"queue":"$(QUEUE)","limit":$(or $(LIMIT),20),"queue_kind":"$(or $(KIND),dlq)"}'
```

### 3.6 runbook

`docs/runbooks/dlq-replay.md` 覆盖：
- DLQ 出现的场景判断（业务 bug / 临时故障 / 协议变更）
- 决策树：要不要 clear-idempotent / 要不要 dry-run 先看
- 完整 replay 流程示例
- 失败回滚（step 4 publish 失败时 nack 还原 + status='publish_failed' audit row 排查路径）
- manual-review queue 的处理方式

---

## 4. Gap 8 — outbox

### 4.1 表 schema

```sql
CREATE TABLE runtime_outbox (
  id BIGSERIAL PRIMARY KEY,
  data_type TEXT NOT NULL,            -- 全限定名：e.g. "app.domain.agent_tool_events.AbstractMemoryCommitted"
  payload_json JSONB NOT NULL,        -- data.model_dump(mode='json')，dispatcher 反序列化后调 emit(data)
  origin_app TEXT NOT NULL,           -- emit() 触发时的 APP_NAME（dispatcher 在该 app 下重建 propagation）
  lane TEXT,                          -- emit() 触发时的 lane_var.get()
  trace_id TEXT,                      -- emit() 触发时的 trace_id_var.get()，冗余便于查询
  state TEXT NOT NULL DEFAULT 'pending',  -- pending / dispatched
  attempts INT NOT NULL DEFAULT 0,
  last_error TEXT,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),  -- backoff 后重试时机
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  dispatched_at TIMESTAMPTZ
);
CREATE INDEX runtime_outbox_pending_idx ON runtime_outbox (state, next_attempt_at)
  WHERE state = 'pending';
CREATE INDEX runtime_outbox_trace_idx ON runtime_outbox (trace_id) WHERE trace_id IS NOT NULL;
```

partial index 让 dispatcher 扫描成本只跟未 dispatched 行数挂钩。dispatched 行清理（保留 7 天）独立 cron node，**不在 7b 范围**（7b+ 任务）。

### 4.2 业务 API：`transactional_emit`

`runtime/outbox.py`：

```python
class OutboxEmitter:
    """事务内 append outbox 行；commit 后由 dispatcher 异步 emit。"""
    def __init__(self, session: AsyncSession): ...
    async def append(self, data: Data) -> None:
        # 1. 取当前 propagation context：origin_app=APP_NAME, lane=lane_var.get(), trace_id=trace_id_var.get()
        # 2. payload_json = data.model_dump(mode='json')
        #    data_type = f"{cls.__module__}.{cls.__qualname__}"
        # 3. session.execute(INSERT runtime_outbox (data_type, payload_json, origin_app, lane,
        #                                          trace_id, state='pending'))
        # 4. 不 commit —— 由调用方 session 上下文管理

@asynccontextmanager
async def transactional_emit(session: AsyncSession) -> AsyncIterator[OutboxEmitter]:
    yield OutboxEmitter(session)
    # 不做 commit / rollback —— 由调用方 session 上下文管理
```

业务写法：

```python
async def commit_abstract_node(data: SomeTrigger):
    async with get_session() as s:
        await insert_abstract_memory(s, ...)
        for fid in supported_by_fact_ids or []:
            await insert_memory_edge(s, ...)
        async with transactional_emit(s) as emitter:
            await emitter.append(AbstractMemoryCommitted(abstract_id=aid, ...))
        # session.commit() 由 get_session() context exit 触发
        # outbox row 与业务行同事务可见
```

`emit()` 顶层 API **不动**（保留给 source / framework 内部用 — cron / debounce / handler retry envelope 等不需要事务保证的场景）。**business mutation node 一律改用 transactional_emit**。

### 4.3 dispatcher loop

**关键设计选择**（接受 reviewer P0-1 + P0-2）：dispatcher **必须调 `emit(data)`** 走完整 wire fan-out（in-process / durable / debounce / sink 四类边都覆盖），**不能**直接 `route_for(data_type) + mq.publish_with_confirm`——后者只覆盖 durable wire 一种，且 durable route 是 `(data_type, consumer)` per-consumer 一条 queue（durable.py:72，`_route_for(w, consumer)`）。

In-process wire 例子（agent_tool_events.py:15）：`AbstractMemoryCommitted` 没有 `.durable()`，`route_for(data_type)` 根本没有这种 route——直接走 mq publish 会丢。

`runtime/outbox_dispatcher.py`：

```python
async def dispatcher_loop(*, batch_size: int = 32, idle_sleep_ms: int = 200):
    while not shutdown_requested():
        async with AsyncSessionLocal() as s:
            rows = await s.execute(text("""
                SELECT id, data_type, payload_json, lane, trace_id, origin_app
                FROM runtime_outbox
                WHERE state = 'pending'
                  AND next_attempt_at <= now()
                  AND origin_app = :app    -- 只发自己 APP_NAME 的 outbox row
                ORDER BY id
                LIMIT :n
                FOR UPDATE SKIP LOCKED
            """), {"n": batch_size, "app": _current_app()})
            for row in rows:
                try:
                    # 1. 重建 origin app/lane/trace propagation context
                    # 2. 反序列化 payload_json 到原 Data 类（按 data_type 解 module.Class）
                    # 3. 调 emit(data) 走 wire fan-out（in-process / durable / debounce / sink）
                    with bind_propagation_from_payload(row):
                        data = deserialize_data(row.data_type, row.payload_json)
                        await emit(data)
                    await s.execute(text("""
                        UPDATE runtime_outbox
                        SET state='dispatched', dispatched_at=now()
                        WHERE id = :id
                    """), {"id": row.id})
                except Exception as exc:
                    await s.execute(text("""
                        UPDATE runtime_outbox
                        SET attempts = attempts + 1,
                            last_error = :err,
                            next_attempt_at = now() + (interval '5 seconds' * power(2, attempts))
                        WHERE id = :id
                    """), {"id": row.id, "err": str(exc)[:500]})
            await s.commit()
        if not rows:
            await asyncio.sleep(idle_sleep_ms / 1000)
```

**关键性质**：
- **dispatcher 唯一调度入口是 emit()**：保留 emit 作为唯一 fan-out 入口，dispatcher 不需要懂 wire 拓扑变化
- **不需要 recursion guard**：emit() 本身**不写 outbox**（写 outbox 的只有 `transactional_emit.append`），所以 dispatcher → emit → in-process consumer 内部如果再调 transactional_emit，那是新业务事务的合理写入，不会循环
- `FOR UPDATE SKIP LOCKED`：多 pod 安全，行被锁的 pod 跳过即可
- backoff：失败 `5s * 2^attempts`（attempt=0 立即；1=5s；2=20s；3=45s；…）
- **永不主动判 fail**：dispatcher 永远 retry（broker 故障最终会恢复），到达上限的运维侧 alert（监控信号 `runtime_outbox.attempts > 10` 计数）
- propagation context 从 row 重建（dispatcher 跟原业务调用栈解耦）—— `origin_app` / `lane` / `trace_id` 已在 §4.1 schema 中。dispatcher SELECT 时按 `origin_app = APP_NAME` 过滤：每个 deployment（agent-service / arq-worker / vectorize-worker，APP_NAME 不同）的 dispatcher 只发自己 app 的 outbox row

**at-least-once 性质**（接受 reviewer P1-2）：dispatcher 是「先 emit 成功再 update DB」——publish/in-process 完成后 UPDATE 到 dispatched 之间崩溃，下一个 dispatcher loop 会再次抓到该 row 重发。consumer 端 `runtime_inflight` 状态机（7a 已 ship）按 `(edge_id, idempotent_key)` 兜底 dedup，业务效果一次。drill 验收措辞要明确这一点（§6.4 drill G）。

### 4.4 lifecycle 接入（双入口）

按记忆 `feedback_main_vs_runtime_run_dual_entry`，dispatcher 必须同时挂 `Runtime.run()` 和 `main.py` lifespan，否则 agent-service 主进程不生效。

```python
# runtime/runtime.py
async def run(self):
    ...
    self._outbox_dispatcher_task = asyncio.create_task(dispatcher_loop())
    ...

# app/main.py lifespan
async def lifespan(app):
    ...
    runtime._outbox_dispatcher_task = asyncio.create_task(dispatcher_loop())
    yield
    runtime._outbox_dispatcher_task.cancel()
```

**部署形态**：所有 agent-service 镜像产出的 pod（agent-service / arq-worker / vectorize-worker，APP_NAME 不同）都启 dispatcher。多个 dispatcher 共享同一张 `runtime_outbox` 表，靠 `FOR UPDATE SKIP LOCKED` 协调；按 `origin_app` 过滤保证每个 deployment 只发自己产生的 row。

### 4.5 业务调用方迁移清单（接受 reviewer P1-1 + P1-3）

**分类原则**：按「本函数是否在自己代码里持有 `async with get_session()` 业务事务并在 commit 后 emit」分类。**不是**按下游消费者最终是否会写 DB。命中 22 处分三类：

#### 4.5.1 类别 A：mutation 节点（必须迁 transactional_emit）— 8 处

本函数显式打开 session、insert/update 业务表、commit 后 emit。

| 文件:行 | 业务 DB 写入 | emit 的 data |
|---|---|---|
| `agent/tools/commit_abstract.py:64` | `insert_abstract_memory + insert_memory_edge` | `AbstractMemoryCommitted` |
| `agent/tools/notes.py:42` | `insert_note` | `NoteCreated` |
| `agent/tools/update_schedule.py:46` | `insert_schedule_revision` | `ScheduleRevisionCreated` |
| `life/proactive.py:148` | proactive message INSERT | `Message` |
| `life/proactive.py:150` | （同上 session）| `ChatTrigger` |
| `life/tool.py:104` | `insert_life_state` | `LifeStateChanged` |
| `life/glimpse.py:242` | `Q.insert_fragment` | `MemoryFragmentRequest` |
| `nodes/memory_pipelines.py:203` | `insert_fragment`（afterthought 路径）| `MemoryFragmentRequest` |

迁移做法：把现有「`async with get_session()` 块外」的 `await emit(...)` 改成块内 `async with transactional_emit(s) as emitter: await emitter.append(...)`，session commit 时 outbox row 与业务行同事务可见。

#### 4.5.2 类别 B：pure transform / fan-out 节点（保持裸 emit）— 12 处

本函数不打开业务 session，只是按输入派生新 Data 调 emit；没有事务原子性需求。

| 文件:行 | 性质 |
|---|---|
| `nodes/life_dataflow.py:72, 275, 296` | cron tick → fan-out per-persona build/glimpse request（3 处）|
| `nodes/chat_node.py:97, 142, 209, 242, 271` | streaming chat 输出 `ChatRequest` / `ChatResponseSegment`（5 处）|
| `chat/post_actions.py:46` | 触发 durable `PostSafetyRequest`（不写 DB）|
| `chat/post_actions.py:62` | `_emit_memory_trigger` fire-and-forget |
| `chat/context.py:117` | 触发 TOS 同步事件 `ConversationMessageContentSynced`（写库由 worker 做）|
| `nodes/memory_pipelines.py:297` | `on_abstract_committed` in-process re-emit `MemoryAbstractRequest`（pure transform）|

#### 4.5.3 类别 C：文档字符串（不是真实调用）— 2 处

| 文件:行 | 性质 |
|---|---|
| `memory/vectorize_memory.py:9, 10` | docstring 反引号包裹的 emit 写法引用 |

grep word-boundary 仍会命中 docstring 反引号包裹的 ``await emit(...)``——计入 CI gate 基准计数，跟代码行同等对待，PR review 时合 §4.5 表看。

#### 4.5.4 完成判据

- 类别 A 8 处全部迁完，每处配单测验证 outbox row 在事务内插入（mutation 失败时事务回滚 → outbox 不应有 row，drill D 验证）
- 类别 B / C 行不变，落到 CI gate baseline（见 §6.1）
- **完成后业务区 `\bawait emit(` 命中数 == 14（类别 B 12 + 类别 C 2，类别 A 8 处迁完后从 grep 中消失）**
- 任何超过 14 的命中说明：(a) 新业务作者绕过 transactional_emit，或 (b) 新增合法 pure transform 但忘了同步 §4.5.2 + 调整 baseline。两种都触发 PR review

### 4.6 in-process wire 是否走 outbox

**走，但「统一性」在 dispatcher 层**：transactional_emit append 的所有 row 都进 outbox，dispatcher 一律调 `emit(data)`，由 `emit()` 自身按 wire 配置选 transport（in-process consumer → `await c(**kwargs)` 直接调，见 emit.py:97-99；durable wire → `publish_durable`；sink → `_dispatch_mq_sink`）。

「统一」的含义是：**所有 mutation node 的 emit 都经过 dispatcher 异步触发**——不是「都走 RabbitMQ」。in-process wire 仍然走内存直调，没有引入额外开销，只是触发时机从「业务调用栈内同步」推迟到「dispatcher loop 异步」。

如果以后有性能压力（in-process 路径不希望经过 PostgreSQL outbox 表）：再加 fast path（mutation node 显式 opt-in"内存直发"），**7b 不做**。

---

## 5. commit 切分

按 **Gap 18 → Gap 12 → Gap 8** 顺序。每 commit 自包含 + 测试 green + ruff 通过 + 当前关闭的 gate 加进 CI。

| # | 范围 | commit 标题 |
|---|---|---|
| 1 | spec | `docs(spec): Phase 7b reliability + error policy + outbox plan` |
| 2 | Gap 18 | `feat(runtime): typed errors (DuplicateData / NeedsReview) + wire .on_error() builder` |
| 3 | Gap 18 | `feat(runtime): durable handler routes by error policy (single-except + helper)` |
| 4 | Gap 18 | `feat(runtime): manual-review queue + publish_to_review_queue` |
| 5 | Gap 18 | `refactor(nodes): drop "不要 catch" comment + explicit .on_error("dlq")` |
| 6 | Gap 12 | `feat(runtime): inflight.delete_inflight() + RabbitMQ management client` |
| 7 | Gap 12 | `feat(runtime): admin DLQ endpoints + audit_log (6-step transaction-like requeue)` |
| 8 | Gap 12 | `feat(ops): Makefile dlq targets + runbook` |
| 9 | Gap 8 | `feat(runtime): runtime_outbox table + transactional_emit` |
| 10 | Gap 8 | `feat(runtime): outbox dispatcher_loop (calls emit) + lifespan dual entry` |
| 11 | Gap 8 | `refactor(business): migrate 8 mutation nodes to transactional_emit` |
| 12 | CI | `chore(ci): close Gap 8 / 12 / 18 in grep gate` |

**关键性质**：
- commit 2-5 之间：commit 2 只暴露 DSL 不改 handler（旧行为保留），3 改 handler 路由（含 publish-confirm 失败 fall-through DLQ），4 加新 queue + publish_to_review_queue（confirm 检查），5 收业务区。每步可独立 ship 并 rollback。
- commit 9-11：9 加表 + API 但业务不接入是中间态（runtime 自洽），10 启 dispatcher（无业务调用方时空转），11 才真接入 8 处 mutation。任意 commit 出问题都可单独回滚不影响其他 Gap。
- 6-7-9 涉及 schema migration：每个独立一个 Alembic file，按时序 apply。

---

## 6. 验收（CI grep gate + drill）

### 6.1 Closed-gap exact-zero

`.github/workflows/grep-gate.yml`（7a 已建）新增 closed-gap 检查项：

```bash
# Gap 8 — 业务区 await emit( 命中数必须 == 14（8 处 mutation 迁完后剩 12+2，见 §4.5）
COUNT=$(grep -rn '\bawait emit(' apps/agent-service/app/{nodes,agent,chat,life,memory,long_tasks}/ \
  --include='*.py' | wc -l)
test "$COUNT" = "14"   # 类别 B 12 + 类别 C docstring 2，见 §4.5
# 同时辅助 grep 注释痕迹 == 0（防止有人加新的 commit-then-emit 注释）
grep -rn "# emit AFTER commit\|# commit-then-emit" apps/agent-service/app/ | wc -l == 0

# Gap 12
grep -q "^dlq-inspect:" Makefile
grep -q "^dlq-replay:" Makefile
grep -q "^dlq-dry-run:" Makefile
test -f docs/runbooks/dlq-replay.md

# Gap 18
grep -rn "# 不要 catch\|# don't catch\|requeue=False\|nack" \
  apps/agent-service/app/{nodes,agent,chat,life,memory}/ | wc -l == 0
```

`requeue=False` / `nack` 字面量当前应**只**出现在 `runtime/durable.py`，业务区零命中。

**allowlist 维护原则**：以后**新业务节点**默认必须用 `transactional_emit`（业务区 `\bawait emit(` 总数不允许增长）。如果有合法新增 pure transform 调用 emit，必须**同 PR 在 §4.5.2 表格里注册** + 调整 CI gate 期望计数（COUNT 值）。这把"业务作者绕过 transactional_emit"从隐性约束变成显式 review 触发点。

### 6.2 baseline 不变

`.github/grep-baselines.json` 中 Gap 13/14/15/16/19 baseline 维持 7a ship 时数值（`gap_13_get_session=97 / gap_14_redis_setnx_business=5 / gap_15_arq_imports=5 / gap_16_httpx_business=3 / gap_19_create_task_business=5`）。7b 不准让任意 baseline 增加。

### 6.3 Contract test 三类齐全

每个 7b primitive 必须配：
1. **Compile-time validation**：DSL 错用立即抛（pytest 单测）
   - `wire(...).on_error("foo")` → 抛 ValueError（合法值仅 "dlq" / "ignore-duplicate" / "manual-review"）
   - `transactional_emit(None)` → 抛 TypeError
2. **Unit contract test**：mock infra
   - `OutboxEmitter.append` 写 outbox 行 + propagation context 注入（mock session.execute）
   - dispatcher 跑一行 → mock `emit()` + `deserialize_data` + `bind_propagation_from_payload`，验证：
     - 注水 row（含 origin_app/lane/trace_id/payload_json）被读出
     - propagation context 在调 emit 之前被 bind（用 contextvar assertion 或 spy）
     - emit 被调用且参数是反序列化后的 Data 实例（**不 mock mq**）
     - emit 成功 → row state 'pending' → 'dispatched'；emit raise → attempts 累加 + next_attempt_at 推后
   - `delete_inflight` by 各种参数组合（trace_id / edge_idempotent 互斥校验）
   - durable handler 单一 except + helper 路由：
     - DuplicateData + ignore-duplicate → mark_succeeded + ack
     - DuplicateData + dlq → 降级为通用 Exception → mark_failed → DLQ
     - NeedsReview + manual-review + publish_to_review confirmed → mark_review + ack
     - NeedsReview + manual-review + publish_to_review **NOT confirmed** → fall-through → mark_failed → raise
     - 通用 Exception + .retry() + publish confirmed → ack
     - 通用 Exception + .retry() + publish **NOT confirmed** → fall-through → raise
3. **Integration / lane test**：真 RabbitMQ + 真 PostgreSQL
   - mutation node 写 DB + transactional_emit → commit → dispatcher 拉走 → 下游 consumer 收到（in-process & durable 各一例）
   - DLQ 注水 → inspect / dry-run / 6-step requeue 全闭环 + audit row 状态机
   - manual-review queue 投递 + inspect

### 6.4 E2E drill（dev 泳道）

| # | 场景 | 通过判据 |
|---|---|---|
| A | 业务 raise DuplicateData (on_error="ignore-duplicate") | log warning + 消息 ack + 不进 DLQ + 不 retry + inflight state='succeeded' |
| B | 业务 raise NeedsReview (on_error="manual-review") | 消息 ack + 进 review queue + body 含失败上下文 + inflight state='review' |
| C | DLQ 注水：人为让节点 raise → 消息进 DLQ → `make dlq-inspect QUEUE=...` 可看 → `make dlq-replay QUEUE=... CLEAR=true` → 节点重新成功消费 | 全链路闭环 + audit_log 有 cleared/requeued status 行 |
| D | mutation node 写 DB 失败（raise in `session.execute`）| outbox 表无对应 row（事务回滚生效）|
| E | mutation node 写成功 → dispatcher 拉走 → in-process consumer 收到（AbstractMemoryCommitted → on_abstract_committed）+ durable consumer 收到（MemoryAbstractRequest → vectorize-worker）| trace_id 完整传播；outbox row state=dispatched |
| F | dispatcher 重发失败：mock `emit` raise | row attempts 累加，next_attempt_at 按 backoff 推后；恢复后成功 dispatch |
| G | 多 pod dispatcher 并发：两个 pod 同时跑 | 同一 row 只被一个 pod 发（FOR UPDATE SKIP LOCKED 生效）；publish/in-process 自身是 at-least-once，下游 `runtime_inflight` (edge_id, idempotent_key) dedup 后**业务效果一次**（验收 consumer 端只 mark_succeeded 一次）|
| H | publish-confirm fall-through：retry envelope publish 失败 / review queue publish 失败 | helper raise → 进 DLQ；inflight state='failed'；DLQ 消息可被后续 replay |

drill 全过 + 写 retrospective `docs/superpowers/retrospectives/2026-MM-DD-phase7b-drill.md`。

---

## 7. 风险与回滚

### 7.1 风险

**R1: dispatcher 多 pod SKIP LOCKED 失效 → 重复 emit**
缓解：下游 idempotent 兜底（`runtime_inflight` 7a 已建状态机 + `(edge_id, idempotent_key)` 复合 PK + history backfill），重复 emit 在 consumer 侧静默 dedup。drill G 真验。

**R2: 业务侧迁移漏改 → 仍有 commit-then-emit**
缓解：CI gate `\bawait emit(` 命中计数 == 14 守底；超过即阻塞 PR。

**R3: outbox 表无限增长**
7b 不做清理（独立 cron node 放 7b+）。短期监控 `runtime_outbox` 行数；如增长率超预期，立即排单做清理 cron。

**R4: manual-review queue 成"垃圾箱"**
runbook 强制周期巡检（`make dlq-inspect QUEUE=... KIND=review` 列入 oncall checklist）。短期可加监控 `<queue>_review depth > 0` alert。

**R5: typed exception 误用**
DuplicateData / NeedsReview 在 mismatch on_error 下被降级为通用 Exception 处理（详见 §2.4）—— 业务作者错配获得安全默认（仍走 retry/DLQ），不是隐式 ack 丢消息。

**R6: RabbitMQ Management API 凭据失败**
`RABBITMQ_USER` / `RABBITMQ_PASSWORD` 跟 AMQP 同账号；management 端口 15672 集群内可达。dispatcher 与 management 解耦（dispatcher 走 AMQP），所以 management 故障不影响 outbox dispatch，只影响 DLQ inspect/replay 工具。

**R7: publish-confirm 失败的 DLQ 兜底**（接受 reviewer P0-1 修订）
helper 在 retry envelope / review queue publish 都不 confirmed 时 fall-through 到 DLQ raise，保证消息不丢；inflight state='failed' 留下排查痕迹。drill H 验收。

### 7.2 回滚

每个 commit 独立 revert 安全：
- commit 2-4 (Gap 18)：revert 后 wire `.on_error()` 不可用，handler 回到 7a 行为。已迁的 `.on_error("dlq")` 显式声明 revert 后会报 AttributeError → 必须连 commit 5 一起 revert。
- commit 6-8 (Gap 12)：runtime 不依赖；revert 后 `make dlq-*` 不可用，admin endpoint 404。手工 SQL replay 流程恢复。
- commit 9-11 (Gap 8)：必须配套 revert（不可只 revert 11 留 9-10 在）。Alembic migration 提供 down 函数（DROP TABLE runtime_outbox）。revert 业务调用方后回到「commit-then-emit」模式。

production rollback 走 `make release APP=agent-service VERSION=<prev>` + 同步 release arq-worker / vectorize-worker（一镜像多服务同步铁律）。

---

## 8. Out of Scope（7b 明确不做）

- **outbox row 清理 cron**：dispatched 行保留 7 天后清理 — 独立 cron node 放 7b+
- **in-process fast path**：dispatcher 走真正"内存直连"绕 outbox — 7b 统一走 outbox + emit fan-out
- **manual-review queue 自动巡检 alert**：监控/告警体系不在 dataflow runtime spec 范围
- **lark-server 侧 outbox**：lark-server (TS) 也有 emit 路径（飞书消息进 chat），但 7b 仅覆盖 agent-service (Python)。lark-server 侧后续单独排
- **跨边界挂账（来自 7a 复盘，不属 dataflow runtime）**：
  - lark-server publish CHAT_REQUEST 不传 trace_id header → 单 PR 改一行 TS 代码
  - RabbitMQ x-delayed-message 插件部署 runbook → 集群重建必须 enable 的 sanity check

## 9. 文档与索引

7b ship 后：
- `MEMORY.md` 项目区 `project_dataflow_phase7.md` 删 Gap 8 / 12 / 18 行
- `project_dataflow_done.md` 加 7b 摘要表行
- `docs/superpowers/specs/2026-05-08-dataflow-phase-7-gap-analysis.md` 表 §3 PR 切分把 7b 标 ✅ shipped
- 本 spec 顶部状态从 Draft → Shipped `<prod version>`
