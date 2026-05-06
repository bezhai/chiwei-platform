# Dataflow Phase 5 — Chat 主 Pipeline 进 Graph

**状态**: Draft v4 (2026-05-06，吸收 reviewer 第 3 轮 4 条意见)
**前置**: PR #207 (Phase 4 life-engine / glimpse) shipped to prod 1.0.0.322
**后续**: Phase 6 清扫（旧 worker 入口、旧 ORM god object、bridge 残留 grep = 0）

**v4 关键变化（vs v3）**：
- §3.2 ChatTrigger 加 `message_id: Annotated[str | None, Key] = None`：`runtime/data.py:54-56` 强制每个 Data 子类至少一个 Key 字段，否则 import 时 raise；v3 ChatTrigger 缺 Key 直接编译炸。route_chat_node 入口校验 `t.message_id is None` 时 raise（让 lark-server 漏发 message_id 这种异常上 DLQ 而不是静默 fan-out 出空 ChatRequest）。修 reviewer P0 #1
- §3.2 ChatResponseSegment 加 `lane: str | None = None` 字段：`sink_dispatch.py:27` `Sink.mq` 只 `data.model_dump()` + `mq.publish(route, body)`，**不会自动把 header `lane` 塞进 body**；chat-response-worker 现行从 `payload.lane` 读取，body 漏 lane 会让 chat-response-worker 收到 None / lane-suffix 路由错。chat_node 构造 base_payload 时显式从 `lane_var.get()` 或 `req` 上下文读取 lane 写入。修 reviewer P0 #2
- 全文 "DLQ 可重放" 措辞精确化为 "DLQ 可观测、可人工排查；直接 replay 默认可能 no-op（durable consumer 先 `insert_idempotent` 后跑 handler，replay 时 dedup 行已存在 → handler 不再被调用）。若要重放需要配套清理对应 `data_chat_request` dedup 行，或另行设计失败状态/重放机制"。这部分**不在本期范围**，但写清楚避免运维误判。修 reviewer P1 #3
- §3.3 redelivered 自查显式带 `is_proactive` 参数：helper 实际签名 `is_chat_request_completed(session, session_id, *, is_proactive=False)`（`queries.py:297-302`）；proactive 场景查的是 `conversation_messages` 表，非 proactive 查 `agent_responses`。v3 调用样例漏了 `is_proactive` 关键字参数，proactive 触发的 chat 重投会走错分支。修 reviewer P2 #4
- §5.1 测试描述里 "已发出 N 段后 BLOCK" 删除：v3 设计下每段边界都 `await pre_task`，verdict 到达前一段都不会发出，所以 "已发出 N 段后 BLOCK" 不可能发生。改为只保留 "verdict 在第一个段边界到达时为 BLOCK → 只 emit 一段 guard"（与 §4.2 不变量第 3 条对齐）。修 reviewer P2 #4 测试部分

**v3 关键变化（vs v2）**：
- §3.1 / §3.2 / §3.4 ChatTrigger 自身不参与 dedup：runtime mq source loop（`engine.py:481-499`）直接 `req_cls(**body)` 调 target node，**没有 insert_idempotent 调用**；idempotent 只在 durable consumer handler（`durable.py:120`）里。所以 v2 写的"data_chat_trigger 表 dedup trigger 重投"是空话——表会被建但没人写。修法：ChatTrigger 改 `transient=True`、wire 不带 `.durable()`；mq 重投改善的描述精确到"ChatRequest 这一层（route → chat 是 in-graph durable 投递走 round-trip mq + insert_idempotent）"。trigger 重投会让 route_chat_node 跑两次（重复 DB 查询 + 重复 emit ChatRequest），下游 ChatRequest dedup 拦下，用户视角无差异。修 reviewer P0 #1
- §3.2 ChatTrigger / ChatRequest / ChatResponseSegment 字段全部加 `| None` 默认值或 `Optional` 标记，与 `chat_consumer.py:59-67` 现行 `.get(..., default)` 行为一字对齐。lark-server 实际不一定带 `is_proactive` / `root_id` / `user_id` / `lane` / `bot_name`，spec 写非可选会让正常消息 `req_cls(**body)` ValidationError → DLQ。修 reviewer P0 #2
- §3.3a chat_node 内部第 1 步显式列出 prep 块（fetch message 内容 + parse + gray config + persona 解析 + guard_message resolve + pre-safety task 启动），照搬 `pipeline.py:78-110`。v2 漏掉这一段是因为 v2 把 prep 当成"`_build_and_stream` 自带"，实际 prep 在 `stream_chat` 而不在 `_build_and_stream` 里。修 reviewer P1 #3
- §3.3a pre-safety blocked 路径明确：verdict=BLOCK 时立即 emit 1 段 `ChatResponseSegment(content=guard_message, is_last=True, full_content=guard_message)` 然后 `return`，不再消费 stream 也不再 emit 后续段。v2 伪代码 split 分支总是 emit `is_last=False` 没区分 blocked，与 §4.2 不变量第 3 条"pre-safety 命中飞书只收到一条"矛盾。修 reviewer P1 #4
- §3.3 redelivered 自查用项目现有 helper `is_chat_request_completed()`（`data/queries.py:316`），不硬编码 status 字符串。该 helper 实际查的是 status ∈ ("completed", "recalled")，v2 spec 写 'success' 不一致；coder 照写会让应用层 redelivered guard 失效。修 reviewer P2 #5

**v2 关键变化（vs v1）**：
- ~~§3.2 Data 类 `transient` 设置全面纠正：`ChatTrigger` 和 `ChatRequest` 都必须 `transient=False`（`graph.py:276` 对所有 `.durable()` wire 强制要求持久化；runtime 自动建 `data_chat_trigger` / `data_chat_request` 表做 message_id 与 (message_id, persona_id) idempotent insert，**这天然解决 mq 重投会重跑 LLM 的隐患**）；`ChatResponseSegment` 保留 `transient=True` 但去掉 wire 上的 `.durable()`，sink 本身就是 `mq.publish` 不是 durable consumer。修 reviewer P0 #1~~ **⚠️ 已被 v3 关键变化第 1 条推翻**：source.mq 入口不调 insert_idempotent，ChatTrigger 改 transient=True 不带 .durable()，dedup 只在 ChatRequest 那一层。data_chat_trigger 表不再存在
- §3.3 chat_node 伪代码彻底重写：`_build_and_stream` 内部已经调过 `handle_token`，对外 yield 的是 str（chat_consumer.py:204-240 现状）；chat_node 直接消费 str 流 + 按 `SPLIT_MARKER` 字符串切段，不再二次调 `handle_token`。修 reviewer P0 #2
- §3.2 / §3.3 / §3.4 加入 `MessageRouter` 的 fan-out 语义：新增 `ChatTrigger` Data（mq chat_request 入口的原始 body）+ `route_chat_node`（per-trigger fan-out 多 persona、第二个起重生成 session_id），`chat_node` 改为 per-persona 输入 `ChatRequest`。dedup 联合 key = `(message_id, persona_id, part_index)`。同时把 chat_consumer.py:148-186 现行的 resolve `response_bot_name` + 写 `agent_responses` 行为搬入 chat_node 前置。修 reviewer P1 #3
- §4.1 错误处理语义纠正：runtime durable 是 `requeue=False` 直接 DLQ，**没有自动重投**（durable.py:12 / engine.py:469）；`Sink.mq` 是直 publish 无 retry（sink_dispatch.py:27）。同时点出 5a 的可观察性改善——现 chat_consumer.py 用 `gather(return_exceptions=True)` 把单 persona 异常吞掉只 log 不进 DLQ；新方案下 chat_node raise 进 DLQ，可重放可监控。修 reviewer P1 #4
- §3.2 `ChatResponseSegment` 字段定义改为完整列表（与 chat_consumer.py:176-186 base_response + line 224-232 / 270-280 mq.publish body 一字对齐）：`session_id`, `message_id`, `chat_id`, `is_p2p`, `root_id`, `user_id`, `is_proactive`, `bot_name`, `persona_id`, `content`, `status`, `part_index`, `is_last`, `full_content` (final only), `published_at`。修 reviewer P1 #5（v1 字段 `text/seq/is_final` 与 lark-server chat-response-worker 实际期望的 `content/part_index/is_last` 不一致会让 worker 收不到内容）

