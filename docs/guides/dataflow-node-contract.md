# Dataflow Node Contract（A0）

本文件是 agent-service dataflow framework 的**契约**：写一个新节点 / 一条新 wire 必须遵守哪些规则，错误怎么分级到 runtime / wire / node / tool / capability 五层各自的职责，每条规则**必须**对应一条运行时断言或 compile-time 检查。

这不是 tutorial（业务上手版见 `dataflow-framework-overview.md` + `dataflow-framework.md`），是契约清单——B 阶段所有 framework 改造、所有自动 emit 行为、所有错误处理路径必须和本文一致。**违反本文契约的代码无论在哪一层，必须在最早的可检测时机硬抛出来**（compile-time > startup > 首次 emit），禁止用静默兜底掩盖。

本文里的"**当前契约**"指代码已经实现且本契约要求保留的事实；"**未来目标**"指本次重构（Phase A/B/C）落地后才生效的契约，未落地前**禁止**下游基于此实现。

## 1. @node 装饰器契约

业务写 `@node async def foo(arg: SomeData) -> SomeOtherData | None`。装饰器在 import 时反射 + 注册（`runtime/node.py:51-103`），违反契约直接 raise TypeError，import 失败。

| 编号 | 规则 | 状态 | 断言位置 |
| --- | --- | --- | --- |
| N1 | 函数必须 `async def` | 当前契约 | `node.py:52-56` |
| N2 | 每个参数必须有类型标注 | 当前契约 | `node.py:67-71` |
| N3 | 每个参数类型必须是 `Data` 子类 | 当前契约 | `node.py:73-77` |
| N4 | 返回值必须是 `Data` / `Data \| None` / `None` | 当前契约 | `node.py:79-86` |
| N5 | 返回值禁止是 `AdminOnly` 标记的 Data | 当前契约 | `node.py:87-90` |
| N6 | 返回 `Data` 实例自动 `await emit(...)` 到下游 | 当前契约 | `node.py:92-99`（commit 0ea263b） |
| N7 | 返回 `None` 跳过 emit | 当前契约 | `node.py:95` isinstance 检查不命中 |
| N8 | 多输出 / fan-out / per-chunk：允许手动 `await emit(...)`，但完成 plan 改造 9 / 改造 11 后必须改用 `emit_and_wait` / `.fan_out_per()` 声明式 | 未来目标 | 无断言，**手动 emit 全量 review 已挂 backlog L3**，跟改造 9/11 一起做 |

**N6 自动 emit 边界**：wrapper emit 完仍 return result（让单测能 assert）。**禁止**写法是手动 `await emit(returned_data)` 后再 return 同一个 Data——产生重复 emit。

**禁止节点级 on_error**：错误策略一律在 wire 边级声明（见 §3.3 W13）。`@node` 装饰器不接 `on_error` 参数；任何"节点级 on_error"的代码必须改成 wire 级。理由：同一个 @node 被多条 wire 引用时，节点级 policy 会和边级冲突——以边为最小决策单元更干净。

## 2. emit 三件套使用边界

| API | 定义位置 | 何时用 | 何时禁用 |
| --- | --- | --- | --- |
| `await emit(data)` | `runtime/emit.py:55` | 业务节点 fan-out 多个 Data；非 @node 上下文产生 Data 进图（cron / source 桥接 / 测试 fixture） | 禁止在 `async with tx():` 内调用——消息发完事务可能 rollback，broker 没法回收。同事务下用 `emit_tx`。 |
| `await emit_tx(data)` | `runtime/db.py:70` | 业务节点在 `async with tx():` 块内，写业务表 + 发消息要原子一致 | 禁止脱离 tx 上下文调用，`db.py:78` 检查到非 tx 上下文直接 `RuntimeError`。 |
| `with transactional_emit(session) as emitter` | `runtime/outbox.py:87` | 业务函数已经在自己的 SQLAlchemy session 上工作（不走 `tx()`），需要在同一个 session/事务内挂 outbox 行 | dispatcher 拾起 outbox 行后 publish；**禁止** commit session 后再 `await emit(...)`——broker 一挂消息就丢。 |

