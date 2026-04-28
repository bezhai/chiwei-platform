# Dataflow Phase 2 — Safety 管线进 Graph

**状态**: Draft (2026-04-28)
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

`main.py:62` 在 FastAPI lifespan 里通过 `start_post_consumer()` 启动这个 consumer。

## 3. 目标架构

```
PreSafetyRequest -> run_pre_safety -> PreSafetyVerdict -> resolve_pre_safety_waiter
PostSafetyRequest --durable--> run_post_safety -> PostSafetyDecision -> apply_post_safety_result
apply_post_safety_result -> Recall | None
Recall -> Sink.mq("recall")
```

**两条管线，两种形态**：

| | Pre | Post |
|---|---|---|
| 触发方 | chat pipeline `emit(PreSafetyRequest)` | chat pipeline `emit(PostSafetyRequest)` |
| Wire 模式 | in-process | `.durable()` |
| 跨进程 | 否（agent-service 进程内） | 是（durable consumer in agent-service） |
| 结果回路 | 本地 Future registry（waiter） | 自动持久化 + Sink.mq("recall") |
| Race 行为 | chat pipeline 内保留 `_buffer_until_pre` | 不需要（异步管线） |

### 3.1 Data 类（`apps/agent-service/app/domain/safety.py`）

```python
class PreSafetyRequest(Data):
    request_id: Annotated[str, Key]   # = pipeline.py 里的 session_id or uuid4
    message_id: str
    message_content: str
    persona_id: str

    class Meta:
        transient = True

class PreSafetyVerdict(Data):
    request_id: Annotated[str, Key]
    message_id: str
    is_blocked: bool
    block_reason: str | None = None  # BlockReason.value 字符串化
    detail: str | None = None

    class Meta:
        transient = True

class PostSafetyRequest(Data):
    session_id: Annotated[str, Key]   # 同一 chat 完成一次产一条
    response_text: str
    chat_id: str
    trigger_message_id: str
    # 不 transient — durable wire 需要 pg 表做 consumer-side dedup

class PostSafetyDecision(Data):
    session_id: Annotated[str, Key]
    chat_id: str
    trigger_message_id: str
    is_blocked: bool
    reason: str | None = None
    detail: str | None = None

    class Meta:
        transient = True

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

**约束验证**（来自 `runtime/graph.py:170-192`）：transient Data 不能跟 `.durable()` 共存（durable 要 pg 表做 dedup）。`PostSafetyRequest` 不带 transient，runtime 自动建表 `post_safety_request`。其余四个全 transient（不落表）。

### 3.2 节点（`apps/agent-service/app/nodes/safety.py`）

四个节点。前两个是 pre 链，后两个是 post 链。

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

@node
async def run_post_safety(req: PostSafetyRequest) -> PostSafetyDecision:
    """跑 banned word + LLM output audit。复用同模块的 `_check_banned_word`
    + `_check_output` 私有 helper。"""
    ...

@node
async def apply_post_safety_result(decision: PostSafetyDecision) -> Recall | None:
    """写 safety_status；blocked 时返回 Recall（自动 emit 出去走 sink）。"""
    async with get_session() as s:
        await set_safety_status(
            s, decision.session_id,
            "blocked" if decision.is_blocked else "passed",
            {"checked_at": ..., "reason": decision.reason},
        )
    if not decision.is_blocked:
        return None
    return Recall(
        session_id=decision.session_id,
        chat_id=decision.chat_id,
        trigger_message_id=decision.trigger_message_id,
        reason=decision.reason or "unknown",
        detail=decision.detail,
    )
```

### 3.3 Wiring（`apps/agent-service/app/wiring/safety.py`）

```python
wire(PreSafetyRequest).to(run_pre_safety)
wire(PreSafetyVerdict).to(resolve_pre_safety_waiter)
wire(PostSafetyRequest).to(run_post_safety).durable()
wire(PostSafetyDecision).to(apply_post_safety_result)
wire(Recall).to(Sink.mq("recall"))
```

`bind` placement（`apps/agent-service/app/runtime/placement.py`）：
- `run_pre_safety` / `resolve_pre_safety_waiter` → `agent-service`（请求路径，跟 chat pipeline 同进程）
- `run_post_safety` / `apply_post_safety_result` → `agent-service`（替代旧 `start_post_consumer`，跑在 FastAPI 主进程）

