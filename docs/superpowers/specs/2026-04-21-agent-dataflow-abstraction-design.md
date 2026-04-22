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

系统里流转的每样东西都有明确类型。Data 是 Pydantic v2 model 的子类，**字段上用 `Annotated[T, Marker]` 标注 identity / dedup / version**——这是 Pydantic v2 官方推荐的扩展方式（和 `AfterValidator`、`Field` 一个 pattern），不是自定义类变量。

```python
from typing import Annotated
from app.runtime.data import Data, Key, DedupKey, Version

class Message(Data):
    message_id: Annotated[str, Key, DedupKey]   # identity 也参与 dedup
    generation: Annotated[int, DedupKey] = 0    # 扩展 dedup，但不影响 identity
    chat_id: str
    persona_id: str
    text: str
    images: list[str] = []

class LifeState(Data):
    persona_id: Annotated[str, Key]             # 每 persona 只一份最新
    version:    Annotated[int, Version]         # as_latest 并发控制
    mood: str
    activity: str

class SafetyVerdict(Data):
    message_id: Annotated[str, Key]
    blocked: bool
    reason: str | None = None

class DriftTrigger(Data):
    chat_id:    Annotated[str, Key, DedupKey]
    persona_id: Annotated[str, Key, DedupKey]
    fired_at: datetime

class ChatResponseChunk(Data):
    """流式一等公民：单条 stream 的一个片段"""
    stream_id: Annotated[str, Key]
    seq:       Annotated[int, Key]
    text: str
    is_final: bool
```

**Marker 语义**：
- `Key` — identity。唯一标识一条 Data 实例；也是 `.as_latest()` upsert 的主键、默认的 dedup key
- `DedupKey` — 扩展 dedup / debounce 分桶 key（叠加在 `Key` 之上）。不标 `DedupKey` 时默认等于 identity
- `Version` — `.as_latest()` 的并发版本字段，runtime 用它做 `UPSERT ON CONFLICT DO UPDATE WHERE new.version > old.version`

Runtime 侧通过 `model.model_fields[name].metadata` 反射拿出 marker，不用额外声明。

**Data 不是**「pg 一张表」也不是「mq 一个 queue」，它就是「一种数据」。持久化和传输是 runtime 根据 wire 属性决定的事。

**Identity/key 的意义**：这是业务必须知道的最小语义层（不是 infra knob）。
- `.as_latest()` 按 `Key` 字段 upsert（`LifeState.persona_id` 作为 Key 意味着每 persona 一份最新）
- `.durable()` 按 dedup key 做幂等（同 `Message.message_id` 重复消费只处理一次；dedup key = `Key` + `DedupKey` 字段）
- `.debounce()` 按 dedup key 分桶（`DriftTrigger` 的 `(chat_id, persona_id)` 每对独立去抖）

**同一 Data 可以有多个 producer**，只要它们产出的实例共享同一 identity 语义。例子：`LifeState` 既来自 `life_engine_tick`（cron 触发），也来自 `state_only_refresh`（schedule 变更触发），两者都按 `persona_id` upsert，消费者始终看到一致的"最新状态"。

**重入/重试场景**（rebuild / afterthought 重跑 / manual retrigger）：给字段加 `DedupKey` marker 就能显式区分"第 N 次重入"。上面 `Message.generation` 标了 `DedupKey`——identity 仍然是 `message_id`（全系统仍把同 message_id 认作同一条消息），但 rebuild 时 `generation=1` 被视为新事件，不会被 debounce 当成 duplicate 丢掉。

如果某条 wire 想临时覆盖 dedup 规则，也可以写 `.debounce(key_fn=...)`，但优先鼓励在 Data 定义里用 marker 表达（字段和角色在一起）。

#### Stream[T] — 流式作为一等公民

LLM token 输出、Agent 多步输出这类"一个调用产出多个值"的场景，**不放在 Node 的返回类型上硬凑 AsyncIterable**，而是把"流"建模成一种 Data：

```python
@node
async def chat_stream(msg: Message, life: LifeState) -> Stream[ChatResponseChunk]: ...
```

