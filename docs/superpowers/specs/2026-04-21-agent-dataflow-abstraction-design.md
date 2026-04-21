# Agent Service Dataflow Abstraction 设计

## 一句话

把整个 agent-service 建模成一张 `(Data × Node)` 二分图，业务代码只写「产出什么数据、谁消费这个数据」，`mq` / `pg` / `redis` / `qdrant` / `cron 扫表` / `asyncio.create_task` / `内存 debouncer` 这些字一律不再出现在业务代码里。

## 背景

当前代码里跨模块的协调 95% 靠共享 DB 完成：chat 写 pg、vectorize cron 扫 pg；life engine cron 写 life_state、chat `build_inner_context` 现读；drift/afterthought 内存 debouncer（重启即丢）；schedule 定时生成、chat 现读。这种协议有三个结构性问题：

1. **谁写谁读看不出来**。`life_state` 字段被四五处读，生产者散在 cron 里；想查一个字段的完整生命周期得 grep 字符串。
2. **异步化手段散落**。`mq.publish` / `asyncio.create_task` / `arq enqueue` / 内存 debouncer / cron 扫表各写各的，业务代码里夹杂大量调度细节。
3. **"一条管线"读不出全貌**。`mq.publish(RECALL, ...)` 之后谁在消费、消费完又写回哪里、是不是会触发下一步——全靠人脑拼接。

## 设计哲学

**业务画接线图，Runtime 负责执行。**

业务代码只回答三个问题：
1. 我流转的是什么类型的数据？
2. 我有哪些处理节点？每个节点的输入输出是什么？
3. 哪种数据被哪些节点消费，消费时有什么语义（持久化、延迟、只要最新……）？

Runtime 负责回答：这张图在物理上怎么跑——什么时候用 mq、什么时候用 pg、什么时候用进程内调用、什么时候 cron 兜底。

## 核心模型

```
agent-service = (Data × Node) 二分图

  Data Node ───edge (produces/consumes)──> Process Node
                                              │
                                              └── produces ──> Data Node
```

- **Data Node**：一种类型化的数据（`Message` / `LifeState` / `SafetyVerdict` / `Fragment` / `DriftTrigger` / `ChatResponse` …）
- **Process Node**：一个业务函数，声明它消费哪些 Data、产出哪些 Data
- **Edge**：由 `wire(...)` 声明，带属性（默认 / durable / as_latest / debounce / broadcast …）

这张图可以 `.to_mermaid()` 画出来。它就是 agent-service 的架构图——不是文档里画一张、代码里跑另一套。

## 三层

### 层 1 · Data

系统里流转的每样东西都有明确类型。Pydantic / dataclass，照常。

```python
class Message(Data):
    chat_id: str
    persona_id: str
    text: str
    images: list[str]
    ...

class LifeState(Data):
    persona_id: str
    mood: str
    activity: str
    ...

class SafetyVerdict(Data):
    blocked: bool
    reason: str | None
    detail: str | None
```

Data 不是「pg 一张表」也不是「mq 一个 queue」，它就是「一种数据」。持久化和传输是 runtime 根据 wire 属性决定的事。

### 层 2 · Node

业务函数，只写输入输出：

```python
@node
async def safety_check(msg: Message) -> SafetyVerdict: ...

@node
async def chat_stream(msg: Message, life: LifeState) -> ChatResponse: ...

@node
async def vectorize(msg: Message) -> Fragment: ...

@node
async def life_engine_tick(persona_id: str) -> LifeState: ...

@node
async def afterthought(trigger: DriftTrigger) -> None: ...
```

Node 里**不允许**出现：
- 基础设施名字：`mq` / `rabbitmq` / `arq` / `redis` / `pg` / `qdrant` / `psycopg` / `AsyncSessionLocal` / `sqlalchemy session`
- 跨节点调度：`asyncio.create_task(call_another_node)` / 手写 `mq.publish` 给下游 / 手写 debouncer
- 具体操作名：`scan_pending_xxx` / `find_latest_xxx`（这些在 runtime 里）

Node 里**允许**出现：
- 调用 capability：`LLM`, `Agent`, `Embedder`, `VectorStore`, `Store[T]`, `HTTP`……
- 节点内部的并发：`asyncio.gather(llm_call_1, llm_call_2)` 等"等多个 capability 返回"的场景不受限制