## 0. 范围切片

Phase 5 拆为两个独立 ship 的 sub-phase（每个 sub-phase 一个独立 PR + 独立 plan）：

| 子阶段 | 主题 | 核心 deliverable |
|---|---|---|
| **5a** | chat 主 pipeline 进 graph | `chat_node` + `ChatRequest` / `ChatResponseSegment` Data + `wiring/chat.py`；删 `workers/chat_consumer.py`、`chat/pipeline.py:stream_chat`；同步删 `runtime/stream.py` 这个 type marker |
| **5b** | bridges 清扫 | 删 `app/bridges/`；`life/proactive.py:148` 改成 `await emit(Message.from_cm(cm))`；删 `tests/bridges/` |

5a 必须先 ship 到 prod 并稳定观察后再启动 5b。两个子阶段互不阻塞各自 review，但部署节奏严格串行。

## 1. 背景

Phase 0+1 落地 runtime 框架 + vectorize；Phase 2 把 safety 收进 graph；Phase 3 落地 `.debounce()` runtime 并把 drift / afterthought 改成节点；Phase 4 把 cron + per-persona fan-out + glimpse 收进 graph。

剩下的最后一片：**chat 主 pipeline**。当前形态是一条传统 mq consumer 链路（`chat_consumer.py` consume `chat_request` → 调 `stream_chat()` AsyncGenerator → 累积 + SPLIT_MARKER 切段 → publish `chat_response` mq），完全没接 runtime。源 spec（`2026-04-21-agent-dataflow-abstraction-design.md` §"Phase 5"）的目标是 `chat/pipeline.py` 退化为几条 wire + 若干 Node。

**业务收益**: 无。这是一次内部架构改造，飞书侧的对话表现完全不变（普通对话分段时机、guard message 替换、tool 穿插、截断提示，每一种 1:1 一致）。

**工程收益**:
- chat 流和其它管线统一在 graph 抽象下，下游（drift / afterthought / safety post）可以原生订阅 `ChatResponseSegment` 而不依赖临时 bridge
- chat 入口失败的重试/DLQ 行为从手写 mq consumer 沉淀到 runtime 统一管理
- bridges 整目录消失（5b），`grep "message_bridge" apps/` = 0

**Phase 5 总验收点**:
- `apps/agent-service/app/workers/chat_consumer.py` 不存在
- `apps/agent-service/app/chat/pipeline.py:stream_chat` 不存在；文件整体退化（< 150 行或整文件删除）
- `apps/agent-service/app/runtime/stream.py` 不存在
- `apps/agent-service/app/bridges/` 不存在（5b）
- `compile_graph()` 通过；飞书 dev bot 单聊 + 群聊 e2e 通过
- 现状全部行为不变量保持（见 §6）

## 2. 现状

### 2.1 chat 流（agent-service）

**入口**: `mq.consume(chat_request)` → `apps/agent-service/app/workers/chat_consumer.py:204` `_process_for_persona()` 调 `stream_chat(message_id, session_id, persona_id)`。

**核心函数链**（`apps/agent-service/app/chat/pipeline.py`）:
1. `stream_chat()` line 56 — 入口 `AsyncGenerator[str, None]`
2. fetch message + parse + gray config + persona 解析（line 78-97）
3. `pre_safety_gate.run_pre_safety_via_graph(...)` 启动异步 task（line 104-110，Phase 2 已 ship）
4. `_build_and_stream(...)` — agent context 构建 + agent.astream（line 128）
5. `_buffer_until_pre()` — 边等 pre-safety verdict 边 yield text（line 115-118）

**逐 token 处理**: `apps/agent-service/app/chat/stream.py:35` `handle_token()` —— 状态机，处理 AIMessageChunk / ToolMessage，识别 content_filter / length truncation，在 text→tool_call 边界注入 `SPLIT_MARKER = "---split---"`（line 22 / 62）。

**跨进程出口**: `chat_consumer.py:204-240` 自己 `async for text in stream_chat()`，累积 `full_content`，检测 SPLIT_MARKER，调 `mq.publish(CHAT_RESPONSE, {...})`（line 222）。`CHAT_RESPONSE` queue 由 lark-server 镜像下的 `chat-response-worker` deployment 消费（独立进程），最终调飞书 `im/v1/messages` API。

### 2.2 bridges 现状（独立于 chat 流）

`apps/agent-service/app/bridges/message_bridge.py:14` `emit_legacy_message(cm: ConversationMessage)`：把 DB 行 lift 成 `Message` Data 并 `emit`，给 `wiring/memory.py:29 wire(Message).to(vectorize).durable()` 消费。

**当前实际调用点**:
- `apps/agent-service/app/life/proactive.py:148` —— proactive 消息提交后调用，用于触发 vectorize

实际上 bridges **不在 chat 流上**——chat 流走的是 `chat_request` mq → `chat_consumer` 的独立路径，与 bridges 平行。bridges 的存在只为了让 proactive 路径能让 vectorize 接到 Message Data。

`apps/agent-service/app/nodes/hydrate_message.py` 已存在同款 `Message.from_cm` 映射，由 lark-server 一侧通过 mq 触发——这意味着用户消息流早已不经过 bridges，仅 proactive 还在用。

### 2.3 `Stream[T]` 现状

`apps/agent-service/app/runtime/stream.py`（26 行）—— 仅一个类型 marker，runtime 显式拒绝任何 `Stream[X]` 参数 / 返回（`runtime/node.py:80, 93`）。源 spec（2026-04-21）当时设想用它表达"一调用产多值"，Phase 1-4 实际落地时**全部用"node 内部多次 emit"代替**（`nodes/life_dataflow.py:59-74` `_fan_out_per_persona` 是范式）。Phase 5 顺手清掉这一抽象失败的痕迹。

