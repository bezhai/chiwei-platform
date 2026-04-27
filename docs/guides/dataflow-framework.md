# Dataflow Framework — AI 上手手册

**读者**：下一次进入这个仓库要加 / 改 Node 的 AI。

**目标**：读完 10 分钟内能独立写一个新 `@node`,接入现有管线,不必再查 `app/runtime/*` 或 `app/capabilities/*` 的源码。

**范围**：`app/runtime/*`(Data / @node / wire / Source / emit / durable)、`app/capabilities/*`(LLM / Embedder / VectorStore / HTTP / Agent)、`app/nodes/*`(业务节点)、`app/domain/*`(Data 定义)、`app/wiring/*`(wire 声明)、`app/deployment.py`(节点→App 绑定)。不在范围:MQ topology、schema migrator 内部、compile_graph 的校验层 —— 这些是 runtime 自己的事,@node 作者不用懂。

---

## 1. 心智模型

**Data 流驱动,不是函数调用链。**

```
Source (外部触发)
   │ emit Data
   ▼
 @node ──── emit Data ────▶ wire ──── @node ──── emit Data ──▶ wire ──── @node
 (纯业务)                (图)   (纯业务)                   (图)   (纯业务)
```

- 一个 `@node` **不知道自己的下游是谁**。它只声明"我拿 `Message` 进来,吐一个 `Fragment | None` 出去"。
- 下游由 `wire(Fragment).to(save_fragment)` 声明,写在 `app/wiring/*.py` 里,和业务代码分离。
- Runtime 是**调度器**,不是 orchestrator:它读 `WIRING_REGISTRY` 决定把谁发给谁,然后按边的类型(进程内 / durable / stream)分派。
- 图可以**跨进程**:同一张 wire 图,多个 Deployment 共享 —— `app/deployment.py` 的 `bind(node).to_app("xxx")` 决定 node 在哪个 pod 跑,`.durable()` 的边自动变成 RabbitMQ 消息。

**关键特性:**

| 对象 | 不可变 | 自动注册 | 运行时校验 |
|---|---|---|---|
| Data 实例 | ✅ `frozen=True, extra="forbid"` | 子类 → `DATA_REGISTRY` | 必须至少一个 `Key` 字段 |
| `@node` 函数 | — | → `NODE_REGISTRY` | 参数 + 返回必须是 Data/Stream[Data]/None |
| `wire(T).to(c)` | — | → `WIRING_REGISTRY` | `compile_graph()` 启动时检查一致性 |

---

## 2. 五个核心对象

### 2.1 Data —— 不可变数据载体

所有业务数据定义都继承 `app.runtime.Data`(基于 Pydantic v2,`frozen=True, extra="forbid"`)。`app.runtime` 包是框架的公开入口 —— 业务侧的 import 都走它,不进 `app.runtime.*` 子模块。

```python
# app/domain/summary.py
from __future__ import annotations
from typing import Annotated
from app.runtime import Data, Key


class SummaryFragment(Data):
    message_id: Annotated[str, Key]  # 必须至少一个 Key
    chat_id: str
    summary: str
    created_at: int
```

**两种模式**:

**Native mode(默认)** —— runtime 自动为你建表(`data_summaryfragment`),表结构 append-only,写入自动算 `dedup_hash` + `version`。无需写 `class Meta`。

**Adoption mode** —— 接管**已存在的老表**(不想让 migrator 碰它)。写 `Meta.existing_table` + `Meta.dedup_column`(指向老表里的真实唯一列),runtime 的 `dedup_hash` / `version` 机制**整体让位**给老表的唯一约束。典型例子:

```python
# app/domain/message.py
class Message(Data):
    message_id: Annotated[str, Key]
    user_id: str
    content: str
    # ... 其他字段 mirror ConversationMessage ORM

    class Meta:
        existing_table = "conversation_messages"  # 跳过 migrator
        dedup_column = "message_id"               # 老表的唯一列

    @classmethod
    def from_cm(cls, cm: "ConversationMessage") -> "Message":
        # Bridge 用这个把 ORM 行 lift 成 Data
        return cls(message_id=cm.message_id, ...)
```