区分：**capability 是业务看得见的「能力」（调 LLM、查向量库、读状态），不是基础设施的名字**（pg/redis/mq）。只要没有通过 capability 接口直接碰基础设施，Node 内部怎么并发都行。

### 层 3 · Wire

**整个 agent-service 只有一个地方画接线图**（`wiring.py` 之类），生产者和消费者显式连起来：

```python
# 外界进来
wire(Message).from_(
    Source.http("/chat"),
    Source.feishu_webhook(),
)

# Message 的所有消费者
wire(Message).to(
    safety_check,
    save_history,
    vectorize.durable(),              # 跨 Pod 可重试
)

# safety_check 的产出被谁消费
wire(SafetyVerdict).to(
    recall.when(lambda v: v.blocked),
    audit_log,
)

# LifeState 是状态流，不是事件流
wire(LifeState).to(chat_stream).as_latest()

# Drift 延迟去抖
wire(DriftTrigger).to(afterthought).debounce(seconds=10, max_buffer=5)

# Life engine 定时触发
wire(LifeEngineTick).from_(Source.cron("*/1 * * * *")).to(life_engine_tick)

# chat_stream 要两种数据：Message（事件）+ LifeState（最新状态）
wire(Message).to(chat_stream).with_latest(LifeState)
```

看一眼 `wiring.py` 就能回答：

- **`LifeState` 谁产出？** → 产出 `LifeState` 的 Node 唯一
- **`Message` 谁消费？** → 一行 `wire(Message).to(...)` 列完
- **某条边是异步吗？持久化吗？** → 看 wire 属性

## Wire 属性清单

业务声明意图，runtime 决定实现：

| 属性 | 业务意图 | Runtime 典型落地 |
|---|---|---|
| 默认 | 产出后立即在同进程内传递给消费者 | 直接 `await` |
| `.durable()` | 跨 Pod / 持久化 / 可重试；消费失败不丢 | mq (arq/rabbitmq) + pg outbox + 消费者订阅 |
| `.as_latest()` | 本条 Data 是「状态」而非「事件」：只保留最新一份，历史覆盖 | pg state table (one row per key)；写时 upsert，读时取最新 |
| `.debounce(seconds, max_buffer)` | 同一 key 短时间多次产出时合并 | redis 计时器 + 延迟触发（arq delayed 或 redis zset） |
| `.broadcast()` | 通知外部业务，不在乎谁接 | mq topic, fire-and-forget |
| `.with_latest(X)` | 消费者函数需要「最新一份 X」作为额外入参；runtime 调用时注入 | 从 X 的 state table 读最新值注入；要求 X 自身已用 `.as_latest()` 声明 |
| `.when(predicate)` | 满足条件才送达该消费者 | runtime 层过滤 |
| `.from_(Source.cron(...))` | 定时触发 | cron 调度器 |

属性可以组合：`wire(Event).to(handler).durable().debounce(...)` = 持久化且去抖。

## Source 和 Sink

外部边界：

- **Source**：数据从外界进入系统的入口
  - `Source.http("/chat")` — HTTP 接口
  - `Source.feishu_webhook()` — 飞书 webhook
  - `Source.mq("chat_request")` — MQ 订阅
  - `Source.cron("0 * * * *")` — 定时
  - `Source.manual("/admin/rebuild")` — 管理触发
- **Sink**：数据离开系统的出口
  - `Sink.feishu_send()` — 发飞书消息
  - `Sink.http_callback(url)` — 回调外部 HTTP
  - `Sink.langfuse_trace()` — 追踪上报

Source/Sink 是业务层**边界注解**，里面仍然声明类型化的 Data。

## Capability 层

业务代码只看到「能力」，看不到「实现」：

| 能力 | 业务 API | 可能的实现 |
|---|---|---|
| `LLM` | `llm.complete(prompt, schema)` | langchain / direct http / mock |
| `Agent` | `agent.stream(ctx)` | LangGraph / custom |
| `Embedder` | `embedder.encode(text)` | openai / bge / jina |
| `VectorStore` | `store.search(vec, k)` | qdrant / pgvector |
| `Store[T]` | `store.get(key) / set(key, v)` | pg / redis，runtime 决定 |
| `HTTP` | `http.get(url, ...)` | httpx, 带 lane routing / trace |

Capability 是业务层第二种语言（除 Data/Node/Wire 之外）。**节点内部**允许通过 capability 做 I/O，但它们不是"mq/pg/redis"的名字。

## Runtime 职责

