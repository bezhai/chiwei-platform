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

系统里流转的每样东西都有明确类型。Pydantic / dataclass，照常。**但每个 Data 类必须显式声明两件事：payload schema 和 identity（key）函数**。

```python
class Message(Data):
    chat_id: str
    persona_id: str
    message_id: str
    text: str
    images: list[str]
    ...
    # identity：用于幂等 / as_latest upsert / debounce 分桶
    key = ("message_id",)

class LifeState(Data):
    persona_id: str
    mood: str
    activity: str
    ...
    key = ("persona_id",)   # 同一 persona 只保留一份最新

class SafetyVerdict(Data):
    message_id: str
    blocked: bool
    ...
    key = ("message_id",)

class DriftTrigger(Data):
    chat_id: str
    persona_id: str
    ...
    key = ("chat_id", "persona_id")   # debounce 按这个 key 聚合

class ChatResponseChunk(Data):
    """流式一等公民：单条 stream 的一个片段"""
    stream_id: str           # 归属的流
    seq: int                 # 流内顺序
    text: str
    is_final: bool
    key = ("stream_id", "seq")
```

**Data 不是**「pg 一张表」也不是「mq 一个 queue」，它就是「一种数据」。持久化和传输是 runtime 根据 wire 属性决定的事。

**Identity/key 的意义**：这是业务必须知道的最小语义层（不是 infra knob）。
- `.as_latest()` 按 key upsert（`LifeState.key=persona_id` 意味着每个 persona 只保留最新）
- `.durable()` 按 key 做幂等（同一 `Message.message_id` 重复消费只处理一次）
- `.debounce()` 按 key 分桶（`DriftTrigger.key=(chat_id, persona_id)` 每对独立去抖）

**同一 Data 可以有多个 producer**，只要它们产出的实例共享同一 key 语义。例子：`LifeState` 既来自 `life_engine_tick`（cron 触发），也来自 `state_only_refresh`（schedule 变更触发），两者都按 `persona_id` upsert，消费者始终看到一致的"最新状态"。

#### Stream[T] — 流式作为一等公民

LLM token 输出、Agent 多步输出这类"一个调用产出多个值"的场景，**不放在 Node 的返回类型上硬凑 AsyncIterable**，而是把"流"建模成一种 Data：

```python
@node
async def chat_stream(msg: Message, life: LifeState) -> Stream[ChatResponseChunk]: ...
```

`Stream[T]` 的语义：
- 是一个带 `stream_id` 的有序 Chunk 序列
- 每个 Chunk 是普通 Data（有自己的 key），可以走 wire 的正常属性
- 流的**边界事件**（开始 / 完成 / 中断）是一等事件，runtime 提供
- `wire(Stream[ChatResponseChunk]).to(Sink.feishu_send())` 等价于"流里每个 chunk 依次发出去"
- 流中断后**不重复发送已确认的 chunk**（see 验收标准"行为不变量"）

这意味着 chat pipeline 的迁移不再依赖"后补 stream 建模"——本设计从 Phase 0 起就把 `Stream[T]` 作为 runtime 一等公民实现。

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

**按业务域拆分 wiring 文件**，启动时 compile 成一张全局图。业务看单域接线，可视化看全局。

```
app/wiring/
    __init__.py          # compile：import 所有子 module 并合并成 GLOBAL_GRAPH
    chat.py              # chat 域的所有 wire(...)
    memory.py            # memory / vectorize 域
    life.py              # life engine / schedule / glimpse 域
    safety.py            # safety 域
```

这样做的理由：
- 单域 wiring 是"业务认知单元"——看 `chat.py` 就知道 chat 相关的所有生产-消费
- 启动 compile 成全局 `GLOBAL_GRAPH` 支持跨域可视化、冲突检测（比如两个域都声明某个 Data 是 producer/consumer 需要校验一致性）
- 避免 `wiring.py` 长成 god file

生产者和消费者显式连起来（下例都来自 `app/wiring/chat.py` 或 `app/wiring/memory.py`）：

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