## 3. 设计

### 3.0 关键约束（mq body schema 不动）

`chat_request` queue 的发布方是 lark-server，`chat_response` queue 的消费方是 lark-server 的 `chat-response-worker`。两个 queue 都跨服务、跨镜像。

**Phase 5a 必须保证**: `ChatRequest` 序列化的 JSON body 与现 `chat_consumer.py:55` `handle_chat_request` 解析的字段一字不差；`ChatResponseSegment` 序列化的 JSON body 与现 `chat_consumer.py:222` `mq.publish(CHAT_RESPONSE, {...})` body 字段一字不差。这是单方修改的硬约束（lark-server 不在本 PR 改）。

具体做法：plan 阶段 grep `chat_consumer.py` 现读 / 现写的所有字段，把字段名（即使不漂亮）照搬进 Data 类。

### 3.1 拓扑（5a 后）

```
lark-server
  ─[mq publish chat.request]─→  mq queue: chat_request
                                          │
                       [wire ChatTrigger, NO .durable()]   # source.mq 入口；
                                                          # ChatTrigger transient=True
                                                          # source loop 不调 insert_idempotent
                                                          # → trigger 重投会让 route_chat_node 跑两次
                                          │
                                          ↓
                       route_chat_node @ agent-service
                          应用层 redelivered 自查（is_chat_request_completed helper）
                          MessageRouter.route → fan-out per persona
                          (第 2 个及以后 persona 重生成 session_id)
                                          │
                                          ↓ N × emit(ChatRequest)
                       [wire ChatRequest, .durable()]   # in-graph durable 投递
                                                       # ChatRequest transient=False
                                                       # → runtime 建 data_chat_request
                                                       # → (message_id, persona_id) Key
                                                       # → insert_idempotent 在这里生效，
                                                       #   trigger 重投经过这里被拦下
                                          │
                                          ↓
                       chat_node @ agent-service (per persona)
                         prep：fetch message + parse + gray config + persona resolve
                              + guard_message resolve + pre-safety task 启动
                         main：resolve response_bot_name + update agent_responses
                              + 跑 _build_and_stream (str stream) + SPLIT_MARKER 切段
                              + 段边界等 pre_safety verdict
                              （pre-safety BLOCK 时只 emit 一段 guard + is_last=True 后 return）
                                          │
                                          ↓ N × emit(ChatResponseSegment)
                       [wire ChatResponseSegment, NO .durable()]
                              ↓ Sink.mq("chat_response")  # 直接 mq.publish
                                          │
                                          ↓
                              mq queue: chat_response
                                          │
                                          ↓
                              lark-server / chat-response-worker
                                          │
                                          ↓ POST im/v1/messages
                                       飞书
```

`chat/pipeline.py:stream_chat` 删除；`workers/chat_consumer.py` 整文件删除；`wiring/chat.py` 共 3 条 wire 声明（trigger 入口、router → chat、chat → mq sink）。

### 3.2 Data 类

**位置**: `apps/agent-service/app/domain/chat_dataflow.py`（新建，类比 `domain/life_dataflow.py`）

#### `ChatTrigger`（mq chat_request 入口的原始 body）

字段（默认值与 `chat_consumer.py:55-67` `handle_chat_request` 现行 `.get(..., default)` 行为一字对齐——lark-server 实际不一定带 is_proactive / root_id / user_id / lane / bot_name / mentions / enqueued_at；非可选会让正常消息 ValidationError → DLQ）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `message_id` | `Annotated[str \| None, Key] = None` | 消息 id；**runtime 要求至少一个 Key 字段**（`data.py:54-56`），ChatTrigger transient=True 仍受此约束。route_chat_node 入口校验 None 时 raise，避免静默 fan-out 出空 ChatRequest |
| `session_id` | `str \| None = None` | lark-server 发起时的 session id |
| `chat_id` | `str \| None = None` | 会话 id |
| `is_p2p` | `bool = False` | 私聊 / 群聊（chat_consumer 默认 False） |
| `root_id` | `str \| None = None` | 回复链 root |
| `user_id` | `str \| None = None` | 发送方用户 id |
| `lane` | `str \| None = None` | 泳道（chat_consumer 走 .get(); runtime mq Source 也读 header） |
| `is_proactive` | `bool = False` | 是否赤尾主动消息（chat_consumer 默认 False） |
| `bot_name` | `str \| None = None` | 触发的 bot |
| `mentions` | `list[str] = []` | @ 列表（chat_consumer 默认 []） |
| `enqueued_at` | `int \| None = None` | 入队 ms 时间戳 |

`Meta.transient = True`：runtime mq source loop（`engine.py:481-499`）直接 `req_cls(**body)` 调 target node，**没有 insert_idempotent 调用** —— 给 ChatTrigger 建表也没人写，因此干脆不建。dedup 在下游 ChatRequest 那一层（route → chat 是 in-graph durable 投递）。

**source 入口的"重投不重处理"由两层兜底**:
1. **应用层 redelivered 自查（route_chat_node 第一步）**：调用 `is_chat_request_completed(session, session_id, is_proactive=...)` helper（`data/queries.py:297-302`，根据 is_proactive 走不同分支：proactive 查 `conversation_messages` 表 assistant 行，非 proactive 查 `agent_responses.status` ∈ ("completed", "recalled")）。是则直接 return。这是 chat_consumer.py:79-95 的现行行为，搬到 route_chat_node。
2. **下游 ChatRequest dedup**：route_chat_node fan-out 出来的 ChatRequest 走 in-graph durable 投递，`(message_id, persona_id)` 联合 Key 在 `data_chat_request` 表上 idempotent insert 拦下重投。

trigger 重投的代价：route_chat_node 跑两次，重复一次 DB 查询 + MessageRouter.route + fan-out emit。下游 chat_node 不会被重复触发（被 ChatRequest dedup 拦下）。性能浪费但用户视角无差异。

#### `ChatRequest`（每个 persona 一个）

字段（route_chat_node 内部 emit，可选标记尽量宽松配合 `ChatTrigger` 的可选；联合 Key `(message_id, persona_id)` 必填）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `message_id` | `Annotated[str, Key]` | 必填，与 ChatTrigger 对齐；route_chat_node emit 时若 trigger.message_id 为 None 应直接 raise（业务前置） |
| `persona_id` | `Annotated[str, Key]` | 必填，router 决定的 persona（`(message_id, persona_id)` 联合 Key 是 dedup 单元） |
| `session_id` | `str \| None = None` | 第 1 个 persona 沿用 trigger.session_id（可能 None），第 2 个及以后 `str(uuid4())`（与 `chat_consumer.py:131` 一致） |
| `chat_id` | `str \| None = None` | 透传 |
| `is_p2p` | `bool = False` | 透传 |
| `root_id` | `str \| None = None` | 透传 |
| `user_id` | `str \| None = None` | 透传 |
| `is_proactive` | `bool = False` | 透传 |
| `bot_name` | `str \| None = None` | trigger 上的 bot_name；chat_node 内部还会再 resolve 出 response_bot_name 用于回复 |
| `lane` | `str \| None = None` | 从 ChatTrigger 透传；chat_node 构造 ChatResponseSegment 时写回 segment 的 lane 字段 |
| `enqueued_at` | `int \| None = None` | 透传，用于 chat_node 内部计算 queue wait 指标 |

