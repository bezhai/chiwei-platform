# Dataflow Phase 2 — Safety 管线进 Graph

**状态**: Draft v5 (2026-04-28，已吸收 reviewer 第 5 轮意见)
**前置**: PR #198 (Phase 0+1) shipped to prod 1.0.0.313；后续 followups 已闭环
**后续**: Phase 3 Drift / Afterthought（消灭 in-memory debouncer + 落地 `.debounce()` runtime）

## 1. 背景

Phase 0+1 把 dataflow runtime 框架（`app/runtime/*`）和 vectorize/memory_vectorize 两条管线落地。Phase 2 把 safety 管线（`safety_pre` / `safety_post`）从"chat 流程的副作用 + 一条手写 RabbitMQ 队列"改造成 graph 上的节点 + wire。

**验收点**（roadmap）：
- safety 节点签名 `(... Data) -> SafetyVerdict`
- `mq.publish(SAFETY_CHECK, ...)` 从 safety 模块消失（队列 `safety_check` 被 `.durable()` 替掉）

## 2. 现状

### Pre-check（请求路径，同步 race）

`apps/agent-service/app/chat/safety.py:run_pre_check(content, persona_id) -> PreCheckResult`

跑 4 个并行检查（banned word / prompt injection / 敏感政治 / NSFW），20s timeout，fail-open。在 `pipeline.py:99-102` 启动：

```python
pre_task = asyncio.create_task(
    run_pre_check(parsed.render(), persona_id=effective_persona)
)
```

`_buffer_until_pre`（`pipeline.py:316`）race 两个 task：pre_task 先 block → 取消 stream，输出 guard_message；stream 先到 EOF → 等 pre 完成。

### Post-check（异步队列）

`pipeline.py` 完成后调 `chat/post_actions.py:_publish_post_check`，`mq.publish(SAFETY_CHECK, ...)` 把 payload 扔进 `safety_check` 队列。

`workers/post_consumer.py:handle_safety_check` 消费：跑 `run_post_check(response_text)` → blocked 写 `safety_status=blocked` + `mq.publish(RECALL, ...)`；passed 写 `safety_status=passed`。

`main.py:62` 在 FastAPI lifespan 里通过 `start_post_consumer()` 启动这个 consumer。`safety_status` 是 `agent_responses` 表的字段（lark-server 那边维护的 schema），`session_id` 是 unique key（`apps/lark-server/src/infrastructure/dal/entities/agent-response.ts:18`）。

## 3. 目标架构

```
PreSafetyRequest -> run_pre_safety -> PreSafetyVerdict -> resolve_pre_safety_waiter
PostSafetyRequest --durable adoption--> run_post_safety -> Recall | None
Recall -> Sink.mq("recall")
```

**两条管线，两种形态**：

| | Pre | Post |
|---|---|---|
| 触发方 | chat pipeline `emit(PreSafetyRequest)` | chat pipeline `emit(PostSafetyRequest)` |
| Wire 模式 | in-process | `.durable()` |
| 跨进程 | 否（agent-service 进程内） | 是（durable consumer in agent-service） |
| 结果回路 | 本地 Future registry（waiter） | 节点内部写 `agent_responses.safety_status` + 返回 `Recall \| None` 走 sink |
| Race 行为 | chat pipeline 内保留 `_buffer_until_pre` | 不需要（异步管线） |
| 幂等机制 | 无（请求路径 best-effort） | **业务侧** — 节点开头查 `safety_status`，落在 `TERMINAL_STATUSES = {passed, blocked, recalled, recall_failed}` 任一直接短路 |

### 3.1 Data 类（`apps/agent-service/app/domain/safety.py`）

```python
class PreSafetyRequest(Data):
    pre_request_id: Annotated[str, Key]   # 每次 pre-check 独立 uuid4，不复用 session_id
    message_id: str
    message_content: str
    persona_id: str

    class Meta:
        transient = True

class PreSafetyVerdict(Data):
    pre_request_id: Annotated[str, Key]
    message_id: str
    is_blocked: bool
    block_reason: str | None = None  # BlockReason.value 字符串化
    detail: str | None = None

    class Meta:
        transient = True

class PostSafetyRequest(Data):
    """Adopts the existing ``agent_responses`` table.

    The row is INSERTed by lark-server when chat completes; agent-service
    only emits this Data type as a durable trigger. ``session_id`` is the
    unique business key on agent_responses (no auto dedup_hash column),
    so durable consumers run business-side idempotency via safety_status.
    """
    session_id: Annotated[str, Key]
    trigger_message_id: str
    chat_id: str
    response_text: str

    class Meta:
        existing_table = "agent_responses"
        dedup_column = "session_id"

class Recall(Data):
    session_id: Annotated[str, Key]
    chat_id: str
    trigger_message_id: str
    reason: str
    detail: str | None = None
    lane: str | None = None  # lark-server recall-worker 从 payload.lane 读，必须带

    class Meta:
        transient = True
```