不开新 `safety-worker` Deployment：post-safety 工作量小（一次 banned word + 一次 guard LLM 调用），跟 vectorize/embedding 那种重 IO 不一样，复用 agent-service 进程合理。

### 3.4 本地 waiter registry（`apps/agent-service/app/chat/pre_safety_gate.py`）

```python
_waiters: dict[str, asyncio.Future[PreSafetyVerdict]] = {}

def register(request_id: str) -> asyncio.Future[PreSafetyVerdict]:
    fut = asyncio.get_running_loop().create_future()
    _waiters[request_id] = fut
    return fut

def resolve(verdict: PreSafetyVerdict) -> None:
    fut = _waiters.get(verdict.request_id)
    if fut is None or fut.done():
        return  # caller 已经超时清理 / 不存在 — 安全无操作
    fut.set_result(verdict)

def cleanup(request_id: str) -> None:
    _waiters.pop(request_id, None)

async def run_pre_safety_via_graph(
    request_id: str, message_id: str, content: str, persona_id: str
) -> PreSafetyVerdict:
    """fail-open 在这里集中处理：超时/异常都转成 pass verdict。"""
    fut = register(request_id)
    try:
        await emit(PreSafetyRequest(
            request_id=request_id,
            message_id=message_id,
            message_content=content,
            persona_id=persona_id,
        ))
        return await asyncio.wait_for(fut, timeout=21.0)  # 比节点内部 20s 多 1s 缓冲
    except (TimeoutError, Exception) as e:
        logger.warning(
            "pre safety fail-open: request_id=%s, error=%s", request_id, e
        )
        return PreSafetyVerdict(
            request_id=request_id, message_id=message_id, is_blocked=False
        )
    finally:
        cleanup(request_id)
```

**关键不变量**：
- chat pipeline 暴露的永远是 `PreSafetyVerdict`（is_blocked / reason / detail）。`_buffer_until_pre` 不需要知道结果是怎么来的。
- 失败模式（timeout / emit 异常 / future cancelled）一律 fail-open（is_blocked=False），跟现有 `run_pre_check` 行为一致。

### 3.5 Runtime 增量：Sink dispatch

**当前阻塞**（`runtime/graph.py:208-214`）：`compile_graph` 把"含 sinks 的 wire"列入 `unimplemented`，启动直接 raise。

**Phase 2 携带改动**：

1. **`graph.py`**：删除 `if w.sinks:` 那段 unimplemented 检查（debounce 那段保留 — Phase 3 才动）。
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
       if route is None:
           raise RuntimeError(
               f"Sink.mq({queue_name!r}): no Route in ALL_ROUTES; "
               f"sink dispatch needs a registered route to know the routing key"
           )
       await mq.publish(route, data.model_dump(mode="json"))

   def _route_by_queue(queue_name: str) -> Route | None:
       from app.infra.rabbitmq import ALL_ROUTES
       for r in ALL_ROUTES:
           if r.queue == queue_name:
               return r
       return None
   ```

**为什么必须查 `ALL_ROUTES`**：lark-server 的 recall-worker 通过 routing key `action.recall` 绑定队列，sink 直接用 queue 名 + 默认 routing key 会绑错。`ALL_ROUTES` 是 queue→routing-key 的权威映射。

**约束**：`Sink.mq(name)` 调用时 `name` 必须存在于 `ALL_ROUTES`。当前 `RECALL = Route("recall", "action.recall")` 已就位，无需新增 route。fail-fast：找不到 route 就 raise（启动 emit 第一次 sink 触发时）。

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

# 新
pre_task = asyncio.create_task(
    run_pre_safety_via_graph(
        request_id=request_id,
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
            response_text=response_text,
            chat_id=chat_id,
            trigger_message_id=trigger_message_id,
        ))
        logger.info("Emitted PostSafetyRequest: session_id=%s", session_id)
    except Exception as e:
        logger.error("Failed to emit PostSafetyRequest: %s", e)
```

`from app.infra.rabbitmq import SAFETY_CHECK, mq` 这行 import 跟着删除。