读所有 `wire(...)` 声明之后：

1. **构建物理拓扑**
   - 把 `.durable()` 边翻译成 mq queue + consumer
   - 把 `.as_latest()` 边翻译成 pg state table + 订阅读
   - 把 `.debounce(...)` 翻译成 redis 计时器 / arq delayed
   - 把默认边翻译成进程内直接调
2. **运行时横切关注**
   - Trace / Langfuse 自动注入
   - Lane routing 自动注入（`x-lane` 从 source 延续到 sink）
   - 错误处理、重试、死信
   - `.durable()` 边自带幂等性（基于 Data 的唯一 key）
3. **可观测性**
   - 每条 wire 是 metric 的天然维度：`wire_latency_seconds{data=Message,consumer=vectorize}`
   - 物理拓扑可以 dump 成 mermaid 供运维看
4. **Test harness**
   - 把 capability 层换成 fake，可以整图 dry-run
   - 把 Source 喂测试数据，断言对应 Data 被某 Node 消费

## 五个真实业务例子

### 1. Chat 消息处理

```python
wire(Message).from_(Source.feishu_webhook())

wire(Message).to(safety_pre_check)
wire(Message).to(chat_stream).with_latest(LifeState)
wire(Message).to(save_history).durable()
wire(Message).to(vectorize).durable()

wire(ChatResponse).to(Sink.feishu_send()).durable()
wire(ChatResponse).to(safety_post_check)

wire(SafetyVerdict).to(recall).when(lambda v: v.blocked).durable()
```

### 2. Vectorize（消灭 cron 扫表）

当前：chat 写 pg 打 `vector_status=pending`，vectorize cron 扫 pg 捞。

新：`wire(Message).to(vectorize).durable()`。runtime 看到 `.durable()` 自动给你 pg outbox + mq + 消费者；cron 扫表的角色退化为 runtime 内部的 "catch-up" 机制，业务代码里看不到。

### 3. Life Engine（消灭"现读 pg"）

```python
wire(LifeEngineTick).from_(Source.cron("*/1 * * * *")).to(life_engine_tick)
wire(LifeState).to(chat_stream).as_latest()
wire(LifeState).to(schedule_generator).as_latest()
```

`chat_stream` 声明 `with_latest(LifeState)`，runtime 在调用时读最新值注入——但业务代码里**不再有** `await find_latest_life_state(persona_id)` 这一行。

### 4. Drift / Afterthought（消灭内存 debouncer）

```python
wire(ChatResponse).to(drift_observer)
wire(DriftTrigger).to(afterthought).debounce(seconds=10, max_buffer=5)
```

`afterthought` 只是一个普通 Node；"延迟 10 秒、短时间聚合"是 wire 属性。runtime 用 redis 计时器实现——不再有内存 `_timers: dict[str, TimerHandle]`，重启不丢。

### 5. Safety 多 guard 并发（复合 Node）

不是 wire 层的事，而是一个 Node 内部 compose 子 Node：

```python
@node
async def safety_pre_check(msg: Message) -> SafetyVerdict:
    # 内部的并发 + first-blocked 语义是"safety 这个业务能力的内部实现"
    # 不是跨模块的 dataflow，不需要上升到 wire
    return await race_until_blocked([
        check_injection(msg),
        check_politics(msg),
        check_nsfw(msg),
    ])
```

**原则**：`Fanout+Join` 这类控制流**只在"对外不可见"的情况下**留在 Node 内部；如果扇出之后的结果会被**外部其他 Node 消费**，就升级到 wire 层。

## 非目标

- **不做 event sourcing / CQRS**。DB 里仍然可以有「当前状态」的表，runtime 自己管。
- **不引入 monad / algebraic effects**。Node 就是 `async def`，不是 generator，不是 `yield Effect(...)`。
- **不强行抽象 Node 内部的 I/O**。Node 内部调 LLM、查 redis、读 pg 都允许——前提是通过 capability 接口（不是裸 `psycopg` / `redis.Redis(...)`）。
- **不替换 agent 执行内核**。LangGraph / `agent.stream()` 保留，只是作为 Node 里的 capability 调用。
- **不重写 Langfuse 集成**。runtime 统一注入，业务代码里不再手动 `start_as_current_observation`。
- **不在本轮内消灭 `asyncio.gather`**。节点内部纯并行（比如多 guard 并发）仍可用；只是"跨节点的调度"不能用。

## 迁移策略