`Meta.transient = False`（**v1 的关键修正**）：runtime 在 compile 时验证 `.durable()` 边要求 Data 有持久化表（`graph.py:276`）；boot 时 migrator 自动 `CREATE TABLE data_chat_request (message_id, persona_id, ...)`；durable consumer handler `insert_idempotent(obj)`（`durable.py:120`）拦下 `(message_id, persona_id)` 重投——**这天然解决 mq 重投会重跑 LLM 的隐患**（v1 列入 Followup 的问题在 v2/v3 里 free 拿到了改善）。这里的 dedup 是真实的，因为 ChatRequest 走的是 in-graph durable 投递，会经过 durable consumer handler。

#### `ChatResponseSegment`

字段（与 `chat_consumer.py:176-186 base_response` + `line 224-232` 中间段 / `line 270-280` final 段 mq.publish body 一字对齐）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `message_id` | `Annotated[str, Key]` | dedup 联合 Key（chat_node 这一层 message_id 必有，是从 ChatRequest 透传） |
| `persona_id` | `Annotated[str, Key]` | dedup 联合 Key |
| `part_index` | `Annotated[int, Key]` | dedup 联合 Key |
| `session_id` | `str \| None = None` | 透传 |
| `chat_id` | `str \| None = None` | 透传 |
| `is_p2p` | `bool = False` | 透传 |
| `root_id` | `str \| None = None` | 透传 |
| `user_id` | `str \| None = None` | 透传 |
| `lane` | `str \| None = None` | 显式带在 body —— `Sink.mq` 不会自动注入 header lane（`sink_dispatch.py:27` 只 `model_dump`），chat-response-worker 现行读 `payload.lane`。chat_node 构造段时从 `lane_var.get()` 或 ChatRequest 上下文读取写入 |
| `is_proactive` | `bool = False` | 透传 |
| `bot_name` | `str \| None = None` | resolve 后的 response_bot_name（chat_node 内部 resolve 失败时 fallback 到 trigger.bot_name，可能 None） |
| `content` | `str = ""` | 段文本（中间段是 split 出的一段，final 段是剩余尾巴或拼接结果） |
| `status` | `str = "success"` | `"success"` / `"failed"` |
| `is_last` | `bool = False` | True 表示这条对话最后一段 |
| `full_content` | `str \| None = None` | 仅 final 段非空：`full_content.replace(SPLIT_MARKER, "\n\n").strip()`（与 `chat_consumer.py:263` 现行 `clean_full` 一致） |
| `published_at` | `int \| None = None` | publish 时的 ms 时间戳 |

`Meta.transient = True` + 不带 `.durable()`：sink 是直 publish 不需要 dedup 表（runtime 不会建表 / 也不会 round-trip mq → consumer）。`(message_id, persona_id, part_index)` 联合 Key 仍然存在用于做语义识别（lark-server 那侧已有自己的 dedup 逻辑，不依赖 runtime 这边）。

**关于 `lane`**：runtime 自动从 `lane_var` ContextVar 注入到 mq publish header（`engine.py` mq source 里已经在 set），不需要在 Data body 里冗余。但 v1 base_response 里有 `lane` 字段——为保 mq body 完全等价，ChatResponseSegment 仍带 `lane: str | None`，runtime 模块在序列化时填充。**plan 阶段**确认这一点：runtime 是不是已经把 ContextVar 转到 mq body？如果不是，Data 字段保留显式 `lane`。

### 3.3 `route_chat_node`（5a 新增）

**位置**: `apps/agent-service/app/nodes/chat_node.py`（与 chat_node 同文件）

**签名**:
```python
@node
async def route_chat_node(t: ChatTrigger) -> None:
    ...
```

**职责**（照搬 `chat_consumer.py:78-148`）:

0. **入口校验**（v4 加）：`if t.message_id is None: raise ValueError("ChatTrigger.message_id missing")`—— ChatTrigger.message_id 是可选字段（lark-server 偶尔字段缺失时也能反序列化进来）但 fan-out 必须有 message_id，缺失时上 DLQ 比静默 fan-out 出空 ChatRequest 安全。等价于 `chat_consumer.handle_chat_request` 当前 message_id 为空时早返回的语义
1. **redelivered 短路**：调用项目已有 helper `is_chat_request_completed(session, t.session_id, is_proactive=t.is_proactive)`（`apps/agent-service/app/data/queries.py:297-302`）—— 该 helper 内部根据 is_proactive 走不同分支：proactive 查 `conversation_messages` 表里 assistant 角色行；非 proactive 查 `agent_responses.status` ∈ `("completed", "recalled")`。True 时 route_chat_node 直接 return，不再 fan-out。**spec 不要硬编码 status 字符串**，跟着 helper 走
2. **MessageRouter.route**：决定回应的 persona id 列表
3. **fan-out emit ChatRequest**：
   ```python
   for i, pid in enumerate(persona_ids):
       session_id_for_persona = (
           t.session_id if i == 0 else str(uuid4())
       )
       await emit(ChatRequest(
           message_id=t.message_id,
           persona_id=pid,
           session_id=session_id_for_persona,
           chat_id=t.chat_id, is_p2p=t.is_p2p, root_id=t.root_id,
           user_id=t.user_id, is_proactive=t.is_proactive,
           bot_name=t.bot_name, lane=t.lane, enqueued_at=t.enqueued_at,
       ))
   ```
4. **错误处理**：函数体不包 try/except；router 失败 raise 进 DLQ。fan-out 内 emit 失败 raise（不模仿 Phase 4 fan-out 内的 try-log，因为这里失败 = 用户没回复，必须可观察）

### 3.3a `chat_node`（5a 的核心）

**位置**: `apps/agent-service/app/nodes/chat_node.py`

**签名**:
```python
@node
async def chat_node(req: ChatRequest) -> None:
    ...
```

**内部分七块（不拆 node，函数体内分块）**:

1. **prep（v3 新增，照搬 `pipeline.py:78-110` 现 stream_chat 头部）**：
   - fetch message content：`async with get_session(): raw_content = await find_message_content(s, req.message_id)`；为空时 emit 一段 ChatResponseSegment(content="抱歉，未找到相关消息记录", is_last=True, ...) 后 return
   - `parsed = parse_content(raw_content)`
   - fetch gray config：`async with get_session(): gray_config = await find_gray_config(s, req.message_id) or {}`
   - 解析 effective_persona + `guard_message = await fetch_guard_message(effective_persona)`
   - 启动 pre-safety task：`pre_task = asyncio.create_task(pre_safety_gate.run_pre_safety_via_graph(message_id=..., content=parsed.render(), persona_id=...))`