`Stream[T]` 的语义：
- 是一个带 `stream_id` 的有序 Chunk 序列
- 每个 Chunk 是普通 Data（有自己的 key），可以走 wire 的正常属性
- 流的**终结**靠 Chunk 自身的 `is_final=True` 标志；**不需要**单独的"流结束事件"原语
- `wire(Stream[ChatResponseChunk]).to(Sink.feishu_send())` 等价于"流里每个 chunk 依次发出去"
- 流中断后**不重复发送已确认的 chunk**（见验收标准"行为不变量"）

**"流完成后触发下游"就是 `.when(lambda c: c.is_final)`**——不是新原语。例如 drift_observer 只关心流的结束时刻：

```python
wire(ChatResponseChunk).to(drift_observer).when(lambda c: c.is_final)
# drift_observer 只在流终结时被触发一次，中间的 token chunks 被 when 过滤
```

其他 post-action（post-safety check、afterthought 触发等）同理，全部用 `.when(is_final)` 表达。这样就避免了当前 `schedule_post_actions()` 里手写 `asyncio.create_task(...)` 的面条代码。

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
- 调用 capability：`LLMClient`, `AgentRunner`, `EmbedderClient`, `VectorStore`, `HTTPClient`, `query(T)` 泛型查询器（无 per-entity Store——持久化由 runtime 根据 wire 属性自动管）
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

业务代码只看到「能力」，看不到「实现」。**持久化不是 capability**——而是 wire 属性的副产物，由 runtime 自动管。所以这一层很薄：

| 能力 | 业务 API | 可能的实现 |
|---|---|---|
| `LLMClient` | `llm.complete(prompt, schema) / llm.stream(...)` | langchain / direct http / mock |
| `AgentRunner` | `agent.stream(ctx, tools)` | LangGraph / custom |
| `EmbedderClient` | `embedder.encode(text)` | openai / bge / jina |
| `VectorStore` | `store.search(vec, k) / store.upsert(frag)` | qdrant / pgvector（向量相似度查询，wire 表达不了） |
| `HTTPClient` | `http.get(url, ...)` | httpx，带 lane routing / trace |
| `query(T)` | `query(Message).where(chat_id=x).order_by_desc("ts").limit(20)` | runtime 根据 Data 定义自动生成 SQL（见下 §Persistence） |

**没有** `LifeStateStore` / `MessageStore` / `ScheduleStore` / `NoteStore` 这类 per-entity Store。这些在旧架构里需要手写，是因为业务代码要自己调 get/save；新架构里业务只 produce/consume Data，不需要主动调存储。

Capability 是业务层第二种语言（除 Data/Node/Wire 之外）。**节点内部**允许通过 capability 做 I/O，但它们不是"mq/pg/redis"的名字。

## Persistence（runtime 自动管，业务不碰）

**核心规则**：**Data 定义 = schema；wire 属性 = CRUD。业务代码里不写 migration，不写 Store 类。**

### 自动建表 / 迁移

Runtime 启动时扫所有注册的 Data 类：

1. 读 Pydantic 字段（名字、类型、nullable、默认值）
2. 读 marker（`Key` 字段作主键；`Version` 字段作并发控制列；`DedupKey` 字段参与 dedup 唯一约束）
3. 生成/对比 pg schema：
   - 首次：`CREATE TABLE data_<snake_case_of_class>`
   - 字段增加：`ALTER TABLE ... ADD COLUMN`（nullable 或带默认值）
   - 字段删除 / 类型 breaking change：**拒绝启动**，要求写显式迁移脚本（保留人类 escape hatch）
4. 根据 wire 属性自动建索引：
   - `.as_latest()` → `PRIMARY KEY (Key 字段)` + `updated_at DESC` 索引
   - `.durable()` → `UNIQUE (dedup_key 字段)` 用于幂等
   - 查询维度索引（`query(T).where(field=...)` 常用字段）→ 可通过 `Annotated[str, Indexed]` 显式声明

### 读写映射

