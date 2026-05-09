# Dataflow Framework — AI 上手手册

**读者**：下一次进入这个仓库要加 / 改 Node 的 AI。

**目标**：读完 10 分钟内能独立写一个新 `@node`,接入现有管线,不必再查 `app/runtime/*` 或 `app/capabilities/*` 的源码。

**范围**：`app/runtime/*`(Data / @node / wire / Source / emit / durable)、`app/capabilities/*`(LLM / Embedder / VectorStore / HTTP / Agent)、`app/nodes/*`(业务节点)、`app/domain/*`(Data 定义)、`app/wiring/*`(wire 声明)、`app/deployment.py`(节点→App 绑定)。不在范围:MQ topology、schema migrator 内部、compile_graph 的校验层 —— 这些是 runtime 自己的事,@node 作者不用懂。

---

## 0. 终态原则

**终态不是"业务代码从 `mq.publish` 换成 `wire(...)`"。终态是：无论写 chat、tool、memory、schedule、long task，业务作者都不需要理解底层 transport / worker / retry / outbox / lane / trace / DLQ。**

判断一个改动是否还在正确方向上：

- 业务代码只表达 Data、业务处理和业务能力调用；不表达 RabbitMQ、Redis、ARQ、FastAPI route、trace/lane header、DLQ replay、事务后补 emit。
- 如果需要一种底层能力，先扩 `app/runtime/*` 或 `app/capabilities/*`，再让业务使用新的公开 API；不要在业务里临时手写绕过。
- 如果业务作者必须读 `app/runtime/*` 才知道怎么正确使用某个能力，说明文档或框架 surface 还不够，应该补框架/补手册。
- 当前仍存在的终态 gap 见 `docs/superpowers/specs/2026-05-07-dataflow-phase-7-gap-analysis.md`。

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
- Runtime 是**调度器**,不是 orchestrator:它读 `WIRING_REGISTRY` 决定把谁发给谁,然后按边的类型(进程内 / durable)分派。"一调用产多值"(LLM token、fan-out)直接在 @node body 里多次 `await emit(...)`,见「常见坑 #1」。
- 图可以**跨进程**:同一张 wire 图,多个 Deployment 共享 —— `app/deployment.py` 的 `bind(node).to_app("xxx")` 决定 node 在哪个 pod 跑,`.durable()` 的边自动变成 RabbitMQ 消息。

**关键特性:**

| 对象 | 不可变 | 自动注册 | 运行时校验 |
|---|---|---|---|
| Data 实例 | ✅ `frozen=True, extra="forbid"` | 子类 → `DATA_REGISTRY` | 必须至少一个 `Key` 字段 |
| `@node` 函数 | — | → `NODE_REGISTRY` | `async def`,参数+返回必须是 `Data`/`Data \| None` |
| `wire(T).to(c)` | — | → `WIRING_REGISTRY` | `compile_graph()` 启动时检查一致性 |

---

## 2. 五个核心对象

### 2.1 Data —— 不可变数据载体

所有业务数据定义都继承 `app.runtime.Data`(基于 Pydantic v2,`frozen=True, extra="forbid"`)。`app.runtime` 包是框架的公开入口 —— 业务侧的 import 都走它,不进 `app.runtime.*` 子模块。

```python
# app/domain/summary.py
from __future__ import annotations
from typing import Annotated
from app.runtime import Data, Key, Version


class SummaryFragment(Data):
    message_id: Annotated[str, Key]   # 必须至少一个 Key
    chat_id: str
    summary: str
    created_at: int
    version: Annotated[int, Version] = 0   # ← 想要 append-only 多版本必须显式标 Version
```

**两种模式**:

**Native mode(默认)** —— runtime 自动为你建表（`data_summary_fragment` —— 表名是 `data_<to_snake(ClassName)>`，PascalCase 会拆词），表结构 append-only，写入自动算 `dedup_hash`。无需写 `class Meta`。

**关于 `version` 列**：**不是 native mode 自动列**，必须在 Data 上显式声明 `Annotated[int, Version]` 字段才生效（见 `app/runtime/persist.py::insert_append` 的 `ver_col = version_field(cls)` 分支）。声明了 Version 后 runtime 才会按 Key 自增版本 + 建 `ix_<table>_key_ver` 索引；没声明就只是普通 append（`dedup_hash` 唯一约束兜底）。

**关于"自动写表"**：runtime 只在存在 `wire(D).as_latest()` 时才把 emit 的 Data 落表（详见 §2.3 / §3 Cookbook），不是有 Data class 就自动建表 + 自动写入。

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
        # 类方法把 ORM 行映射成 Data 字段；纯字段映射，不碰 DB / MQ。
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