看一眼对应域的 wiring 文件（或者 compile 后的 `GLOBAL_GRAPH`）就能回答：

- **`LifeState` 谁产出？** → grep 所有返回 `LifeState` 的 Node（可能不止一个，但按同一 key upsert）
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

## Deployment / Placement 层

**业务代码不碰 infra。Node 跑在哪个 Pod 是一个「归属」关系——绑定到 PaaS 里已存在的 App。**

PaaS Engine 的模型是：**App 是独立的部署实体**（`port=0` 时就是 worker）。`agent-service` / `arq-worker` / `vectorize-worker` 是 PaaS 里**已存在的三个 App 记录**（同一镜像、不同 entry command）。资源、副本、镜像、namespace 全部由 PaaS API 管。业务代码只做一件事：**把 Node 绑到哪个 App**。

```
app/deployment.py    # Node → PaaS App 的归属声明
```

示例：

```python
# 引用的 app name 必须在 PaaS 里已经存在
# 不声明 = 跑在主 App（agent-service，HTTP 服务所在 Pod）

bind(vectorize).to_app("vectorize-worker")
bind(save_fragment).to_app("vectorize-worker")
bind(afterthought).to_app("arq-worker")
bind(drift_observer).to_app("arq-worker")
bind(life_engine_tick).to_app("arq-worker")
# chat_stream / safety_check / recall_* 不声明，默认跑在 agent-service
```

Runtime 启动时根据当前 App name 筛选要跑的 Node：

```python
# app/main.py 等入口统一读：
app_name = os.environ["APP_NAME"]      # PaaS 注入
runtime.run(nodes_bound_to(app_name))
```

关键性质：
- **Node 作者不知道也不关心自己跑在哪个 Pod**。同一份代码跑在 agent-service Pod 和 arq-worker Pod，只是筛出的 Node 集合不同。
- **资源 / 副本 / 镜像 / 环境变量**全部由 PaaS 管，业务代码里没有这些字段——不像我最初草案那样 `as_worker(replicas=..., resources=...)`，那是在业务代码里重新定义 PaaS 已有的东西。
- **加新 worker 的唯一流程**：先通过 PaaS API 建新 App → 然后在 `deployment.py` 里 `bind(...).to_app(new_app_name)`。顺序反过来报错（绑到不存在的 App）。
- **`.durable()` 与 deployment 正交**：`.durable()` 说"边不能丢"，deployment 说"Node 跑在哪个 App"。两者可以任意组合。

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
2. **Phase 1 — Vectorize 管线**：最独立、最好验证。验收：
   - `apps/agent-service/app/workers/vectorize_worker.py` 的业务逻辑全部迁移到 Node；原 `handle_vectorize` / `cron_scan_pending_messages` 不再由业务代码实现
   - **PaaS 侧的 `vectorize-worker` App 不变**——新 Node 通过 `bind(vectorize).to_app("vectorize-worker")` 绑定到已有 App；资源、副本、镜像由 PaaS 管，业务代码不碰
   - 该 App 的 entry command 从旧的 `python -m app.workers.vectorize` 换成 runtime 统一入口（runtime 根据 APP_NAME 筛 Node）——这是 PaaS 侧 App 配置的一次修改，不是业务代码的事
   - 即：消失的是业务代码文件，不是 PaaS App
3. **Phase 2 — Safety 管线**：把 safety pre/post 抽成独立 Node，通过 wire 被 chat 调用；此阶段 chat pipeline 本身不迁（仍通过临时适配层调 safety Node）。验收：safety Node 签名清晰 `(Message) -> SafetyVerdict`，`mq.publish(SAFETY_CHECK_QUEUE, ...)` 从 safety 模块消失。
4. **Phase 3 — Drift / Afterthought**：消灭内存 debouncer。验收：进程重启不丢待触发事件。
5. **Phase 4 — Life Engine / Schedule / Glimpse**：消灭 cron + 现读 pg 模式。
6. **Phase 5 — Chat 主 pipeline**：最复杂，留最后。验收：`chat/pipeline.py` 退化成几条 wire 声明 + 若干 Node。
7. **Phase 6 — 清扫**：删除旧代码、旧 worker 入口、旧 orm crud god object。