| 操作 | 业务代码 | Runtime 落地 |
|---|---|---|
| 写 | `return Message(...)` + `wire(Message).durable()` | `INSERT INTO data_message ... ON CONFLICT (dedup_hash) DO NOTHING` |
| 读单条（最新） | `@node def f(..., life: LifeState)` + `wire(LifeState).as_latest()` | `SELECT * FROM data_life_state WHERE persona_id=$1 ORDER BY version DESC LIMIT 1` |
| 按 key 读 | runtime 内部，业务看不到 | `SELECT * FROM data_X WHERE <key fields>=$...` |
| 动态查询 | `query(Message).where(chat_id=x).limit(20)` | 翻译成 SQL（过滤 + 排序 + 分页） |

### 不做的事

- **不做跨 Data 的 JOIN**：想要聚合，定义**派生 Data**（未来可能）或在 Node 里先 `query` 再合并；不允许 `query(Message).join(User).on(...)`
- **不做 lazy relationships / eager loading**：Data 是扁平值
- **不做事务嵌套**：每次 Node 执行的所有写在一个事务里；跨 Node 靠 `.durable()` 的 at-least-once + 幂等，不是分布式事务

### Node 内部的受控并发

Node 内部**允许**做"受控的 I/O 并发"——最典型的场景是 safety pre-check 的多 guard 并发 + first-blocked 短路，以及 chat pipeline 的 pre-check 和 token stream 竞速：

```python
@node
async def safety_pre_check(msg: Message) -> SafetyVerdict:
    # 多 guard 并发 + first-blocked 短路：这是 Node 自己的业务逻辑
    return await race_until_blocked([
        guard_injection(msg),
        guard_politics(msg),
        guard_nsfw(msg),
    ])

@node
async def chat_stream(msg: Message, life: LifeState) -> Stream[ChatResponseChunk]:
    # pre-check 和 stream 竞速 + 首 token 缓冲：Node 内部的性能优化
    pre_task = asyncio.create_task(safety.quick_precheck(msg))
    async for chunk in agent.stream(msg.text):
        if not pre_task.done():
            buffer(chunk)
            continue
        if pre_task.result().blocked:
            yield abort_chunk()
            return
        yield chunk
    ...
```

**原则**：只要 Node 没有通过裸 infra（mq / pg client / create_task 触发其他 Node）做**跨节点调度**，内部爱怎么并发怎么并发。Node 的性能/延迟优化是它自己的事，不应上升到 wire 层。

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
   - **Trace context 穿越 durable 边**：runtime 在 `.durable()` 序列化 Data 时，把当前 contextvar（`trace_id` / `session_id` / `request_id` / lane / langfuse `observation_id`）打入 mq payload 的 header 字段；消费端反序列化时恢复 contextvar。业务代码无感知，一条完整 trace 可以跨 http-service → mq → arq-worker 拼出来
   - Lane routing 自动注入（`x-lane` 从 source 延续到 sink，durable 边也不丢）
   - 错误处理、重试、死信
   - `.durable()` 边自带幂等性（基于 Data 的 dedup key = `Key` 字段 + `DedupKey` 字段的集合）
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

- **不做全图 event-sourced**。单个 Data 允许 `.durable() + .as_latest()` 组合（这是 event stream + 投影状态的局部使用，合理）；但不把**整个** agent-service 建模成 append-only log，也不搞 command/query 职责分离到分别的服务。DB 里仍然可以有「当前状态」的表，runtime 自己管。
- **不引入 monad / algebraic effects**。Node 就是 `async def`，不是 generator，不是 `yield Effect(...)`。
- **不强行抽象 Node 内部的 I/O**。Node 内部调 LLM、查 redis、读 pg 都允许——前提是通过 capability 接口（不是裸 `psycopg` / `redis.Redis(...)`）。
- **不替换 agent 执行内核**。LangGraph / `agent.stream()` 保留，只是作为 Node 里的 capability 调用。
- **不把 agent 内部 tool 的副作用暴露给 wire**（本轮）。agent 调工具（如 `commit_life_state`）写入 DB 的 LifeState，由**专门的 life_engine Node** 在下一次产出（读最新状态），不通过 wire 自动流出。Phase 5 之后可能重新考虑"tool call 产出 Data"，本轮维持开放。
- **不重写 Langfuse 集成**。runtime 统一注入（包括穿越 durable 边的 context 传递），业务代码里不再手动 `start_as_current_observation`。
- **不在本轮内消灭 `asyncio.gather`**。节点内部纯并行（比如多 guard 并发、pre-check 和 stream 的受控竞速）仍可用；只是"跨节点的调度"不能用。