1. 校验 `fn` 是 `async def`(`inspect.iscoroutinefunction`)否则 import 时 `TypeError`。
2. 读类型注解,校验所有参数 + 返回都是 `Data` 子类或 `Data | None`;否则 import 时 `TypeError`。
3. 校验返回的 Data **不是** `AdminOnly`。
4. 把函数包装成 wrapper:`return result` 时若 `isinstance(result, Data)`,自动 `await emit(result)` 把 Data 推进图;`None` 跳过。wrapper 仍会把 result 原样返回给调用者,方便单元测试直接 assert。
5. 注册到 `NODE_REGISTRY` + `_NODE_META`(供 `inputs_of(fn)` / `output_of(fn)` 反射使用)。

**签名约束**:

| 允许 | 不允许 |
|---|---|
| `async def f(x: Message) -> Fragment` | `def f(x: Message)` —— 必须 async,装饰时直接 `TypeError` |
| `async def f(x: Message) -> Fragment \| None` | `def f(x) -> Fragment` —— 参数/返回必须带注解 |
| `async def f(x: M, y: User) -> F` —— 多输入 | `async def f(x: str)` —— 非 Data 参数 |
|  | `async def f(x: Message) -> (Fragment, Log)` —— 不能返回 tuple |

**注意**:单输出 `@node` 直接 `return Data`，wrapper 会自动 emit。fan-out / streaming segment / 循环产出多个 Data 这种多输出场景允许在 node 内 `await emit(...)`，但不要再 `return` 同一个 Data 造成重复 emit。任何场景都不要手写 `mq.publish(...)`。见「常见坑 #1」。

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
| `.to(*targets)` | 消费者(@node 或 SinkSpec) | 必填，可多个(fan-out)。**例外**：state-only Data（只想被 `with_latest()` / `query()` 读取，不需要触发任何 consumer）可以只声明 `wire(D).as_latest()` 不带 `.to(...)`，runtime 单独 persist 不分派 |
| `.from_(*sources)` | 入口 Source | 外部触发才需要(MQ / cron / HTTP) |
| `.durable()` | 跨进程:RabbitMQ + `runtime_inflight` dedup + lease 续约 | 跨 Deployment、要重启续跑、失败需可回放时 |
| `.retry(n=, backoff=, base_delay_ms=, max_delay_ms=, lease_ms=)` | 边级重试（指数/线性 backoff，n 是含首次的总尝试数）；耗尽后按 `.on_error()` 决定终态 | 想自动重试瞬时失败前再进 DLQ |
| `.on_error("dlq" \| "ignore-duplicate" \| "manual-review")` | consumer 抛异常的终态分发；默认 `"dlq"`，详见 §2.7 | 想让特定异常类型走 DLQ 之外的路径 |
| `.as_latest()` | `emit()` 持久化新版本(append + version),下游 `with_latest` / `query()` 读最新 | Data 是"状态快照",需要被后续节点引用 |
| `.when(predicate)` | 谓词过滤 | Data 到了但某些场景不想触发 |
| `.debounce(seconds=, max_buffer=, key_by=...)` | 防抖合流 | 已实现；适合 drift / afterthought 这类"同一 key 短时间多次触发只跑最后一次" |
| `.with_latest(*types)` | 自动 join 最新的 `T`(按同名 Key) | consumer 需要同一上下文的另一种 Data。⚠️ **只在 in-process 边支持**:`.durable().with_latest(...)` 会被 `compile_graph()` 拒绝(durable handler 是单参分派,不会注入 latest 参数) |

> **fan-out 默认是 broadcast 语义**：`wire(T).to(a, b).durable()` 让每个 consumer 在 RabbitMQ 上各自一个独立队列(`durable_<data>_<consumer>`)，各自 dedup、各自 ack，互不影响。无须显式声明。

> **不支持的组合会启动报错**：runtime 故意把"surface 暴露但引擎未接入/未定义"的组合做成 `GraphError`，避免静默 noop。典型例子：`.durable().with_latest(...)`、`.debounce().durable()`、`.debounce().to(Sink.xxx)`。

**默认边 vs durable 边**:

```
默认(同进程):   emit Data ──直接 await consumer(Data)──▶ 异常向上传
durable(跨进程): emit Data ──RabbitMQ 发到 consumer 所在 App──▶ consumer 侧 dedup + ack
```

经验规则:
- 同一个 Deployment 内部的节点之间 → 默认边(省一次 MQ 跳转)。
- 跨 Deployment(比如 vectorize-worker → chat-response-worker) → `.durable()`。
- 做状态机、事件源、要回放、失败需进 DLQ 由运维 replay → `.durable()`。