**Transient mode** —— 进程内传递,根本不落 pg:

```python
class Fragment(Data):
    fragment_id: Annotated[str, Key]
    # ... 向量、payload 等

    class Meta:
        transient = True  # migrator 跳过,runtime 不尝试持久化
```

**Markers(字段级注解)**:

- `Key` —— 自然主键,必须至少一个。多字段联合 Key 合法。
- `DedupKey` —— 参与 `dedup_hash` 的额外字段(默认只有 Key 参与)。**Adoption mode 下不能用**,和 `Meta.dedup_column` 冲突。
- `Version` —— append-only 版本列,runtime 自增。Adoption mode 下不能用。

**AdminOnly(类级 mixin)** —— 极少用。标记这个 Data 只能由系统/人工产生,任何 `@node` 返回它都会在装饰器时报错:

```python
from app.runtime import Data, AdminOnly, Key
class ManualApproval(Data, AdminOnly):
    approval_id: Annotated[str, Key]
    ...
```

### 2.2 `@node` —— 纯业务函数

```python
# app/nodes/summarize.py
from __future__ import annotations
import time

from app.capabilities.llm import LLMClient
from app.domain.message import Message
from app.domain.summary import SummaryFragment
from app.runtime import node

llm = LLMClient(model_id="gpt-5.4")  # 模块级实例,不要每次调用都 new


@node
async def summarize(msg: Message) -> SummaryFragment | None:
    if not msg.content.strip():
        return None  # 返回 None = 跳过本次,不 emit,下游不触发
    summary = await llm.complete(f"Summarize in one line:\n{msg.content}")
    return SummaryFragment(
        message_id=msg.message_id,
        chat_id=msg.chat_id,
        summary=summary,
        created_at=int(time.time() * 1000),
    )
```

**装饰器做什么(`app/runtime/node.py`)**:

1. 读类型注解,校验所有参数 + 返回都是 `Data` 子类、`Data | None`、`Stream[Data]` 或 `None`;否则 import 时 `TypeError`。
2. 校验返回的 Data **不是** `AdminOnly`。
3. 把函数包装成 wrapper:`return result` 时若 `isinstance(result, Data)`,自动 `await emit(result)` 把 Data 推进图;`None` 跳过。wrapper 仍会把 result 原样返回给调用者,方便单元测试直接 assert。
4. 注册到 `NODE_REGISTRY` + `_NODE_META`(供 `inputs_of(fn)` / `output_of(fn)` 反射使用)。

**签名约束**:

| 允许 | 不允许 |
|---|---|
| `async def f(x: Message) -> Fragment` | `def f(x: Message)` —— 必须 async |
| `async def f(x: Message) -> Fragment \| None` | `def f(x) -> Fragment` —— 参数/返回必须带注解 |
| `async def f(x: Stream[Chunk]) -> None` | `async def f(x: str)` —— 非 Data 参数 |
| `async def f(x: M, y: User) -> F` —— 多输入 | `async def f(x: Message) -> (Fragment, Log)` —— 不能返回 tuple |

**注意**:装饰器只装最顶层。在 @node 里**不要**手写 `await emit(...)` 或 `await mq.publish(...)`。见「常见坑 #1」。

### 2.3 `wire()` —— 声明边

边在 `app/wiring/<topic>.py` 里声明,通过 side-effect import 生效:

```python
# app/wiring/memory.py
from app.domain.message import Message
from app.domain.summary import SummaryFragment
from app.nodes.summarize import summarize
from app.runtime import Source, wire

# 从 MQ 入口 hydrate_message,已在文件其他地方声明
# ...

# 关键声明:
wire(Message).to(summarize).durable()       # durable = 跨进程(MQ+dedup)
wire(SummaryFragment).to(save_summary)      # 默认 = 同进程直调
```