## 迁移策略

一次性大爆炸不可行。分阶段，每阶段一条管线迁到新框架，旧代码删掉。

每 phase 完成时的验收线：
- 该管线对应的业务代码里 `grep -rE "mq\.publish|rabbitmq|arq\.|redis_pool|AsyncSessionLocal|asyncio.create_task|scan_pending|find_latest_life_state"` 为空
- wire 图能 `.to_mermaid()` 画出该管线的拓扑
- 新老代码可共存，通过 wire 的运行时开关切换

**关键前提：Phase 顺序的依赖方向**

vectorize / safety_post / drift / afterthought 这些下游 Node 都消费 `Message` 或 `ChatResponseChunk`，这两个 Data 的 producer 在 chat pipeline（最复杂，最后迁）。如果 chat 不先迁，下游 Phase 就拿不到新 Data 模型的输入。

解决方式：**Phase 0 包含一个 Legacy Bridge**，把旧 chat pipeline 的产出桥接成新 Data。Bridge 不是业务代码，是临时 adapter，每消灭一个下游 Phase 就缩小一圈，Phase 5 chat 迁完后删除。

**Phase 顺序**（最终由 plan 阶段细化）：

1. **Phase 0 — Runtime 骨架 + Capability 层 + Schema Migrator + Legacy Bridge**：
   - Data / Node / Wire / Stream[T] 基类；runtime 最小实现
   - Capability adapters：`LLMClient`, `AgentRunner`, `EmbedderClient`, `VectorStore`, `HTTPClient`, `query(T)` 泛型查询器（**无 per-entity Store**）
   - **Schema Migrator**：启动时扫 Data 类，自动 `CREATE TABLE` / `ALTER TABLE ADD COLUMN`；breaking change 拒绝启动并要求人工迁移脚本（保留 escape hatch）
   - **Legacy Bridge**：两个 adapter source——
     - `LegacyMessageBridge`：订阅旧 chat pipeline 写入 pg 后触发的事件（或直接监听旧 mq queue），lift 成 `Message(Data)` 投入新 runtime
     - `LegacyChatResponseBridge`：捕获旧 `stream_chat` 的 token stream，转成 `Stream[ChatResponseChunk]` 投入新 runtime
   - Bridge 在每个后续 Phase 落地后按需收缩；Phase 5 chat 迁完后**完全删除**
   - **与现有 pg schema 的对接**：旧 pg 表（`life_state`, `messages`, `schedules`, `notes`, ...）在 migrator 里配 `existing_table="xxx"` 映射，让新 Data 类直接接管旧表（不迁数据、不改表名）——Phase 0 验收的硬指标之一
2. **Phase 1 — Vectorize 管线**：最独立、最好验证。验收：
   - `apps/agent-service/app/workers/vectorize_worker.py` 的业务逻辑全部迁移到 Node；原 `handle_vectorize` / `cron_scan_pending_messages` 不再由业务代码实现
   - **PaaS 侧的 `vectorize-worker` App 不变**——新 Node 通过 `bind(vectorize).to_app("vectorize-worker")` 绑定到已有 App；资源、副本、镜像由 PaaS 管，业务代码不碰
   - 该 App 的 entry command 从旧的 `python -m app.workers.vectorize` 换成 runtime 统一入口（runtime 根据 APP_NAME 筛 Node）——这是 PaaS 侧 App 配置的一次修改，不是业务代码的事
   - 新 vectorize Node 的 `Message` 输入由 `LegacyMessageBridge` 提供；Phase 5 后换成原生 chat Node 产出