> **重试 + 失败终态（Phase 7a/7b 已上线）**：边级重试用 `.retry(...)`，framework 用 RabbitMQ delayed-message exchange 自己 republish，consumer 侧 `runtime_inflight` 状态机做 dedup + lease 续约。重试耗尽后按 `.on_error(...)` 决定走 DLQ / ignore / manual-review（详见 §2.7）。DLQ 不再是黑洞 —— `make dlq-replay` 一行重放，详见 `docs/runbooks/dlq-replay.md`。
>
> 业务 node **仍然不允许**用 `try/except + sleep` 自实现边级 retry。capability 内部的 provider retry（LLM/HTTP client）依然是例外。

### 2.4 `Source` —— 图的入口

```python
from app.runtime import Source

Source.mq("vectorize")               # 消费外部 publisher 的 MQ queue
Source.cron("*/5 * * * *")           # crontab 表达式(分钟级)
Source.interval(seconds=10)          # 秒级定时
Source.http("/api/trigger")          # HTTP endpoint(Runtime 自动注册 FastAPI)
```

> **不在这里**：飞书 webhook 在 lark-proxy(TS) 收，转给 lark-server publish 到 MQ；agent-service 这一侧的入口是 `Source.mq("chat_request")` / `Source.mq("vectorize")` / `Source.mq("memory_fragment_vectorize")` / `Source.mq("memory_abstract_vectorize")` 等（实际队列清单见 `app/infra/rabbitmq.py::ALL_ROUTES`），不是直接收 webhook。运维手工触发(rebuild / afterthought)走 `/ops` 命令调内部 endpoint，写法是 `Source.http("/api/internal/rebuild")` —— 没有专门的 `Source.manual`，因为它跟 http 没有运行时差异。

用法:

```python
wire(MessageRequest).to(hydrate_message).from_(Source.mq("vectorize"))
```

**MQSource 的特殊约定**:
- 目标 @node 必须**单参数**(第一个 Data 就是 decode 目标)。
- runtime 读 MQ body 时会**过滤掉**不在 `req_cls.model_fields` 里的字段(适配老 publisher 带额外字段),所以 Data 保持严格 `extra="forbid"` 不会误伤。
- Queue 名按 lane 自动加后缀:`"vectorize"` 在 df-v0 lane 变成 `"vectorize_df-v0"`。
- **队列归属与 lazy ensure**：`ALL_ROUTES` 里登记的队列（chat_request / vectorize / memory_*_vectorize 等），consumer 启动时如果当前 lane 队列不存在，runtime 会按 Route 的参数 idempotent 声明该 lane 队列（见 `app/runtime/engine.py::_source_loop_mq` 的 lazy ensure 分支），新 lane 部署不再硬性要求"先起 publisher 后起 consumer"。`ALL_ROUTES` 之外的老队列（adoption 测试场景）仍是 passive `get_queue`，要先 declare。
- 如果这个 `Source.mq` 还被 `emit()` 当作跨进程 publish bridge（producer 在 dataflow 图内，consumer 在另一个 app），queue 必须能在 `ALL_ROUTES` 中找到 `Route`，否则 runtime 不知道 routing key。新加 cross-app durable 边时记得在 `ALL_ROUTES` 注册。

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

> `Sink.mq` 已接入 sink dispatch。`compile_graph()` 会校验 queue 是否在 `ALL_ROUTES` 中；未知 queue 启动即失败。

### 2.6 错误策略（`.on_error(...)`）

`from app.runtime import DuplicateData, NeedsReview`。

durable consumer 抛异常时，framework 根据 wire 的 `.on_error(...)` 分发，**业务作者不要 try/except 包整个 body**（包了 framework 看不到，DLQ 永远空，监控失效）。

**`.on_error(...)` 是 wire-level 失败终态策略**，决定整个 wire 上"重试耗尽 / 异常路径"的最终去向。consumer 抛 typed exception（`DuplicateData` / `NeedsReview`）只是匹配上对应 policy 时**立即走快路径**，并不是触发条件 —— 即便 consumer 抛普通 `Exception`，retry 耗尽后也走相同的终态。

| `.on_error(...)` | 立即匹配（快路径） | 普通 `Exception` 路径 | 业务场景 |
|---|---|---|---|
| `"dlq"`（默认） | — | 走 `.retry(...)`；耗尽后进 DLQ + 报警；运维 `make dlq-replay` 重放 | **大多数情况选这个**：瞬时失败 + 真 bug 都能兜住 |
| `"ignore-duplicate"` | `raise DuplicateData(...)` → ack + log warning + 不进 DLQ | 不匹配 → fall through 到 `dlq` 通用路径（retry → DLQ） | consumer 自己的业务表已有相同记录（不是 inflight 的 message-level 去重，是业务语义层面的"我已经做过这件事了"） |
| `"manual-review"` | `raise NeedsReview(...)` → 立即进 review queue + `mark_review` | retry 耗尽（或无 retry policy）后**也进 review queue + mark_review** | 整条 wire 的失败决策都需要人工看 —— LLM tool 路径、需要 KOL 确认的链路 |