**关键变化（vs draft v1）**：
- `PreSafetyRequest.pre_request_id` 用 uuid4 而非复用 `session_id`：避免同一 session 并发或 DLQ replay 时 waiter Future 互相覆盖
- `PostSafetyRequest` adopt `agent_responses` 表，**不新建 `data_post_safety_request` 表**：durable handler 在 adoption 模式下跳过 `insert_idempotent`（`runtime/durable.py:130-140`），重放时 consumer 重跑，业务侧用 `safety_status` 短路
- 删掉了 v1 的 `PostSafetyDecision` 中间 Data：post 链路合并成一个节点（见 3.2）

**约束验证**：transient Data 不能跟 `.durable()` 共存（`runtime/graph.py:170-192`，需 pg 表做 dedup）。`PostSafetyRequest` 用 adoption mode 满足"有真实 pg 行"的要求，且对应字段全部是 agent_responses 已有 column。其余三个 transient（不落表）。

### 3.2 节点（`apps/agent-service/app/nodes/safety.py`）

三个节点。Pre 链 2 个，Post 链 1 个（合并了 v1 的 audit + apply 两步）。

```python
@node
async def run_pre_safety(req: PreSafetyRequest) -> PreSafetyVerdict:
    """跑 4 个并行 pre-check（banned word + injection + politics + nsfw）。
    内部调用同模块的 `_check_*` 私有 helper。"""
    ...

@node
async def resolve_pre_safety_waiter(verdict: PreSafetyVerdict) -> None:
    """把 verdict 塞回本进程的 Future registry。"""
    pre_safety_gate.resolve(verdict)
    return None

TERMINAL_STATUSES = {"passed", "blocked", "recalled", "recall_failed"}

@node
async def run_post_safety(req: PostSafetyRequest) -> Recall | None:
    """Audit + 决定是否撤回。Blocked 路径不写 status="blocked"，
    由 recall-worker 写最终 status。

    业务幂等：节点入口查 safety_status，落在 TERMINAL_STATUSES 任一直接短路。
    Adoption mode 跳过 runtime dedup → DLQ replay 时节点会被重跑，
    业务幂等保证已审消息不会重复处理。

    Blocked 路径：return Recall，@node 装饰器自动 emit Recall →
    wire(Recall).to(Sink.mq("recall")) → recall-worker 消费 → 撤回 →
    写 status="recalled" 或 "recall_failed"（recall-worker 自身行为，
    见 apps/lark-server/src/workers/recall-worker.ts:101）。
    run_post_safety **不再写 status="blocked"**，避免 recall-worker
    写终态后被覆盖的 race（reviewer round 3 high risk 2）。

    Passed 路径：直接写 status="passed"（终态）。

    DLQ replay 边界 case：
      - emit Recall 失败（mq 抖动 → @node 装饰器抛 → handler 抛 → DLQ）：
        status 仍为 pending → 人工 replay 时短路逻辑判 pending → 重跑
        audit + 重新 emit Recall。dup recall 在 lark-server 表现为 API
        二次撤回失败（见 §4.4），通过 DLQ replay runbook 控制。
      - emit Recall 成功，handler 后续无副作用（不写 status）→ 永远不
        会因 status 写入失败进 DLQ。
    """
    async with get_session() as s:
        current = await get_safety_status(s, req.session_id)
    if current is None:
        # Invariant 违反：lark-server 应在 chat 完成时已 INSERT
        # agent_responses。继续执行会让 set_safety_status("passed") 静默
        # UPDATE 0 行，或让 recall-worker 因为找不到 row 反复重试到 DLQ
        # 后状态永远停在 pending。raise 让 durable handler 进 DLQ，由人
        # 工查 lark-server INSERT 链路（reviewer round 4 H/M issue 2）。
        raise RuntimeError(
            f"agent_responses row missing for session_id={req.session_id}; "
            f"lark-server must INSERT before agent-service emits "
            f"PostSafetyRequest"
        )
    if current in TERMINAL_STATUSES:
        logger.info(
            "post safety short-circuit: session_id=%s already %s",
            req.session_id, current,
        )
        return None

    decision = await _run_audit(req.response_text)  # banned word + LLM output audit
    checked_at = datetime.now(UTC).isoformat()

    if decision.is_blocked:
        # 不写 status——recall-worker 会写 recalled / recall_failed 终态
        return Recall(
            session_id=req.session_id,
            chat_id=req.chat_id,
            trigger_message_id=req.trigger_message_id,
            reason=decision.reason or "unknown",
            detail=decision.detail,
            lane=get_lane(),
        )
    else:
        async with get_session() as s:
            await set_safety_status(s, req.session_id, "passed",
                                    {"checked_at": checked_at})
        return None
```