2. **resolve response_bot_name + 更新 agent_responses 行**（照搬 `chat_consumer.py:155-174`）
3. **构造 base segment payload**（照搬 `chat_consumer.py:176-186`）—— content / status / part_index / is_last / full_content / published_at 之外的字段。**必须显式包含 `lane=req.lane`**（v4：`Sink.mq` 不会自动注入 header lane，body 漏 lane 会让 chat-response-worker 路由错）
4. **跑 agent stream + 切段 emit**（**v3 在 v2 上加 blocked 路径**）：
   ```python
   sent_length = 0
   part_index = 0
   full_content = ""

   async def _emit_segment(content: str, is_last: bool, full: str | None) -> None:
       await emit(ChatResponseSegment(
           **base_payload,
           content=content,
           status="success",
           part_index=part_index,
           is_last=is_last,
           full_content=full,
           published_at=int(time.time() * 1000),
       ))

   async for text in _build_and_stream(req.message_id, gray_config, req.session_id, persona_id=req.persona_id):
       if not text:
           continue
       full_content += text
       pending = full_content[sent_length:]
       while SPLIT_MARKER in pending and part_index < MAX_MESSAGES - 1:
           idx = pending.index(SPLIT_MARKER)
           part = pending[:idx].strip()
           if part:
               result = await _resolve_pre_safety_for_part(part, pre_task, guard_message)
               if result.blocked:
                   # v3: pre-safety BLOCK 即终止 — 只 emit 一段 guard，is_last=True
                   await _emit_segment(
                       content=guard_message,
                       is_last=True,
                       full=guard_message,
                   )
                   return  # 不再消费 stream，不再 emit 后续段
               await _emit_segment(content=result.content, is_last=False, full=None)
               part_index += 1
           sent_length += idx + len(SPLIT_MARKER)
           pending = full_content[sent_length:]
   # final 段（照搬 chat_consumer.py:262-281）
   remaining = full_content[sent_length:].replace(SPLIT_MARKER, "").strip()
   clean_full = full_content.replace(SPLIT_MARKER, "\n\n").strip()
   final_content = (remaining or full_content) if (remaining or part_index == 0) else ""
   result = await _resolve_pre_safety_for_part(final_content, pre_task, guard_message)
   if result.blocked:
       await _emit_segment(
           content=guard_message,
           is_last=True,
           full=guard_message,
       )
       return
   await _emit_segment(content=result.content, is_last=True, full=clean_full)
   ```
5. **`_resolve_pre_safety_for_part` helper 语义**（替代 v2 的 `_maybe_replace_with_guard`）：
   - 段边界等 verdict：未到 verdict 则 await（带原 timeout）；fail-open 时返回 `(blocked=False, content=part)`
   - verdict=BLOCK：返回 `(blocked=True, content=guard_message)`，由调用方决定如何 emit（中段 / final 段都走 blocked 终止路径）
   - verdict=ALLOW：返回 `(blocked=False, content=part)`
   - 是命名 tuple 或小 dataclass，和 v2 把 guard 替换吞进去的 `_maybe_replace_with_guard` 不同——blocked 状态需要返回出来让调用方走终止路径
6. **错误兜底**：函数体不包 try/except——失败 raise 由 durable consumer requeue=False → DLQ。对比现状（chat_consumer.py 用 `gather(return_exceptions=True)` 把单 persona 异常吞掉只 log），新方案下进 DLQ 是**可观察性改善**：DLQ 消息可观测、可人工排查、可监控告警。**注意 replay 限制**：durable consumer 是先 `insert_idempotent` 后跑 handler，直接 replay 同一条 DLQ 消息会被 dedup 拦下成 no-op。要重放需要先清理对应 `data_chat_request` dedup 行，或另行设计失败状态机/重放工具——本期不做，但运维需要知道这个事实。

**关键点解读**:
- `_build_and_stream` 已经在内部消费 `agent.stream()` + 调用 `handle_token()`，对外 yield str。chat_node 不再 import / 调用 `handle_token` —— 它消费的是已经过 `handle_token` 处理后的纯文本流（与现 `chat_consumer.py:204-240` 完全一致）
- `MAX_MESSAGES` 沿用 `chat_consumer.py` 现有常量
- content_filter / length truncation 的边界处理已经在 `_build_and_stream` 内部完成（pipeline.py:215 的 `handle_token` 返回 `[None]` / `["(后续内容被截断)"]` 时由 `_build_and_stream` 的封装层 yield 字符串或终止 generator）；chat_node 这一层不再单独处理

**pre-safety 同步等待 = 段边界等**:
- 现 `_buffer_until_pre()` 是 token 边界等 verdict
- chat_node 改成段边界等：每到 SPLIT_MARKER（或 final）时如果 verdict 还没回来则 await（带原 timeout）；fail-open 时按现状用 guard message 替换
- 用户感知零差异：跨进程发送的最小单位本来就是段，token 边界等 / 段边界等用户都看不到 token

**复用 vs 重写**:

| 现有模块 | 处置 |
|---|---|
| `chat/stream.py:handle_token`, `StreamState`, `SPLIT_MARKER` | `SPLIT_MARKER` 保留 import；`handle_token` / `StreamState` 留在 `_build_and_stream` 内部使用，chat_node 不直接用 |
| `chat/pipeline.py:_build_and_stream` | 保留并搬到 `nodes/chat_node.py` 同文件（chat/pipeline.py 整体可删） |
| `chat/pipeline.py:_buffer_until_pre` | 重写为 `_maybe_replace_with_guard`（段边界版，搬到 `chat_node.py`） |
| `chat/pipeline.py:stream_chat` | 删除（chat_node 取代它的 orchestration 角色） |
| `workers/chat_consumer.py` | 整文件删除（route + per-persona 切到 route_chat_node + chat_node） |
| `chat/__init__.py` 的 `from app.chat.pipeline import stream_chat` | 删除 |
| `MessageRouter` (`chat/router.py`)、`resolve_bot_name_for_persona` | 保留，被 route_chat_node / chat_node 调用 |

### 3.4 wiring

**位置**: `apps/agent-service/app/wiring/chat.py`（新建）

```python
"""Chat 主 pipeline.

  mq(chat_request)
       ─[wire ChatTrigger, NO .durable()]→  route_chat_node
                                              │
                                              ↓ N × emit(ChatRequest)  (per persona)
       ─[wire ChatRequest, .durable()]→  chat_node
                                              │
                                              ↓ N × emit(ChatResponseSegment)
       ─[wire ChatResponseSegment, NO .durable()]→  Sink.mq("chat_response")
                                              ↓
                                  lark-server / chat-response-worker → 飞书
"""
from app.domain.chat_dataflow import ChatRequest, ChatResponseSegment, ChatTrigger
from app.nodes.chat_node import chat_node, route_chat_node
from app.runtime import Sink, Source, wire

wire(ChatTrigger).from_(Source.mq("chat_request")).to(route_chat_node)
wire(ChatRequest).to(chat_node).durable()
wire(ChatResponseSegment).to(Sink.mq("chat_response"))
```

