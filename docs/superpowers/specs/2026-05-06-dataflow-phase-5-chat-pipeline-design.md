# Dataflow Phase 5 — Chat 主 Pipeline 进 Graph

**状态**: Draft v1 (2026-05-06)
**前置**: PR #207 (Phase 4 life-engine / glimpse) shipped to prod 1.0.0.322
**后续**: Phase 6 清扫（旧 worker 入口、旧 ORM god object、bridge 残留 grep = 0）

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
                          [wire ChatRequest, durable]
                                          │
                                          ↓
                       chat_node @ agent-service
                          内部跑 agent.astream + handle_token + 段边界等 verdict
                                          │
                                          ↓ N × emit(ChatResponseSegment)
                              [wire ChatResponseSegment, durable]
                              ↓ Sink.mq("chat_response")
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

`chat/pipeline.py:stream_chat` 删除；`workers/chat_consumer.py` 整文件删除；`wiring/chat.py` 仅 2 条 wire 声明。

### 3.2 Data 类

**位置**: `apps/agent-service/app/domain/chat_dataflow.py`（新建，类比 `domain/life_dataflow.py`）

**`ChatRequest`**:
- 字段：`message_id: Annotated[str, Key]`, `session_id: str | None`, `persona_id: str | None`, `chat_id: str`, 以及 `chat_consumer.handle_chat_request` 现读的全部字段
- `Meta.transient = True`（不持久化；durable 边的 dedup 由 message_id Key 配合 runtime `insert_idempotent` 完成）
- 注：现 chat_request mq body 的具体字段在 plan 阶段从 `chat_consumer.py:55` 抓取后写入

**`ChatResponseSegment`**:
- 字段：`chat_id: str`, `message_id: Annotated[str, Key]`, `seq: Annotated[int, Key]`, `text: str`, `is_final: bool`，以及现 chat_response mq body 的所有字段
- `(message_id, seq)` 联合 Key 用于 dedup
- `Meta.transient = True`
- 注：现 chat_response mq body 的具体字段在 plan 阶段从 `chat_consumer.py:222` mq.publish 抓取后写入

### 3.3 `chat_node`（5a 的核心）

**位置**: `apps/agent-service/app/nodes/chat_node.py`（新建）

**签名**:
```python
@node
async def chat_node(req: ChatRequest) -> None:
    ...
```

**内部分四块（不拆 node，函数体内分块）**:

1. **fetch + parse + gray + persona**（照搬 `pipeline.py:78-97`）
2. **触发 pre-safety**（照搬 `pipeline.py:104-110`，Phase 2 已 ship 不动）：`asyncio.create_task(pre_safety_gate.run_pre_safety_via_graph(...))`
3. **跑 agent stream + 切段 emit**（这块新写）：
   ```python
   buffer = ""
   seq = 0
   async for token in _build_and_stream(...):
       pieces = handle_token(token, state)
       for p in pieces:
           if p == SPLIT_MARKER:
               text = await _maybe_replace_with_guard(buffer, pre_task, ...)
               await emit(ChatResponseSegment(
                   chat_id=req.chat_id,
                   message_id=req.message_id,
                   seq=seq,
                   text=text,
                   is_final=False,
               ))
               seq += 1; buffer = ""
           elif p is None:           # content_filter
               # 沿用现 pipeline.py 的 content_filter 处理
               ...
           elif p == "(后续内容被截断)":  # length truncation
               buffer += p
           else:
               buffer += p
   # final 段
   text = await _maybe_replace_with_guard(buffer, pre_task, ...)
   await emit(ChatResponseSegment(
       chat_id=req.chat_id,
       message_id=req.message_id,
       seq=seq,
       text=text,
       is_final=True,
   ))
   ```
4. **错误兜底**：函数体不包 try/except——失败 raise 由 durable consumer nack→DLQ（与 `run_glimpse_node` 一致）

**pre-safety 同步等待 = 段边界等**:
- 现 `_buffer_until_pre()` 是 token 边界等 verdict
- chat_node 改成段边界等：每到 SPLIT_MARKER（或 final）时如果 verdict 还没回来则 await（带原 timeout）；fail-open 时按现状用 guard message 替换
- 用户感知零差异：跨进程发送的最小单位本来就是段，token 边界等 / 段边界等用户都看不到 token

**复用 vs 重写**:

| 现有模块 | 处置 |
|---|---|
| `chat/stream.py:handle_token`, `StreamState`, `SPLIT_MARKER` | 保留，`chat_node.py` import |
| `chat/pipeline.py:_build_and_stream` | 保留 + 搬到 `nodes/chat_node.py` 同文件（让 `chat/pipeline.py` 整体删除） |
| `chat/pipeline.py:_buffer_until_pre` | 重写为 `_maybe_replace_with_guard`（段边界版，搬到 `chat_node.py`） |
| `chat/pipeline.py:stream_chat` | 删除 |
| `workers/chat_consumer.py` | 整文件删除 |
| `chat/__init__.py` 的 `from app.chat.pipeline import stream_chat` | 删除 |

### 3.4 wiring

**位置**: `apps/agent-service/app/wiring/chat.py`（新建）