**节点设计要点**：
- **不手动 emit**：blocked 路径 `return Recall` 让 @node 装饰器走标准 dispatch，遵守 `runtime/node.py:13` 的契约
- **不写 status="blocked"**：blocked 路径只 emit Recall；recall-worker 写最终 `recalled` / `recall_failed`。"blocked" 在新链路下不再是中间状态，但留在 `TERMINAL_STATUSES` 短路集合里，**为了兼容迁移期间旧 post_consumer 写过 "blocked" 的遗留行**（旧 consumer 切换前若已写 blocked 但 recall-worker 还没 ack，replay 看到 "blocked" 应该跳过避免 dup recall）
- **业务幂等用 `safety_status` 短路**：terminal 状态集合包含 passed / blocked（迁移兼容） / recalled / recall_failed
- **签名名副其实**：blocked 真返 Recall（自动 emit），passed 真返 None
- **新增 helper `get_safety_status`**：当前 `app/data/queries.py` 只有 set，需新增 get（见 §3.9）

**已知监控影响**：旧链路下 status 路径是 `pending → blocked → recalled/recall_failed`，"blocked" 是几秒钟瞬态。新链路下 `pending → recalled/recall_failed` 直接跳，"blocked" 不再出现（除迁移期遗留）。任何用 `WHERE safety_status='blocked'` 的监控查询需要适配（实际上由于"blocked"瞬态时间极短，这种查询本身价值低）。

### 3.3 Wiring（`apps/agent-service/app/wiring/safety.py`）

```python
wire(PreSafetyRequest).to(run_pre_safety)
wire(PreSafetyVerdict).to(resolve_pre_safety_waiter)
wire(PostSafetyRequest).to(run_post_safety).durable()
wire(Recall).to(Sink.mq("recall"))
```

`bind` placement（`apps/agent-service/app/runtime/placement.py`）：
- `run_pre_safety` / `resolve_pre_safety_waiter` → `agent-service`（请求路径，跟 chat pipeline 同进程）
- `run_post_safety` → `agent-service`（替代旧 `start_post_consumer`，跑在 FastAPI 主进程）

不开新 `safety-worker` Deployment：post-safety 工作量小（一次 banned word + 一次 guard LLM 调用），跟 vectorize/embedding 那种重 IO 不一样，复用 agent-service 进程合理。

### 3.4 本地 waiter registry（`apps/agent-service/app/chat/pre_safety_gate.py`）

```python
_waiters: dict[str, asyncio.Future[PreSafetyVerdict]] = {}

def register(pre_request_id: str) -> asyncio.Future[PreSafetyVerdict]:
    fut = asyncio.get_running_loop().create_future()
    _waiters[pre_request_id] = fut
    return fut

def resolve(verdict: PreSafetyVerdict) -> None:
    fut = _waiters.get(verdict.pre_request_id)
    if fut is None or fut.done():
        return  # caller 已经超时清理 / 不存在 — 安全无操作
    fut.set_result(verdict)

def cleanup(pre_request_id: str) -> None:
    _waiters.pop(pre_request_id, None)

async def run_pre_safety_via_graph(
    message_id: str, content: str, persona_id: str
) -> PreSafetyVerdict:
    """fail-open 在这里集中处理：超时/异常都转成 pass verdict。

    每次调用生成独立 pre_request_id，避免并发 / DLQ replay 时 future 互相覆盖。
    pre_request_id 跟 session_id 完全解耦。

    实现要点：
    1. emit() 是 in-process dispatch，会同步 await 整链路
       (run_pre_safety -> @node 自动 emit verdict -> resolve_pre_safety_waiter
       -> set future)。节点卡住时 await emit 也卡住，所以必须把 emit 包成
       独立 task。
    2. 用 asyncio.wait({fut, emit_task}, FIRST_COMPLETED) 而不是
       wait_for(shield(fut))：
       - emit_task 早失败（mq 抖动 etc）时立即触发 fail-open，不用等满 21s
       - fut 先完成时直接拿 verdict
    3. emit_task 收尾区分两条路径：
       - fut 成功：emit_task 这时通常已完成（emit 链路里 set future 是
         倒数第二步，set 后 wrapper 还要 return，但都是同步收尾）。
         await emit_task 等它收尾 + 收 exception，绝对不要 cancel。
       - 超时/异常：emit_task 还在跑（节点卡住）→ cancel + 吃掉
         CancelledError，避免 unhandled task exception。
    """
    pre_request_id = str(uuid.uuid4())
    fut = register(pre_request_id)
    emit_task: asyncio.Task = asyncio.create_task(
        emit(PreSafetyRequest(
            pre_request_id=pre_request_id,
            message_id=message_id,
            message_content=content,
            persona_id=persona_id,
        ))
    )

    timed_out = False
    try:
        done, _pending = await asyncio.wait(
            {fut, emit_task},
            timeout=21.0,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            # 21s 都没完成 — 节点卡住
            timed_out = True
            logger.warning("pre safety timeout: pre_request_id=%s", pre_request_id)
        elif fut in done:
            # 正常路径：等 emit_task 收尾（通常已经 done），传播任何 exception
            if not emit_task.done():
                await emit_task
            elif emit_task.exception():
                logger.error(
                    "pre safety emit failed after verdict: pre_request_id=%s, error=%s",
                    pre_request_id, emit_task.exception(),
                )
            return fut.result()
        else:
            # emit_task 先完成且 fut 还没 set —— emit 抛了异常，verdict 永远不会来
            assert emit_task.done()
            logger.warning(
                "pre safety emit failed before verdict: pre_request_id=%s, error=%s",
                pre_request_id, emit_task.exception(),
            )
    finally:
        if timed_out and not emit_task.done():
            emit_task.cancel()
            try:
                await emit_task
            except (asyncio.CancelledError, Exception):
                pass  # 已经 log 过，不再扩散
        cleanup(pre_request_id)

    # fail-open（timeout 或 emit 早失败）
    return PreSafetyVerdict(
        pre_request_id=pre_request_id, message_id=message_id, is_blocked=False
    )
```