**B8（自动 emit）+ 改造 9/11（手动 emit review）完成后**，三件套主要场景退化为：跨服务事务用 `emit_tx`、业务自有 session 用 `transactional_emit`、装饰器自动 emit 的 happy path 用 N6。直接调 `emit()` 的位置应当只剩 cron/source 桥接和测试 fixture。

## 3. wire DSL 边级装饰器组合规则

每条 wire 是 `wire(DataT).to(consumer).durable()...` 链。规则全部在 `runtime/wire.py` 的 builder 校验 + `runtime/graph.py` 的 `compile_graph()` 校验。**启动失败优于运行时坏**——所有契约违反 boot 时 `GraphError`。

### 3.1 consumer / 数据流约束

| 编号 | 规则 | 状态 | 断言位置 |
| --- | --- | --- | --- |
| W1 | wire 的所有 consumer 必须 `@node` 注册过 | 当前 | `graph.py:40-46` |
| W2 | `.with_latest(X)` 要求 graph 中存在 `wire(X).as_latest()` | 当前 | `graph.py:48-56` |
| W2a | `.with_latest(X)`：`X` 必须有至少一个 Key 字段，且 emit 的 primary data 类必须有同名属性 | **未来目标（补 compile-time）** | **缺失断言 W2a**，加到 `graph.py` |
| W3 | consumer 函数签名的参数类型集合 == `{wire.data_type} ∪ wire.with_latest` 严格相等 | 当前 | `graph.py:66-91` |
| W4 | wire 的所有 consumer 必须 placement 到同一个 app（不能跨 app fan-out） | 当前 | `graph.py:127-142` |
| W4a | wire 的 consumer 跨 app（与 emit 进程不同）时必须有 transport：`.durable()` 或 `Source.mq` 至少一种。**emit 触发时**（runtime）检查，不在 compile-time（compile_graph 是无状态的、看不到 emit 触发方 app）| 当前契约 | `emit.py:108-117`（A0 阶段补：把原 100-107 静默 skip 改为 `raise RuntimeError`） |

### 3.2 Source / Sink 约束

| 编号 | 规则 | 状态 | 断言位置 |
| --- | --- | --- | --- |
| W5 | `Source.mq(queue)` 的 wire 必须有且仅有一个 consumer | 当前 | `graph.py:99-109` |
| W6 | `Source.mq` 的 consumer 必须只接一个 Data 参数 | 当前 | `graph.py:110-117` |
| W7 | `Source.http(...)` 的 consumer 必须 bind 到 `DEFAULT_APP`（agent-service main 进程） | 当前 | `graph.py:331-346` |
| W8 | `Sink.mq(queue)` 的 queue 必须在 `ALL_ROUTES` 注册 | 当前 | `graph.py:300-322` |

### 3.3 durable / retry / on_error 组合

| 编号 | 规则 | 状态 | 断言位置 |
| --- | --- | --- | --- |
| W9 | `.retry(...)` 必须配 `.durable()` | 当前 | `wire.py:144-145` |
| W10 | `.retry(n=...)`：`n >= 1`，`backoff ∈ {'exponential', 'linear'}`，`lease_ms >= 1` | 当前 | `wire.py:146-153` |
| W11 | `.durable()` 不能跟 `.with_latest(...)` 组合 | 当前 | `graph.py:261-274` |
| W12 | `.durable()` 要求 data type 不是 `Meta.transient = True` | 当前 | `graph.py:286-298` |
| W13 | `.on_error(policy)`：`policy ∈ {'dlq', 'ignore-duplicate', 'manual-review', 'swallow_and_log'}` | 当前 4 种（B4 已落地 `swallow_and_log`） | `wire.py:VALID_ON_ERROR + on_error()`；`durable.py:_route_consumer_exception`（typed-match → swallow → generic retry/DLQ 三段优先级） |
| W14 | `.on_error(...) != 'dlq'` 时 wire 必须 `.durable()`——in-process 边异常直接 propagate，on_error 仅对 durable 边有意义 | **未来目标（补 startup）** | **缺失断言 W14**，加到 `graph.py` |

### 3.4 debounce 组合

W15-W18 见原代码（`graph.py:144-248` 完整覆盖），当前契约，状态全部为已实现。略表展开。

### 3.5 emit_delayed / emit_at 契约

W19-W22 见 `emit.py:208-275`，当前契约。略表展开。

### 3.6 `.fan_out_per(extractor)` 组合（B7）

