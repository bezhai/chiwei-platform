# Dataflow Phase 7 — 终态 Gap Analysis

**状态**: Draft v2 (2026-05-08，吸收 subagent 两轮扫描)
**前置**: Phase 0-6 shipped；Phase 6 v4 已完成 HTTP source / cross-process emit / tool events / arq state-sync removal / fire-and-forget cleanup / main chat graph cutover。
**核心校正**: 终态不是"业务代码改用 dataflow API"；终态是**无论写什么业务，作者都不需要理解底层框架**。

## 1. 终态判定标准

一个业务改动合格，不是因为它用了 `wire(...)`，而是因为业务作者只需要回答：

1. 这个业务产生 / 消费什么 Data？
2. 哪个 Node 处理它？
3. 需要哪些业务能力（LLM / HTTP / VectorStore / Agent / state query）？

业务作者不应该知道：

- RabbitMQ queue / routing key / DLX / DLQ / replay
- Redis key / lock / delayed message
- ARQ worker / cron poller / pod placement
- trace / lane header propagation
- DB commit 后才能 emit
- retry/backoff/outbox 细节
- FastAPI route 注册细节

如果新增业务需要上述知识，先扩 `app/runtime/*` 或 `app/capabilities/*`，再写业务。

## 2. 已闭合的能力面（Phase 0-6）

| 面 | 当前状态 | 证据 |
|---|---|---|
| Data / Node / Wire 图 | 已有 runtime 核心抽象 | `app/runtime/{data,node,wire,emit,graph}.py` |
| HTTP source | GET / POST / DELETE / path params / query / RPC 已支持 | `tests/runtime/test_http_source.py` |
| cross-process emit | consumer 在其它 app + `Source.mq` 时自动 publish | `tests/runtime/test_emit_cross_process.py` |
| Sink.mq | Data 出图到 MQ queue | `tests/runtime/test_sink_dispatch.py` |
| durable edge | RabbitMQ + consumer-side idempotent + trace/lane header | `tests/runtime/test_durable.py` |
| debounce | Redis + delayed MQ + compile-time shape validation | `tests/runtime/test_debounce.py`, `tests/runtime/test_graph_debounce.py` |
| chat pipeline | `ChatTrigger -> ChatRequest -> ChatResponseSegment -> Sink.mq` | `app/wiring/chat.py` |
| tool side effects | 部分 mutation tool emit event Data | `app/domain/agent_tool_events.py`, `app/wiring/agent_tool_events.py` |

这些能力让业务从手写 `mq.publish` / `@router` / `asyncio.create_task` 迁到统一图里，但还没有达到"不需要理解底层"。

## 3. Phase 7 Gap Surface（Gap 7-19）

Phase 7 不只包含 memory 中的 Gap 7-12。Gap 7-12 关闭 transport / reliability primitive；Gap 13-19 关闭"业务作者仍要理解底层"的实际泄漏面。若 13-19 不在本轮实现，也必须有 owner、约束和禁止 workaround，不能只叫"遗漏面"。

### Gap 7 — durable retry 策略仍泄漏

**现状**: `.durable()` 固定 fail-to-DLQ，`runtime/durable.py` 明确无自动 retry。

**为什么未达终态**: 业务/运维需要知道 transient error 会直接进 DLQ，且不能在业务里手写 retry。

**目标**: `wire(T).to(node).durable().retry(n=3, backoff="exponential")` 或等价策略对象。业务声明失败策略，runtime 负责 delayed retry / DLQ。

**验收**:
- 业务代码 grep `try.*sleep.*retry` / `await asyncio.sleep` 自实现 retry 为 0（agent capability 内部 LLM retry 作为 capability 例外）。
- durable retry contract test 覆盖：前 N-1 次失败重投，最后成功 ack；超过次数进 DLQ。

### Gap 8 — outbox / 事务边界仍泄漏

**现状**: 业务仍靠"commit 后 emit"纪律；`life/proactive.py` 等处显式提醒。

**代表位置**: `life/proactive.py` 注释要求 `get_session()` 退出后 emit；`agent/tools/update_schedule.py` 在已提交 schedule revision 后再 emit，并要考虑 emit 失败后的已提交状态。