**WireBuilder 方法(`app/runtime/wire.py`)**:

| 方法 | 语义 | 何时用 |
|---|---|---|
| `.to(*targets)` | 消费者(@node 或 SinkSpec) | 必填,可多个(fan-out) |
| `.from_(*sources)` | 入口 Source | 外部触发才需要(MQ / cron / HTTP) |
| `.durable()` | 跨进程:RabbitMQ + consumer 侧 `insert_idempotent` dedup | 跨 Deployment 或要重启续跑时 |
| `.as_latest()` | 写入时只保留最新版本(原子替换) | Data 是"状态快照"而非事件流 |
| `.when(predicate)` | 谓词过滤 | Data 到了但某些场景不想触发 |
| `.debounce(seconds=, max_buffer=)` | 防抖合流 | ⚠️ **未实现**：声明会让 `compile_graph()` 启动报错。引擎尚未支持，节点签名侧的 `Batched[T]` 配套也未设计 |
| `.with_latest(*types)` | 自动 join 最新的 `T`(按同名 Key) | consumer 需要同一上下文的另一种 Data |

> **fan-out 默认是 broadcast 语义**：`wire(T).to(a, b).durable()` 让每个 consumer 在 RabbitMQ 上各自一个独立队列(`durable_<data>_<consumer>`)，各自 dedup、各自 ack，互不影响。无须显式声明。

> **未实现的边语义会启动报错**：runtime 故意把"surface 暴露但引擎未接入"的修饰符做成"用了就 `GraphError`"，避免静默 noop。当前覆盖的是 `.debounce(...)` 和 `.to(Sink.xxx)`。等引擎接入后会同步放开。

**默认边 vs durable 边**:

```
默认(同进程):   emit Data ──直接 await consumer(Data)──▶ 异常向上传
durable(跨进程): emit Data ──RabbitMQ 发到 consumer 所在 App──▶ consumer 侧 dedup + ack
```

经验规则:
- 同一个 Deployment 内部的节点之间 → 默认边(省一次 MQ 跳转)。
- 跨 Deployment(比如 vectorize-worker → chat-response-worker) → `.durable()`。
- 做状态机、事件源、要回放、要重试 → `.durable()`。

### 2.4 `Source` —— 图的入口

```python
from app.runtime import Source

Source.mq("vectorize")               # 消费外部 publisher 的 MQ queue
Source.cron("*/5 * * * *")           # crontab 表达式(分钟级)
Source.interval(seconds=10)          # 秒级定时
Source.http("/api/trigger")          # HTTP endpoint(Runtime 自动注册 FastAPI)
```

> **不在这里**：飞书 webhook 在 lark-proxy(TS) 收，转给 lark-server publish 到 MQ；agent-service 这一侧的入口永远是 `Source.mq("vectorize" / "safety_check" / "chat_request")`，不是直接收 webhook。运维手工触发(rebuild / afterthought)走 `/ops` 命令调内部 endpoint，写法是 `Source.http("/api/internal/rebuild")` —— 没有专门的 `Source.manual`，因为它跟 http 没有运行时差异。

用法:

```python
wire(MessageRequest).to(hydrate_message).from_(Source.mq("vectorize"))
```

**MQSource 的特殊约定**:
- 目标 @node 必须**单参数**(第一个 Data 就是 decode 目标)。
- runtime 读 MQ body 时会**过滤掉**不在 `req_cls.model_fields` 里的字段(适配老 publisher 带额外字段),所以 Data 保持严格 `extra="forbid"` 不会误伤。
- Queue 名按 lane 自动加后缀:`"vectorize"` 在 df-v0 lane 变成 `"vectorize_df-v0"`。

### 2.5 `Sink` —— 图的出口

```python
from app.runtime import Sink

Sink.mq("chat_response")             # 把 Data publish 到指定 MQ 队列(图外部消费者读)
```