**关键不变量**：
- chat pipeline 暴露的永远是 `PreSafetyVerdict`（is_blocked / reason / detail）。`_buffer_until_pre` 不需要知道结果是怎么来的
- 失败模式（timeout / emit 异常 / future cancelled）一律 fail-open（is_blocked=False），跟现有 `run_pre_check` 行为一致
- `pre_request_id` 是 chat 请求内的局部 id，不出 chat pipeline；不要拿 session_id 替代（reviewer 中风险 3）

### 3.5 Runtime 增量：Sink dispatch + compile-time 校验

**当前阻塞**（`runtime/graph.py:208-214`）：`compile_graph` 把"含 sinks 的 wire"列入 `unimplemented`，启动直接 raise。

**Phase 2 携带改动**：

1. **`graph.py`**：删除 `if w.sinks:` 那段 unimplemented 检查（debounce 那段保留 — Phase 3 才动）；**同时增加新校验**：所有 `Sink.mq(name)` 的 `name` 必须存在于 `ALL_ROUTES`，否则 raise GraphError。这个校验在启动阶段就抓出 typo / 漏注册路由的问题，不是等到第一次 emit 才暴露（reviewer 高风险 2）：
   ```python
   from app.infra.rabbitmq import ALL_ROUTES
   known_queues = {r.queue for r in ALL_ROUTES}
   for w in wires:
       for s in w.sinks:
           if s.kind == "mq":
               q = s.params["queue"]
               if q not in known_queues:
                   raise GraphError(
                       f"wire({w.data_type.__name__}).to(Sink.mq({q!r})): "
                       f"queue not in ALL_ROUTES; sink dispatch needs a "
                       f"registered route to know the routing key. "
                       f"Add Route({q!r}, ...) to ALL_ROUTES first."
                   )
   ```

2. **`emit.py`**：在 `emit(data)` 的 wire 循环里，处理 `w.sinks` 分支：
   ```python
   for s in w.sinks:
       if s.kind == "mq":
           await _dispatch_mq_sink(s, data)
   ```

3. **新增 `runtime/sink_dispatch.py`**：
   ```python
   async def _dispatch_mq_sink(sink: SinkSpec, data: Data) -> None:
       queue_name = sink.params["queue"]
       route = _route_by_queue(queue_name)
       # _route_by_queue 不会返回 None — compile_graph 已经把不存在的 queue
       # 拒了。这里再 None-check 是 defensive，不是预期路径。
       assert route is not None, f"compile_graph should have rejected {queue_name!r}"
       await mq.publish(route, data.model_dump(mode="json"))

   def _route_by_queue(queue_name: str) -> Route | None:
       from app.infra.rabbitmq import ALL_ROUTES
       for r in ALL_ROUTES:
           if r.queue == queue_name:
               return r
       return None
   ```