## 开放问题（writing-plans 阶段需要细化）

1. **多入参 Node**：`chat_stream(msg: Message, life: LifeState)` 时 wire 语法到底长什么样？草案用 `.with_latest(LifeState)`，但如果多个 `with_latest(X, Y, Z)` 怎么写、怎么读？
2. **错误边**：Node raise 时是不是变成 Data？`wire(NodeError).to(error_handler)` 作为一等公民？
3. **Capability 的粒度**：`Store` 要不要按业务实体拆（`MessageStore`, `LifeStateStore`），还是通用 `Store[T]`？
4. **DynamicConfig 怎么接**：目前 `dynamic_configs` 是 global side-state。是作为 capability（`config.get(...)`）还是作为 `.with_config(X)` 注入？
5. **Agent 内部的 tool call**：Agent 调工具（比如 `commit_life_state`）算不算产出 Data？——倾向算。工具执行完产出 `LifeState` 这种 Data，通过 wire 路由出去。

## 验收标准

### 静态标准（grep / 签名 / 可视化）

1. 在 `apps/agent-service/app/**/*.py`（排除 `app/runtime/`, `app/capabilities/`, `app/wiring/`, `app/deployment.py`）里：
   - `grep -rE "(rabbitmq|arq\.|redis\.Redis|qdrant_client|AsyncSessionLocal|scan_pending_|asyncio\.create_task\(.*_pipeline|find_latest_life_state)"` 为空
2. 对任意一种 Data，能在 `app/wiring/*.py` 里一行列出所有消费者
3. 对任意一个 Node，能从签名读出它消费什么 Data、产出什么 Data
4. 每个 Data 类必须声明 `key = (...)` tuple（静态校验）
5. `compile_graph().to_mermaid()` 的输出等价于 agent-service 的架构图
6. 用 fake capability 可以整图 dry-run，断言事件传播正确

### 行为不变量（每个迁移 phase 必须通过的行为测试）

7. **`.durable()` 幂等**：同一 Data 实例（按 key 判定）重复送达消费者，消费者副作用只发生一次（通过消费者幂等标记或 runtime 去重确保）
8. **`.durable()` 不丢**：进程 kill -9 + 重启后，未 ack 的 Data 继续被消费
9. **`.debounce()` 跨重启不丢**：进入 debounce buffer 的事件在进程重启后仍能按预期延迟触发（内存 debouncer 的退化场景，必须通过 redis/pg 持久化解决）
10. **`.as_latest()` 一致性**：多 producer 对同一 key 并发写入时，最终读到的是**时间上最后一次写入的值**（按 wall-clock 或版本号），不出现"先写的 producer 覆盖后写的 producer"的 lost update
11. **`.with_latest(X)` 可见性**：`wire(X).as_latest()` 的写入对下游 `with_latest(X)` 的读取可见延迟有明确上限（runtime 需声明：例如 <1s 或强一致读）
12. **Stream 不重发**：Stream 中断恢复时（比如 consumer crash 后重启），已 ack 的 chunk 不重复发送；未 ack 的 chunk 从断点继续（seq 连续性保证）
13. **Deployment 隔离**：被 `deploy(...).as_worker(name=X)` 声明的 Node，运行在名为 X 的独立 K8s Deployment 中，崩溃不影响其他 worker 和主 service

行为不变量每条都必须有对应的自动化测试或演练脚本；`grep 为空` 只能证明 infra 没泄漏到业务代码，不能证明行为等价。

## 相关文档

- `docs/superpowers/specs/2026-04-10-agent-service-architecture-refactor.md` — 表层去重方向（LLM 统一、CRUD 拆分、worker 基类）。本设计在其之上做本质抽象；两者可以协同推进，本设计的 Phase 0 可以复用它的基础设施。
- `MANIFESTO.md` — 不要用工程思维消灭 agent 的不确定性。本设计只触及「调度/IO/持久化」的工程层，不涉及 agent 的 prompt/行为。
