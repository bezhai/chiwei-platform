# Dataflow Phase 7 — 终态 Gap Analysis（v5+ 接续）

**状态**: Draft v1（2026-05-08）
**前置**: PR #210（Phase 6 v4 cleanup）已 ship 到 prod 1.0.1.2，framework capability gap surface (Gap 1-6) 已闭合
**后续**: 本文件覆盖 Gap 7-19 全 13 项；按 PR 切分 7a→7e 五次 ship；本期分支 `refactor/dataflow-parse-7` 仅承载 7a (Gap 7+9+11) 实现，其余 PR 各自独立分支

## 0. Phase 6 → Phase 7 的承接

Phase 6 v4 (PR #210) 关闭了 Gap 1-6：业务代码不再手写 FastAPI route / mq.publish / arq enqueue / asyncio.create_task，且新建第二套 worker 入口。剩余 12 项原 v5+ 候选（Gap 7-12）合并 Phase 6 spec §3 之外暴露出的实际泄漏面（Gap 13-19），统一在 Phase 7 处理。

Phase 6 v4 spec（`docs/superpowers/specs/2026-05-07-dataflow-phase-6-cleanup-design.md`）顶部已加历史校正：v5/v6/v7 旧说法废弃，Gap 7+ 由本 spec 统一承接。**`long_tasks/task_executor` arq 绕路、builtin `/health`、Gap 7-12 全部留给 Phase 7**，不能按旧 Phase 6 验收草率删除。

## 1. 终态判定升级

**Phase 6 v4 终态**写的是「业务代码不准 workaround framework」。这只是「约束」层面。

**Phase 7 终态**：framework surface 不泄漏，业务作者根本不需要懂 RabbitMQ / DLX / Redis lock / ARQ worker / FastAPI route / trace/lane header / DB commit-then-emit / retry / outbox / DLQ replay 这些底层概念。无论写 chat / tool / memory / schedule / long task，业务作者只回答 3 个问题：

1. 这个业务**产生 / 消费什么 Data**？
2. 哪个 **Node** 处理？
3. 需要哪些**业务能力**（LLM / HTTP / VectorStore / Agent / state query）？

**判一个改动是否在「正确方向」上**：用上面 3 个问题筛。如果业务作者要去读 `runtime/*` 才知道怎么用，说明 framework surface 或文档还不够，应该补 framework / 补手册，**不允许业务自己绕**。

## 2. Framework Capability Gap Surface（共 13 项：Gap 7-19）

每条格式：**framework 现状 / 业务绕痕 / 缺什么 / v5+ 目标 / 不留隐患约束**。

按归属 PR 分组（7a-7e）。

---

### 7a 范围（transport 语义层，本期分支闭合）

#### Gap 7 — durable wire retry 不可配置

**Framework 现状**:

- `runtime/durable.py:_build_handler` 用 `async with message.process(requeue=False)` context manager：成功 ack；handler 内抛 exception → aio-pika nack(requeue=False) → 路由到 DLX/DLQ。无 in-place retry / 无 backoff / 无 retry budget。`.durable()` builder（`runtime/wire.py:57-59`）只设 flag，不接受参数。
- `runtime/persist.py:insert_idempotent` 直接对 Data 业务表 `INSERT ... ON CONFLICT (<dedup_target>) DO NOTHING`。**dedup row 就是 Data 行本身**，没有独立 idempotent state 表。
- handler 当前流程：① bind context → ② decode body → ③ `insert_idempotent(obj)` 写 Data 行（n=0 视为 duplicate 直接 ack）→ ④ 调 consumer。adoption-mode Data（`Meta.existing_table`）跳过步骤 ③，幂等靠 consumer 侧自管。

**业务绕痕**: 目前业务被禁止自实现 retry 循环（v4 §1 Gap 7 业务约束）。但当前 framework 有两个连锁问题，让 retry 实施前必须先解决：

1. **dedup 写入早于 consumer**：consumer 抛错时 Data 行已 INSERT 成功 → 同 dedup_hash 的重投消息回到 handler 时 `insert_idempotent` 返回 0 → 当成 duplicate 静默 ack。**「失败后自然重投」永远跑不到 consumer**，除非运维 SQL 删 dedup row 后重投 DLQ。
2. **transient error 必然进 DLQ**：DLQ replay 又被 idempotent dedup 拦成 no-op（v4 spec acknowledged，本 spec Gap 12 收口），形成"消息丢但 DLQ 有"的死局。

**缺什么**:

##### 7.1 idempotency 状态机（前置必做）

新建独立表 `runtime_inflight`（runtime migrator 自动建，业务表不动）：

```sql
CREATE TABLE runtime_inflight (
    edge_id        TEXT NOT NULL,            -- 见下方定义；区分同 Data 多 consumer
    idempotent_key TEXT NOT NULL,            -- Data 行的 dedup_hash 或 dedup_column 值
    data_table     TEXT NOT NULL,            -- Data 类对应的表名（观测用）
    state          TEXT NOT NULL,            -- 'processing' | 'succeeded' | 'failed'
    attempts       INT  NOT NULL DEFAULT 0,
    last_error     TEXT,
    locked_until   TIMESTAMPTZ,              -- processing lease；NULL ⇔ 非 processing
    worker_id      TEXT,                     -- 持有当前 lease 的 worker（pod hostname + pid）
    trace_id       TEXT,                     -- 第一次进入 handler 时的 trace_id（观测用）
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (edge_id, idempotent_key)
);
CREATE INDEX runtime_inflight_state_idx ON runtime_inflight (state, locked_until);
```

**edge_id 定义**：`f"{data_type.__qualname__}::{consumer.__qualname__}"`（runtime 启动时 compile_graph 阶段确定，与 durable route 队列一一对应）。同一 Data 配多个 consumer 时每条 wire 各自有独立 inflight 行——不会出现「consumer A succeeded → consumer B 被 dedup 跳过」。

**lease 语义**：`locked_until` 是 processing 状态的硬性 lease 时间（默认 `now() + 5 minutes`，可由 wire 配置 override）。lease 到期前**任何同 (edge_id, idempotent_key) 的新消息必须 skip**（视为重复，ack 让 broker 删消息）；过期后允许下一 worker 接管（worker 死或长尾任务超时的兜底）。

handler 流程改为：

```
1. bind context (trace_id / lane)
2. idempotent_key = dedup_hash(obj) (or Meta.dedup_column value)
   edge_id        = f"{data_type.__qualname__}::{consumer.__qualname__}"
   worker_id      = f"{hostname}:{pid}"

3. open short tx — acquire pg_advisory_xact_lock(hash(edge_id, idempotent_key))
4. SELECT state, attempts, locked_until, worker_id FROM runtime_inflight
   WHERE edge_id=:e AND idempotent_key=:k

5. branch:
   - row missing:
       a. compatibility check (一次性 backfill 兼容历史 Data 行，详见 7.1.1)
       b. else INSERT runtime_inflight (state='processing', attempts=1,
                                       locked_until=now()+lease,
                                       worker_id=:wid, trace_id=:tid)
       c. for non-adoption Data: INSERT INTO <data_table> ON CONFLICT DO NOTHING
       d. COMMIT short tx → release advisory lock → run consumer
   - row.state='succeeded':
       COMMIT → ack original message → return  (dedup 正常路径)
   - row.state='processing' AND locked_until > now():
       COMMIT → ack original message → return  (别的 worker 还在跑，跳过)
   - row.state='processing' AND locked_until <= now():
       UPDATE state='processing', attempts=attempts+1,
              locked_until=now()+lease, worker_id=:wid, updated_at=now()
       COMMIT → run consumer  (lease 过期，接管)
   - row.state='failed':
       UPDATE state='processing', attempts=attempts+1,
              locked_until=now()+lease, worker_id=:wid, updated_at=now()
       COMMIT → run consumer  (retry 进入)

6. consumer success:
       short tx — UPDATE state='succeeded', locked_until=NULL,
                          worker_id=NULL, updated_at=now()
       COMMIT → ack original message
   consumer failure:
       see retry transport (7.2)
```

###### 7.1.1 兼容历史 Data 行

升级前已存在的 Data 行没有 inflight 记录。如果升级后 RabbitMQ 重投/重复消息进入 handler，row missing 分支会被当成 first-time → 重跑 consumer（业务执行多次）或 INSERT Data 表 UniqueViolation。

`row missing` 分支 step (a)：

```
SELECT 1 FROM <data_table> WHERE <dedup_target> = :k LIMIT 1
```

- 有 row（历史已处理）→ INSERT runtime_inflight (state='succeeded', attempts=0,
                                            locked_until=NULL,
                                            trace_id='backfill') → ack
- 无 row → 走 5b/5c 正常 first-time 路径

adoption-mode Data 不写 Data 行，无法 backfill 检查。这类 Data 在升级窗口内**所有重投都视为新消息**：业务侧的 idempotent 保护（Meta.dedup_column 已定义）会让 INSERT 阶段 ON CONFLICT 仍能正确 dedup，consumer 端的领域级幂等（如 qdrant upsert by id）兜底。inflight 状态机在升级后逐步建立。

##### 7.1.2 关键约束

- PK = (edge_id, idempotent_key)：同 Data 多 consumer 各自独立状态
- `succeeded` 是唯一 dedup 终态
- `processing` 仅在 lease 过期后才允许接管，未过期一律 skip + ack
- advisory lock + 短事务：lock key = `hash(edge_id, idempotent_key)`，事务边界仅覆盖 inflight 行的读/写，consumer 调用在事务**之外**（避免长事务持锁）
- adoption-mode Data：跳过 `insert_data_row` 但仍 INSERT runtime_inflight；compatibility check 不可用，业务侧 idempotent 兜底
- inflight 行不做自动 GC（小数据量；GC 策略归 Phase 8+ 监控）；schema 只 additive
- lease 默认 5 min，可由 `wire(...).durable().retry(..., lease_ms=300_000)` 配置覆盖（DSL 字段在 7.3）

##### 7.2 retry transport（application-level）

不依赖 RabbitMQ DLX 自动 retry，因为 DLX 不能携带自定义 attempt counter 且 x-death 累加语义脆弱（取决于 broker 配置）。改用 application-level publish + confirm：

```
on consumer exception:
  decision = decide_retry(attempts=row.attempts, policy=w.retry)
  if decision.action == 'retry':
    UPDATE runtime_inflight SET state='failed', last_error=..., updated_at=now()
    new_headers = original_headers
                  + {'x-delivery-count': decision.attempt}      -- 自管 counter
                  + propagation.inject_context(...)              -- trace_id/lane
    confirmed = mq.publish_with_confirm(
        same_route, body=original_body,
        headers=new_headers, delay_ms=decision.delay_ms,
    )
    if confirmed:
        ack original message            -- broker 已收新副本，原消息可丢
    else:
        nack(requeue=False) original    -- 进 DLQ 兜底，运维可见
  else:  # dlq
    UPDATE runtime_inflight SET state='failed', last_error=...
    nack(requeue=False)                -- 进 DLQ
```

关键约束：
- delivery_count 来源**唯一**：自管 `x-delivery-count` header（不再读 `x-death`）；首次消息无该 header 视为 0
- `mq.publish_with_confirm` 走 RabbitMQ publish confirm（aio-pika `Channel.confirm_select` + `publish` 等待 ack），broker 持久化前不返回成功
- publish 失败 → 原消息走 DLQ 兜底（不丢，运维可 replay）
- 重试消息的 body 不变 → 同 idempotent_key → 与 7.1 状态机配合不重复跑业务

##### 7.3 wire DSL

```
wire(...).durable().retry(
    n=3,                      # 最多 attempts 数（首次 + 重试合计）
    backoff="exponential",    # 'exponential' | 'linear'
    base_delay_ms=500,
    max_delay_ms=30_000,
    lease_ms=300_000,         # processing lease（默认 5 min）
)
```

未配置 retry 时维持当前 fail-to-DLQ 语义（向后兼容）；但 inflight 状态机仍生效（consumer 仍受状态机保护，只是 attempts=1 失败即 DLQ）。

**v5 目标**: durable handler 失败 → 按 wire.retry 走 application-level publish + confirm 带 delay 重投；同时 idempotent 状态机让重投真能跑到 consumer。

**不留隐患约束**:
- `grep -rn "while.*retry\|for.*range.*retry" apps/agent-service/app/` 业务代码 0 命中
- `grep -rn "asyncio.sleep" apps/agent-service/app/{agent,chat,life,memory,nodes,api}/` 0 命中（仅 framework `runtime/` 内部允许）
- 未配置 retry 的 wire 行为与当前**业务可观测**完全一致（runtime_inflight 表会多写，但消息行为不变）
- adoption-mode Data 与现状一致（不写 Data 行，仅 runtime_inflight 走状态机）
- consumer 失败抛错 → 重投回来必须能跑到 consumer（drill 必须能复现「故意 raise → backoff 后再次进入 consumer」）

---

#### Gap 9 — delayed / scheduled emit

**Framework 现状**: 没有 `emit_delayed(data, delay=10s)` / `emit_at(data, ts)` API。`runtime/debounce.py` 走 redis SETNX + Lua 自实现的延迟（最终落到 `mq.publish(..., delay_ms=...)`），但只在 debounce wire 内部使用，不是开放 framework primitive。`infra/rabbitmq.py:247-279` 已支持 `delay_ms` 参数（基于 RabbitMQ x-delayed-message exchange），但 emit 层没暴露。

**业务绕痕**: v4 后业务被禁止 `await asyncio.sleep(N) + emit`（v4 §1 Gap 9 业务约束）；现状是**需要延迟触发的业务别无选择，只能借用 debounce wire 模式**（即使本质不是 debounce 而是 scheduled）。例如：

- 长任务执行后想隔 30s 触发 retry / 状态轮询：当前没有干净 API
- proactive 触发后想 5min 后跑 reviewer：当前借 debounce wire（语义不对）
- update_schedule 后想隔 1min 触发 state sync（避免短 burst 抖动）：当前 in-process emit + 节点内 sleep（已经被 Gap 9 禁止，但 framework 无替代）

**缺什么**:

##### 9.1 显式 durability 参数（避免可靠性歧义）

业务作者**不需要懂 mq / x-delay**，但必须知道「这条延迟触发是否要 survive 部署/重启」。同一 API 不能在不同 wire 拓扑下给出不同可靠性保证（in-process 会在 pod 重启时丢，跨进程不会），否则隐藏丢事件风险。

API 形态：

```python
async def emit_delayed(
    data: Data,
    *,
    delay_ms: int,
    durability: Literal["durable", "best_effort"] = "durable",
) -> None: ...

async def emit_at(
    data: Data,
    *,
    when: datetime,
    durability: Literal["durable", "best_effort"] = "durable",
) -> None: ...
```

###### 9.1.1 语义：emit_delayed = "delay 后调 emit(data)"

`emit_delayed(data, delay_ms=N)` 的 contract 是：**N 毫秒后**对 data 跑一次完整 `emit(data)`，**完全保留 emit() 的 fan-out 语义**（同一 Data 多 wire 时，所有 wire 都收到，包括 in-process / durable / sink）。emit_delayed 自身不感知 wire 拓扑，调用方也不需要知道下游 wire 是几条、是什么类型。

这与 Phase 5/6 的 emit() 语义一致——业务只声明 Data，runtime 负责 fan-out。

###### 9.1.2 实现：runtime-owned delayed trigger 队列（按 origin_app + lane 隔离）

`emit_delayed` 必须在「N 毫秒后由原发起进程跑 emit(data)」语义下成立，否则 emit() 的 in-process / cross-process fan-out 决定（依赖 `APP_NAME` 与 `lane`）会跑偏：被错误 app 消费时 in-process node 跳过或用错代码版本。

因此 runtime 不能用一条全局共享 queue，必须按 `(origin_app, lane)` 隔离 trigger queue：

- queue / route 命名：`runtime_delayed_trigger_{app}`，lane 通过现有 `lane_queue(base, lane)` 机制扩展为 `runtime_delayed_trigger_{app}_{lane}`（lane 为 None 即 prod queue）
- queue 配置 `lane_fallback=False`（与 debounce route 一致）：lane queue 不能 TTL 短路 fallback 到 prod，否则 lane envelope 错投 prod consumer
- 每个 runtime 启动时声明并消费 `runtime_delayed_trigger_{APP_NAME}` 的 base queue + 自己 lane 对应的 lane queue（与现有 `_ensure_lane_queue` 机制一致，lane 由部署时 `LANE` env / runtime startup 决定）
- 不同 app（agent-service / vectorize-worker / arq-worker 取代版 / future event-worker）各跑各自的 trigger consumer，互不串味

`DelayedTriggerEnvelope`：

```python
class DelayedTriggerEnvelope(Data):
    origin_app:    str          # APP_NAME at emit_delayed call time
    origin_lane:   str | None   # lane at emit_delayed call time（None ⇔ prod）
    data_type:     str          # 反序列化目标 Data 类的 fully-qualified name
    payload:       dict         # data.model_dump(mode="json")
    trace_id:      str | None   # 发起方 trace_id（envelope 内携带，与 header 双写）
```

`emit_delayed(data, delay_ms=N, durability="durable")` 的实现：

1. `app = current_app()` (`APP_NAME` env or DEFAULT_APP)；`lane = lane_var.get()`
2. 构造 `DelayedTriggerEnvelope(origin_app=app, origin_lane=lane, ...)`
3. route = `runtime_delayed_trigger_{app}` (base) → 由 `mq.publish` 内部按 lane 自动 resolve 为 `runtime_delayed_trigger_{app}_{lane}` queue + lane routing key
4. `mq.publish_with_confirm(route, envelope.model_dump(mode="json"), headers=propagation.inject_context(...), delay_ms=N, lane=lane)`
5. publish-confirm 成功后返回；失败抛 `EmitDelayedDispatchFailed`（调用方决定降级或抛错）

runtime 启动时同时启动一个内部 consumer（`Source.mq("runtime_delayed_trigger_{APP_NAME}")`）。consumer 收到 envelope：

1. 校验 `envelope.origin_app == APP_NAME`；不匹配 → log error + ack（不应发生；防御性）
2. 反序列化 payload 回 `data_type` 对应 Data 实例
3. 在 envelope 携带的 trace_id / lane context 下调 `emit(data)`
4. 走标准 fan-out：本 app + 本 lane 的 in-process consumer 直接调；跨进程 wire 走标准 mq publish

好处：
- emit_delayed 不需要查询 graph、不需要按 wire 拓扑分支
- 多 wire / 混合拓扑（同一 Data 同时被 in-process consumer + durable consumer + sink 消费）天然支持
- 不同 app / lane 的 trigger envelope 互不污染：feat-x lane 进程的延迟触发不会被 prod 进程误消费，反之亦然
- delayed trigger consumer 自身就是 dataflow node，享受 Gap 7 retry / inflight 状态机保护

###### 9.1.3 路由决策

| durability | 实现 | survive 重启 |
|---|---|---|
| `durable`（默认） | publish_with_confirm 到 `runtime_delayed_trigger_{origin_app}` queue（按 origin_app + lane 隔离）+ x-delay → 自身进程的内部 consumer → emit(data) | ✅ |
| `best_effort` | in-process `schedule_after` → 直接调 `emit(data)` | ❌ |

`durable` 模式不依赖业务 wire 是否 durable，只需要 RabbitMQ 可达且本 app+lane 进程仍然运行（消费自身 trigger queue）。如本 app+lane 长时间下线，envelope 在 queue 里堆积直到 lane 重启或运维清理（与其他 lane queue 行为一致）。

`best_effort` 必须由调用方显式传入（默认不允许），docstring 明确警告：runtime stop / 部署 / pod 重启时 pending task 全丢。

##### 9.2 in-process scheduled task lifecycle（best_effort 路径）

- task 句柄统一存 `runtime/scheduled.py` 的 task set
- `Runtime.stop_source_loops` 时 `cancel_all_scheduled()` 全部取消 + `await gather(return_exceptions=True)`
- task 内 exception 仅 log，不上抛（避免污染调用栈）
- `best_effort` 路径下 scheduled task fire 时**不**自动恢复发起方 contextvar；调用方应假设触发的下游链路是独立 trace（与 cron 触发同语义）

##### 9.3 durable 路径（runtime_delayed_trigger_{app} queue）

- 复用 `infra/rabbitmq.py:mq.publish(... delay_ms=...)` 已有 RabbitMQ x-delayed-message exchange 能力（不动拓扑，仅新增 N 条 route — 每个 app 一条）
- 每个已知 `APP_NAME`（agent-service / vectorize-worker / 后续替换 arq-worker 的 event-worker / ...）注册一条 base route `runtime_delayed_trigger_{app}`：`durable=true`、`lane_fallback=False`（防止 lane envelope 错落 prod queue）；不设 `x-message-ttl`（trigger 应当尽量交付）
- 实际消费的 queue 名按现有 `lane_queue` 机制扩展：prod 进程消费 `runtime_delayed_trigger_{app}`，lane 进程消费 `runtime_delayed_trigger_{app}_{lane}`
- publish 走 publish-confirm（与 Gap 7 retry transport 一致），确保 broker 持久化后再返回
- delay_ms 上限受 RabbitMQ x-delayed-message exchange 限制（`x-delay` int32 ms ≈ 24 天）；超限抛 ValueError
- runtime 内部 trigger consumer 反序列化时：
  - `envelope.origin_app != APP_NAME` → log error + ack（防御性，不应发生）
  - `data_type` 已被删除（过期 envelope）→ log warning + ack，不进 DLQ

##### 9.4 trace / lane 传播

`emit_delayed` 在 envelope 里同时携带当前 trace_id / lane（通过 Gap 11 propagation primitive 写入 outbound headers + envelope payload 双写）。trigger consumer 处理时用 envelope 的 trace_id / lane bind context，然后调 emit(data)。这样 delayed emit 触发的下游链路 trace_id 与发起方一致，Langfuse 上能看到完整链路（不像 cron 触发那种独立 trace）。

`best_effort` 路径不传播 contextvar（同 9.2 末尾说明）。

**v5 目标**: 业务代码任何「N 秒/N 分钟后触发某 Data」走 `emit_delayed` 一行；可靠性等级由调用方显式选择（默认 durable）。

**不留隐患约束**:
- `grep -rn "asyncio.sleep" apps/agent-service/app/{agent,chat,life,memory,nodes}/` 0 命中
- 业务代码引入 delayed emit 时不需要碰任何 mq / x-delay / scheduled task / origin_app 概念
- `runtime_delayed_trigger_{app}` queue 必须 `lane_fallback=False`，且 envelope 反序列化时校验 `origin_app == APP_NAME`（双重防御，避免 wrong-process emit）
- in-process delayed emit 在 runtime stop 时必须取消（不允许 task 泄漏到下个进程实例）
- 所有显式 `durability="best_effort"` 的调用点必须在 PR review 中标注理由（grep `durability="best_effort"` 列出业务调用）

---

#### Gap 11 — trace / lane context propagation 散落

**Framework 现状**: trace_id / lane 通过 `app.api.middleware` 的 `trace_id_var` / `lane_var` contextvar 传播。

| 入口 | trace_id 来源 | lane 来源 | propagation 实现 |
|---|---|---|---|
| HTTP endpoint | `X-Trace-Id` header | `x-ctx-lane` header | `HeaderContextMiddleware` 自动设 contextvar |
| Source.mq (`engine.py:454-467`) | `headers["trace_id"]` | `headers["lane"]` | 手动读 + `set_context` + finally `reset` |
| Source.cron / interval | **无** | **无** | 无 propagation（自动生成的 trigger 没 trace_id） |
| Durable publish (`durable.py:74-85`) | `trace_id_var.get()` | `lane_var.get()` OR `data.lane` | 手动塞 headers |
| Debounce publish (`debounce.py:204-209`) | `trace_id_var.get()` | `lane_var.get()` | 手动塞 headers |
| Sink dispatch (`sink_dispatch.py`) | **不读** | **塞在 body** | body-level lane（chat-response-worker 直接读 payload.lane，**ts 侧合约**） |

**业务绕痕**:
- cron / interval 触发的 Data 走 in-process emit 时**没有 trace_id**，在 Langfuse 上断链（无法关联到后续触发的链路）
- 跨进程 sink dispatch 的 lane **塞在 body 字段**而非 header（`apps/lark-server/src/workers/chat-response-worker.ts:172-188` 的 `ChatResponsePayload.lane`）；这是**body-level lane contract**，与 durable / debounce 的 header-level 不一致
- 每个 Source / publish path 都重复实现了一遍 read header / set contextvar / finally reset 逻辑（durable.py / debounce.py / engine.py 三处近似副本）；新增 Source 类型必须复制粘贴这套样板代码，容易漏

**缺什么**:
1. **runtime 层 propagation primitive**：
   - `runtime/propagation.py` 提供 `extract_context(headers) -> Context` / `inject_context(headers, ctx)` / `bind_context(ctx) -> AsyncContextManager` 三个函数
   - 所有 Source loop / publish path 必须用这套 primitive，禁止自己读 contextvar / 写 header
2. **cron / interval 自动生成 trace_id**：
   - Source.cron / interval 触发时，runtime 自动生成 `trace_id = "cron:" + uuid4().hex` 或 `"interval:" + uuid4().hex`，bind 到 contextvar
   - 这样 Langfuse 能看到「这条链路源自 cron 触发」，且后续 emit 链路完整传播
3. **body-level lane 兼容路径**：
   - sink dispatch 必须保持 body 中的 lane 字段（chat-response-worker 已在生产读它）
   - 但 sink dispatch 也必须**同时塞 header**（向前兼容；下个版本 ts 侧切 header 后删 body 字段）
   - 兼容窗口在 spec 里固化下来，避免后续误删

**v5 目标**: runtime 内部统一 propagation；业务代码 / 新 Source 类型不需要碰 trace_id / lane 概念。

**不留隐患约束**:
- `grep -rn "trace_id_var\|lane_var\|current_lane\|headers\[\"trace_id\"\]\|headers\[\"lane\"\]" apps/agent-service/app/` 仅在 `runtime/propagation.py` / `runtime/middleware.py` / `infra/rabbitmq.py` 命中（业务代码 0）
- `grep -rn "headers=.*trace_id" apps/agent-service/app/` 仅在 `runtime/propagation.py` 1 处命中（durable / debounce / sink dispatch 全部走 propagation primitive）
- chat-response-worker 仍能正常读 `payload.lane`（body 字段保留，至少跨 1 个 PR 的兼容窗口）

---

### 7b 范围（reliability + error policy，下个分支闭合）

#### Gap 8 — emit 跨事务边界（outbox）

**Framework 现状**: 业务靠注释「emit AFTER commit」自觉（`life/proactive.py:141` 等）；DB 回滚 / emit 已发出会引入数据脏化。v4 已把约束写进 `runtime/emit.py` docstring，但仍是约束级别，不是 primitive。

**业务绕痕**: 业务作者每次写 mutation node 都要记得「先 commit 再 emit」；review 时人肉检查；漏一个就埋雷。

**缺什么**:
- `outbox` 表：`(id, data_type, payload_json, created_at, dispatched_at, attempts, last_error)`
- 业务侧：`async with transactional_emit(session) as emitter: emitter.append(data); await session.commit()` —— append 在事务内 insert outbox 行（与业务状态写入同事务），commit 后 outbox row 可见
- runtime publisher 后台 task：定期 SELECT 未 dispatched 的 row，emit() 后 mark dispatched（带 retry / DLQ 语义）
- in-process wire 也走 outbox（统一语义，避免「同进程不走 outbox 跨进程才走」的脑负担）

**v6 目标**: 业务 mutation 写完直接 commit，不用记得 emit 顺序；runtime 保证「commit 成功 = emit 一定发出（可能延迟）」。

**不留隐患约束**: 
- `grep -rn "# emit AFTER commit\|# commit-then-emit" apps/agent-service/app/` 0 命中
- 业务代码不出现 `await emit(...)` 紧跟 `await session.commit()` 的样板（统一 `transactional_emit` 上下文）

---

#### Gap 12 — DLQ replay 语义不闭合

**Framework 现状**: DLQ 重放被 consumer-side `insert_idempotent` dedup 跳过 → replay 默认 no-op（v3 §2.3 + Phase 5 spec acknowledged，v4 docstring 已明确）。运维需要重放时只能手工 SQL 清对应 idempotent 行 + 重投 DLQ 消息。

**业务绕痕**: 无（业务接受现状），但运维负担大；事故复盘时「DLQ 里有消息但 replay 没用」反复出现。

**缺什么**:
- `dlq inspect <queue>` CLI：列 DLQ 消息（含 trace_id / data_type / first_failed_at / last_error）
- `dlq clear-idempotent <message_id|trace_id>` CLI：清对应 idempotent 行
- `dlq dry-run <queue> [--limit N]` CLI：模拟重放（不实际改 DB），输出 plan
- `dlq requeue <queue> [--limit N]` CLI：清 idempotent + 重投到原 queue
- 实现走 admin HTTP source（Phase 6 v4 已支持）+ admin CLI 客户端

**v6 目标**: 运维 DLQ replay 一行命令搞定；事故 SOP 简化。

**不留隐患约束**:
- 现网 DLQ replay 流程文档化（runbook）
- replay 操作必须有审计（哪个运维 / 何时 / 哪条消息），写到 `audit_log` 表

---

#### Gap 18 — node error policy / DLQ 语义泄漏

**Framework 现状**: 业务节点用注释要求「不要 catch 否则不会 nack/DLQ」（`nodes/life_dataflow.py` / `nodes/safety.py` / `nodes/save_fragment.py`）。错误语义靠 Python exception propagation + handler 的 `requeue=False`，没有 typed error policy。

**业务绕痕**: 节点作者必须知道「raise 才能进 DLQ」「不能 catch」；如果想区分「重复消息 silently skip」vs「真错 DLQ」vs「需人工审 DLQ but 别 retry」，没有 framework 表达方式。

**缺什么**:
- `wire(...).on_error("dlq" | "retry" | "ignore-duplicate" | "manual-review")` 配置
- node decorator `@node(error_policy=...)` 同义
- 区分语义：
  - **dlq**：节点内 raise → fail-to-DLQ（当前默认）
  - **retry**：节点内 raise → 触发 Gap 7 retry 链
  - **ignore-duplicate**：节点 raise `DuplicateData` → log warning 不进 DLQ（用于 idempotent dedup 之外的业务级去重）
  - **manual-review**：节点 raise `NeedsReview` → 进 manual review 队列（独立队列，需运维干预）

**v6 目标**: 节点错误语义 typed；新业务作者不需要知道 DLQ / nack 概念，只声明 error policy。

**不留隐患约束**:
- `grep -rn "# 不要 catch\|# don't catch" apps/agent-service/app/nodes/` 0 命中
- 节点代码 0 处出现 `requeue=False` / `nack` 字面量

---

### 7c 范围（arq 退场，独立 PR）

#### Gap 15 — long_tasks/arq 仍是第二套执行框架

**Framework 现状**: `arq-worker` Deployment 仍跑 `task_executor_job`，`pyproject.toml` 仍依赖 arq；`app/long_tasks/task_executor.py` 内有「每分钟 poll long_tasks 表」的 arq cron job，绕过 dataflow runtime 的 `Source.cron`。Phase 6 v4 仅闭合了 `state_sync_after_schedule` 的 arq 绕路；long_tasks 这条线被显式留给 Phase 7。

**业务绕痕**:
- `long_tasks/crud.py` 的 INSERT / UPDATE / SELECT 由 `task_executor_job` 通过 arq pool 触发
- 第二套 lifecycle（arq 启动、scheduler、worker 注册）仍存在
- K8s `arq-worker` Deployment 占独立 pod 资源

**缺什么**:
- 把 `LongTaskRequested` / `LongTaskStep` / `LongTaskCompleted` 建模成 Data
- `Source.cron("* * * * *")` 触发 `poll_long_tasks_node`，扫表后 fan-out emit 各 task 的 step Data
- step 执行用 dataflow node + durable wire（含 retry，依赖 Gap 7）
- 完成 / 失败状态通过 emit `LongTaskCompleted` 触发后续 wire（如 reviewer 通知）
- **删 `app/workers/arq_settings.py` + arq 依赖 + `arq-worker` Deployment**

**v6 目标**: long_tasks 完全跑 dataflow runtime，arq 退出代码库。

**不留隐患约束**:
- `grep -rn "arq\|enqueue_job\|create_pool\|from arq" apps/agent-service/app/` = 0
- `pyproject.toml` 不依赖 arq
- K8s `arq-worker` Deployment 下线（PaaS API delete app）

---

### 7d 范围（capability 收敛，按业务域逐步迁移）

#### Gap 13 — DB / session 大面积泄漏

**Framework 现状**: `runtime/query.py` 提供 `query()` 包装 SELECT，但 INSERT / UPDATE / DELETE 业务仍直接 `async with get_session()` + `await session.execute(text(...))`。约 140 处分布在 `nodes/memory_pipelines.py` / `nodes/admin.py` / `life/glimpse.py` / `life/proactive.py` / `memory/*` / `long_tasks/crud.py`。

**业务绕痕**: 业务节点必须知道 SQLAlchemy session 生命周期、ON CONFLICT 语法、commit 时机。

**缺什么**:
- 扩 `runtime/query.py`：`mutate(sql, params)` 支持 INSERT / UPDATE / DELETE，自动管理 session + commit + retry on serialization conflict
- 按业务域拆 repository capability：`memory_repo` / `schedule_repo` / `long_task_repo` / `safety_repo`，每个 repo 暴露**领域级 API**（`memory_repo.append_fragment(...)`、`schedule_repo.create_revision(...)`），不暴露 SQL
- repository 内部走 Gap 8 outbox 模式（同事务写 + 自动 emit）

**v6 目标**: 业务节点只调 repo API，看不到 session / SQL / commit。

**不留隐患约束**:
- `grep -rn "get_session(\|AsyncSessionLocal" apps/agent-service/app/{nodes,agent,chat,life,memory,long_tasks}/` = 0
- `grep -rn "session\.execute\|session\.commit" apps/agent-service/app/` 仅 `runtime/` / `data/` 命中

---

#### Gap 14 — Redis lock / single-flight / Redis-backed registry 泄漏

**Framework 现状**: 业务节点直接用 Redis SETNX + Lua 实现 single-flight（`nodes/memory_pipelines.py`）；ImageRegistry 用 Redis hash 存（`chat/context.py`）；safety 直接 `redis.smembers("banned_words:...")`（`nodes/safety.py`）。

**业务绕痕**:
- 每个想做 single-flight 的节点都要自己写 SETNX + TTL 续期 + 释放
- 业务级 registry / config 散落 Redis key naming（`image:reg:...` / `banned:...` / `latest:...`）
- TTL / 序列化 / null 处理各自实现

**缺什么**:
- `SingleFlight(key, ttl=60)` async context manager：进入即抢锁，离开释放；超时自动续期；冲突时抛 `SingleFlightConflict`
- typed `Registry[T](name, schema)` capability：`registry.set(key, value)` / `registry.get(key)` / `registry.list()` / `registry.delete(key)`
- `Set[T](name)` capability：`banned_words.contains(word)` / `banned_words.add(word)`，下面是 Redis SADD/SMEMBERS

**v6 目标**: 业务节点不引 redis client；single-flight / registry / set 都走 typed capability。

**不留隐患约束**:
- `grep -rn "redis.set(.*nx=True\|redis.eval(\|redis.smembers(\|redis.sadd(" apps/agent-service/app/{nodes,agent,chat,life,memory}/` = 0
- `grep -rn "from app.infra.redis\|from app.infra import redis" apps/agent-service/app/{nodes,agent,chat,life,memory}/` 仅 capability 实现层命中

---

#### Gap 16 — 外部 / 内部 HTTP client 绕 capability

**Framework 现状**: `agent/tools/search.py` / `agent/tools/image_search.py` 直接 `httpx.AsyncClient(...)` + 手塞 API key / lane / trace；`skills/sandbox_client.py` 自己组 lane / trace / auth header。

**业务绕痕**: 每个 HTTP 调用都重复实现 retry / timeout / lane header / trace header / API key 注入 / 错误处理。

**缺什么**:
- `runtime/http_client.py` 提供 `HTTPClient(base_url, *, auth, default_headers)`，自动注入 trace_id / lane（按 Gap 11 propagation primitive）
- 域 capability：`search_capability(query) -> SearchResults` / `image_search(query) -> ImageResults`，下面用 HTTPClient 实现
- API key 注入走 dynamic config（`/internal/dynamic-config/resolved`），业务节点不写死 env var 名

**v6 目标**: 业务节点 0 处 import httpx；调外部服务走 typed capability。

**不留隐患约束**:
- `grep -rn "import httpx\|from httpx" apps/agent-service/app/{nodes,agent,chat,life,memory}/` 仅在 capability 实现层命中

---

### 7e 范围（streaming / async join / lifecycle 收口）

#### Gap 10 — streaming / segment 协议泄漏

**Framework 现状**: chat 段输出走 fan-out emit 多段 `ChatResponseSegment`，业务代码自定义 `part_index` / `is_last` / `full_content` 字段。Phase 5a 已实践证伪 `Stream[T]` 抽象，删除该路径。

**业务绕痕**: 任何新业务想做「分段流式输出」必须自己定义字段名（`part_index` / `is_last` / `full_content`），可能跟 chat 的字段名冲突或不一致。

**缺什么**:
- 共享 `Segment[T]` mixin / capability：声明 `segment_id` / `part_index` / `is_last` / `payload` 标准字段
- `wire(SomeSegment).to(...)` 自动按 segment_id 聚合（消费者收到完整 stream 而非散段）
- chat-response-worker 兼容窗口：先支持 mixin 字段名，后续迁移现有 ChatResponseSegment

**v7 目标**: 任何新流式业务复用 Segment capability，不自定义字段。

**不留隐患约束**:
- chat 既有 ChatResponseSegment 字段不动（兼容飞书侧）；新业务流必须用 Segment capability
- `grep -rn "part_index\|is_last" apps/agent-service/app/` 仅 `runtime/segment.py` + `chat/` 命中

---

#### Gap 17 — `/health` 仍是手写 route 例外

**Framework 现状**: Phase 6 v4 闭合 Gap 1 时，`/health` 作为 lifecycle 例外保留为手写 FastAPI route（`api/health.py`）。原因：runtime 启动顺序里没有「就绪信号」概念，业务声明的 wire 不能在还没注册时响应 health。

**业务绕痕**: 业务作者疑惑「为什么别的 endpoint 都走 wiring，health 不走」；新增 liveness / readiness 探针时不知道遵循哪条路。

**缺什么**:
- runtime 暴露 builtin health/liveness/readiness endpoint（`Source.http_health` / `Source.http_liveness` / `Source.http_readiness`），业务**完全不需要声明**
- 或：runtime 提供 `lifecycle.is_ready()` / `lifecycle.is_alive()` 钩子，业务在节点里调
- spec 决定：是 builtin endpoint 还是 lifecycle hook，**二选一不留例外**

**v7 目标**: `/health` / `/liveness` / `/readiness` 全部 builtin，业务代码 0 行手写 lifecycle route；或文档明确「lifecycle 一律走 lifecycle hook」。

**不留隐患约束**:
- `grep -rn "@router\.\|@app\." apps/agent-service/app/` = 0（含 `/health`）
- 文档化 lifecycle 决策（spec section + runbook）

---

#### Gap 19 — graph request/reply / async join 仍手写

**Framework 现状**: `nodes/chat_node.py` + `chat/pre_safety_gate.py` 用 `asyncio.create_task` + `Future` + race 实现「pre-safety 边界 await」（Phase 5a 设计已记录为合法例外）。但这只是手写编排，不是 framework primitive。

**业务绕痕**: 类似的「emit Y 等结果后再继续」需求出现时，业务作者必须 copy chat 的 task + Future 模式；模式不规范化会扩散。

**缺什么**:
- `await emit_request_reply(req, reply_type=ReplyData, timeout=5)` API：emit 后 await 直到对应 reply Data 出现（按 trace_id 关联）
- `await join(*data_types, timeout=...)` API：等多个异步事件汇合
- `Awaitable[Data]`-like primitive，下层用 contextvar + Future 实现

**v7 目标**: chat pre-safety 模式收敛到 framework primitive；新业务 0 行 `asyncio.create_task` + Future。

**不留隐患约束**:
- `grep -rn "asyncio.create_task\|asyncio.ensure_future" apps/agent-service/app/{nodes,agent,chat,life,memory}/` 0 命中（含 `chat_node.py` / `pre_safety_gate.py`，必须迁到 primitive）

---

## 3. PR 切分

| PR | 分支 | 范围 | Gap | 依赖 |
|---|---|---|---|---|
| 7a | `refactor/dataflow-parse-7` | transport 语义 | 7, 9, 11 | — |
| 7b | `refactor/dataflow-parse-7b`（待建） | reliability + error policy | 8, 12, 18 | 7a (Gap 11 propagation primitive) |
| 7c | `refactor/dataflow-parse-7c`（待建） | arq 退场 | 15 | 7a (Gap 7 retry), 7b (Gap 8 outbox) |
| 7d | `refactor/dataflow-parse-7d`（待建） | DB / Redis / HTTP capability | 13, 14, 16 | 7b (Gap 8 outbox) |
| 7e | `refactor/dataflow-parse-7e`（待建） | streaming / async join / lifecycle | 10, 17, 19 | 7a, 7b |

**为什么不并入一个 PR**：Phase 6 v4 经验是 «一个 PR 13 gap diff 太大，泳道验证负担成倍«。Phase 7 切 5 个 PR 单 PR diff 控在 +1500/-500 内，每 PR 单独泳道验证 + ship。

**为什么 7a 先**：transport 是其他 4 个 PR 的依赖（retry / propagation / delayed emit 是 reliability + arq + segment 都要用的 primitive）。

**为什么 7e 最后**：streaming / async join 涉及 ts 侧（chat-response-worker），需要 ts/python 双向兼容窗口；放最后单独验证 ts/python 联调。

### 7a commit 切分（本期）

1. `docs(spec): Phase 7 gap analysis + 7a transport plan` (spec + plan 落盘，review 后再开干)
2. `feat(runtime): propagation primitive — extract / inject / bind context` (Gap 11 框架)
3. `refactor(runtime): durable / debounce / source-mq / sink-dispatch use propagation primitive` (Gap 11 切换)
4. `feat(runtime): cron / interval source auto-generate trace_id` (Gap 11 补强)
5. `feat(runtime): runtime_inflight schema + state machine + lease + history backfill` (Gap 7.1 状态机；含 (edge_id, idempotent_key) 复合 PK、locked_until lease、row missing 时 Data 表兼容检查；替换 insert_idempotent 调用点)
6. `feat(runtime): publish_with_confirm + durable retry transport` (Gap 7.2 application-level retry + ack-after-confirm + x-delivery-count header)
7. `feat(runtime): wire(...).durable().retry(n, backoff, lease_ms) DSL` (Gap 7.3 DSL 字段，handler 接入)
8. `feat(runtime): in-process scheduled task pool` (Gap 9.2 best_effort 实现)
9. `feat(runtime): runtime_delayed_trigger_{app} queue + internal consumer` (Gap 9.1.2/9.3 框架内部 trigger 通道；按 origin_app + lane 隔离声明 queue + DelayedTriggerEnvelope(origin_app, origin_lane, ...) + internal consumer 校验 origin_app 后调 emit(data))
10. `feat(runtime): emit_delayed / emit_at top-level API with durability param` (Gap 9 整合 + 默认 durable 走 trigger queue + best_effort 走 scheduled task)
11. `chore(ci): grep gate for Gap 7+9+11 closed + baseline for Gap 13/14/15/16/19 open` (per-PR CI gate；closed gap exact-zero、open gap baseline no-new；见 §4.1)

每 commit 自包含 + 测试 green + ruff 通过 + 当前 commit 关闭的 gate 加进 CI（commit 11 兜底统一加 + 创建 baseline 文件）。

### 7b/c/d/e commit 切分

各 PR 独立 spec 节段会列；本 spec 仅给框架。

## 4. 验收（per-PR CI gate + 全期 E2E drill）

**核心原则**：CI 必须同时守住「已关闭 gap 清零」和「未关闭 gap 不增量」。每个 PR ship 后立即更新 CI gate；后续 PR 任何一项触发都阻塞合并。

**两类 gate**：

1. **Closed gap → exact zero**：本期已闭合的 surface，grep 命中数必须 == 0。CI 用 `grep ... | wc -l` 与 0 比较。
2. **Open gap → no new occurrences (baseline gate)**：尚未闭合但 spec 已纳入 surface 的 gap。CI 仓库内提交基线计数文件 `.github/grep-baselines.json`：

```json
{
  "gap_13_get_session": 142,
  "gap_14_redis_setnx_business": 8,
  "gap_15_arq_imports": 17,
  "gap_16_httpx_business": 6,
  "gap_19_create_task_business": 12
}
```

CI 跑 grep 数计：count > baseline 时阻塞；count <= baseline 通过；显著下降时提示更新 baseline（但不强制——baseline 在该 gap 关闭的 PR 里集中清零）。这样后续 PR 不能在 open surface 上继续添加 workaround，与 memory `feedback_spec_capability_gap_surface.md`「未做 gap 业务代码不准 workaround」对齐。

CI 文件位置：`.github/workflows/ci.yml`（如不存在则新建独立 `grep-gate.yml` workflow）；每个 PR 合并前 CI 必须 green。`grep-baselines.json` 在闭合对应 gap 的 PR 里删除该条。

### 4.1 Phase 7 全期硬验收（5 个 PR ship 后跑一次）

#### CI grep gate（按 allowlist 区分 runtime / capability / infra 边界）

```
# Gap 7
grep -rn "while.*retry\|asyncio.sleep" apps/agent-service/app/{agent,chat,life,memory,nodes,api}/ | wc -l == 0

# Gap 8
grep -rn "# emit AFTER commit\|# commit-then-emit" apps/agent-service/app/ | wc -l == 0

# Gap 9
grep -rn "asyncio.sleep" apps/agent-service/app/{agent,chat,life,memory,nodes}/ | wc -l == 0

# Gap 11
grep -rn "trace_id_var\|lane_var\|headers\[\"trace_id\"\]\|headers\[\"lane\"\]" apps/agent-service/app/ \
  | grep -v "runtime/propagation.py\|runtime/middleware.py\|infra/rabbitmq.py" | wc -l == 0

# Gap 12
test -f apps/agent-service/scripts/dlq_cli.py  # CLI 必须存在
test -f docs/runbooks/dlq-replay.md  # runbook 必须存在

# Gap 13
grep -rn "get_session(\|AsyncSessionLocal" apps/agent-service/app/{nodes,agent,chat,life,memory,long_tasks}/ | wc -l == 0

# Gap 14
grep -rn "redis.set(.*nx=True\|redis.eval(\|redis.smembers(\|redis.sadd(" apps/agent-service/app/{nodes,agent,chat,life,memory}/ | wc -l == 0

# Gap 15
grep -rn "arq\|enqueue_job\|create_pool\|from arq" apps/agent-service/app/ | wc -l == 0
grep -q "arq" apps/agent-service/pyproject.toml && exit 1  # arq 不应在依赖中

# Gap 16
grep -rn "import httpx\|from httpx" apps/agent-service/app/{nodes,agent,chat,life,memory}/ | wc -l == 0

# Gap 17
grep -rn "@router\.\|@app\." apps/agent-service/app/ | wc -l == 0

# Gap 18
grep -rn "# 不要 catch\|# don't catch\|requeue=False\|nack" apps/agent-service/app/{nodes,agent,chat,life,memory}/ | wc -l == 0

# Gap 19
grep -rn "asyncio.create_task\|asyncio.ensure_future" apps/agent-service/app/{nodes,agent,chat,life,memory}/ | wc -l == 0
```

#### Contract test 三类齐全

每个 runtime primitive 必须有：
1. **Compile-time validation**：DSL 错用立即抛（pytest 单测）
2. **Unit contract test**：mock infra，验证 primitive 行为
3. **Integration / lane test**：真 RabbitMQ + 真 Redis + lane 路由验证

#### E2E 证据

- 飞书 dev bot 群聊 + p2p 正常对话
- proactive / glimpse 触发主动消息
- update_schedule → state sync 节点跑通
- vectorize-worker 消费正常
- admin HTTP RPC（GET / POST / DELETE）功能等价
- **durable failure → DLQ → replay drill**：人为制造节点 raise，验证消息进 DLQ；运维 CLI replay 后消息成功消费
- Langfuse trace 完整（cron 触发链路 / debounce 触发链路 / durable retry 链路全部连续）

### 4.2 7a 本期硬验收

#### CI gate（必须随 7a PR 一起进 CI）

```bash
# Gap 7：业务代码不准自实现 retry / sleep；不准自管 idempotent
grep -rn "while.*retry\|for.*range.*retry" apps/agent-service/app/{agent,chat,life,memory,nodes,api}/ | grep -v "__pycache__\|test_" | wc -l   # == 0
grep -rn "asyncio.sleep" apps/agent-service/app/{agent,chat,life,memory,nodes,api}/ | grep -v "__pycache__\|test_" | wc -l                # == 0
grep -rn "insert_idempotent\b" apps/agent-service/app/ | grep -v "runtime/\|__pycache__\|test_" | wc -l                                    # == 0

# Gap 9：业务代码不准自实现延迟
grep -rn "asyncio.sleep" apps/agent-service/app/{agent,chat,life,memory,nodes}/ | grep -v "__pycache__\|test_" | wc -l                    # == 0

# Gap 11：业务代码不准直接读写 contextvar / header
grep -rn "trace_id_var\|lane_var" apps/agent-service/app/ \
  | grep -v "runtime/propagation.py\|runtime/middleware.py\|api/middleware.py\|infra/rabbitmq.py\|__pycache__\|test_" | wc -l               # <= 3 (publish_durable data.lane fallback + sink_dispatch lane field read)
grep -rn "headers\[\"trace_id\"\]\|headers\[\"lane\"\]" apps/agent-service/app/ | grep -v "runtime/propagation.py\|__pycache__\|test_" | wc -l   # == 0
```

#### 行为验收（dev 泳道）

- `tests/runtime/test_propagation.py` / `test_emit_delayed.py` / `test_durable_retry.py` / `test_inflight_state_machine.py` / `test_scheduled.py` 全 green
- 现有 `test_durable.py` / `test_debounce.py` / `test_emit_*.py` 不破坏（refactor 后业务可观测行为完全等价）
- runtime migrator 自动建 `runtime_inflight` 表（部署日志可见）
- 飞书 dev bot 群聊 + p2p 正常对话，Langfuse trace 完整
- cron 触发的 minute_tick 链路在 Langfuse 上能看到 `cron:` 开头的 trace_id（不再断链）

#### Retry drill（必须复现一次）

挂临时 wire `wire(SafetyCheckRequest).to(failing_node).durable().retry(n=3, backoff="exponential", base_delay_ms=200, max_delay_ms=2000)`，failing_node 必抛 RuntimeError。emit 一条 SafetyCheckRequest，观察：

1. `runtime_inflight` 出现 `(edge_id, idempotent_key)` 行：`state='processing', attempts=1, locked_until≈now()+5min, worker_id='<host>:<pid>'`
2. 第一次抛错 → row 转 `state='failed', attempts=1, locked_until=NULL, worker_id=NULL, last_error=RuntimeError(...)` → mq 后台 republish 带 `x-delay≈200ms` + `x-delivery-count=1`
3. ~200ms 后 handler 再次进入 → row 转 `state='processing', attempts=2, locked_until≈now()+5min` → 抛错 → row 转 `state='failed', attempts=2` → republish `x-delay≈400ms` + `x-delivery-count=2`
4. 第 3 次同样抛错 → `attempts=3 == n` → DLQ + row 终态 `state='failed', attempts=3, last_error=RuntimeError(...)`
5. RabbitMQ DLQ 看到该消息

并发 drill（额外验证 lease）：

6. worker A 持锁 processing 期间，再投同 (edge_id, idempotent_key) 的消息 → handler skip + ack（看 `state='processing' AND locked_until > now()`）；row 不变
7. 手工 `UPDATE runtime_inflight SET locked_until = now() - INTERVAL '1 second'` 模拟 lease 过期 → 再投同消息 → handler 接管，row 转 `state='processing', attempts=attempts+1, worker_id` 改为新 worker

兼容历史 backfill drill：

8. drill 前：手工 INSERT 一条 Data 表 row（模拟升级前已处理消息）→ 不写 inflight 行
9. emit 同 idempotent_key 消息 → handler row missing 分支 → SELECT Data 表 hit → INSERT inflight `state='succeeded', trace_id='backfill'` + ack；consumer 不被调用

drill 完后立刻 revert 临时 wire 和手工写入的行，不污染线上。drill 截图 / 日志记录到 `docs/superpowers/retrospectives/2026-05-XX-phase7a-retry-drill.md`

## 5. Out of Scope（Phase 7 明确不做）

- 业务功能变化（任何用户感知层面）
- 新业务功能添加
- agent tool 内部业务逻辑变化（仅扩 framework + 业务调用方迁移到新 API）
- DB schema 重构（除新增 `outbox` 表 / DLQ replay 审计表，皆 runtime migrator 自动建）
- ts 侧（lark-server / lark-proxy / chat-response-worker）大改：仅按需保留兼容字段
- runtime 之外的基础设施重构（Redis / RabbitMQ / Postgres 拓扑不动）
- 监控告警体系扩展（沿用现网 Prometheus + Loki）

## 6. 风险与回滚

### 风险

#### 7a 风险

- **Gap 7.1 状态机 + 业务执行非幂等**：状态机让重投真能跑到 consumer，但如果业务节点本身有副作用（写外部系统），重试会让副作用执行多次。**对策**：retry policy 默认不开（`.retry(n=...)` 显式声明）；声明 retry 的 wire 必须在节点 docstring 注明「业务执行幂等」；spec Gap 18 (7b) 引入 `manual-review` error policy 处理非幂等场景
- **Gap 7.1 advisory lock 跨进程**：同 idempotent_key 的并发 handler 必须串行化。pg_advisory_xact_lock 默认按 transaction 释放——这里要求事务是 `INSERT/SELECT/UPDATE runtime_inflight`，consumer 调用在事务**之外**（避免长事务持锁）。**对策**：handler 把 inflight 状态读写放独立短事务，consumer 调用前 commit；contract test 覆盖「lock 释放后另一 worker 能看到最新 state」
- **Gap 7.1 worker 死后 row 卡 processing**：处理「state=processing 视为可执行」语义，但要求 broker 把消息重投回来——只有 broker 重投触发才会跑 consumer，row 自身不会自动 retry。**对策**：观测告警（`runtime_inflight` 中 `state='processing' AND updated_at < now() - INTERVAL '10 min'` 的行数）放进监控（Phase 8+，本期不做）；本期 docstring 警告
- **Gap 7.2 publish-confirm 失败**：broker 完全断连时 publish-with-confirm 失败 → 原消息走 DLQ 兜底。这条路径**业务可观测**（运维看到 DLQ 出现消息），不丢但需手工 replay。**对策**：drill 时把 RabbitMQ 拿掉一段时间验证；docstring 明确"publish 失败 → DLQ"
- **Gap 9 trigger queue 错配 origin_app / lane → wrong-process emit**：runtime_delayed_trigger 按 `(origin_app, lane)` 隔离，envelope 携带 origin_app 双重校验；如果 queue 注册 / consume 路径错配（如某个 app 漏声明自己的 trigger queue，或 lane queue fallback 到 prod），延迟到期后会被错误进程消费，emit() 的 `APP_NAME` / 代码版本 / lane fan-out 全部跑偏。**对策**：a) 启动时所有已知 `APP_NAME` 必须声明 trigger queue（runtime startup contract test 验证）；b) lane queue 强制 `lane_fallback=False`；c) consumer 反序列化时 `origin_app != APP_NAME` 直接 log error + ack（防御性兜底）；d) drill 期间观察跨 lane / 跨 app 的延迟事件不应被错配进程消费
- **Gap 9 in-process scheduled task 泄漏**：runtime stop 时未取消的 task 继续跑下个进程实例。**对策**：`runtime/scheduled.py` 维护 task set，`stop_source_loops` 时全部 `task.cancel()` + `await gather`
- **Gap 11 propagation primitive 抽象错位**：`bind_context` 必须在 finally / contextmanager 退出时严格 reset，否则跨 wire 串味（已是当前痛点）。**对策**：契约测「bind 后 reset，下一次调用读到旧值或 None」