3. **Phase 2 — Safety 管线**：把 safety pre/post 抽成独立 Node，通过 wire 被 chat 调用；此阶段 chat pipeline 本身不迁（仍通过临时适配层调 safety Node）。验收：safety Node 签名清晰 `(Message) -> SafetyVerdict`，`mq.publish(SAFETY_CHECK_QUEUE, ...)` 从 safety 模块消失。
4. **Phase 3 — Drift / Afterthought**：消灭内存 debouncer。输入来自 `LegacyChatResponseBridge` 产出的 `ChatResponseChunk` + `.when(is_final)`。验收：进程重启不丢待触发事件。
5. **Phase 4 — Life Engine / Schedule / Glimpse**：消灭 cron + 现读 pg 模式。
6. **Phase 5 — Chat 主 pipeline**：最复杂，留最后。验收：
   - `chat/pipeline.py` 退化成几条 wire 声明 + 若干 Node
   - Legacy Bridge 完全删除；所有下游 Phase 的 Data 输入换成原生 producer
7. **Phase 6 — 清扫**：删除旧代码、旧 worker 入口、旧 orm crud god object、bridge 残留。

### 行为变更声明（breaking change）

本次重构会改变**两条行为语义**，必须在迁移 PR 的 description 里明确告知运维：

- **部署中断 → 部署不中断**：当前 CLAUDE.md 明文"部署 = 杀 Pod = 中断所有异步任务"，用户已熟悉"部署时正在跑的 afterthought/rebuild 会丢、需要手工重跑"。新框架下 `.durable()` 边的任务会在 Pod 重启后自动从 mq 恢复，不再中断。这改变了用户对"部署副作用"的心理模型——从 Phase 3 开始生效（drift/afterthought 迁移完毕）。
- **rebuild 语义**：当前 rebuild 用户需手动重跑；新框架下 `Message.generation` 字段（标 `DedupKey`）显式声明"这是第 N 次重入"，runtime 按 `(message_id, generation)` 分桶区分而非去重。两种行为不冲突，但使用方式变化。

## 开放问题（writing-plans 阶段需要细化）

1. **多入参 Node**：`chat_stream(msg: Message, life: LifeState)` 时 wire 语法到底长什么样？草案用 `.with_latest(LifeState)`，但如果多个 `with_latest(X, Y, Z)` 怎么写、怎么读？
2. **错误边**：Node raise 时是不是变成 Data？`wire(NodeError).to(error_handler)` 作为一等公民？
3. **DynamicConfig 怎么接**：目前 `dynamic_configs` 是 global side-state。是作为 capability（`config.get(...)`）还是作为 `.with_config(X)` 注入？
4. **Agent 内部 tool call 是否未来需要暴露给 wire**（本轮不做，见非目标）。

## 验收标准

### 静态标准（grep / 签名 / 可视化）

1. 在 `apps/agent-service/app/**/*.py`（排除 `app/runtime/`, `app/capabilities/`, `app/wiring/`, `app/deployment.py`）里：
   - `grep -rE "(rabbitmq|arq\.|redis\.Redis|qdrant_client|AsyncSessionLocal|scan_pending_|asyncio\.create_task\(.*_pipeline|find_latest_life_state)"` 为空
2. 对任意一种 Data，能在 `app/wiring/*.py` 里一行列出所有消费者
3. 对任意一个 Node，能从签名读出它消费什么 Data、产出什么 Data
4. 每个 Data 类必须至少有一个 `Annotated[..., Key]` 字段（启动时校验，无 Key 的 Data 拒绝注册）
5. 业务代码库（排除 runtime / capabilities / wiring / deployment）中，`grep -rE "CREATE TABLE|ALTER TABLE|Alembic|alembic"` 为空——所有 schema 由 Data 定义 + migrator 自动生成
5. `compile_graph().to_mermaid()` 的输出等价于 agent-service 的架构图
6. 用 fake capability 可以整图 dry-run，断言事件传播正确

### 行为不变量

所有不变量分两类：**CI 可自动化** 和 **需演练脚本**。后者放 `tests/integration/chaos/`，运行在人工触发的集成环境里（不在每次 PR 的 CI）。