**关键点（v3 修正）**:
- `wire(ChatTrigger).from_(Source.mq("chat_request")).to(route_chat_node)` **不带 .durable()**：source.mq 入口已经是 mq consumer，runtime source loop（`engine.py:481-499`）`incoming.process(requeue=False)` 直接调 target node，**没有 insert_idempotent 调用**——给 ChatTrigger 加 `.durable()` 没有 dedup 效果；ChatTrigger 同步 `transient=True`（不建表，因为没人会写）
- `wire(ChatRequest).to(chat_node).durable()` 不带 `from_()`：来源是 route_chat_node 的 emit（in-graph 投递）；durable 边走 RabbitMQ round-trip + durable consumer handler（`durable.py:120` 调 `insert_idempotent`）。**这才是真正的 dedup 层**：trigger 重投经 route_chat_node 重跑产出同样 (message_id, persona_id) 的 ChatRequest，被 idempotent 拦下不重新触发 chat_node
- ChatRequest `transient=False`：`graph.py:276` 对所有 `.durable()` wire 强制要求持久化；runtime 自动建 `data_chat_request (message_id, persona_id, ...)` 表
- `wire(ChatResponseSegment).to(Sink.mq("chat_response"))` 不带 `.durable()`：sink 是直接 mq.publish（`sink_dispatch.py:27`），不是 durable consumer；因此 ChatResponseSegment `transient=True` 即可
- `Source.mq("chat_request")` / `Sink.mq("chat_response")` 已现成（Phase 2 safety / Phase 1 vectorize 在用）

### 3.5 `Stream[T]` 处置（搭车 5a）

- 删 `app/runtime/stream.py`（全文 26 行）
- 删 `app/runtime/node.py` 里 `is_stream` import + Stream 校验代码（约 6 行）+ 文档段 "Stream[T] is not supported"
- 同时把 `node.py` 文档头里的 "spec forbids business code from calling emit / mq.publish to the next hop manually" 修正为 "@node 默认 auto-emit 返回值；多产出场景（fan-out / streaming segment）由 node 内部主动 `emit()` 多次"——把 Phase 4 已经在用的 fan-out 模式扶正
- 在源设计文档 `docs/superpowers/specs/2026-04-21-agent-dataflow-abstraction-design.md` §"Stream[T]" 段落末尾追加 errata 注：
  > **Errata（Phase 5）**: `Stream[T]` 经 Phase 1-4 实践证伪。"一产多" 场景由 `@node` 内部多次 `await emit(...)` 表达即可（fan-out 已大规模在用）。Phase 5 落地时删除 `runtime/stream.py` 与 `node.py` 的 `Stream` 校验。

### 3.6 5b 设计

**改动**:
1. 删 `apps/agent-service/app/bridges/`（`message_bridge.py` + `__init__.py`）
2. `apps/agent-service/app/life/proactive.py:144-148`:
   ```python
   # 现状
   from app.bridges.message_bridge import emit_legacy_message
   await emit_legacy_message(msg)
   # 改为
   from app.domain.message import Message       # 现 Message Data 实际位置
   from app.runtime.emit import emit
   await emit(Message.from_cm(msg))
   ```
3. 删 `apps/agent-service/tests/bridges/test_message_bridge.py`
4. 删 `wiring/memory.py:8` 头部注释里"emit_legacy_message"段
5. 删 `apps/agent-service/app/main.py:31` 注释里"proactive.py's Bridge calls emit_legacy_message"段

**前置依赖**: 5a ship 后稳定观察。5b 没有功能依赖 5a 的代码（bridges 与 chat node 平行），但顺序上想让 5a 先稳。

## 4. 错误处理 + 不变量

### 4.1 错误处理（基于 runtime 实际行为，**v2 修正**）

runtime durable consumer 的语义（`durable.py:12-18`, `engine.py:469`）：**fail-to-DLQ, no in-place retry**。`message.process(requeue=False)` 触发后 broker 走 DLX → DLQ，没有自动 delay-retry。replay 是 operator action。Sink.mq 是 `mq.publish` 一次（`sink_dispatch.py:27`），无 retry。

| 场景 | 行为 |
|---|---|
| `route_chat_node` 内部 raise | source loop 的 `incoming.process(requeue=False)`（`engine.py:469`）→ DLQ。下游 ChatRequest 不会被产出 → 飞书无回复 |
| `chat_node` 内部 raise | durable consumer handler `incoming.process(requeue=False)`（`durable.py:`）→ DLQ。该 persona 的回复丢失，**其他 persona 的 chat_node 不受影响**（每个 persona 独立 ChatRequest 实例 / 独立 durable handler 跑） |
| mq 重投 ChatTrigger（source.mq 入口） | source loop 无 insert_idempotent → route_chat_node 跑两次。下游 ChatRequest（in-graph durable）的 idempotent 拦下 chat_node 的二次触发。**用户视角无重复段**，但 route_chat_node 浪费一次 DB 查询 + router 调用 |
| mq 重投 ChatRequest（in-graph durable） | durable consumer handler 调 `insert_idempotent`（`durable.py:120`）；第二次 insert 返 0 → chat_node 不被调用。**重投不重跑 LLM、不重发段**——这是 v3 真正的改善层 |
| 应用层 redelivered（is_chat_request_completed helper 返 True） | route_chat_node 第 1 步用 `is_chat_request_completed(s, t.session_id, is_proactive=t.is_proactive)` 判断（proactive / 非 proactive 走不同分支），True 时直接 return，连 ChatRequest 都不 emit。这是 chat_consumer.py:79-95 现行行为的搬运 |
| `Sink.mq("chat_response")` publish 失败 | 直接抛（runtime 不 retry）→ chat_node raise → DLQ |
| pre-safety timeout / emit 异常 / 外层 cancel | 沿用 `pre_safety_gate.run_pre_safety_via_graph` 已有的 fail-open（Phase 2 已 ship） |
| LLM 调用失败 | `_build_and_stream` 内现有兜底，不动 |

**5a 相对现状的差异**（值得显式说明）:

- **改善**：现 chat_consumer.py 的 multi-persona 路径 `gather(return_exceptions=True)`（line 142-147）把单 persona 的 exception 吞掉只 log；agent_responses 行可能停在 `processing` 状态，mq message 仍 ack。新方案下 chat_node raise → DLQ，**消息不丢失但人能看见**：DLQ 监控告警 + 可人工排查
  - 关于"重放"：durable consumer 是先 `insert_idempotent` 后跑 handler，DLQ replay 同一条 ChatRequest 会被 dedup 拦下成 no-op。要重放需要先清理对应 `data_chat_request` dedup 行，或另行设计失败状态/重放机制。本期不做，写明白避免运维误判