声明式 per-key fan-out。emit 时调 extractor 返回 `list[dict]`，对每个 dict 做 `data.model_copy(update=item)` 触发 consumer。

| ID | 规则 | 状态 | 实现 |
|----|------|------|------|
| W23 | `.fan_out_per(extractor)` 的 `extractor` 必须 callable，sync / async 均可，需返回 `list[dict]`（或 awaitable→list[dict]）| 当前 | `wire.py:WireBuilder.fan_out_per`（callable 校验）+ `emit.py:_dispatch_fan_out`（await/同步分支）|
| W24 | per-key consumer 调用**强制隔离**：用 `asyncio.gather(return_exceptions=True)` 跑，一个 key 抛异常**不阻断**其他 key | 当前 | `emit.py:_dispatch_fan_out` |
| W25 | extractor 抛异常 fail-soft：log warning + return（一拍丢，下一拍自然恢复）| 当前 | `emit.py:_dispatch_fan_out` |
| W26 | `.fan_out_per()` 不能跟 `.durable()` / `.debounce()` / `.with_latest(...)` 组合 | 当前 | `graph.py` block 4f |

## 4. 错误分类边界（**A0 核心**）

谁负责把异常分到哪一档。**不允许任何一层越界 catch**。

### 4.1 runtime 层：Source loop

`engine.py` 的 source loop 有四种异常源，**当前各自路径明确，统一收口为契约**：

| 异常源 | 处理 | 状态 | 位置 |
| --- | --- | --- | --- |
| Source loop 内 `CancelledError`（任何 source） | shutdown 信号，安静 unwind 退出 | 当前契约 | `engine.py:385-386, 426-427, 618-619` |
| MQ Source 单条消息 decode 失败（JSONDecodeError / UnicodeDecodeError / ValidationError / TypeError） | log warning + re-raise → `process(requeue=False)` → DLX；外层 catch + log + 继续下一条 | 当前契约 | `engine.py:594-616, 620-630` |
| MQ Source 内 @node target 抛 `Exception` | 同上：`process(requeue=False)` → DLX；外层 catch + log + 继续下一条 | 当前契约 | `engine.py:617, 620-630` |
| Source loop infra 故障（queue 不存在、connection 异常、authentication 失败） | `_record_source_error` → `_stop_event` → watchdog `os._exit(1)` → PaaS 重启 pod | 当前契约 | `engine.py:221-231, 527-536` |
| cron / interval source 内 `emit()` 抛 `Exception` | **当前**：捕获后 `_record_source_error` → 杀 pod。**未来目标**：log + 继续下一个 tick（错过一个 tick 不应是 fatal） | **未来目标**（A2 落地，把 emit() 的异常和 connection 异常分清） | `engine.py:387-389, 428-430` |

**禁止**：Source loop 内任何"silent swallow + continue"——必须 ack/nack 到 broker（消息走 DLX）或杀 pod 让 PaaS 重启（infra 故障），不能两者都不做。

### 4.2 runtime 层：durable handler

`durable.py:_route_consumer_exception` 是 durable wire consumer 异常的总分路由器：

| 异常类型 + 配套 wire policy | 处理 | 状态 | 位置 |
| --- | --- | --- | --- |
| consumer 抛 `DuplicateData` + `on_error='ignore-duplicate'` | `mark_succeeded` + ack | 当前 | `durable.py:270-276` |
| consumer 抛 `NeedsReview` + `on_error='manual-review'` | `publish_to_review_queue` + `mark_review`；publish 失败 fall through DLQ | 当前 | `durable.py:278-294` |
| consumer 抛任何 `Exception` + `.retry()` 未 exhausted | `mark_failed` + `republish` delayed copy | 当前 | `durable.py:296-326` |
| consumer 抛任何 `Exception` + retry exhausted + `on_error='manual-review'` | `publish_to_review_queue`；publish 失败 fall through DLQ | 当前 | `durable.py:328-343` |
| consumer 抛任何 `Exception` + retry exhausted + `on_error='dlq'`（默认） | re-raise → `process(requeue=False)` → DLX | 当前 | `durable.py:345` |
| consumer 抛任何 `Exception` + `on_error='swallow_and_log'` | log warning + `mark_succeeded` + ack（**禁止默认开启**——必须显式声明）。**优先级**：放在 typed-policy match 之后、generic retry/DLQ 之前；DuplicateData/NeedsReview 配套自己的 policy 仍先匹配，typed exception 落到 swallow wire（policy 不匹配）按 generic 处理一并吃掉 | **已落地（B4）** | `durable.py:_route_consumer_exception` 第 2 段 swallow_and_log 分支 |