```python
"""Chat 主 pipeline.

  mq(chat_request)  ─[durable]→  chat_node  ─N×emit→  ChatResponseSegment
                                                          │
                                                          ↓ [durable]
                                              Sink.mq("chat_response")
                                                          ↓
                                              lark-server / chat-response-worker → 飞书
"""
from app.domain.chat_dataflow import ChatRequest, ChatResponseSegment
from app.nodes.chat_node import chat_node
from app.runtime import Sink, Source, wire

wire(ChatRequest).from_(Source.mq("chat_request")).to(chat_node).durable()
wire(ChatResponseSegment).to(Sink.mq("chat_response")).durable()
```

`Source.mq` / `Sink.mq` 已现成（Phase 2 safety / Phase 1 vectorize 在用）。

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

### 4.1 错误处理

| 场景 | 行为 |
|---|---|
| `chat_node` 内部任意异常 raise | durable consumer nack → mq 重投 1 次 → 仍失败进 DLQ |
| mq 重投 chat_request | 重跑整条对话，可能让飞书收到部分重复段（现状同等行为，5a 不更糟） |
| `Sink.mq("chat_response")` publish 失败 | runtime 层 publish retry 兜底；连续失败 segment emit 抛 → chat_node raise → 全条对话 nack → mq 重投 |
| pre-safety timeout / emit 异常 / 外层 cancel | 沿用 `pre_safety_gate.run_pre_safety_via_graph` 已有的 fail-open（Phase 2 已 ship） |
| LLM 调用失败 | `_build_and_stream` 内现有兜底，不动 |

### 4.2 行为不变量（5a 验收）

1. 飞书单聊 / 群聊普通对话：分段时机、段数、段内容与现状逐字一致
2. tool 调用穿插：text → tool_call 边界仍注入 SPLIT_MARKER，导致飞书收到的"前半句话 + 后半句话"分两条独立消息（与现状一致）
3. pre-safety 命中：飞书只收到 guard message 一条，不会先回正常话再补 guard
4. content_filter / length truncation：飞书显示同样的预设提示
5. mq 重投：行为与现状一致（不更糟）
6. DLQ 入队消息可读，包含 message_id，便于人工排查

## 5. 测试策略

### 5.1 单元测试（5a，挡回归）

**位置**: `apps/agent-service/tests/nodes/test_chat_node.py`（新建）

用 fake `_build_and_stream`（注入预编排 token 序列）+ `capture_emit` 断言 emit 出的 Data 序列：
- 普通分段：3 段 token + 2 个 SPLIT_MARKER → 3 个 `ChatResponseSegment`，seq=0,1,2，最后 `is_final=True`
- pre-safety 拦截：fake pre_task 返回 BLOCK → 第一个 segment.text 是 guard message，is_final=True，无后续段
- content_filter 边界：断言 segment.text
- length truncation 边界：断言 segment.text 末尾带 "(后续内容被截断)"
- chat_node 内部 raise（fake `_build_and_stream` 抛异常）→ chat_node raise（不被 try/except 吞）

**位置**: `apps/agent-service/tests/wiring/test_chat_wiring.py`（新建）
- `compile_graph()` 不报错；wire 数量 + 类型正确

**位置**: `apps/agent-service/tests/dataflow/test_chat_node_durable.py`（新建）
- mq 重投同一 ChatRequest → chat_node 被调两次、emit 行为可重复（业务上重跑 LLM）

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

- chat_node 跑到一半被杀 → 已 publish 的段保留在 chat_response queue → 未跑完段不再发 → durable mq 重投 chat_request → 新 Pod 重跑整条对话 → 飞书可能看到前 N 段重复 + 完整重跑的 N+M 段
- 现状同等行为，5a 不更糟。但部署前按项目铁律 2 确认无活跃 chat（飞书安静窗口部署）

### 6.3 回滚

- **5a 回滚**: revert PR + 重新 release。可行的硬约束保证：`ChatRequest` 与 `ChatResponseSegment` 的 mq body schema 与现 chat_consumer 完全等价，回滚后老代码可以消费 5a 时代留下的 chat_request queue 消息
- **5b 回滚**: revert PR；`proactive.py` 那一行 emit Message 改回 emit_legacy_message，bridges 文件 git revert 恢复

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

- mq 重投导致段重复发飞书 → lark-server 侧 dedup 提到全局 message_id，跨服务改造
- agent tool 副作用进 wire（commit_abstract_memory → emit AbstractMemorySaved）→ Phase 6 工作（源 spec 明确"本轮维持开放"）
- "chat 重跑"语义改进（断点续传，避免完整重串 LLM）→ 长期，需要 chat_node 状态持久化
- chat_node 内部更细的拆分（如出于可测试性需要）→ 长期

## 8. 验收清单（汇总）

**5a**:
- [ ] `compile_graph()` 通过
- [ ] §5.1 单元测试全绿
- [ ] §5.4 grep 自检全部为 0
- [ ] §5.2 e2e 飞书 dev bot 4 个场景通过
- [ ] prod 部署后 1h 观察（§6.5）无异常
- [ ] 现状全部行为不变量（§4.2）保持

**5b**:
- [ ] `grep "message_bridge\|emit_legacy_message" apps/` = 0
- [ ] `ls apps/agent-service/app/bridges/` 不存在
- [ ] `tests/life/test_proactive.py` 全绿
- [ ] proactive e2e 触发后 vectorize 跑通