用法:

```python
wire(Reply).to(Sink.mq("chat_response"))
# Reply 出图 → RabbitMQ chat_response 队列 → chat-response-worker(TS) 消费 → 飞书 API
```

**为什么只有 `Sink.mq`**:runtime 只懂协议(写 MQ 队列),不懂业务(发飞书 / 调外部 webhook)。"图的 Data 出口"本质上就是写一个队列,具体业务由队列的消费者(独立服务)实现 —— `chat-response-worker` 是 TS 服务,在图外消费 `chat_response` 队列调飞书 send_message API。要新增"出口业务",新建一个消费者服务,不动 runtime。

> Lane 后缀:`"chat_response"` 在 df-v0 lane 变成 `"chat_response_df-v0"`,跟 `Source.mq` 同规则。

> ⚠️ **当前未实现**:`Sink.mq` 的 surface 已经定下来,但引擎尚未实现 sink dispatch —— 在 wire 上声明任何 `Sink.xxx` 会让 `compile_graph()` 启动 `GraphError`。这是**故意的**:防止"声明了静默 noop"。等引擎接入第一个 sink 用例时同步开通。

### 2.6 `Bridge` —— 过渡层

老代码写 `ConversationMessage` ORM 行是事实,把它"lift" 成 Data 推进图,就是 Bridge 的唯一职责:

```python
# app/bridges/message_bridge.py
async def emit_legacy_message(cm: ConversationMessage) -> None:
    await emit(Message.from_cm(cm))
```

**每个老写入点在 `session.commit()` 之后调一次**(见 `app/life/proactive.py`)。commit 之前 emit 会让下游 @node 查不到行。

**Bridge 是临时的**:等老 writer 全迁到 @node(或 @node 拿到直接 write 权限),Bridge 文件整体删除。不要基于 Bridge 搭长期逻辑。

---

## 3. Cookbook:加一个新 @node

场景:**读每条 Message,生成一句话摘要,存进 pg 的 `data_summaryfragment` 表**(新建 Data,native mode)。

### Step 1 — 建 Data

```python
# app/domain/summary.py
from __future__ import annotations
from typing import Annotated
from app.runtime import Data, Key


class SummaryFragment(Data):
    message_id: Annotated[str, Key]
    chat_id: str
    summary: str
    created_at: int
```

没有 `class Meta` → native mode → migrator 自动建 `data_summaryfragment` 表(含 `dedup_hash`、`version`、以上四个字段)。

### Step 2 — 写 @node

```python
# app/nodes/summarize.py
from __future__ import annotations
import logging
import time

from app.capabilities.llm import LLMClient
from app.domain.message import Message
from app.domain.summary import SummaryFragment
from app.runtime import node

logger = logging.getLogger(__name__)
llm = LLMClient(model_id="gpt-5.4")


@node
async def summarize(msg: Message) -> SummaryFragment | None:
    if not msg.content.strip():
        logger.info("summarize: message=%s empty, skip", msg.message_id)
        return None
    summary = await llm.complete(f"Summarize in one short sentence:\n{msg.content}")
    logger.info("summarize: done message=%s len=%d", msg.message_id, len(summary))
    return SummaryFragment(
        message_id=msg.message_id,
        chat_id=msg.chat_id,
        summary=summary.strip(),
        created_at=int(time.time() * 1000),
    )
```

### Step 3 — 声明 wire

```python
# app/wiring/memory.py 追加:
from app.domain.summary import SummaryFragment
from app.nodes.summarize import summarize

wire(Message).to(summarize).durable()  # 和 vectorize 共享 Message durable,fan-out 两条独立消费
# SummaryFragment 不声明下游 = 只走持久化(runtime 自动 persist 到 data_summaryfragment)
```

**注意**:`Message` 已经有一条 `wire(Message).to(vectorize).durable()`。再加 `.to(summarize)` 就是 fan-out —— 两个消费者各自独立 dedup + 重试,互不影响。