### 3.8 旧 `chat/safety.py` 处理

整文件合并进 `apps/agent-service/app/nodes/safety.py`，`chat/safety.py` 删除。

合并后 `nodes/safety.py` 的内部布局：
- module-level 私有 helper：`_check_banned_word` / `_check_injection` / `_check_politics` / `_check_nsfw` / `_check_output`（每个 LLM helper 用 module-level `_GUARD_*` AgentConfig）
- module-level 私有 `BlockReason` enum
- 节点 `run_pre_safety` / `resolve_pre_safety_waiter` / `run_post_safety` / `apply_post_safety_result`

理由：`chat/safety.py` 现在没有任何外部 import 它的代码（chat pipeline 改调 `pre_safety_gate.run_pre_safety_via_graph`，post_actions 改 emit）—— 保留就是死代码。把 helper 跟节点放同一文件，看一处就懂整条 safety 链路。

## 4. 兼容/迁移策略

### 4.1 旧 `safety_check` 队列

**问题**：deploy 之后 `mq.publish(SAFETY_CHECK, ...)` 不再发生，但 `safety_check` 队列还存在；切换瞬间可能有 lark-server 已经发出去的（不会，SAFETY_CHECK 只有 agent-service 自己发自己消费）或者 backlog。

**当前事实**：搜了一下，`SAFETY_CHECK` 只被 agent-service 自己 publish + consume。所以 deploy 完成的瞬间：
- 旧消费者（`start_post_consumer`）随进程退出停止
- 旧生产者（`_publish_post_check` 走 `mq.publish`）改成走 emit
- `safety_check` 队列里如果有 in-flight 消息，**没人消费 → 24h 后 prod 队列也没 TTL，会一直留**

**处理**：
1. 切换前确认 `safety_check_<lane>` 队列 backlog 为 0（运维查 RabbitMQ）
2. 切换后队列保留一段时间观察（durable 队列里没人发不会有新消息）
3. ship 稳定后从 `ALL_ROUTES` 移除 `SAFETY_CHECK` route + 手动删队列（独立 followup）

### 4.2 PostSafetyRequest 表迁移

新增表 `post_safety_request`（runtime migrator 自动建）。Phase 0+1 已经验证 migrator 在启动时跑 DDL（vectorize 那波建了 `message` / `fragment` 等表）。

需要的字段：runtime 根据 Data class 自动推断 + dedup_hash + version + created_at（标准 runtime 字段）。

**保留时间**：表会无限增长。Phase 2 不处理 retention（PR #198 followup 也没动）。当下 SAFETY_CHECK 队列没 retention，新表也没问题，等观察实际增长后再加 retention 策略（独立 followup）。

### 4.3 Recall 兼容性

**关键**：lark-server recall-worker 的消费契约不能变。

当前 `mq.publish(RECALL, payload)` 的 payload schema（`workers/post_consumer.py:63-74`）：
```json
{
  "session_id": "...",
  "chat_id": "...",
  "trigger_message_id": "...",
  "reason": "...",
  "detail": "...",
  "lane": "..."
}
```

`apps/lark-server/src/workers/recall-worker.ts:42,68,83` 从 `payload.lane` 读 lane，所以新方案 Recall Data **必须包含 lane 字段**（已在 3.1 加上）。`apply_post_safety_result` 节点产出 Recall 时填 `get_lane()`：

```python
return Recall(
    session_id=...,
    chat_id=...,
    trigger_message_id=...,
    reason=...,
    detail=...,
    lane=get_lane(),
)
```

`Recall(...).model_dump(mode="json")` 产出的字段集与旧 schema **一致**。Sink dispatch 走 `mq.publish(RECALL, body)` 时，`mq.publish` 内部还会按 `current_lane()` 选 lane 队列（routing key 加 lane 后缀）—— body 里的 `lane` 是给消费方读的，不影响路由。

### 4.4 灰度

Pre 在请求路径，每条 chat 都过一次。Post 异步，跟 chat 解耦。两条同时切，但分开验证：