### 4.3 runtime 层：in-process emit

`emit.py` 直接调 consumer。任何 consumer 抛异常 → propagate 到 `emit()` 调用方 → 中断同条 wire 的后续 fan-out + 后续匹配 wire。**这是 fan-out 边的强一致语义**，业务需要独立隔离时必须用 `.durable()`。

| 异常源 | 处理 | 状态 | 位置 |
| --- | --- | --- | --- |
| in-process consumer 抛 `Exception` | propagate 到 `emit()` 调用方 | 当前契约 | `emit.py` docstring 第 8-11 行 |
| in-process `with_latest` 解析失败（X 不存在 / Key 字段缺失） | `RuntimeError` 抛到 `emit()` 调用方 | 当前 | `emit.py:131-142` |

### 4.4 runtime 层：outbox / dispatcher

| 异常源 | 处理 | 状态 | 位置 |
| --- | --- | --- | --- |
| `outbox_dispatcher` 循环内任何 `Exception` | log + `mark_failed`，循环继续 | 当前契约 | `outbox_dispatcher.py:106` |

### 4.5 wire 层

contract 违反 → `GraphError`，启动失败。运行时无 wire 层异常。

### 4.6 node 层（业务 @node 函数）

**节点抛异常是合法的契约表达**。framework 不要求节点 try-except 兜底。**目标契约**（plan 改造 4 落地）：所有业务节点的 `except Exception as e: logger.error(...); raise` 兜底模式**全部移除**——节点直接抛、wire on_error 决定路径。

三种特殊语义异常 + 一种 swallow policy 对应 W13 四 policy：

| 异常类型 | 配套 wire policy | 语义 |
| --- | --- | --- |
| `DuplicateData` | `ignore-duplicate` | 这条 Data 我已经处理过了 |
| `NeedsReview` | `manual-review` | 我处理不了，要人审 |
| 其他 `Exception` | `dlq`（默认）| 我崩了 |
| 其他 `Exception` | `swallow_and_log` | 知道会偶尔出错且不关心，显式声明吃掉 |

### 4.7 tool 层（agent/tools/）

**当前现状**：`agent/tools/_common.py @tool_error` catch 异常字符串化给 LLM。

**未来目标（A0 在此定型；A3 + C3 落地）**：定 5 个最小 capability 异常类（见 §4.8），tool 层把它们映射到 LLM 可见的结构化 outcome：

| capability 异常类 | tool 层映射 | LLM 可见 outcome 字段 | 是否重试（wire 级） |
| --- | --- | --- | --- |
| `CapabilityInvalidArg`（参数不对） | 转 `ToolInvalidArgs` typed exception，结构化给 LLM | `kind="invalid_args"`, `message`, `param` | 不可重试 |
| `CapabilityNotFound`（资源不存在） | 转 `ToolNotFound`，结构化给 LLM | `kind="not_found"`, `resource_id` | 不可重试 |
| `CapabilityTimeout` | 不给 LLM 看，节点抛 `Exception` → wire on_error | — | 可重试（`.retry(n=2..3)`） |
| `CapabilityRateLimited` | 不给 LLM 看，节点抛 `Exception` → wire on_error | — | 可重试（`.retry(n=3..5)` + 较长 backoff） |
| `CapabilityCallFailed`（底层 5xx / 不明故障） | 不给 LLM 看，节点抛 `Exception` → wire on_error | — | 可重试 |

**关键边界**：业务语义错误（前两类）= LLM 可见 outcome；系统失败 / 可重试错误（后三类）= 节点失败 → wire on_error 决定 DLQ / review / swallow。**禁止**当前 `@tool_error` 那种"所有异常字符串化给 LLM"——会让模型把系统故障当业务结果对待。

### 4.8 capability 层（capabilities/）

**当前现状**：`return False` / `return None` / `except + log + None`，业务节点 catch 后字符串化（plan A3 + C3 改造对象）。

