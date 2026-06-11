# 重写赤尾 life / world engine：现状实证笔记

> 两次 Explore 核实的确切结构，供写"量化设计"用。事实截至 2026-06-03，动手前仍需自己再核一遍（代码会变）。

## 卡死根因
- `life/engine.py:204-208`：cron 每分钟拉，LLM 拍一个带 `state_end_at` 的大状态，没到期就 `return None` 干等，中途 event 进不来。

## 外部刺激（消息）侧
- **`ChatTrigger`**（`domain/chat_dataflow.py:16-41`）：`channel`, `message_id`, `chat_id`, `session_id`, `is_p2p`(bool), `user_id`, `bot_name`, `persona_ids`(list[str]，channel-server 已解析的 @提及), `is_proactive`(bool), `root_id`, `lane`, `enqueued_at`。
- **`ChatRequest`**（同文件:43-67）：route 后 per-persona，加 `persona_id`。
- **"指向她"判定 = `MessageRouter.route()`**（`chat/persona_filter.py:21-56`）：`is_p2p` 或 `is_proactive` → 回（resolved persona）；群里 `persona_ids` 非空 → 回这些；否则 `[]`（群里没点名不回）。channel-server 提前把 @ 解析成 `persona_ids`。
- **`CommonMessage`**（`data/models.py:83-118`，SQLAlchemy）：`common_message_id`(UUID), `channel`, `common_conversation_id`(UUID,=会话), `common_user_id`, `sender_display_name`, `role`, `content`(jsonb，含 mentions), `content_text`, `scope`("direct"/"group"), `message_type`, `bot_name`, `event_time`(ms,bigint), `created_at` 等。读模型 `CommonMessageRecord` 把 scope→chat_type("p2p"/"group")。
- 消息第一站：`MQ(chat_request)` → `ChatTrigger`(transient) → `route_chat_node`(`nodes/chat_node.py:40`) → `ChatRequest`(durable) → `chat_node`。**当前没有现成的"某会话有新消息"广播事件供 life 订阅**（要新接）。

## 思考核心 / 状态 / 自调度侧
- **`Agent.run(messages: list[Message], *, prompt_vars: dict|None, context: AgentContext|None, max_retries: int) -> Message`**（`agent/core.py:498`）。另有 `stream`(→AsyncGenerator[StreamChunk]) / `extract`(→BaseModel)。
- `AgentConfig(prompt_id, model_id, trace_name=None, recursion_limit=6)`。
- `Message(role: Role, content, reasoning_content, tool_calls, tool_call_id)`；`Role`: SYSTEM/USER/ASSISTANT/TOOL（`agent/neutral.py`）。`ToolDef(name, description, parameters)`、`ToolCall(id, name, arguments, signature)`。
- life 现 `prompt_vars` 11 键（`life/engine.py:157-169`）：persona_name, persona_lite, current_time, current_state, activity_type, activity_duration, response_mood, schedule, activity_timeline, recent_experiences, prev_state_end_at。
- **`LifeEngineState`**（`data/models.py:259-278`，SQLAlchemy）：persona_id, current_state, activity_type, response_mood, reasoning, skip_until, state_end_at, created_at。
- **对外读取的最小字段集**：`context._build_life_state` 取 current_state + response_mood + state_end_at；`voice` 取 current_state + response_mood；`reviewer/heavy` 取 activity_type + current_state + response_mood + created_at。→ 新快照对外须提供 **current_state / response_mood / activity_type（+ 时间）**。
- `find_latest_life_state(persona_id)` / `insert_life_state(...)`（`data/queries/life.py:27-63`）。
- 框架快照：`select_latest(cls, {key:val})`（`runtime/persist.py:154`）、`insert_append`（`persist.py:59`）、`wire().as_latest()` / `with_latest()`（`runtime/wire.py:105/131`）。**as_latest Data 约束**：Data 子类 + Key 字段(metadata=[Key]) + 可选 Version 字段(metadata=[Version]) + frozen + JSON-serializable。
- **自调度**：`emit_delayed(data: Data, *, delay_ms: int, durability="durable")`（`runtime/emit.py:306`，上限 ~24 天，durable 重启不丢）、`emit_at(data, *, when, durability)`（`emit.py:404`）。投递的 Data 须 frozen + ≥1 Key + JSON-serializable。

## 删除影响面（caller coverage）
- 她的状态被读：`chat/agent_stream` → `memory/context._build_life_state`、`memory/voice`、`memory/reviewer/heavy`。
- 日程读：`memory/context`(`build_schedule_section`→`get_current_schedule`)、`memory/voice`。
- 日程写隐藏入口：主对话 `ALL_TOOLS` 的 `update_schedule`(emit `ScheduleRevisionCreated`)、admin schedule 路由。
- `LifeStateChanged`：仅 `glimpse_event_node` 消费。
- glimpse / proactive：自洽，proactive 仅 glimpse 调，下游 `ChatTrigger`。
- **voice + light/heavy reviewer 的 cron 也在 `life_dataflow` wiring 里，但要保留**（非本次删除）。删除须精确到 wire/cron，不按文件整删。
- 相关表均 SQLAlchemy Base，无别的服务依赖。