#### CI 可自动化（每次 PR 运行）

7. **`.durable()` 幂等**：同一 Data（按 dedup key 判定）重复送达，消费者副作用只发生一次
8. **`.with_latest(X)` 可见性 SLO**：runtime 声明并测量——从 X 写入到下游 `with_latest(X)` 读到，P99 延迟 < 500ms（可配）
9. **`.debounce()` 正常路径正确**：同一 key 短时间多次事件在延迟后只触发一次聚合，跨 key 互不影响
10. **Stream 顺序 + 终结**：Chunk 按 `seq` 递增送达；`is_final=True` 的 Chunk 之后没有后续 chunk；当前端 EOF 后 runtime 发出终结事件
11. **`.broadcast()` fire-and-forget**：broadcast 失败不阻塞 producer，不影响其他消费者
12. **Deployment 隔离**：`bind(...).to_app("X")` 声明的 Node 只在 App `X` 的 Pod 里跑，绝对不在其他 App 里执行（启动时枚举可验证）

#### 需演练脚本（跨 phase 必须通过一次，不跑在 PR CI）

位置：`tests/integration/chaos/` + `scripts/chaos/` 下的 shell/python 脚本，手动或预定期触发。

13. **`.durable()` 跨 Pod 重启不丢**：producer 在 A Pod 产出 100 条 durable Data，在 consumer 处理过程中 `kill -9` consumer Pod，重启后全部 100 条必须被消费且只被消费一次（结合不变量 7 的幂等）
14. **`.debounce()` 跨重启不丢**：进入 debounce buffer 的事件在进程重启后仍能按预期延迟触发——需要 redis/pg 持久化 debounce 状态，而不是内存 dict
15. **`.as_latest()` 多 replica 并发写入一致性**：
   - 2 个 replica 对同一 `persona_id` 并发各写 100 次 LifeState
   - 最终读到的 LifeState 必须是**按 serial version 或 `updated_at` 最晚的一次**
   - **实现要求**：`.as_latest()` 落地用 `UPSERT ON CONFLICT (key) DO UPDATE SET ... WHERE new.version > old.version`（或用 `updated_at >` 的同类模式），保证并发写入不出现 lost update；**不允许**用"先 SELECT 再 INSERT"两步。
16. **Stream 消费者 crash 不重发**：消费者处理到第 50 个 chunk 时 crash，重启后从 seq=51 继续，不重复发送前 50 个（runtime 层维护 consumer offset）
17. **Trace 穿越 durable 边连续**：http-service 发起一条 request，trace 里能看到 mq 传递后 arq-worker 里的 node 执行是**同一个 trace**（langfuse session_id 一致）

### Reviewer 已采纳 vs 保留

- **采纳并写进本 spec**：流式一等公民、Data identity/key（Pydantic Annotated marker）、多 producer、Placement 绑定 PaaS App、按域拆 wiring、行为不变量、流终结语义、rebuild 重入 dedup、trace 穿越 durable、Phase 顺序 + Legacy Bridge、as_latest 一致性机制、受控并发合法、行为不变量分类、non-goal 措辞、部署行为变更声明
- **反驳 reviewer**：pre-check 和 stream 竞速不升级到 wire（放 Node 内部，见"受控并发"一节）
- **保留为开放**：Agent 内部 tool call 是否产出 Data（本轮不做）
- **设计演进**（相对 reviewer 建议的简化）：持久层从"per-entity Store"改为"Data 定义 + wire 属性 + runtime 自动 migration"——业务代码里不再出现 Store 类，schema 从 Data 定义推导，通用动态查询统一走 `query(T)` 泛型

## 相关文档

- `docs/superpowers/specs/2026-04-10-agent-service-architecture-refactor.md` — 表层去重方向（LLM 统一、CRUD 拆分、worker 基类）。本设计在其之上做本质抽象；两者可以协同推进，本设计的 Phase 0 可以复用它的基础设施。
- `MANIFESTO.md` — 不要用工程思维消灭 agent 的不确定性。本设计只触及「调度/IO/持久化」的工程层，不涉及 agent 的 prompt/行为。