**为什么未达终态**: 业务作者必须理解 DB transaction 和 emit 的顺序，否则会出现 DB rollback 但事件已发，或 DB commit 成功但 emit 失败。

**目标**: outbox 模式。业务在同一事务里写状态 + append outbox，runtime publisher 在 commit 后统一 emit。

**验收**:
- 新 mutation 业务不需要写"commit 后 emit"注释。
- 失败注入测试覆盖：DB rollback 不发事件；MQ publish 失败不会丢事件，outbox 可重试。

### Gap 9 — delayed / scheduled emit 仍不是通用 primitive

**现状**: debounce 内部有 delayed MQ，但业务没有 `emit_delayed` / `emit_at`。

**为什么未达终态**: 新业务如果想"10 分钟后再做"，会被迫理解 RabbitMQ delayed exchange 或手写 sleep。

**目标**: `await emit_delayed(data, delay=...)` / `await emit_at(data, at=...)`，或 `wire(...).delay(...)`。底层由 runtime 决定用 delayed MQ、outbox scheduler 或 cron wheel。

**验收**:
- 业务代码 grep `asyncio.sleep.*emit` 为 0。
- delayed emit 测试覆盖 lane/trace propagation、进程重启后仍可投递。

### Gap 10 — streaming/segment 协议仍泄漏

**现状**: chat 用 `ChatResponseSegment(part_index, is_last)`，能跑，但新流式业务仍要懂这套字段协议。

**为什么未达终态**: 业务作者要自己设计 chunk key、final marker、full_content 等约定。

**目标**: 标准化 segment/stream capability。可以不恢复旧 `Stream[T]`，但必须把 part index、final、dedup、sink fan-out 变成共享协议或 helper。

**验收**:
- 新流式业务不自定义 `part_index/is_last` 字段名。
- chat 现有协议通过 shared abstraction 表达，飞书侧 body 兼容。

### Gap 11 — trace / lane propagation 散在多处

**现状**: durable、debounce、Source.mq、HTTP capability 各自处理 trace/lane。

**为什么未达终态**: 每新增一种 Source/Sink/transport 都可能漏传 context；业务偶尔还会读 `current_lane()` 补字段。更麻烦的是 lane 还有历史 body-level contract：`ChatResponseSegment` / lark-server `chat-response-worker` 读 payload 里的 `lane`，不能只把 lane 收进 header 而漏掉兼容迁移。

**目标**: runtime-level propagation hook：publish/consume/sink/source 统一 encode/decode context。

**验收**:
- `trace_id_var` / `lane_var` 只在 runtime/capability 边界出现。
- cross-process / debounce / sink / HTTP source contract tests 共享同一 propagation helper。
- 明确列出并迁移/兼容 body-level lane contract；验收包含 proactive/chat_response lane 路由 e2e。

### Gap 12 — DLQ replay 语义不闭合

**现状**: durable consumer 先 `insert_idempotent` 再跑 handler；直接 replay 同一 DLQ 消息可能被 dedup 拦下成 no-op。

**为什么未达终态**: 运维必须知道清哪张 data table / idempotent row 后才能 replay。

**目标**: runtime 提供 replay 工具和策略：inspect、clear idempotent、requeue、dry-run。业务 Data 暴露 replay identity，工具负责底层动作。

**验收**:
- 有 CLI/API runbook：给 DLQ message id 或 Data key，可 dry-run 展示会清什么、重投什么。
- replay integration test 覆盖 no-op 模式和 clear-idempotent 模式。

### Gap 13 — DB/session/query 仍大面积泄漏到业务

**扫描结果**: 排除 `runtime/` 和 `infra/` 后，`get_session()` / `AsyncSessionLocal` / `app.data.session` 仍约 140 处。

**代表位置**:
- `app/nodes/memory_pipelines.py`
- `app/nodes/admin.py`
- `app/life/glimpse.py`
- `app/life/proactive.py`
- `app/memory/*`
- `app/long_tasks/crud.py`

**为什么未达终态**: Node 作者仍要知道 SQLAlchemy session、commit、row shape、查询 helper 分布。原始 dataflow 设计里 Node 应优先使用 `query(T)` / capability，而不是直接碰 DB。