**关键澄清（与 Phase 7b 实现一致）**：`"manual-review"` 不是"只有 NeedsReview 才进 review"，而是"这条 wire 的最终失败终态就是 review queue"。逻辑见 `app/runtime/durable.py::_route_consumer_exception` 的 dlq fallback 分支：retry 耗尽时，如果 `wire.on_error == "manual-review"` 就 publish_to_review_queue；否则 raise 进 DLQ。

**注意：这两个 typed exception 是 Phase 7b 新加的 framework surface，目前业务代码还没有实际使用案例（grep `DuplicateData` / `NeedsReview` 在 `apps/agent-service/app/{nodes,agent,chat,life,memory}/` 下零结果）。下面的"何时用"是判断标准 + 假想场景，第一个用上的人记得写 retrospective 验证经验。**

**`ignore-duplicate` vs `dlq`** —— 两个去重层别混：

```
runtime_inflight 表（framework 自动）：同一条 message 多次投递只跑一次（message-level）
DuplicateData（业务作者主动 raise）：两条不同 message，业务语义上是同一件事
```

判断标准：你的 consumer 在 `if` 检查里 `return None` 跳过的那段逻辑，**如果你想让 audit 看到"这条进来了但被业务级判重跳过了"**，就改成 `raise DuplicateData(...)` + wire 配 `.on_error("ignore-duplicate")`。区别只是观测性 —— `ignore-duplicate` 路径会 `mark_succeeded` + log warning，留下"被吞了"的痕迹；`return None` 静默跳过没有任何痕迹。

**`manual-review` vs `dlq`**（这是选 wire-level policy 的判断，不是选异常类型）：

```
DLQ：    整条 wire 的失败默认假设是"代码 bug" → 改代码 + redeploy + make dlq-replay 一把过
review： 整条 wire 的失败默认假设是"每条要单独看" → 运营逐条决策（replay / 丢弃 / 修业务数据再 replay）
```

选 wire policy 的判断标准：**这条 wire 上跑的所有消息**，失败时是更适合"批量 replay"还是"逐条人审"？前者选 `dlq`，后者选 `manual-review`。一旦选了 `manual-review`，无论 consumer 抛什么异常（typed 或普通），retry 耗尽后都进 review queue。

`raise NeedsReview(...)` 只是在已经选了 `manual-review` 的 wire 上**跳过 retry 直接走 review** 的快路径 —— 业务作者觉得这条不值得 retry（比如 LLM 提取的 payload 字段内在不合法，再 retry 几次也是同样错），用 NeedsReview 立即送审。

**假想场景**（业务还没真的这么写）：`run_glimpse_node` 这种 LLM 重活如果改 wire 配 `.on_error("manual-review")`，那么 `_run_glimpse` 内部失败（包括 LLM API 报错 retry 耗尽、payload 解析失败等等）都进 review queue，让运营看 last_error 决定。如果某次 consumer 内部检查发现 LLM 输出明显坏掉，还可以直接 `raise NeedsReview(f"glimpse output malformed: {raw!r}")` 跳过 retry。

**确认契约**：

- helper `_route_consumer_exception`（`app/runtime/durable.py`）根据 `.on_error(...)` 分发；business code 永远不直接读 framework 的 ack/nack/process —— 该 raise 就 raise。
- review queue **没有 consumer**，是 inspect-only 终态；要从 review queue 处理消息走 `make dlq-replay QUEUE=<...>_review KIND=review`。
- `DuplicateData` / `NeedsReview` 在 `.on_error` 不匹配时（比如配的是 `"dlq"` 但 consumer raise 了 `NeedsReview`），fall through 到通用 Exception 路径 —— 等于退化为 DLQ。**别依靠这条 fallback**，配置写明显比兜底更可读。

---

## 3. Cookbook:加一个新 @node

场景:**读每条 Message,生成一句话摘要,存进 pg 的 `data_summary_fragment` 表**(新建 Data,native mode)。

### Step 1 — 建 Data

```python
# app/domain/summary.py
from __future__ import annotations
from typing import Annotated
from app.runtime import Data, Key, Version


class SummaryFragment(Data):
    message_id: Annotated[str, Key]
    chat_id: str
    summary: str
    created_at: int
    version: Annotated[int, Version] = 0   # 想要 append-only 多版本必须显式标 Version（详见 §2.1）
```