**为什么必须查 `ALL_ROUTES`**：lark-server recall-worker 通过 routing key `action.recall` 绑定队列；sink 直接用 queue 名 + 默认 routing key 会绑错。`ALL_ROUTES` 是 queue→routing-key 的权威映射。

### 3.6 Main lifespan 改造

`apps/agent-service/app/main.py`：

**移除**：
- `from app.workers.post_consumer import start_post_consumer`
- `consumer_tasks.append(asyncio.create_task(start_post_consumer()))`

**新增**：在 `declare_durable_topology()` 之后调用：
```python
from app.runtime.durable import start_consumers
await start_consumers(app_name="agent-service")
```

**关闭**：lifespan teardown 处加：
```python
from app.runtime.durable import stop_consumers
await stop_consumers()
```

`start_consumers(app_name="agent-service")` 自动按 `placement.bind` 过滤，只启动 `run_post_safety` 这一条 durable wire 的 consumer。如果将来 agent-service 加更多 durable wire 自动接进来。

**整文件删除**：`apps/agent-service/app/workers/post_consumer.py`（功能完全被 durable consumer 替代）。

### 3.7 Chat pipeline 接入点

**`apps/agent-service/app/chat/pipeline.py:99-102`**（pre 触发）：

```python
# 旧
pre_task = asyncio.create_task(
    run_pre_check(parsed.render(), persona_id=effective_persona)
)

# 新（pre_request_id 在 run_pre_safety_via_graph 内部生成，不复用 session_id）
pre_task = asyncio.create_task(
    pre_safety_gate.run_pre_safety_via_graph(
        message_id=message_id,
        content=parsed.render(),
        persona_id=effective_persona,
    )
)
```

`_buffer_until_pre` 接的 task 现在产 `PreSafetyVerdict` 而不是 `PreCheckResult`。修改它读结果的字段名（`is_blocked` / `block_reason` / `detail`），race 逻辑不动。

**`apps/agent-service/app/chat/post_actions.py:32-52`**（post 触发）：

```python
# 旧 _publish_post_check 整个删掉，改成
async def _publish_post_check(
    session_id: str, response_text: str, chat_id: str, trigger_message_id: str
) -> None:
    try:
        await emit(PostSafetyRequest(
            session_id=session_id,
            trigger_message_id=trigger_message_id,
            chat_id=chat_id,
            response_text=response_text,
        ))
        logger.info("Emitted PostSafetyRequest: session_id=%s", session_id)
    except Exception as e:
        logger.error("Failed to emit PostSafetyRequest: %s", e)
```

`from app.infra.rabbitmq import SAFETY_CHECK, mq` 这行 import 跟着删除。

### 3.8 新增 `get_safety_status` helper

`apps/agent-service/app/data/queries.py` 当前只有 `set_safety_status`，新增配套：

```python
async def get_safety_status(
    session: AsyncSession, session_id: str
) -> str | None:
    """Read safety_status from agent_responses; None if row doesn't exist."""
    result = await session.execute(
        text("SELECT safety_status FROM agent_responses WHERE session_id = :sid"),
        {"sid": session_id},
    )
    return result.scalar_one_or_none()
```

**Row 不存在的语义**（v3 改了一次：v3 把它当 fail-open 处理，v4 改成 raise）：
- `get_safety_status` 是普通查询函数，row 不存在返回 None，不带语义判断
- **节点 `run_post_safety` 入口判 None 时 raise**（见 §3.2），让 durable handler 进 DLQ
- 理由：row 不存在意味着 lark-server 还没写 agent_responses，但 agent-service 已经 emit PostSafetyRequest —— 这是 invariant 违反，不是常规路径。继续执行会让：
  - passed 路径 `set_safety_status` UPDATE 0 行无声丢失
  - blocked 路径 emit Recall，recall-worker 因为找不到 row 反复重试到 DLQ（见 §4.4），status 永远停在 pending
- raise 进 DLQ 是显式失败信号，让运维查 lark-server INSERT 链路

### 3.9 旧 `chat/safety.py` 处理

整文件合并进 `apps/agent-service/app/nodes/safety.py`，`chat/safety.py` 删除。

合并后 `nodes/safety.py` 的内部布局：
- module-level 私有 helper：`_check_banned_word` / `_check_injection` / `_check_politics` / `_check_nsfw` / `_check_output`（每个 LLM helper 用 module-level `_GUARD_*` AgentConfig）
- module-level 私有 `BlockReason` enum
- module-level 私有 `_run_audit(response_text)` → 包 banned word + LLM output audit，给 `run_post_safety` 调
- 节点 `run_pre_safety` / `resolve_pre_safety_waiter` / `run_post_safety`