### Step 4 — 绑定 Deployment

```python
# app/deployment.py 追加:
from app.nodes.summarize import summarize

bind(summarize).to_app("vectorize-worker")  # 和 vectorize/save_fragment 同 pod
```

不绑定 = 默认 `agent-service`(HTTP 主服务)。绑到 `vectorize-worker` 意味着在 worker pod 里跑,主 HTTP pod 不启动此 node。

### Step 5 — 单元测试

```python
# apps/agent-service/tests/nodes/test_summarize.py
from unittest.mock import AsyncMock, patch
import pytest
from app.domain.message import Message
from app.domain.summary import SummaryFragment

from app.nodes.summarize import summarize as summarize_node  # @wrapper


def _msg(content: str = "hello") -> Message:
    return Message(
        message_id="m1", user_id="u1", content=content, role="user",
        root_message_id="r1", reply_message_id=None, chat_id="c1",
        chat_type="p2p", create_time=1700_000_000, message_type="text",
        bot_name=None, response_id=None,
    )


@pytest.mark.asyncio
async def test_summarize_skips_empty():
    # wrapper 会尝试 emit None,emit 自然跳过 —— wiring 是空的,无副作用
    result = await summarize_node(_msg(content="   "))
    assert result is None


@pytest.mark.asyncio
async def test_summarize_returns_fragment():
    with patch(
        "app.nodes.summarize.llm.complete",
        new_callable=AsyncMock,
        return_value="brief summary",
    ):
        # 同理,单测环境 WIRING_REGISTRY 为空,wrapper 的 emit 是 no-op
        result = await summarize_node(_msg(content="hello world"))
    assert isinstance(result, SummaryFragment)
    assert result.summary == "brief summary"
    assert result.message_id == "m1"
```

跑:`cd apps/agent-service && uv run pytest tests/nodes/test_summarize.py -v`

### Step 6 — 泳道验证

```bash
# 1. 推分支
git push origin <branch>

# 2. 部署(构建 + release)
make deploy APP=vectorize-worker LANE=<your-lane> GIT_REF=<branch>

# 3. 绑 dev bot 到泳道
/ops bind bot dev <your-lane>

# 4. 去飞书 dev bot 发消息

# 5. 查日志验证 5 跳 + summarize 新跳
make logs APP=vectorize-worker LANE=<your-lane> SINCE=5m

# 期望看到:
# mq source received frame
# hydrate_message: start/done
# durable consumer vectorize: processing Message
# vectorize: start/done
# save_fragment: start/done
# durable consumer summarize: processing Message  ← 新的
# summarize: done                                  ← 新的

# 6. 查 DB 确认 summary 落表
/ops-db @chiwei "SELECT * FROM data_summaryfragment ORDER BY version DESC LIMIT 5"
```

---

## 4. Capabilities —— 调外部能力的正确姿势

在 `@node` 里**不要直接 import `app.infra.*`**。用 capability 适配器:`app/capabilities/` 下每个文件都是一层"固定好参数的瘦客户端",单测时 mock 一个 capability 比 mock 整个 infra module 容易得多。

### `LLMClient` —— 调 LLM

```python
from app.capabilities.llm import LLMClient
llm = LLMClient(model_id="gpt-5.4")  # 模块级单例

text = await llm.complete(prompt)
async for chunk in llm.stream(prompt):
    ...
```

- `model_id` 走项目的 model-mapping 表(在 DB 里),解析出真实 provider+model。
- `complete()` 只支持文本输出(multimodal response 会 raise)。
- 所有 LLM 调用**必须**接入 Langfuse trace —— `LLMClient` 内部已经接好,直接用就行。禁止绕过 capability 直接写 `langchain_core.language_models`。

### `EmbedderClient` —— 生成向量