**未来目标（A0 在此定型；A3 + C3 落地）**：5 个 capability 异常类 + 路由表：

| 异常类 | 含义 | 何时抛 |
| --- | --- | --- |
| `CapabilityInvalidArg` | 调用参数不对（schema / 业务 validation 失败） | LLM / 业务传错了；不是 capability 自己的问题 |
| `CapabilityNotFound` | 资源不存在（user / persona / file / record） | 业务语义"查不到"明确成异常，不再 return None 混淆 |
| `CapabilityTimeout` | 上游超时（HTTP / DB / LLM 调用） | 网络 / 服务慢导致的 timeout |
| `CapabilityRateLimited` | 上游限流（429 / quota exceeded） | 显式 429 / rate-limit header / 业务限流 |
| `CapabilityCallFailed` | 上游不明故障（5xx / 协议错误 / connection refused） | 其他 capability 调用失败的兜底类 |

**A3 落地标准**：
- `capabilities/` + `infra/` 内 `return None` / `return False` 仅保留**有明确业务语义**的位置（例如 "缓存未命中 = None"），其余必须抛指定异常。
- 业务节点（`nodes/*.py`）禁止 catch capability 异常字符串化——直接 propagate，让 wire on_error 决定。

**例外（明确允许的"业务语义 None / False"）**：
- 缓存查询未命中
- "查这条记录是否存在" 业务返回 bool
- 配置 lookup "有没有这个 key" 返回 Optional

例外清单要在 A3 落地时和代码 review 一起 freeze——不在清单内的 None/False 都要抛异常。

#### A3 例外清单（2026-05-13 落地，代码已加 docstring 标注 `contract-allowed`）

每条标注 `file:function | reason | follow-up`。后续新增 capability / infra 函数若返回 None/False 必须**先**加进此清单 + 加 docstring，否则禁止合码。

| file:function | 类型 | 理由 | follow-up |
| --- | --- | --- | --- |
| `app/capabilities/banned_words.py:contains` | None | "查不到 banned word" 是业务结果，非失败 | 无（永久例外） |
| `app/capabilities/image_search.py:image_search` | `[]` | 配置未配 → empty 是部署语义 | transport 错误已透传，无需改 |
| `app/capabilities/web_search.py:web_search` | `[]` | 同上 | 同上 |
| `app/capabilities/web_search.py:read_webpage` | `""` | 配置未配 + 服务端 contents 为空 → empty | 同上 |
| `app/infra/qdrant.py:create_collection` | False | 幂等 create，已存在 collection 返回 False；启动时调用方靠此分支判断 | L1：收窄 catch 到 `UnexpectedResponse`-only |
| `app/infra/qdrant.py:create_hybrid_collection` | False | 同上 | 同上 |
| `app/infra/rabbitmq.py:current_lane` | None | "无 lane = prod" 是正常值 | 无（永久例外） |
| `app/infra/rabbitmq.py:publish_with_confirm` | False | 显式的"caller-decision"语义：retry 走 dlq-fallback，emit_delayed 走 raise；docstring 已写明 | 无（永久例外） |
| `app/infra/image.py:_post` | None | 历史遗留：4 个内部 + 3 个外部调用方都靠 `if data:` 分支；外层 `upload_and_register` 等已经 `except Exception` 兜底 | L1：image_client typed-error 迁移（单 commit 改 raise + 逐调用方验证） |
| `app/infra/image.py:download_image_as_base64` | None | 同上：`vectorize.py` 用 `gather(return_exceptions=True)` 过滤 | 同上 |

**非例外**（不在清单上、不允许新增 `return None` / `return False`）：
- 任何 capability / infra 函数捕获 transport 异常（httpx Timeout / 5xx / connection refused / DB connection error / aio_pika 异常）后吞掉
- 任何"上游说找不到资源"的语义——必须 `raise CapabilityNotFound`
- 任何参数 validation 失败——必须 `raise CapabilityInvalidArg`

业务节点（`app/nodes/`、`app/chat/`、`app/life/`、`app/memory/`）现状：grep 未发现"catch capability 异常 → 字符串化进 LLM context"的实例（唯一一处 `str(e)` 在 `app/nodes/admin.py:142`，是 admin API 的 user-facing 响应，不进 LLM）。`@tool_error` 的字符串化是 C3 改造对象（依赖 B4 `swallow_and_log`），本 A3 不动。其余 catch 均为 fail-open / fire-and-forget / config-fallback，属于 B4 节点级 try-except 兜底清零范围，本 A3 不动。