**目标方向**:
- 读：扩 `runtime/query.py`，提供 typed query / latest / list / join 能力。
- 写：Data-native write 或 domain repository capability；需要副作用时走 outbox。
- 旧表 adoption mode 继续保留，但 ORM 细节收进 capability/repository。

**验收**:
- 新 Node 禁止直接 import `get_session`。
- 业务目录中 DB 访问逐域收敛到少数 capability/repository 文件。

### Gap 14 — Redis lock / single-flight 泄漏到业务

**扫描结果**: `nodes/memory_pipelines.py` 直接使用 Redis SETNX + Lua release；`chat/context.py` 使用 Redis-backed `ImageRegistry`；`nodes/safety.py` 直接从 Redis set 读取 banned words。

**为什么未达终态**: 业务作者需要理解 Redis key、TTL、Lua compare-delete，甚至知道某个业务 registry/config 存在 Redis set 里。

**目标方向**:
- runtime/capability 提供 `SingleFlight(key, ttl)` / `Registry` primitive。
- 业务只表达"同一 persona/chat 同时只能跑一个 reviewer"。
- Redis-backed business registry/config 收敛为 typed capability，业务不直接读 Redis key。

**验收**:
- 业务 node 中无 `redis.set(... nx=True ...)` / `redis.eval(...)`。
- 业务 node 中无 `redis.smembers(...)` 这类直接读业务配置集合。
- lock contention 行为有 capability contract tests。

### Gap 15 — long_tasks / arq 子系统仍是第二套执行框架

**现状**: `arq-worker` 仍存在，只剩 `task_executor_job` 每分钟 poll `long_tasks`。

**为什么未达终态**: 写 long task 仍要理解 arq worker、SQL poller、retry_count/max_retries，而不是 Dataflow runtime。

**目标方向**:
- 把 `LongTaskRequested/LongTaskStep/LongTaskCompleted` 建模成 Data。
- 用 `Source.cron("* * * * *")` 或 outbox scheduler 驱动 executor node。
- 统一 retry / status / observability，不再由 arq cron 独立实现。

**验收**:
- 删除 `app/workers/arq_settings.py` 和 arq 依赖。
- K8s 不再有 `arq-worker`；或改名为 dataflow worker 并跑 `runtime_entry.py`。

### Gap 16 — 外部 / 内部 HTTP client 仍绕 capability

**扫描结果**: `agent/tools/search.py`、`agent/tools/image_search.py` 直接用 httpx headers/API key；`skills/sandbox_client.py` 自己组 lane/trace/auth headers。

**为什么未达终态**: tool/client 作者要知道 headers、auth、trace/lane 是否需要注入。内部服务 client 绕过 `HTTPClient` 时尤其容易漏 lane/trace。

**目标方向**:
- 把外部搜索/图片搜索变成 capability。
- HTTPClient 负责 trace/lane；搜索 capability 负责 auth/header/schema。
- 内部 service client 统一用 HTTP capability 或 service-specific capability，禁止业务手写 lane_router / get_trace_id headers。

**验收**:
- agent tool / skills client 不直接 new HTTP client 或手写 auth/lane/trace headers（外部 provider SDK 作为 capability 内部例外）。

### Gap 17 — 健康检查 / lifecycle 仍是 route 例外

**现状**: `app/api/routes.py` 只剩 `/health` 手写 route；这是合理例外，但仍说明 HTTP source 没有 builtin lifecycle surface。

**目标方向**:
- `runtime/http_source.py` 提供 builtin health/liveness/readiness 注册，或者明确把 health 归为 app lifecycle 不属于业务 framework。

**验收**:
- 文档和测试固定 `/health` 是唯一允许的 hand-written route。

### Gap 18 — node error policy / DLQ 语义泄漏到业务

**扫描结果**: `nodes/life_dataflow.py`、`nodes/safety.py`、`nodes/save_fragment.py` 的注释和控制流要求业务作者知道"不要 catch，否则不会 nack/DLQ"、"row missing 要 raise 进 DLQ"。

**为什么未达终态**: 业务节点在表达错误语义时必须理解 RabbitMQ ack/nack 和 DLQ，而不是声明"这个错误可重试/不可重试/进入人工处理"。