理由：`chat/safety.py` 现在没有任何外部 import 它的代码（chat pipeline 改调 `pre_safety_gate.run_pre_safety_via_graph`，post_actions 改 emit）—— 保留就是死代码。把 helper 跟节点放同一文件，看一处就懂整条 safety 链路。

## 4. 失败模式 / 兼容性 / 迁移

### 4.1 旧 `safety_check` 队列

**当前事实**：`SAFETY_CHECK` route 只被 agent-service 自己 publish + consume。所以 deploy 完成的瞬间：
- 旧消费者（`start_post_consumer`）随进程退出停止
- 旧生产者（`_publish_post_check` 走 `mq.publish`）改成走 emit
- `safety_check` 队列里如果有 in-flight 消息，**没人消费 → 24h 后 prod 队列也没 TTL，会一直留**

**处理**：
1. 切换前确认 `safety_check_<lane>` 队列 backlog 为 0（运维查 RabbitMQ）
2. 切换后队列保留一段时间观察（durable 队列里没人发不会有新消息）
3. ship 稳定后从 `ALL_ROUTES` 移除 `SAFETY_CHECK` route + 手动删队列（独立 followup）

### 4.2 schema 影响

**没有新建表**。`PostSafetyRequest` 用 `Meta.existing_table = "agent_responses"`，runtime migrator 在 adoption 模式下不发 DDL（`runtime/migrator.py:200`）。`safety_status` / `safety_result` 字段在 `agent_responses` 表里**已存在**（lark-server entity 定义有这些 column），所以零 schema 变更。

### 4.3 Recall 兼容性

`apps/lark-server/src/workers/recall-worker.ts:42,68,83` 从 `payload.lane` 读 lane，所以 Recall Data 必须包含 lane 字段（已在 3.1 加上）。`run_post_safety` 节点产出 Recall 时填 `get_lane()`（见 3.2 示例）。

`Recall(...).model_dump(mode="json")` 产出的字段集与旧 schema **一致**（session_id / chat_id / trigger_message_id / reason / detail / lane）。Sink dispatch 走 `mq.publish(RECALL, body)` 时，`mq.publish` 内部还会按 `current_lane()` 选 lane 队列（routing key 加 lane 后缀）—— body 里的 `lane` 是给消费方读的，不影响路由。

### 4.4 DLQ replay 注意事项（runbook 补充）

Adoption mode 下 runtime 不做 dedup，consumer 每次都跑。`run_post_safety` 节点入口靠 `safety_status` 短路。短路集合 = `TERMINAL_STATUSES = {passed, blocked, recalled, recall_failed}`：

| Status (replay 入口) | replay 时节点行为 | 备注 |
|---|---|---|
| `null` (row 未 INSERT) | **raise → DLQ**（v4 改） | invariant 违反；查 lark-server INSERT 链路 |
| `pending`（默认，row 存在） | 跑 audit；blocked 路径 return Recall（自动 emit），passed 路径写 status="passed" | 正常路径 |
| `passed` | 短路 return None | 终态 |
| `blocked` | 短路 return None | 迁移期遗留状态（旧 post_consumer 写）；新链路不再产生 |
| `recalled` | 短路 return None | recall-worker 写的终态 |
| `recall_failed` | 短路 return None | recall-worker 写的终态 |

**已知边界 case**：
- 节点 audit 中途崩溃（return Recall 之前抛）→ status 仍 pending → replay 时重跑 audit + 重新决策 — **正确**
- emit Recall 触发的 sink dispatch 失败（mq 抖动）→ @node 装饰器抛 → handler 抛 → status 仍 pending → DLQ → 人工 replay → 重跑 audit + 重新 emit Recall — **dup recall！**
  - lark-server recall-worker 第二次执行：消息已撤回，飞书 API 返回 already-recalled，`recalledCount=0`，**会把 `safety_status` 从 `recalled` 改写成 `recall_failed`**（覆盖正确终态，是 UX 偏差不是数据丢失）
  - 接受偏差的理由：DLQ replay 是**人工操作**（runbook `docs/superpowers/runbooks/2026-04-dlq-replay.md`），不是自动重试
  - 缓解：runbook 加注释 — replay `durable_post_safety_request_run_post_safety` 队列消息之前先 `SELECT safety_status FROM agent_responses WHERE session_id=?`，落在 `TERMINAL_STATUSES` 任一的**不要 replay**

**recall-worker 找不到 replies 进 DLQ 的情况**（reviewer round 4 issue 3）：

`apps/lark-server/src/workers/recall-worker.ts:54-78` 当前逻辑：如果 `agent_responses` row 不存在或 `replies` 为空，重试 3 次（5/10/15s 延时）后 `nack(false)` 进 DLQ，**不写 safety_status**。