## 5. 契约违反时的行为

无论哪一层违反契约：

1. **import 时可检测**（如 `@node` 反射约束）→ `TypeError`，import 失败。
2. **boot 时可检测**（如 wire DSL 组合规则）→ `GraphError`，进程不启动。
3. **首次 emit 时才能检测**（如 `with_latest` 找不到 row）→ `RuntimeError`，emit() 抛到调用方。
4. **运行时由 framework 兜底**（如 durable handler 异常）→ 按 §4.2 表分路由处理，**禁止静默吞**。

任何"silent swallow + log"必须有显式契约依据（如 §4.4 outbox dispatcher loop 不中断），违反此原则的代码必须改。

## 6. 缺失断言清单（A/B 阶段补齐）

按状态分两类——

### 6.1 必须补的运行时 / 启动断言

| 编号 | 缺失位置 | 描述 | 归属阶段 |
| --- | --- | --- | --- |
| W2a | `graph.py` block 4e（W11 之后）| `with_latest(X)` 的 X 必须有 Key，emit 的 primary data 必须有同名属性 | **A0 已完成**（`graph.py` block 4e） |
| W4a | `emit.py` cross-app 分支 | cross-app wire 必须至少有 `.durable()` / `Source.mq` 一种 transport；runtime 触发时 raise RuntimeError，禁止静默 skip | **A0 已完成**（`emit.py:108-117`） |
| W14 | `graph.py` block 4d | `on_error != 'dlq'` 但 wire 不 `.durable()` → `GraphError` | **A0 已完成**（`graph.py` block 4d） |
| swallow_and_log | `wire.py:VALID_ON_ERROR` 扩 + `durable.py:_route_consumer_exception` 加分支 | 第 4 种 on_error policy | **B4 已落地** |
| cron/interval emit 异常 vs infra 异常分清 | `engine.py:_source_loop_cron / _source_loop_interval` | 当前一律杀 pod，目标：emit() 抛的异常 log+继续；connection 等 infra 异常杀 pod | A2 阶段补 |

### 6.2 不是断言，是 manual review 待办

| 编号 | 描述 | 归属 |
| --- | --- | --- |
| N8 | 手动 `await emit(...)` 全量 review，三选一处理（保留合理 fan-out / 改 wrapper 自动 emit / 改 transactional_emit） | backlog L3，跟改造 9 / 改造 11 一起做 |

## 7. 跟 plan 的对应关系

| plan 编号 | contract 章节 / 缺失断言 |
| --- | --- |
| A0（本文） | 全部 |
| A1（启动统一化） | §4.1 runtime 层职责的前提（启动期 vs 运行期分清） |
| A2（runtime 内部错误处理分级） | §4.1 cron/interval 异常分级补齐；§4.2 / §4.4 表的实现 review |
| A3（capability 异常上浮） | §4.8 落地（定义 5 个异常类 + 例外清单） |
| B4（节点错误声明 → 实为补 wire 级 swallow_and_log） | W13 第 4 种 policy + §4.2 swallow_and_log 分支 |
| B8（@node 自动 emit） | §1 N6（已在 commit 0ea263b 实现，本文档化） |
| C3（@tool_error 改契约） | §4.7 落地（5 个异常类 → 2 路由：LLM 可见 outcome / 节点失败） |
| C4（声明式 fan-out） | N8（手动 emit review） |

## 8. 修订记录

- 2026-05-13 初版：把 node.py / wire.py / graph.py / emit.py / durable.py / db.py / outbox.py 已有的隐式契约集中。
- 2026-05-13 codex T1 review 后修订：（必改 1）禁止节点级 on_error，明确 on_error 在 wire 边级唯一；（必改 2）§4.1 Source loop 按异常源分级（消息 decode / @node 异常 / infra 故障 / cron emit 异常）；（必改 3）§4.7 + §4.8 把 tool 层 + capability 层异常类定型为 5 个 + 路由表；（建议 1）补 W4a cross-app transport 断言；（建议 2）补 W2a with_latest join key 断言；（建议 3）"当前契约 / 未来目标"双轨标记。