```python
from app.capabilities.embed import EmbedderClient
embedder = EmbedderClient(model_id="embedding-model")

dense = await embedder.dense(text="hello", instructions="Retrieve similar content")
hybrid = await embedder.hybrid(
    text="hello",
    image_base64_list=["...base64..."],
    instructions="Index document",
)
# hybrid.dense: list[float]; hybrid.sparse.indices / .values
```

### `VectorStore` —— 读写 qdrant

```python
from app.capabilities.vector_store import VectorStore
recall_store = VectorStore("messages_recall")

await recall_store.upsert(point_id="uuid...", embedding=hybrid, payload={...})
await recall_store.upsert_dense(point_id="uuid...", dense=[...], payload={...})
hits = await recall_store.search(embedding=hybrid, limit=10, query_filter=...)
```

`point_id` 用**确定性 UUID**(例如 `vector_id_for(message_id)` 在 `app/nodes/_ids.py`),这样重试和 upsert 天然幂等。

### `HTTPClient` —— 外部 HTTP

```python
from app.capabilities.http import HTTPClient
client = HTTPClient(service="lark-server")    # 或 service=None + 绝对 URL

resp = await client.get("/internal/xyz")
resp = await client.post("/api/foo", json={"x": 1})
```

`service="xxx"` 时自动走 LaneRouter —— `{app}-{lane}:port` 优先,落到 `{app}:port` fallback。trace / lane header 自动注入。

### `AgentRunner` —— 跑多步 agent

```python
from app.capabilities.agent import AgentRunner
from app.agent.core import AgentConfig

runner = AgentRunner(
    AgentConfig(name="my_agent", system_prompt="You are..."),
    tools=[...],
)

result = await runner.run(messages=[{"role": "user", "content": "hi"}])
async for chunk in runner.stream(messages=[...]):
    ...
structured = await runner.extract(MyPydanticModel, messages=[...])
```

---

## 5. 常见坑(AI 最容易错的地方)

### #1 手写 emit / 手写 mq.publish

@node 已经被 wrapper 包了,返回 Data 就自动 emit。**不要再手写**。

```python
# ❌
@node
async def summarize(msg: Message) -> None:
    frag = SummaryFragment(...)
    await emit(frag)             # 重复 emit,下游会收到两次
    await mq.publish(VECTORIZE, ...)   # 直接捅 infra,绕过图

# ✅
@node
async def summarize(msg: Message) -> SummaryFragment:
    return SummaryFragment(...)  # wrapper 自动 emit
```

### #2 直接 import infra

infra 是 runtime + capability 的内部,@node 不碰:

```python
# ❌
from app.infra.qdrant import qdrant
from app.infra.rabbitmq import mq

# ✅
from app.capabilities.vector_store import VectorStore
store = VectorStore("messages_recall")
```

例外:Bridge 层(`app/bridges/*`)为了 lift legacy ORM,可以碰 infra —— 它本来就是过渡适配层。

### #3 Data 继承错基类

```python
# ❌
from pydantic import BaseModel
class SummaryFragment(BaseModel):        # 不会注册,不会参与 wire,migrator 看不到
    ...

# ✅
from app.runtime import Data
class SummaryFragment(Data):
    ...
```

### #4 对 Data 表写 UPDATE

Data 表是 append-only。想"更新"就 emit 新版本,runtime 自增 `version`。

```python
# ❌
await session.execute("UPDATE data_userprofile SET name='x' WHERE user_id='u1'")

# ✅
await emit(UserProfile(user_id="u1", name="x"))  # runtime 追加新行,version +1
# 读取最新版本(Versioned Data 用 query() 默认就是 latest-per-key):
from app.runtime import query
rows = await query(UserProfile).where(user_id="u1").all()
row = rows[0] if rows else None
```

想要"总是只保留最新"的语义,在 wire 声明时 `.as_latest()`。

### #5 Adoption mode 漏写 `dedup_column`

接管老表没写 `dedup_column`,runtime 会尝试写 `dedup_hash` 列 —— 老表没这列,INSERT 直接炸。