- **改善**：mq 重投不再重跑 LLM —— ChatRequest（in-graph durable）层的 `insert_idempotent` 拦下重投。trigger 重投会让 route_chat_node 跑两次但下游 chat_node 不会重复触发。v1 列入 Followup 的问题在 v2/v3 里 free 拿到了
- **持平**：单次 LLM 失败 → 用户感知"赤尾没回我"，与现状一致
- **可能退化**：现状 `gather(return_exceptions=True)` 让 message ack 了，新方案 raise → DLQ；如果 DLQ 没人监控，长期堆积可能成"看不见的失败"。**plan 阶段必须确认 DLQ 监控告警已就位**（Phase 3/4 已经在做 DLQ 告警，Phase 5 沿用即可）

### 4.2 行为不变量（5a 验收）

1. 飞书单聊 / 群聊普通对话：分段时机、段数、段内容与现状逐字一致
2. tool 调用穿插：text → tool_call 边界仍注入 SPLIT_MARKER，导致飞书收到的"前半句话 + 后半句话"分两条独立消息（与现状一致）
3. pre-safety 命中：飞书只收到 guard message 一条，不会先回正常话再补 guard
4. content_filter / length truncation：飞书显示同样的预设提示
5. mq 重投：5a 相对现状是**改善**——runtime idempotent insert 拦下重投，飞书不会收到重复段（现状会）
6. DLQ 入队消息可读，包含 message_id 和 persona_id，便于人工排查

## 5. 测试策略

### 5.1 单元测试（5a，挡回归）

**位置**: `apps/agent-service/tests/nodes/test_route_chat_node.py`（新建）

- 单 persona：fake MessageRouter 返 1 个 persona → emit 1 个 ChatRequest（session_id 透传）
- 多 persona：fake MessageRouter 返 3 个 persona → emit 3 个 ChatRequest，第 1 个 session_id 透传，第 2/3 个 session_id 是 uuid（不等于 trigger.session_id）
- redelivered 短路：fake `is_chat_request_completed()` helper 返 True → 直接 return，不 emit。**断言 helper 被调时带了 `is_proactive=t.is_proactive` 关键字参数**（v4 修 reviewer P2 #4）
- proactive 场景的 redelivered：构造 `is_proactive=True` 的 ChatTrigger，verify helper 走 conversation_messages 分支
- 空 persona：fake MessageRouter 返 [] → 直接 return，不 emit
- router 异常 raise → 不被 try/except 吞（让 durable consumer 进 DLQ）
- ChatTrigger 字段宽松：trigger 不带 `is_proactive` / `user_id` / `bot_name` 时（chat_consumer.py:59 的 `.get(default)` 场景）route_chat_node 仍能正常工作，使用默认值（v3 修 reviewer P0 #2）
- **message_id 缺失（v4 加）**：trigger.message_id is None → route_chat_node raise（不要静默 fan-out 出空 ChatRequest），让 durable consumer 进 DLQ

**位置**: `apps/agent-service/tests/nodes/test_chat_node.py`（新建）

用 fake `_build_and_stream`（注入预编排 **str** 序列）+ `capture_emit` 断言 emit 出的 ChatResponseSegment 序列：
- **prep 块测试（v3 新增，修 reviewer P1 #3）**：
  - fake `find_message_content` 返 None → emit 1 段 ChatResponseSegment(content="抱歉，未找到相关消息记录", is_last=True) 后 return；不再调 `_build_and_stream`
  - fake `find_gray_config` 返 None → 当成空 dict 不报错
  - fake `fetch_guard_message` 返某 guard 字符串 → 后续 blocked 测试断言用此字符串
- 普通分段：3 段 str + 2 个 SPLIT_MARKER → 3 个 `ChatResponseSegment`，part_index=0/1/2，最后 `is_last=True` + `full_content=拼接结果`
- **pre-safety 拦截（v4 修正，修 reviewer 测试不一致）**：每段边界 `await pre_task` 决定本段是否 emit；verdict 在 verdict 到达前没有任何段被 emit（与 §4.2 不变量第 3 条对齐）。fake pre_task 返回 BLOCK：
  - verdict 在第一个段边界到达时为 BLOCK：emit **1 段** guard + is_last=True + full_content=guard，**不再消费剩余 stream**，**没有任何"已发出的正文段"**
  - verdict 在 final 段到达时为 BLOCK（stream 已结束但 verdict 才返回）：emit 1 段 guard + is_last=True
  - **断言**：每个 BLOCK 场景下飞书侧仅看到 1 段消息，content=guard_message
- 段字段完整性：断言每段都带 `session_id` / `message_id` / `chat_id` / `persona_id` / `bot_name` / `is_p2p` / `root_id` / `user_id` / `is_proactive` / `published_at` / `status="success"` / **`lane`（v4 加，verify Sink.mq 不会在 publish 前丢失）**
- chat_node 内部 raise（fake `_build_and_stream` 抛异常）→ chat_node raise（不被 try/except 吞）
- agent_responses 行更新：fake DB session 断言 `update_agent_response` 被调用一次，参数包含 resolved bot_name + persona_id

**位置**: `apps/agent-service/tests/wiring/test_chat_wiring.py`（新建）
- `compile_graph()` 不报错（包含 ChatTrigger / ChatRequest / ChatResponseSegment 三条 wire）
- wire 数量 + 类型正确
- 验证 `data_chat_request` 表会被 migrator 建（v3 修：删除 `data_chat_trigger` 验证项，因为 ChatTrigger transient=True 不建表）；可通过 `migrate_schema` 在 in-memory pg 上跑一次断言表存在
- **断言 ChatTrigger 不在 migrator 建表列表里**（防止有人误把 transient 改回 False）

**位置**: `apps/agent-service/tests/dataflow/test_chat_dedup.py`（新建）
- 同一 ChatRequest emit 两次 → 第二次 `insert_idempotent` 返 0 → chat_node 不被调用第二次
- 模拟 trigger 重投：手动调 route_chat_node 两次（同一 ChatTrigger）→ emit 出的 ChatRequest 经 durable wire 投递，第二次被 idempotent 拦下；chat_node 仅被调用一次
- **断言 ChatTrigger 自身不被 idempotent 拦**（v3 精确：mq source loop 无 dedup，重投会让 route_chat_node 跑两次）

### 5.2 e2e（5a，部署泳道前必须）

1. `make deploy APP=agent-service GIT_REF=<branch> LANE=feat-flow-parse-5`（同步 release `arq-worker` / `vectorize-worker`，按项目铁律 4）
2. `/ops bind TYPE=bot KEY=dev LANE=feat-flow-parse-5`
3. 飞书 dev bot 实测：
   - 单聊普通对话（验 split 分段）
   - 单聊问需要工具的（如"搜下 xx"，验 tool 穿插的两段）
   - 群聊 @ 赤尾对话
   - 故意触发 pre-safety（用已知会拦的话术）