旧链路下这个分支不影响 status 观测，因为旧 post_consumer 已先写 `safety_status="blocked"`。新链路下 run_post_safety **不再写 blocked**，所以 recall-worker 这个分支命中时 status 会永远停在 `pending`，监控看不出来"已审完但 recall 没成"。

**Phase 2 范围内修这个 hole**（5 行 TS 改动，跟 agent-service 改动同步发布）：在 `apps/lark-server/src/workers/recall-worker.ts:73-78` 的 max retry 分支，nack 之前补一段 `repo.update({ session_id }, { safety_status: 'recall_failed', safety_result: { reason, detail, recalled: 0, failed: 0, checked_at: ... } })`。这样新链路 status 路径完整：`pending → recalled / recall_failed`，无 hole。

**lark-server 自身 recall 幂等**（基于 trigger_message_id 去重）是消除 dup recall 偏差的更彻底修法，但**不在 Phase 2 范围**，作为 followup 跟踪。

### 4.5 灰度

Pre 在请求路径，每条 chat 都过一次。Post 异步，跟 chat 解耦。两条同时切，但分开验证：

1. **泳道 deploy** → bind dev bot → 真实消息（pre 阻断 / pre 通过 / post block 触发 recall / post pass 写 status）四类都验证
2. **观测**：langfuse trace（pre/post 的 LLM 调用都有 trace）；rabbitmq backlog（旧 safety_check + 新 durable_post_safety_request_run_post_safety + recall）；postgres `agent_responses` 表 safety_status 字段实际写入
3. **回滚路径**：单 PR 改动较多，但都是替换同一职责。回滚就是 revert PR。adoption mode 没动 schema，无影响

## 5. 测试

### 5.1 节点单元测试

- `app.nodes.safety.run_pre_safety`：mock 同模块 `_check_*` 私有 helper，验证返回 `PreSafetyVerdict` 的 is_blocked / reason 字段映射
- `resolve_pre_safety_waiter`：register Future → 调节点 → assert future.result() == verdict；future 不存在不抛
- `run_post_safety`：
  - mock `get_safety_status` 返回 `TERMINAL_STATUSES` 各值（passed/blocked/recalled/recall_failed）→ 短路 return None，未调 `_run_audit`
  - mock `get_safety_status` 返回 None → **raise RuntimeError**（v4 改）
  - blocked 路径：mock `_run_audit` 返回 blocked → 节点 return Recall（不调 set_safety_status）
  - passed 路径：mock `_run_audit` 返回 pass → 节点 return None + 调 set_safety_status("passed")
- `get_safety_status`：row 存在返回 status；row 不存在返回 None
- `run_pre_safety_via_graph`：
  - 节点正常完成：返回 verdict
  - 节点超时（mock 节点 sleep > 21s）：fail-open + emit_task 被 cancel
  - emit 自身抛异常：fail-open + 异常被 log
  - 并发多次调用：每次独立 pre_request_id，互不干扰

### 5.2 端到端 emit 测试

- `run_pre_safety_via_graph`：emit + 等 future + cleanup 的全链路（用 in-memory graph + 真节点）
- 超时路径：mock 节点 sleep 大于 timeout，断言 fail-open
- 并发 pre_request_id：每次 uuid4 独立，不互相覆盖

### 5.3 Sink dispatch 测试

- `Sink.mq("recall")` + `wire(Recall).to(Sink.mq("recall"))` + emit Recall → 验证 `mq.publish` 被调用，参数是 RECALL route + 正确 payload
- **`compile_graph` 启动校验**：`Sink.mq("not_in_routes")` → `compile_graph()` raise GraphError

### 5.4 泳道集成测试

部署到 `phase2-safety` 泳道，**两个镜像都 deploy**：

- agent-service 镜像 → 同步发布 `agent-service` / `arq-worker` / `vectorize-worker` 三个 Deployment（Phase 2 改了 agent-service 主进程 + 删 post_consumer）
- lark-server 镜像 → 同步发布 `lark-server` / `recall-worker` / `chat-response-worker` 三个 Deployment（Phase 2 改了 recall-worker 的 max retry 分支）

bind dev bot 跑四类消息（pre block / pre pass / post block 触发 recall / post pass 写 status）。

## 6. 部署 & 切换

**Phase 2 跨两个镜像，必须同步发布**（按 CLAUDE.md "一镜像多服务同步" 铁律）：