```python
# ❌
class Message(Data):
    message_id: Annotated[str, Key]
    class Meta:
        existing_table = "conversation_messages"  # 少了 dedup_column

# ✅
class Message(Data):
    message_id: Annotated[str, Key]
    class Meta:
        existing_table = "conversation_messages"
        dedup_column = "message_id"  # 指向老表真实的唯一列
```

同时:adoption mode 下**禁止**给字段标 `DedupKey`、`Version`(__pydantic_init_subclass__ 会直接 `TypeError`)。

### #6 runtime_entry 的 import 顺序

> 仅 `runtime_entry.py` 启动器需要,业务节点不会用 `Runtime`。`Runtime` 不在 `app.runtime` 公开入口,故走子模块 `app.runtime.engine`。

`app/workers/runtime_entry.py` 的前两行 side-effect import **必须**在 `from app.runtime.engine import Runtime` 之前:

```python
# ❌
from app.runtime.engine import Runtime
import app.deployment    # 太晚了,Runtime 已经锁定空图
import app.wiring

# ✅
import app.deployment    # 先注册 bindings
import app.wiring        # 再注册 wires
from app.runtime.engine import Runtime  # 最后 Runtime 读已注册的 WIRING_REGISTRY
```

`noqa: F401` 显式标注 side-effect import 避免 linter 误删。

### #7 给 Data 开 extra="allow"

`Data` 基类锁定 `extra="forbid"`,别覆盖:

```python
# ❌
class LooseData(Data):
    message_id: Annotated[str, Key]
    model_config = ConfigDict(extra="allow")  # 契约破坏,下游拿到什么字段不确定

# ✅  —— 真的需要"额外字段"就放一个 dict:
class MessageWithExtra(Data):
    message_id: Annotated[str, Key]
    extras: dict[str, str] = {}
```

### #8 在 @node 里按类型分支路由

@node 一个出口只出一种 Data。要"分流" → 用 `wire.when(...)` 谓词过滤:

```python
# ❌
@node
async def classify(msg: Message) -> SummaryFragment | TagFragment | None:
    if msg.content.startswith("#"):
        return TagFragment(...)
    return SummaryFragment(...)

# ✅ 拆成两个 @node,在 wire 层过滤
@node
async def summarize(msg: Message) -> SummaryFragment:
    return SummaryFragment(...)
@node
async def extract_tag(msg: Message) -> TagFragment:
    return TagFragment(...)

# wiring:
wire(Message).to(summarize).when(lambda m: not m.content.startswith("#")).durable()
wire(Message).to(extract_tag).when(lambda m: m.content.startswith("#")).durable()
```

### #9 用工程规则消 agent 不确定性

来自项目 CLAUDE.md 的铁律:

> 不要用工程思维解决 agent 的不确定性问题。
> 赤尾的行为不符合预期时,优化她的输入(context、prompt、stimulus、agent 协作),
> 而不是在逻辑层加确定性规则(阈值、计数器、格式化函数、随机池、if/else 分支)。

具体到 @node:如果 LLM 输出不稳,**不要**加 `if len(output) > 100: truncate`、`retry 3 times`、`random.choice(["a", "b"])` 之类兜底。改 prompt 或让 LLM 输出结构化字段。

### #10 cutover 期保留两条写路径

改造一个老功能,**写路径只能有一条**。Bridge 只在读侧 lift 老数据,不是"老代码一份新代码一份都保留"。

```python
# ❌
async def write_message(cm):
    session.add(cm)
    await session.commit()
    await emit_legacy_message(cm)        # Bridge 发 Data
    await mq.publish(VECTORIZE, {...})   # 同时又走老队列

# ✅  Bridge 是老 writer 的延长线,老 publisher 下游切到 runtime 就结束
async def write_message(cm):
    session.add(cm)
    await session.commit()
    await emit_legacy_message(cm)  # 仅此一条
```

### #11 忘记在返回 `None` 时做真正的 early return