**目标方向**:
- 给 node 或 wire 增加 error policy：fail-to-DLQ、retryable、ignore duplicate、manual-review 等。
- runtime 负责把异常映射到 ack/nack/retry/DLQ；业务只抛 typed domain error 或返回 typed failure Data。

**验收**:
- 新 node 不需要写"不要 catch 否则不进 DLQ"这类注释。
- error policy contract tests 覆盖 typed error -> retry/DLQ/no-op 的映射。

### Gap 19 — graph request/reply / async join 仍手写 event-loop 编排

**扫描结果**: `nodes/chat_node.py` 和 `chat/pre_safety_gate.py` 通过 `asyncio.create_task` / Future / race 方式等待 pre-safety verdict。它不是 fire-and-forget，但仍是业务层 event-loop orchestration。

**为什么未达终态**: 业务作者要理解 task 生命周期、取消、timeout、并发 race，而不是声明"ChatRequest 需要等待 SafetyVerdict，直到段边界再决策"。

**目标方向**:
- runtime 提供 graph request/reply、join、barrier 或 awaitable Data primitive。
- pre-safety 这种"先发请求，后在业务边界等待 verdict"成为声明式 pattern。

**验收**:
- 除 runtime/capability 内部外，业务 node 不直接用 `asyncio.create_task` 管 graph 子流程。
- pre-safety tests 从 task/future 细节转为 request/reply contract tests。

## 5. 有效性保障

### 5.1 CI / grep gate

Phase 7 后应新增一个 framework guard test。不要用粗暴全文 grep 清零；按 allowlist 区分 runtime/capability/infra 边界和业务目录，避免命中文档、README、注释后误删代码。

```bash
rg "mq\.publish|enqueue_job|create_pool|from arq|asyncio\.create_task" apps/agent-service/app \
  --glob '!apps/agent-service/app/runtime/**' --glob '!**/README.md'
rg "await asyncio\.sleep\(.*emit|emit_delayed TODO|outbox TODO" apps/agent-service/app \
  --glob '!apps/agent-service/app/runtime/**'
rg "trace_id_var|lane_var|current_lane|headers=.*x-lane" apps/agent-service/app \
  --glob '!apps/agent-service/app/runtime/**' --glob '!apps/agent-service/app/capabilities/**' \
  --glob '!apps/agent-service/app/infra/**' --glob '!apps/agent-service/app/api/middleware.py'
rg "get_session\(|AsyncSessionLocal" apps/agent-service/app/nodes apps/agent-service/app/agent/tools
rg "redis\.set\(.*nx=True|redis\.eval\(|redis\.smembers\(" apps/agent-service/app/nodes
```

不是所有命中都必须立刻归零，但每个例外必须有 owner 和 close path。否则框架会重新被业务绕开。

### 5.2 Contract tests

每个 runtime primitive 必须有三类测试：

- compile-time validation：错误组合启动失败，不静默 noop。
- unit contract：不依赖真实服务时能证明 API 语义。
- integration / lane test：真实 RabbitMQ/Postgres 时证明 trace/lane/retry/idempotent 行为。

### 5.3 E2E 证据

框架有效性最终靠真实链路证明：

- dev bot 群聊 + p2p chat
- proactive / glimpse
- update_schedule tool -> state sync
- vectorize-worker 消费
- admin HTTP RPC
- durable failure -> DLQ -> replay drill
- Langfuse trace 中能看到完整上下文

## 6. 建议切分

| PR | 范围 | 原因 |
|---|---|---|
| Phase 7a | Gap 7, 9, 11 | transport 语义：retry / delayed / propagation 同属 MQ/runtime 层 |
| Phase 7b | Gap 8, 12, 18 | reliability 语义：outbox / replay / error policy 共享 idempotent 与状态表设计 |
| Phase 7c | Gap 15 | arq partial close，独立 blast radius |
| Phase 7d | Gap 13, 14, 16 | capability 收敛，按业务域逐步迁移 |
| Phase 7e | Gap 10, 19 | streaming / request-reply / async join，收敛 chat/pre-safety 特例 |

Phase 7a/7b 是"框架是否可信"的关键；Phase 7c/7d/7e 是"业务作者是否真的不用懂底层"的关键。