没有 `class Meta` → native mode → migrator 自动建 `data_summary_fragment` 表（表名 `data_<to_snake(ClassName)>`；含 `dedup_hash`、`version`、以上五个字段）。**但只有在下面 Step 3 声明了 `wire(SummaryFragment).as_latest()` 后，emit 时 runtime 才真的写表** —— Data class 自身不触发持久化，必须有 wire 声明 `.as_latest()`（详见 §2.3 表格 + `app/runtime/emit.py::emit`）。

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

wire(Message).to(summarize).durable().on_error("dlq")  # 和 vectorize 共享 Message durable，fan-out 两条独立消费
wire(SummaryFragment).as_latest()                       # ← 不能省。没这行 emit 走完不写 data_summary_fragment 表。
```

**两条 wire 各管一件事**：

- 第一条 `wire(Message).to(summarize)` — 触发 consumer。`Message` 已经有一条 `.to(vectorize)`，再加 `.to(summarize)` 就是 fan-out（两条独立队列、独立 dedup、独立 DLQ；任一边 handler 失败只影响自己）。`.on_error("dlq")` 是默认值，写出来自描述（详见 §2.6）。
- 第二条 `wire(SummaryFragment).as_latest()` — 触发持久化。**state-only 的 Data 必须显式声明 `.as_latest()`**，否则 emit 时 runtime 不会写表（runtime 不会因为有 Data class 就自动建表 + 自动写入；落表条件见 `app/runtime/emit.py::emit` —— "any wire(cls).as_latest()"）。下游能用 `with_latest(SummaryFragment)` / `query(SummaryFragment).where(...).all()` 读最新版本。

### Step 4 — Deployment placement（仅框架维护者）

```python
# app/deployment.py 追加:
from app.nodes.summarize import summarize

bind(summarize).to_app("vectorize-worker")  # 和 vectorize/save_fragment 同 pod
```

普通业务作者只定义 Data / node / wire；placement 由 graph/framework owner 按既有域策略维护。不绑定 = 默认 `agent-service`(HTTP 主服务)。绑到 `vectorize-worker` 意味着在 worker pod 里跑,主 HTTP pod 不启动此 node。只有新增跨 Deployment 执行域或调整 worker 拓扑时才改 `app/deployment.py`。

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
/ops-db @chiwei "SELECT * FROM data_summary_fragment ORDER BY version DESC LIMIT 5"
```

---

---

## 3-bis. Cookbook B：写 mutation function（写业务表 + 触发 EventData）

§3 Cookbook A 教的是 **pure transform**：输入 Data → 输出 Data，runtime 自动持久化（`data_<X>` 表）。

但项目里很多业务**不是** pure transform，而是要：

1. **打开业务事务**写自己的业务表（不是 Data 自动持久化的表，而是已存在的 ORM 表，比如 `notes` / `schedule_revisions` / `abstract_memories`）
2. 同时**触发一个 EventData**让下游知道发生了什么

这种代码**形态不是 @node，是普通 async function**，被以下三种入口之一调用：

| 入口 | 代码位置 | 例子 |
|---|---|---|
| LLM tool（被 agent 调用） | `apps/agent-service/app/agent/tools/` | `commit_abstract_memory` / `write_note` / `update_schedule` |
| @node 内部委托 | `app/nodes/*.py` 里 `@node` 调底层 mutation | `afterthought_check`(@node) → `_generate_fragment`(mutation) |
| 主动触发（cron / chat loop） | `app/life/*.py` | `submit_proactive_chat` / `commit_life_state_impl` |

**所有这种 mutation function 必须用 `transactional_emit(s)` 在事务内 append**，不能 commit 后再 `await emit(...)`。

### 旧错误模式 vs 新正确模式

```python
# ❌ 老模式：commit-then-emit
async def _commit_abstract_impl(persona_id, text, fact_ids):
    async with get_session() as s:
        abstract_id = await insert_abstract_memory(s, persona_id, text)
        await insert_memory_edge(s, abstract_id, fact_ids)
    # session 已 commit
    await emit(AbstractMemoryCommitted(abstract_id=abstract_id))
    # ↑ broker 挂 / 网络抖一下，emit 失败但业务表已 commit
    #   → 下游永远不知道，数据不一致就此埋下
```

历史上这种代码会再补一个 `try/except: logger.warning("emit failed; row committed; downstream may be delayed")` 来"兜" —— 那是用日志掩盖丢消息。Phase 7b 之前 `update_schedule.py` 和 `life/tool.py` 两处真有这种代码，已经在迁移中删除。