`@node` 返回 `None` 表示"跳过下游",wrapper 看到 `None` 就不 emit。但如果先算了昂贵的外部调用再 `return None`,钱就烧了。早退。

```python
# ❌
@node
async def vectorize(msg: Message) -> Fragment | None:
    emb = await embedder.hybrid(text=msg.content or "")   # 空字符串也花 LLM 费
    if not msg.content:
        return None
    ...

# ✅
@node
async def vectorize(msg: Message) -> Fragment | None:
    if not msg.content:
        return None  # 早退
    emb = await embedder.hybrid(text=msg.content)
    ...
```

---

## 6. 现有节点清单 + 源码索引

### Phase 1 已上线节点

| 节点 | 文件 | 输入 | 输出 | Deployment |
|---|---|---|---|---|
| `hydrate_message` | `app/nodes/hydrate_message.py` | `MessageRequest` | `Message \| None` | vectorize-worker |
| `vectorize` | `app/nodes/vectorize.py` | `Message` | `Fragment \| None` | vectorize-worker |
| `save_fragment` | `app/nodes/save_fragment.py` | `Fragment` | `None`(终点) | vectorize-worker |

### 读源码最短路径

想快速建立体感,按下面顺序读(加起来 <200 行):

1. `app/domain/message_request.py` —— 最简单的 Data(单字段)。
2. `app/domain/message.py` —— adoption-mode Data + `from_cm` 模式。
3. `app/domain/fragment.py` —— transient Data。
4. `app/nodes/hydrate_message.py` —— 最短的 @node,只有 DB 查询 + 返回 Data。
5. `app/nodes/vectorize.py` —— 标准 @node,两个 capability + 两次 early return。
6. `app/nodes/save_fragment.py` —— 消费 transient Fragment、并行写两个 store。
7. `app/wiring/memory.py` —— 整条 pipeline 的 wire 声明(18 行)。
8. `app/deployment.py` —— 3 行 bind。
9. `app/workers/runtime_entry.py` —— Worker 启动入口。

### 源码参考点(不必记,需要时回查)

| 想了解 | 看哪里 |
|---|---|
| @node 装饰器做什么 | `app/runtime/node.py` |
| Data 校验规则 | `app/runtime/data.py::__pydantic_init_subclass__` |
| wire DSL 全部方法 | `app/runtime/wire.py::WireBuilder` |
| 所有 Source 种类 | `app/runtime/source.py::Source` |
| emit 分派逻辑 | `app/runtime/emit.py::emit` |
| durable 边实现 | `app/runtime/durable.py` |
| MQSource 消费循环 | `app/runtime/engine.py::_source_loop_mq` |
| Node → App 绑定 | `app/runtime/placement.py::bind` |
| Capability 清单 | `app/capabilities/` |

---

## 附录:核心不变量(违反会炸)

运行时会在启动 / import / 调用时强制的契约,遇到 `TypeError`/`RuntimeError` 先回来对这份清单:

1. 每个 `Data` 子类至少一个 `Key` 字段(`__pydantic_init_subclass__`)。
2. Adoption mode 的 Data **不能**带 `DedupKey` / `Version`(`__pydantic_init_subclass__`)。
3. `@node` 参数 + 返回必须是 `Data / Data | None / Stream[Data] / None`(`node()` 装饰时)。
4. `@node` 不能返回 `AdminOnly` Data(`node()` 装饰时)。
5. 一个 @node 只能绑一个 App(`placement.bind` 重复绑定 raises)。
6. durable 边的 consumer 必须**单 Data 参数**(MQSource 契约,`_source_loop_mq` 检查)。
7. Runtime 启动时 `compile_graph()` 会检查 wire 一致性(生产者 Data 类型 ↔ 消费者签名)—— 启动报错看这里。
8. `emit(data)` 不会匹配任何 wire 时静默 no-op(不是错),测试里的 wiring 清空是利用这一点。