一次性大爆炸不可行。分阶段，每阶段一条管线迁到新框架，旧代码删掉。

每 phase 完成时的验收线：
- 该管线对应的业务代码里 `grep -rE "mq\.publish|rabbitmq|arq\.|redis_pool|AsyncSessionLocal|asyncio.create_task|scan_pending|find_latest_life_state"` 为空
- wire 图能 `.to_mermaid()` 画出该管线的拓扑
- 新老代码可共存，通过 wire 的运行时开关切换

**建议的 phase 顺序**（最终由 plan 阶段确定）：

1. **Phase 0 — Runtime 骨架 + Capability 层**：无业务迁移，只搭框架（Data/Node/Wire 三个基类 + 最小 runtime + Queue/Store/LLM/Agent/Embedder/VectorStore 的 capability adapter）。
2. **Phase 1 — Vectorize 管线**：最独立、最好验证。验收：`vectorize_worker` 消失，cron 扫表逻辑隐入 runtime。
3. **Phase 2 — Safety 管线**：把 safety pre/post 抽成独立 Node，通过 wire 被 chat 调用；此阶段 chat pipeline 本身不迁（仍通过临时适配层调 safety Node）。验收：safety Node 签名清晰 `(Message) -> SafetyVerdict`，`mq.publish(SAFETY_CHECK_QUEUE, ...)` 从 safety 模块消失。
4. **Phase 3 — Drift / Afterthought**：消灭内存 debouncer。验收：进程重启不丢待触发事件。
5. **Phase 4 — Life Engine / Schedule / Glimpse**：消灭 cron + 现读 pg 模式。
6. **Phase 5 — Chat 主 pipeline**：最复杂，留最后。验收：`chat/pipeline.py` 退化成几条 wire 声明 + 若干 Node。
7. **Phase 6 — 清扫**：删除旧代码、旧 worker 入口、旧 orm crud god object。

## 开放问题（writing-plans 阶段需要细化）

1. **StreamStage**：`chat_stream` 产出 `ChatResponse` 是流式的（token-by-token）。Node 是 `async def` 返回 `AsyncIterable[ChatResponse]`？还是 Node 返回 `Stream[Token]`，Token 和 ChatResponse 是不同的 Data？倾向后者：流式是 Data 类型的特性（`Stream[T]`），不是 Node 的特性。
2. **多入参 Node**：`chat_stream(msg: Message, life: LifeState)` 时 wire 语法到底长什么样？草案用 `.with_latest(LifeState)`，但如果多个 `with_latest(X, Y, Z)` 怎么写、怎么读？
3. **错误边**：Node raise 时是不是变成 Data？`wire(NodeError).to(error_handler)` 作为一等公民？
4. **Capability 的粒度**：`Store` 要不要按业务实体拆（`MessageStore`, `LifeStateStore`），还是通用 `Store[T]`？
5. **DynamicConfig 怎么接**：目前 `dynamic_configs` 是 global side-state。是作为 capability（`config.get(...)`）还是作为 `.with_config(X)` 注入？
6. **Agent 内部的 tool call**：Agent 调工具（比如 `commit_life_state`）算不算产出 Data？——倾向算。工具执行完产出 `LifeState` 这种 Data，通过 wire 路由出去。

## 验收标准

设计落地的硬标准：

1. 在 `apps/agent-service/app/**/*.py`（排除 `app/runtime/` 和 `app/capabilities/` 之后）里：
   - `grep -rE "(rabbitmq|arq\.|redis\.Redis|qdrant_client|AsyncSessionLocal|scan_pending_|asyncio\.create_task)"` 为空
2. 对任意一种 Data，能在 `wiring.py` 里一行列出所有消费者
3. 对任意一个 Node，能从签名读出它消费什么 Data、产出什么 Data
4. `wiring.to_mermaid()` 的输出等价于 agent-service 的架构图
5. 用 fake capability 可以整图 dry-run，断言事件传播正确

## 相关文档

- `docs/superpowers/specs/2026-04-10-agent-service-architecture-refactor.md` — 表层去重方向（LLM 统一、CRUD 拆分、worker 基类）。本设计在其之上做本质抽象；两者可以协同推进，本设计的 Phase 0 可以复用它的基础设施。
- `MANIFESTO.md` — 不要用工程思维消灭 agent 的不确定性。本设计只触及「调度/IO/持久化」的工程层，不涉及 agent 的 prompt/行为。