```python
# ✅ 新模式：transactional_emit
from app.runtime import transactional_emit

async def _commit_abstract_impl(persona_id, text, fact_ids):
    async with get_session() as s:
        abstract_id = await insert_abstract_memory(s, persona_id, text)
        await insert_memory_edge(s, abstract_id, fact_ids)
        async with transactional_emit(s) as emitter:
            await emitter.append(AbstractMemoryCommitted(abstract_id=abstract_id))
    # session.__aexit__ commit：业务表 + outbox 行同事务可见
    # 后台 dispatcher 拾起 outbox 行 → emit(data) → wire fan-out
    # broker 挂了 outbox 行就在那躺着，broker 恢复 dispatcher 自己重试
```

### 多个 emit 共用一个 emitter

同事务里 emit 多个 EventData，复用同一个 `emitter`（不要嵌套 `transactional_emit`）：

```python
async def submit_proactive_chat(persona_id, chat_id, content):
    async with get_session() as session:
        msg = await insert_proactive_message(session, persona_id, chat_id, content)
        async with transactional_emit(session) as emitter:
            await emitter.append(Message.from_cm(msg))
            await emitter.append(ChatTrigger(chat_id=chat_id))
    # 一次 commit，两条 outbox 行都进
```

参见 `app/life/proactive.py` 真实代码。

### 决策树：要不要用 `transactional_emit`？

判断标准是**谁持有 session**，不是写代码的形态：

```
你的函数 / @node body 里有 async with get_session() as s 写业务表吗？
 ├─ 没有，纯 Data → Data（pure transform） → 不用 transactional_emit。Cookbook A 路径。
 ├─ 有 → 写完业务表后要触发 EventData 吗？
 │    ├─ 要 → 在 session 块内 async with transactional_emit(s) as emitter: ...
 │    └─ 不要 → 不用 transactional_emit
```

**关键**：@node 也可以持有 session 写业务表（或委托 mutation function 持有）。这种 @node 内部写或者它调用的 mutation function **必须**用 `transactional_emit`。

真实例子：`@node afterthought_check`（`app/nodes/memory_pipelines.py:283`）调用 `_generate_fragment`，后者持有 session 写 `fragments` 表 + `transactional_emit` append `MemoryFragmentRequest`（`memory_pipelines.py:203`）—— 完全合法且推荐。

**反例**：在 pure transform @node 里硬给 `return Data` 配一个 `transactional_emit` 是过度复杂（runtime 已经接管了 Data 持久化，业务不需要再管事务）。

### 测试

`transactional_emit(s)` 是 contextmanager，单测里 mock 它要用 `asynccontextmanager` stub + 用 `captured` list 验证 `append` 行为。具体范式见 `tests/unit/agent/tools/test_commit_abstract.py` 现成例子。

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
    AgentConfig(
        prompt_id="my_prompt",       # Langfuse prompt id（不是 inline system prompt）
        model_id="gpt-5.4",
        trace_name="my_agent",        # 可选；用作 Langfuse trace name
    ),
    tools=[...],
)

result = await runner.run(messages=[{"role": "user", "content": "hi"}])
async for chunk in runner.stream(messages=[...]):
    ...
structured = await runner.extract(MyPydanticModel, messages=[...])
```

> **prompt 来自 Langfuse**，不是 inline string。`prompt_id` 在 Langfuse 平台上注册并管理（含变量、版本、A/B test）；代码侧只引用 id。改 prompt 走 Langfuse skill，不是改代码。

---

## 5. 常见坑(AI 最容易错的地方)

### #1 重复 emit / 手写 mq.publish

单输出 @node 已经被 wrapper 包了,返回 Data 就自动 emit。**不要再手写同一个 Data**。多输出场景可以 `await emit(...)` 多次，但函数应 `return None` 或只返回测试需要的非重复结果。

```python
# ❌
@node
async def summarize(msg: Message) -> None:
    frag = SummaryFragment(...)
    await emit(frag)
    return frag                  # 重复 emit,下游会收到两次
    await mq.publish(VECTORIZE, ...)   # 直接捅 infra,绕过图

# ✅
@node
async def summarize(msg: Message) -> SummaryFragment:
    return SummaryFragment(...)  # wrapper 自动 emit

# ✅ 多输出/fan-out
@node
async def fan_out(req: BatchRequest) -> None:
    for item in req.items:
        await emit(ItemRequest(item_id=item.id))
    return None
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

从 ORM 行 lift 到 Data 用 `Data.from_cm(...)` 这类纯字段映射类方法（见 §2.1 Adoption mode 例子），不直接读/写 infra；写业务表 + 触发 Data 走 `transactional_emit`（见 §3-bis）。

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