#### Phase 7 全期风险

- **PR 跨度长（5 个 PR，~3-5 周）→ main 漂移**：每个 PR 完成后立即 rebase 后续分支
- **chat-response-worker（ts）兼容窗口管理**：body-level lane 字段切 header 必须分两步（先双写 header + body，验证 1 周后 ts 切读 header，再删 body）。**对策**：每步 ship 后写 retrospective，明确切换时点

### 回滚

回滚不是无脑独立的，依赖关系如下：

- **7a ship 后短窗口（< 1 个 PR 周期）独立可 revert**：trace propagation / retry / inflight 状态机抽象的依赖方还没合入，单独 revert 不影响其他模块。窗口内发现严重问题优先 revert。
- **7b+ 合入后必须按依赖栈反向回滚**：7b/c/d/e 的实现都依赖 7a 的 propagation / retry / inflight primitive。如果在 7c 上线后发现 7a 的某项问题，回滚顺序必须是 7c → 7b → 7a，不能直接 revert 7a。每个 PR 的 retrospective 必须更新依赖栈记录。
- **schema 只 additive，不自动 drop**：runtime migrator 创建的表（`runtime_inflight`、Phase 7b 的 `outbox` 等）只 CREATE，不 DROP。即使 PR 被 revert 也保留表（业务无影响，下次升级直接复用），避免「revert PR + 再次合入」时因表已存在 / 已删除导致的 migration 失败。GC / 清理通过单独的 ops SQL 工单处理。
- **CI gate 配套回滚**：revert 业务 PR 时同时 revert `.github/workflows/ci.yml` / `grep-baselines.json` 对应改动，保持 CI 状态与代码一致。
- **commit 级别 revert**：单 PR 内的 commit 在 PR merge 前可独立 revert（rebase 重排）；merge 后只能整 PR revert（squash 模式），单 commit 级粒度不再可用。

## 7. Phase 8+ 候选（仅记录，不实施）

Phase 7 关闭 Gap 7-19 后，下一轮关注点：

- **Phase 8**: dataflow primitive 跨语言（ts / go runtime 共享同一 protocol，lark-server / lark-proxy / chat-response-worker 用 ts runtime 而非手写 mq consumer）
- **Phase 9**: 测试框架（dataflow-test）—— 业务节点测试不再需要 mock framework，统一 fixture
- **Phase 10**: 可观测增强（Langfuse 自动 instrument 所有 wire / source / capability，无需业务声明 trace span）

每版独立 spec / plan / PR，不再合并。

## 8. 文档与索引

本 spec 完成后立即更新：
- `MEMORY.md` 索引：`project_dataflow_phase7.md` 指向本文件
- `project_dataflow_phase7.md`：spec 文件名修正为 `2026-05-08-...`，补 7a-7e 分支命名约定
- 7a plan：`docs/superpowers/plans/2026-05-08-dataflow-phase-7a-transport.md`