4. e2e 通过后告知用户验收，等用户验收完毕再 `/ops unbind` + `make undeploy`

### 5.3 5b 测试

- `tests/life/test_proactive.py` 已有 case 改用 `capture_emit` 断言 `Message` 直接 emit，不再 mock `emit_legacy_message`
- e2e：proactive 触发（让赤尾自言自语一次）→ 断言 vectorize 还能跑通
- grep 验收：`grep -rn "message_bridge\|emit_legacy_message" apps/` = 0

### 5.4 ship 前自检 grep

```
grep -rn "stream_chat\|workers/chat_consumer" apps/agent-service/         # 5a 后 = 0
grep -rn "from app.runtime.stream\|Stream\[" apps/agent-service/          # 5a 后 = 0
grep -rn "message_bridge\|emit_legacy_message" apps/agent-service/        # 5b 后 = 0
grep -rn "AsyncGenerator\[str" apps/agent-service/app/chat/               # 5a 后 = 0
ls apps/agent-service/app/bridges/                                        # 5b 后不存在
ls apps/agent-service/app/runtime/stream.py                               # 5a 后不存在
```

## 6. 部署 + 回滚 + 监控

### 6.1 一镜像多服务影响

agent-service 镜像产 3 个 deployment（`agent-service` / `arq-worker` / `vectorize-worker`）：

- chat_node 只在 `agent-service` 这个 deployment 跑——`Source.mq("chat_request")` 由 runtime placement 在 `main.py` lifespan 启 source loop；`arq-worker` / `vectorize-worker` 的 entry 不会启 chat source（Phase 4 cron 已经验证 placement 按进程区分 source）
- 5a / 5b 部署都按项目铁律 4 同步 release 三个 deployment

### 6.2 部署中断的副作用

- chat_node 跑到一半被杀 → 该 ChatRequest 的 mq message 没 ack（durable consumer 上下文管理器异常退出 → broker 视为未消费）→ 重投到新 Pod
- 这里有个微妙点：runtime `insert_idempotent(ChatRequest)` 在 chat_node 入口已经成功（否则 handler 不会被调）；新 Pod 重投同一 ChatRequest，durable consumer 入口 `insert_idempotent` 返 0 → chat_node 不再被调用。**但用户也不会收到剩余的段**。结果是用户看到部分段后停止。
- 现状同等场景：chat_consumer.py 用 `gather(return_exceptions=True)` 吞掉异常 + ack message，结果同样是部分段后停止。5a 不更糟
- 关于 chat_request mq 重投（被 lark-server 端 / 部署 chat_response_worker 没 ack 的情形）：source.mq 入口无 dedup，route_chat_node 跑两次，但下游 ChatRequest dedup 拦下重复触发 chat_node。仍然是部分段后停止
- 部署前按项目铁律 2 确认无活跃 chat（飞书安静窗口部署）

### 6.3 回滚

- **5a 回滚**: revert PR + 重新 release。**关键风险**：5a 跑过的对话已经在 `data_chat_request` 表里有 idempotent 行（v3 后 ChatTrigger 不再建表）；回滚到 v1 chat_consumer 后，该表对老代码不可见——意味着回滚后这一时段的 chat_request 重投会被 5a 表的 dedup 残留**不知不觉地阻挡**？不会，因为 v1 chat_consumer.py 不查 `data_chat_request`。但反过来要确认：v1 chat_consumer.py 只查 `agent_responses` 表做应用层 dedup（line 78-95），跟 runtime 表无关，所以回滚后行为完全等价 v1 现状
- 第二个回滚硬约束：`ChatTrigger` / `ChatRequest` / `ChatResponseSegment` 的 mq body schema 与现 chat_consumer 完全等价，老代码可消费 5a 时代留在 mq queue 上的消息
- **5b 回滚**: revert PR；`proactive.py` 那一行 emit Message 改回 `emit_legacy_message`，bridges 文件 git revert 恢复

### 6.4 监控

- 现有 Prometheus 指标 `CHAT_PIPELINE_DURATION`（`chat/pipeline.py:91`）保留，搬到 chat_node.py 同名指标，标签 `stage=prep` 等保持不变
- chat_request / chat_response queue backlog：现有 RabbitMQ 监控不变
- LLM 调用 langfuse trace：现有不变（Phase 0 已统一注入 langfuse 不重写）
- **不新增**业务指标。Phase 4 经验：改造期不做指标扩张，先稳

### 6.5 ship 后观察

5a ship 到 prod 后人工观察 1h（用户手动监控），核对：
- prod 三服务 v\<version\> 全 Running，0 restart
- agent-service prod 的 source loop 启动日志包含 "chat_request" source
- chat_request / chat_response queue backlog 正常
- 飞书侧赤尾响应正常（用户自然对话观察）

## 7. Followups（不在 Phase 5 范围）

- ~~mq 重投导致段重复发飞书~~ — v3+ 已解决：ChatRequest 这一层 in-graph durable 边的 `insert_idempotent` 拦下重投；ChatTrigger 自身（source.mq 入口）不参与 dedup 但 trigger 重投经 route_chat_node 重跑后产出同样的 ChatRequest，被下游 dedup 间接拦下
- agent tool 副作用进 wire（commit_abstract_memory → emit AbstractMemorySaved）→ Phase 6 工作（源 spec 明确"本轮维持开放"）
- "chat 部分段后中断、新 Pod 不续传剩余段" 改进 → 长期。需要把 stream 进度持久化（每段 emit 后写一行 `data_chat_response_segment`），重投时根据 part_index 续跑。本期不做
- chat_node 内部更细的拆分（如出于可测试性需要）→ 长期
- 应用层 redelivered 自查（`agent_responses` status 检查）和 runtime idempotent 是否两层兜底过头 → 等 5a 在 prod 跑稳后评估能否简化

## 8. 验收清单（汇总）

**5a**:
- [ ] `compile_graph()` 通过（含 ChatTrigger / ChatRequest / ChatResponseSegment 三条 wire）
- [ ] runtime migrator 自动建出 `data_chat_request` 表（boot 时 + integration test 双重验证）；ChatTrigger 不建表（v3 修正：transient=True）
- [ ] §5.1 单元测试全绿（route_chat_node + chat_node + wiring + durable dedup）
- [ ] §5.4 grep 自检全部为 0
- [ ] §5.2 e2e 飞书 dev bot 4 个场景通过（含群聊多 persona 和单聊单 persona）
- [ ] prod 部署后 1h 观察（§6.5）无异常
- [ ] 现状全部行为不变量（§4.2）保持
- [ ] DLQ 监控告警就位（plan 阶段确认）

**5b**:
- [ ] `grep "message_bridge\|emit_legacy_message" apps/` = 0
- [ ] `ls apps/agent-service/app/bridges/` 不存在
- [ ] `tests/life/test_proactive.py` 全绿
- [ ] proactive e2e 触发后 vectorize 跑通