Data 表是 append-only。想要"更新"语义，必须 **(a) Data 显式声明 Version + (b) wire 声明 `.as_latest()`** 两步都有，runtime 才会按 Key 追加新版本；只声明其中一项不会工作（详见 §2.1 + §2.3）。

```python
# ❌ 直接 UPDATE Data 表
await session.execute("UPDATE data_user_profile SET name='x' WHERE user_id='u1'")

# ❌ 只 emit 不声明 Version + .as_latest()
class UserProfile(Data):                       # 没标 Version
    user_id: Annotated[str, Key]
    name: str
await emit(UserProfile(user_id="u1", name="x"))
# ↑ 第二次 emit 同 user_id 会撞 dedup_hash UNIQUE 约束（一个 Key 一行），不是"更新成新版本"

# ✅ 完整三件套：Version 字段 + .as_latest() wire + emit
from app.runtime import Data, Key, Version, wire

class UserProfile(Data):
    user_id: Annotated[str, Key]
    name: str
    version: Annotated[int, Version] = 0   # ← 必须显式标 Version 才有自增

# wiring 文件:
wire(UserProfile).as_latest()              # ← state-only：runtime 持久化，按 Key 追加版本

# 业务代码:
await emit(UserProfile(user_id="u1", name="x"))      # runtime 写 version=1
await emit(UserProfile(user_id="u1", name="x2"))     # runtime 写 version=2

# 读取最新版本（query 默认 latest-per-key）：
from app.runtime import query
rows = await query(UserProfile).where(user_id="u1").all()
row = rows[0] if rows else None  # name='x2' version=2
```

**只有 Versioned Data 能用 emit 追加新版本**。无 Version 字段的 Data 在 dataflow 里语义是"事件"或"transient"，不是"可更新的状态"；要 update 状态请用专门的 ORM 表 + `transactional_emit` 触发 EventData（详见 §3-bis Cookbook B）。

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

改造一个老功能,**写路径只能有一条**。从 ORM 行 lift 成 Data 用 `Data.from_cm(...)` 类方法（纯字段映射）+ `transactional_emit`，把老队列 publish 一次性切掉。

```python
# ❌ 双写
async def write_message(cm):
    async with get_session() as s:
        s.add(cm)
        async with transactional_emit(s) as emitter:
            await emitter.append(Message.from_cm(cm))
    await mq.publish(VECTORIZE, {...})   # 老队列也 publish 了一份 → 下游会被触发两次

# ✅ 单一路径，老 publisher 下游已切到 runtime
async def write_message(cm):
    async with get_session() as s:
        s.add(cm)
        async with transactional_emit(s) as emitter:
            await emitter.append(Message.from_cm(cm))
```

### #12 mutation function 在 `async with get_session()` 块外 emit

写业务表后想触发 EventData，**必须**在 session 块**内** `transactional_emit` 而不是块**外** `await emit(...)`。

```python
# ❌
async with get_session() as s:
    await insert_note(s, ...)
await emit(NoteCreated(...))   # broker 挂这一步，DB 已 commit，下游永远收不到

# ✅
async with get_session() as s:
    await insert_note(s, ...)
    async with transactional_emit(s) as emitter:
        await emitter.append(NoteCreated(...))
```

详见 §3-bis。CI grep gate `Gap 8` 卡 `await emit(` 在业务区的总数（当前 14：12 个 category-B 合法 + 2 个 docstring）；新加 mutation function 必须用 `transactional_emit`，否则 CI 红。

### #13 durable consumer 内 try/except 包整个 body

包了 framework 看不到异常 → 永远 ack → DLQ 永远空 → 监控失效。该 raise 就 raise，配 `wire(...).on_error("...")` 决定终态（详见 §2.7）。