| 镜像 | 改动内容 | 必须同步 release 的 Deployment |
|---|---|---|
| `agent-service` | 业务侧节点 + wiring + main lifespan + 删除旧 post_consumer + runtime sink dispatch | `agent-service`、`arq-worker`、`vectorize-worker` |
| `lark-server` | recall-worker max retry 分支补 `safety_status="recall_failed"` 写入 | `lark-server`、`recall-worker`、`chat-response-worker` |

切换步骤：

1. 泳道部署两个镜像 + bind dev bot，验证四种 case 全过
2. 检查 `safety_check_<lane>` 队列 backlog = 0
3. ship 顺序（先 lark-server 再 agent-service，让"max retry 写 recall_failed"先生效，避免 agent-service 切到新链路后命中老 recall-worker 的 hole）：
   1. release `lark-server` / `recall-worker` / `chat-response-worker` 到 prod
   2. 观察 5min，确认 recall-worker 正常消费 + 老链路无 regression
   3. release `agent-service` / `arq-worker` / `vectorize-worker` 到 prod
4. 部署后 5min 观察：
   - `make logs APP=agent-service KEYWORD="durable consumer started"` 出现 `durable consumer started: durable_post_safety_request_run_post_safety -> run_post_safety`
   - 新 chat 流入产生 `agent_responses.safety_status` 从 `pending` 变成 `passed`（pass 路径）或 `recalled` / `recall_failed`（block 路径，由 recall-worker 写终态）
   - 旧 `safety_check` 队列 message rate = 0
   - `recall_failed` 计数（如果有 recall 失败的 case）非零，证明 lark-server max retry 分支生效
5. 24h 稳定后启动 followup PR：`SAFETY_CHECK` route 移除 + 队列删除

## 7. 不在本期范围

- **Stream[T] @node 支持**：Phase 5 chat pipeline 重写时统一上。Phase 2 不动 `_buffer_until_pre` 的 race 模型。
- **Pre 通过 emit-await 解耦**（chat pipeline 不再持有 Future registry）：需要 runtime 加新 primitive。等 Phase 5 跟 Stream 一起设计。
- **`safety_check` 队列删除 + `SAFETY_CHECK` route 从 `ALL_ROUTES` 移除**：单独 followup PR，等 prod 稳定后做。
- **lark-server recall-worker 幂等**（基于 trigger_message_id 去重）：消除 DLQ replay 时 dup recall 的 UX 偏差，独立 followup。
- **PostSafetyRequest row-not-found DLQ 自动告警**：当前节点 raise 进 DLQ 后只有 `DeadLettersBacklog` 通用告警，没有专门的 "row missing" 监控。Phase 2 不加，等先观察实际发生频率。
- **Pre/Post 跨 persona 行为变化**：节点内部完全复用现有 `_check_*` helper，不改 LLM prompt 和阈值。

## 8. 验收 checklist

- [ ] `grep -rn "SAFETY_CHECK\|safety_check" apps/agent-service/app` 在 `chat/` 和 `workers/` 下零结果（route 定义本身保留到 followup）
- [ ] `grep -rn "mq.publish" apps/agent-service/app/chat apps/agent-service/app/nodes/safety.py` 零结果
- [ ] `apps/agent-service/app/workers/post_consumer.py` 不存在
- [ ] `apps/agent-service/app/chat/safety.py` 不存在（合并到 `nodes/safety.py`）
- [ ] `compile_graph()` 接受 `wire(Recall).to(Sink.mq("recall"))`，并对 `Sink.mq("not_in_routes")` 启动报错
- [ ] 泳道部署后 `make logs APP=agent-service KEYWORD=consumer` 出现 `durable consumer started: durable_post_safety_request_run_post_safety_<lane> -> run_post_safety`
- [ ] 4 种 case（pre block / pre pass / post block / post pass）泳道验证全过
- [ ] `agent_responses.safety_status` 在新链路下：passed 路径直接 `pending → passed`；blocked 路径 `pending → recalled / recall_failed`（不再经过 "blocked" 中间状态）
- [ ] `grep -n "await emit" apps/agent-service/app/nodes/safety.py` 零结果（无手动 emit，全靠 @node 装饰器）
- [ ] lark-server recall-worker 收到 Recall 消息（payload schema 与改造前一致，`payload.lane` 字段填充正确）
- [ ] DLQ replay runbook 补充 "查 safety_status 再决定 replay" 提示
- [ ] `apps/lark-server/src/workers/recall-worker.ts` max retry 分支补 `safety_status="recall_failed"` 写入（Phase 2 范围内修，跟 agent-service 同步发布）
- [ ] 单元测试覆盖：`run_post_safety` 在 row missing 时 raise；`run_pre_safety_via_graph` 在节点 21s 卡住时超时 fail-open（验证 emit_task 被 cancel）