1. **泳道 deploy** → bind dev bot → 真实消息（pre 阻断 / pre 通过 / post block 触发 recall / post pass 写 status）四类都验证
2. **观测**：langfuse trace（pre/post 的 LLM 调用都有 trace）；rabbitmq backlog（旧 safety_check + 新 durable_post_safety_request_run_post_safety + recall）；postgres `post_safety_request` 表行数
3. **回滚路径**：单 PR 改动较多，但都是替换同一职责。回滚就是 revert PR。`PostSafetyRequest` 表保留无影响。

## 5. 测试

### 5.1 节点单元测试

- `run_pre_safety`：mock `chat.safety` 的 4 个 helper，验证返回 `PreSafetyVerdict` 的 is_blocked / reason 字段映射
- `resolve_pre_safety_waiter`：register Future → 调节点 → assert future.result() == verdict；future 不存在不抛
- `run_post_safety`：mock guard LLM，验证 banned word / output unsafe / pass 三种路径
- `apply_post_safety_result`：mock `set_safety_status`，验证 blocked → 返回 Recall + 写 blocked；passed → 返回 None + 写 passed

### 5.2 端到端 emit 测试

- `run_pre_safety_via_graph`：emit + 等 future + cleanup 的全链路（用 in-memory graph + 真节点）
- 超时路径：拒绝节点完成（mock 节点 sleep 大于 timeout），断言 fail-open
- 重复 request_id：第二次 emit 同一 request_id 不爆（waiter 覆盖）

### 5.3 Sink dispatch 测试

- `Sink.mq("recall")` + `wire(Recall).to(Sink.mq("recall"))` + emit Recall → 验证 `mq.publish` 被调用，参数是 RECALL route + 正确 payload
- `Sink.mq("not_in_routes")` → emit 时 raise

### 5.4 泳道集成测试

部署到 `phase2-safety` 泳道（agent-service + arq-worker + vectorize-worker 一起 release，因为同镜像），bind dev bot 跑四类消息。

## 6. 部署 & 切换

1. 泳道验证四种 case 全过
2. 检查 `safety_check_<lane>` 队列 backlog = 0
3. ship → release agent-service / arq-worker / vectorize-worker 到 prod
4. 部署后 5min 观察：
   - `start_consumers` log 出现 `durable consumer started: durable_post_safety_request_run_post_safety_<lane> -> run_post_safety`
   - 新 chat 流入产生 `post_safety_request` 表新行
   - 旧 `safety_check` 队列 message rate = 0
5. 24h 稳定后启动 followup PR：`SAFETY_CHECK` route 移除 + 队列删除

## 7. 不在本期范围

- **Stream[T] @node 支持**：Phase 5 chat pipeline 重写时统一上。Phase 2 不动 `_buffer_until_pre` 的 race 模型。
- **Pre 通过 emit-await 解耦**（chat pipeline 不再持有 Future registry）：需要 runtime 加新 primitive。等 Phase 5 跟 Stream 一起设计。
- **`safety_check` 队列删除 + `SAFETY_CHECK` route 从 `ALL_ROUTES` 移除**：单独 followup PR，等 prod 稳定后做。
- **`post_safety_request` 表 retention 策略**：独立 followup，跟 PR #198 的 message/fragment 表 retention 一起讨论。
- **Pre/Post 跨 persona 行为变化**：节点内部完全复用现有 `_check_*` helper，不改 LLM prompt 和阈值。

## 8. 验收 checklist

- [ ] `grep -rn "SAFETY_CHECK\|safety_check" apps/agent-service/app` 在 `chat/` 和 `workers/` 下零结果（route 定义本身保留到 followup）
- [ ] `grep -rn "mq.publish" apps/agent-service/app/chat apps/agent-service/app/nodes/safety.py` 零结果
- [ ] `apps/agent-service/app/workers/post_consumer.py` 不存在
- [ ] `compile_graph()` 接受 `wire(Recall).to(Sink.mq("recall"))`
- [ ] 泳道部署后 `kubectl logs -l app=agent-service` 出现 `durable consumer started: durable_post_safety_request_run_post_safety -> run_post_safety`
- [ ] 4 种 case（pre block / pre pass / post block / post pass）泳道验证全过
- [ ] `post_safety_request` 表在 prod schema 中存在
- [ ] lark-server recall-worker 收到 Recall 消息（payload schema 与改造前一致）