```python
# ❌ Phase 7b 之前 update_schedule.py / life/tool.py 的真实代码
try:
    await emit(SomethingHappened(...))
except Exception:
    logger.exception("emit failed; row committed; downstream may be delayed")
# 用日志掩盖丢消息

# ✅ transactional_emit 把 emit 失败的可能性消除了；该抛就抛
async with transactional_emit(s) as emitter:
    await emitter.append(SomethingHappened(...))
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

不列每个 @node 的逐字段，按 pipeline 维度列**当前 topology**，让你一眼判断"我的改动落在哪一片"。每片下面给 wiring 文件 + 入口 Source。详细 @node 名 / Data 名读对应 wiring 文件即可（每个文件 < 80 行）。

### 当前 topology（按 pipeline）

| Pipeline | 入口 Source | wiring 文件 | 主要 @node | Deployment |
|---|---|---|---|---|
| **message → vectorize** | `Source.mq("vectorize")` | `wiring/memory.py` | hydrate_message → vectorize → save_fragment | vectorize-worker |
| **chat 主链路** | `Source.mq("chat_request")` | `wiring/chat.py` | route_chat_node → chat_node (durable) → ChatResponseSegment Sink ← lark-server | agent-service |
| **safety check** | 由 chat_node 内部 emit | `wiring/safety.py` | run_pre_safety / run_post_safety (durable) → Recall Sink | agent-service |
| **life cron 循环** | `Source.cron("* * * * *")` 等 | `wiring/life_dataflow.py` | MinuteTick / LightDayTick / GlimpseTick / HeavyReviewTick / DailyPlanTick → 各 fan_out → 业务 node | agent-service |
| **memory debounce** | 由 chat 流程 emit | `wiring/memory_triggers.py` | DriftTrigger / AfterthoughtTrigger（`.debounce()` 合流） → drift_check / afterthought_check | agent-service |
| **memory fragment/abstract vectorize** | `Source.mq("memory_fragment_vectorize")` / `Source.mq("memory_abstract_vectorize")` | `wiring/memory_vectorize.py` | vectorize_memory_fragment / vectorize_memory_abstract | vectorize-worker |
| **agent tool events** | tool 调用触发 (transactional_emit) | `wiring/agent_tool_events.py` | AbstractMemoryCommitted → on_abstract_committed → MemoryAbstractRequest（再走 vectorize 队列） | agent-service |
| **admin HTTP（运维触发）** | `Source.http("/admin/trigger-*")` | `wiring/admin.py` | trigger-life-engine-tick / trigger-glimpse / debug-glimpse / trigger-voice / trigger-schedule + Phase 7b 的 `/admin/dlq/*` | agent-service |

要看具体 wire / @node，去对应 `wiring/<file>.py` —— 每个文件就 30~80 行，wire 声明顺序 = 数据流顺序，读起来最快。

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
| wire DSL 全部方法（含 `.retry` / `.on_error`） | `app/runtime/wire.py::WireBuilder` |
| 所有 Source 种类 | `app/runtime/source.py::Source` |
| emit 分派逻辑 | `app/runtime/emit.py::emit` |
| durable 边实现 + 错误路由 helper | `app/runtime/durable.py::_route_consumer_exception` |
| MQSource 消费循环 | `app/runtime/engine.py::_source_loop_mq` |
| Node → App 绑定 | `app/runtime/placement.py::bind` |
| Capability 清单 | `app/capabilities/` |
| `transactional_emit` / `OutboxEmitter` | `app/runtime/outbox.py` |
| Outbox 后台调度循环 | `app/runtime/outbox_dispatcher.py::dispatcher_loop` |
| `DuplicateData` / `NeedsReview` | `app/runtime/errors.py` |
| Inflight 状态机（claim / mark_review / delete_inflight） | `app/runtime/inflight.py` |
| Manual-review queue 声明 + publish | `app/runtime/review_queue.py` |
| DLQ 运维操作 runbook | `docs/runbooks/dlq-replay.md` |

---

## 附录:核心不变量(违反会炸)

运行时会在启动 / import / 调用时强制的契约,遇到 `TypeError`/`RuntimeError` 先回来对这份清单:

1. 每个 `Data` 子类至少一个 `Key` 字段(`__pydantic_init_subclass__`)。
2. Adoption mode 的 Data **不能**带 `DedupKey` / `Version`(`__pydantic_init_subclass__`)。
3. `@node` 必须 `async def`、参数 + 返回必须是 `Data / Data | None / None`(`node()` 装饰时)。
4. `@node` 不能返回 `AdminOnly` Data(`node()` 装饰时)。
5. 一个 @node 只能绑一个 App(`placement.bind` 重复绑定 raises)。
6. durable 边的 consumer 必须**单 Data 参数**(MQSource 契约,`_source_loop_mq` 检查)。
7. Runtime 启动时 `compile_graph()` 会检查 wire 一致性(生产者 Data 类型 ↔ 消费者签名)—— 启动报错看这里。
8. `emit(data)` 不会匹配任何 wire 时静默 no-op(不是错),测试里的 wiring 清空是利用这一点。
9. **mutation function（普通 async function 持有 session 写业务表 + 触发 EventData）必须用 `transactional_emit(s)` 在 session 块内 append**，不能在块外 `await emit(...)`。CI grep gate `Gap 8` 卡业务区 `await emit(` 计数。
10. **durable consumer 内禁止 try/except 包整个 body** —— 让 framework 看到异常它才能按 `.on_error(...)` 分发。该 raise 就 raise。
