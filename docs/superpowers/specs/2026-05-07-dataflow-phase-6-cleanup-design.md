# Dataflow Phase 6 — 清扫（修订版）

**状态**: Draft v3 (2026-05-07，吸收 reviewer blocking)
**v3 关键变化（vs v2）**：
- §2.3 推翻 v2 "整段删 try/except"，改为**保留 catch 但升级监控**。reviewer blocking 命中要害：`glimpse.py:228-242` 在 submit 之前已经 insert_fragment + enqueue_fragment_vectorize，submit 之后 insert_glimpse_state（line 291-299）；`wiring/life_dataflow.py:77` 是 `GlimpseRequest.durable()`，`durable.py:137-145` 先 insert_idempotent 后跑 consumer，DLQ 重放会被 dedup 跳过 → glimpse fail 后 last_seen 不推进 → 下次 cron tick 同样 messages 走一遍 → fragment_id `new_id("f")` 每次新 → **重复 fragment + 重复 vectorize**。删 try 引入数据脏化代价远大于"主动消息观测性升级"的收益。修法：保留 try/except，`logger.error` → `logger.exception` 拿 traceback；`state_observation` 拼接 `[chat_submit_failed]` 标记（与现有 `[want_to_speak:throttled]` 同款）。失败可 Loki 关键字告警，glimpse_state 仍落盘。修 reviewer v2 blocking
- §4 新增测试断言：`app.data.queries.__all__` 是 7 个 domain `__all__` 的并集 + 与硬编码已知函数清单（spec §3.3 全部 67 函数）比对完全相等 + 无重复名字（reviewer #5）。`from X import *` 重名是后者覆盖不报错，仅靠 ruff/mypy 不可靠 —— v2 §3.4 那句删除/订正
- §2.5 风险描述同步：v2 写"glimpse 删 try 后失败可观测性提升"已不成立，整条删除。新增：保留 catch 后旧的"边界异常被吞"行为不变，但 `logger.exception` + state_observation 标记让 Loki 关键字告警可建。

**v2 关键变化（vs v1）**：
- §2.3 / §2.4 第一刀同时**收窄 glimpse.py:277-287 的 try/except**：旧路径下 `submit_proactive_chat()` 走 fire-and-forget mq.publish + glimpse 业务层 catch 吞错；新路径下 emit 走 in-process，route_chat_node 任何异常会上抛到 submit 调用点。如果保留 glimpse 的 catch，错会被业务层吞掉，**比旧路径 mq source 进 DLQ 的可观测性退化**。第一刀必须同时改 glimpse 让异常正常传播。修 reviewer #1
- §4 新增 placement 硬验收：`route_chat_node in nodes_for_app("agent-service")` + `chat_node in nodes_for_app("agent-service")`。`emit.py:96` 按 APP_NAME 过滤 in-process consumer，当前 `deployment.py` 没显式 bind chat node 所以 fall through 到 default app（agent-service）；未来一加 bind 会静默跳过 fan-out，必须有测试兜底。修 reviewer #2
- §3.3 函数归类调整：新增 `agent_response.py`（4 函数：set_agent_response_bot / is_chat_request_completed / get_safety_status / set_safety_status，主表都是 `agent_responses`，原 v1 错放 messages.py）；`find_gray_config` 从 persona.py 移到 messages.py（主表 LarkBaseChatInfo 是 chat 维度，与 message 共享 chat_id 主键，不是 BotPersona 表）。Domain 数 6 → 7。修 reviewer #3
- §4 验收清单测试路径修正：`tests/wiring/test_chat_pipeline*.py` → 实际 `tests/wiring/test_chat_wiring.py` + `tests/nodes/test_route_chat_node.py` + `tests/nodes/test_chat_node.py`；`tests/unit/life/test_proactive.py` 第一刀后断言改成"先 emit Message 再 emit ChatTrigger"，不再 patch `mq.publish`。修 reviewer #4
**前置**: PR #209 (Phase 5b chat pipeline + bridges 清扫) shipped to prod 1.0.0.328
**后续**: 无（dataflow 重构主线收尾）

## 0. 修订说明：4 刀 → 2 刀

`memory/project_dataflow_phase6_scope.md` 原计划 4 刀清扫。Phase 6 启动时核对实现细节，发现 4 刀里有两刀**前提不成立**，本期降级为 2 刀（**第一刀 + 第三刀**）。

| 原刀 | 原计划 | 实际状态 | 结论 |
|---|---|---|---|
| 第一刀 | `proactive.py` mq.publish → emit ChatTrigger | publisher / consumer 同进程，emit in-process 命中 | ✅ 本期做 |
| 第二刀 | `vectorize_memory.py` mq.publish → emit Data | publisher（agent-service）和 consumer（vectorize-worker）跨进程；`runtime/emit.py:54` 不会反查 wire 的 `Source.mq(...)` 自动 publish 到 mq queue —— emit 当前**不支持跨进程**，硬改会让消息丢失 | ❌ 推迟到 Phase 6.5（runtime emit 跨进程能力升级） |
| 第三刀 | `data/queries.py` 1283 行拆 6 domain | 纯机械拆，无业务变化 | ✅ 本期做 |
| 第四刀 | `workers/state_sync_worker.py` 死代码删除 | `update_schedule.py:38-42` 通过 arq enqueue `"sync_life_state_after_schedule"` 字符串名，`arq_settings.py:88` 注册它为 worker function —— 两个文件都活的，**没有死代码** | ❌ 取消 |

## 1. 背景与动机

Phase 0–5b 全部 ship 后，按 `2026-04-21-agent-dataflow-abstraction-design.md` 的目标对齐扫了一遍 `apps/agent-service`，剩两处仍偏离设计目标：

1. **`life/proactive.py:153`** —— Glimpse 决定主动开口时，proactive submit 走的是裸 `mq.publish(CHAT_REQUEST, ...)`，没收敛到 Phase 5a 的 `ChatTrigger` Data 入口。chat 主入口（lark-server publish）和 proactive 入口（agent-service 内部 submit）应统一走同一个 Data，否则 `route_chat_node` 不是单一入口，dataflow 抽象漏了一个口子。

2. **`data/queries.py` 1283 行** —— 跨 6 个 domain（Model Provider / Persona / Messages / Schedule / Life / Memory v4）的 god module，违反 CLAUDE.md "单文件 <300 行 + 单一职责"。每次改 memory v4 查询都要在巨长文件里翻，新人理解成本高。

**业务收益**: 无。纯架构清扫。
**工程收益**:
- chat 入口收敛到单一 `ChatTrigger` Data（route_chat_node 唯一入口）
- `data/queries.py` 拆 6 个 domain 文件，单文件可读、单 domain 单一职责
- `grep "mq.publish" apps/agent-service/app/` 减少一处（proactive）

## 2. 第一刀 — proactive emit ChatTrigger

### 2.1 现状

`apps/agent-service/app/life/proactive.py:95-174` `submit_proactive_chat()`：

```python
async def submit_proactive_chat(chat_id, persona_id, target_message_id, stimulus) -> str:
    # ... 1. resolve target、bot_name、构造 synthetic ConversationMessage
    async with get_session() as session:
        msg = ConversationMessage(...)
        session.add(msg)
    # 2. emit Message（已在 graph 里）
    from app.domain.message import Message
    from app.runtime import emit
    await emit(Message.from_cm(msg))

    # 3. publish 到 chat_request mq queue ← 本刀目标
    from app.infra.rabbitmq import current_lane
    lane = current_lane()
    await mq.publish(CHAT_REQUEST, {
        "session_id": session_id, "message_id": message_id, "chat_id": chat_id,
        "is_p2p": False, "root_id": target_lark_id or "", "user_id": PROACTIVE_USER_ID,
        "bot_name": bot_name, "is_proactive": True, "lane": lane, "enqueued_at": now_ms,
    })
    return session_id
```

### 2.2 改造

第 3 步替换为：

```python
from app.domain.chat_dataflow import ChatTrigger
from app.infra.rabbitmq import current_lane

await emit(ChatTrigger(
    message_id=message_id,
    session_id=session_id,
    chat_id=chat_id,
    is_p2p=False,
    root_id=target_lark_id or None,  # ChatTrigger.root_id 是 Optional，传 None 而非空字串
    user_id=PROACTIVE_USER_ID,
    bot_name=bot_name,
    is_proactive=True,
    lane=current_lane(),
    enqueued_at=now_ms,
))
```

文件顶部 `from app.infra.rabbitmq import CHAT_REQUEST, mq` 删除（如 `mq` 没有其它使用）。

### 2.3 同时修：升级 glimpse 的异常监控（保留 catch）

**reviewer #1 + reviewer v2 blocking 共同触发的改动，必须随第一刀同步落地**。

`apps/agent-service/app/life/glimpse.py:277-287` 现状：

```python
try:
    await submit_proactive_chat(chat_id=..., persona_id=..., target_message_id=..., stimulus=...)
except Exception as exc:
    logger.error("[%s] Glimpse proactive submit failed: %s", persona_id, exc)
```

#### 旧路径（mq.publish fire-and-forget）

submit 内部 publish chat_request 队列，本进程几乎不会抛错。route_chat_node 实际失败发生在远端 mq source loop（agent-service 主进程 chat_request 消费），失败 → DLQ。glimpse 这一帧不感知，catch 只兜底极少数 mq broker 不可达 / DB insert 失败。

#### 新路径（emit ChatTrigger）

emit 走 in-process 直接调 `route_chat_node`（`emit.py:8-11`：in-process dispatch 是 strict，consumer raise 会上抛）。route_chat_node 任何异常 → 上抛到 submit_proactive_chat → 上抛到 glimpse 的 catch（如果保留）。

#### 为什么不能直接删 catch（reviewer v2 blocking）

`glimpse.py` 的副作用顺序（line 228-242, 277-287, 291-299）：

```
  6. 写 fragment + enqueue_fragment_vectorize  ← 已有副作用
  7. submit_proactive_chat  ← 本刀目标
  8. insert_glimpse_state（推进 last_seen）  ← 在 submit 之后
```

`wiring/life_dataflow.py:77` `wire(GlimpseRequest).to(run_glimpse_node).durable()`；`durable.py:137-145` 先 `insert_idempotent` 后跑 consumer，**DLQ 重放会被 dedup 跳过**。

如果删 catch、让 emit 异常一路上抛：
- route_chat_node 抛错 → glimpse_node 抛错 → DLQ 进 GlimpseRequest 的 durable DLQ
- DLQ 重放该消息时 idempotent 行已存在 → 跳过 consumer，无效
- **last_seen 没推进**：下次 cron tick 因 last_seen 仍旧，重新查 unseen messages → 再走 step 6 insert 新 fragment（`new_id("f")` 每次新）+ 再 enqueue vectorize → **fragment 行数随失败次数线性膨胀，vectorize 队列对应膨胀**。
- 数据脏化的代价 >> proactive 主动消息可观测性升级的收益。

#### 修法：保留 catch + 升级监控

```python
try:
    await submit_proactive_chat(...)
except Exception as exc:
    logger.exception(  # 改成 exception 拿到 traceback
        "[%s] Glimpse proactive submit failed: %s", persona_id, exc
    )
    state_observation = (
        f"{state_observation}\n[chat_submit_failed] reason={type(exc).__name__}"
    )
```

要点：
- `logger.error` → `logger.exception`：自动带 traceback，便于 Loki 排查
- `state_observation` 拼接 `[chat_submit_failed]` 标记：与现有 `[want_to_speak:throttled]`、`[no_speak]` 同款风格，glimpse_state 历史里可追溯哪一帧 submit 失败
- 不引入 metrics 基础设施（项目目前无统一 metrics 框架；Loki 关键字 `chat_submit_failed` 可建告警）
- step 8 `insert_glimpse_state` 仍执行：last_seen 推进、fragment 不重复

**这与旧路径 mq.publish 的 fire-and-forget 语义不严格等价**（旧路径远端 DLQ 仍兜底 route_chat_node 失败；新路径 route_chat_node 失败被本进程 catch 吞）—— **本期显式接受这一可观测性差异**：换得"chat 入口收敛到单一 ChatTrigger Data"的架构收益，且新增 `[chat_submit_failed]` 标记 + traceback 让失败仍然可见可追溯，工程上可接受。

### 2.4 行为不变量

- proactive trigger 仍能让 `route_chat_node` fan-out per-persona ChatRequest → chat_node
- emit 走 in-process 路径（proactive 调用方 = glimpse_node 在 agent-service 主进程；route_chat_node 也在主进程）；与 lark-server 跨进程 publish chat_request 走 mq 路径殊途同归（runtime mq source loop 解码 body → ChatTrigger 后调用同一个 `route_chat_node`）。
- `ChatTrigger.message_id` 是 `Annotated[str | None, Key]`，proactive 生成的 `f"proactive_{ts}"` 满足约束。
- `ChatTrigger.root_id` 当前是 `str | None`；原 mq.publish 用空字串 `""`，emit 改成 `None` 与字段类型一致。下游 `route_chat_node` 对 root_id 的处理需要在 plan 阶段验证 None 与 "" 行为等价（grep `root_id` 在 chat_dataflow / nodes / chat 路径的所有用法）。
- chat submit 失败 → glimpse `catch` → `logger.exception` + state_observation 标 `[chat_submit_failed]` → glimpse_state 仍落盘、last_seen 推进、fragment 不重复。Loki 关键字告警可建。

### 2.5 风险

- `ChatRequest` 走的是 `.durable()` wire（Phase 5a），所以 emit ChatTrigger → in-process route_chat_node → emit ChatRequest（durable）这条链路里，第二跳是异步 publish 到 mq，glimpse 进程不会等 chat_node 跑完才返回，与原 mq.publish 异步语义一致。
- 保留 catch 后，旧路径下"远端 mq source loop DLQ 兜底 route_chat_node 失败"的可观测性丢失（新路径下被 glimpse 吞掉，仅 logger.exception + state_observation 标记）。这是**显式接受的折衷**：换"chat 入口收敛到单一 ChatTrigger Data"的架构收益。如果未来要恢复 DLQ 语义，需要重排 glimpse 副作用顺序（先 submit 后 insert_fragment）+ 加补偿，超出 Phase 6 清扫范畴。

## 3. 第三刀 — queries.py 拆 6 个 domain

### 3.1 现状

`apps/agent-service/app/data/queries.py` 1283 行 / 50+ async def，跨 6 个 domain 的查询。调用方约 44 个文件 import 它（`grep -rln "from app.data.queries\|from app.data import queries" apps/agent-service/`）。

### 3.2 目标结构

```
apps/agent-service/app/data/
  queries/
    __init__.py        # 重导出全部 public 函数（保持调用方零改动）
    model_provider.py  # 3 函数
    persona.py         # 6 函数
    messages.py        # 11 函数
    agent_response.py  # 4 函数（agent_responses 表的全部 read/write）
    schedule.py        # 11 函数
    life.py            # 8 函数
    memory.py          # 30+ 函数
```

`queries.py` 文件本身**删除**（不留任何 re-export shim）。`__init__.py` 是 package 标准用法（不算兼容层）。

### 3.3 函数归类（按操作的表归 domain，不按调用方）

| domain | 函数 |
|---|---|
| **model_provider** | `parse_model_id` / `find_model_mapping` / `find_provider_by_name` |
| **persona** | `find_persona` / `list_all_persona_ids` / `resolve_persona_id` / `resolve_bot_name_for_persona` / `resolve_mentioned_personas` / `find_bot_names_for_persona` |
| **messages** | `find_cross_chat_messages` / `find_message_content` / `find_messages_in_range` / `find_username` / `find_group_name` / `find_group_download_permission` / `find_message_by_id` / `resolve_message_id_by_row_id` / `find_last_bot_reply_time` / `find_context_messages_for_anchors` / `find_group_members` / `find_gray_config` |
| **agent_response** | `set_agent_response_bot` / `is_chat_request_completed` / `get_safety_status` / `set_safety_status` |
| **schedule** | `find_active_schedules_for_date` / `find_latest_plan` / `find_plan_for_period` / `find_daily_entries` / `list_schedules` / `upsert_schedule` / `delete_schedule` / `insert_schedule_revision` / `get_current_schedule` / `get_schedule_revision_by_id` / `list_recent_schedule_revisions` |
| **life** | `find_latest_life_state` / `insert_life_state` / `find_today_activity_states` / `find_life_states_in_range` / `find_latest_glimpse_state` / `insert_glimpse_state` / `insert_reply_style` / `find_latest_reply_style` / `list_recent_life_states` |
| **memory** | `list_today_fragments` / `find_fragments_since` / `get_fragment_by_id` / `get_abstract_by_id` / `insert_fragment` / `touch_fragment` / `get_fragments_by_ids` / `touch_fragments_bulk` / `insert_abstract_memory` / `touch_abstract` / `touch_abstracts_bulk` / `count_abstracts_by_persona` / `insert_memory_edge` / `insert_note` / `get_active_notes` / `resolve_note` / `update_abstract_content_query` / `set_clarity` / `delete_fragment_query` / `delete_edge` / `list_fragments_window` / `list_abstracts_window` / `list_edges_to` / `list_edges_from` / `get_abstracts_by_subject` / `get_abstracts_by_subjects` / `get_recent_abstract_titles` / `count_abstracts_per_subject_prefix` / `get_recent_fragments_for_injection` |

**归类原则**：函数操作哪张/哪组表，就归到对应 domain。例：
- `list_today_fragments` 在 life 模块被使用，但操作 `fragments` 表 → 归 memory.py。
- `find_gray_config` 通过 message_id JOIN 到 `LarkBaseChatInfo` 取 chat 灰度配置；主表是 chat info 不是 BotPersona → 归 messages.py（chat 维度，与 message 共享 chat_id 主键，不开新 chat_config domain 因为只有 1 个函数）。
- `set_agent_response_bot` / `is_chat_request_completed` / `get_safety_status` / `set_safety_status` 主表都是 `agent_responses`（即使 `is_chat_request_completed` 在 proactive 分支也查 ConversationMessage，主体语义仍是"chat request 完成判定"属 agent_response 域）→ 独立 `agent_response.py`，不混进 messages.py。
- `find_group_download_permission` 查 group 配置（共享 ConversationMessage 上下文）→ 归 messages.py（不开新 group domain）。

### 3.4 `__init__.py` 写法

```python
"""Data queries — split per domain. 调用方 import 不变 (`from app.data.queries import X`)."""
from app.data.queries.agent_response import *  # noqa: F401,F403
from app.data.queries.life import *  # noqa: F401,F403
from app.data.queries.memory import *  # noqa: F401,F403
from app.data.queries.messages import *  # noqa: F401,F403
from app.data.queries.model_provider import *  # noqa: F401,F403
from app.data.queries.persona import *  # noqa: F401,F403
from app.data.queries.schedule import *  # noqa: F401,F403
```

每个 domain 文件用 `__all__` 显式列 export，`__init__.py` 用 `from X import *` 收口。注意 `from X import *` 重名时**后者覆盖、不报错**，所以重名检测靠测试断言，不靠 ruff/mypy（见 §4 验收里"`__all__` 完整 + 无重名"测试）。

### 3.5 行为不变量

- 调用方 0 改动：`from app.data.queries import find_message_content` 等 44 个 import site 全部仍 work。
- 函数签名 / 行为 / SQL 0 变化（纯文件搬运）。
- 测试 0 改动：`tests/unit/data/test_queries*.py` 等 import path 不变。

### 3.6 行数预估 + 拆超的应对

按 def 行号粗算：

- model_provider: ~32 行
- persona: ~70 行（移走 `find_gray_config` 后）
- messages: ~210 行（移走 4 agent_response 函数 + 移入 `find_gray_config` 后）
- agent_response: ~80 行（4 函数从 messages 抽出）
- schedule: ~200 行
- life: ~140 行
- memory: ~440 行 ⚠️ **超 300 行**

memory.py 超 300 行时，再拆为：
- `memory.py`（fragment / abstract 主表 CRUD + read，约 250 行）
- `memory_edges.py`（edges + notes 子表，约 100 行）
- `memory_search.py`（list_*_window / get_abstracts_by_* / count_* / get_recent_* 这些查询助手，约 150 行）

具体拆点在执行 plan 阶段量行决定，spec 不强行钉死边界。`__init__.py` 跟着加 `from .memory_edges import *` / `from .memory_search import *` 即可。

## 4. 验收标准

### 第一刀
- `grep -n "mq.publish" apps/agent-service/app/life/proactive.py` 无命中
- `grep -n "try:" apps/agent-service/app/life/glimpse.py` 不再包含 submit_proactive_chat 周边的 try/except（§2.3 收窄）
- `grep -rn "mq.publish" apps/agent-service/app/` 命中数 = main 命中数 - 1
- 飞书 dev bot 群聊 + 单聊 e2e 通过；glimpse 触发 proactive → 主动消息正常发出
- **placement 硬验收**（reviewer #2）：新加测试断言 `route_chat_node in nodes_for_app("agent-service")` 且 `chat_node in nodes_for_app("agent-service")`，加在 `tests/wiring/test_chat_wiring.py`
- `tests/unit/life/test_proactive.py` 改造：删除 `patch("app.infra.rabbitmq.mq.publish", ...)`，改成断言 `submit_proactive_chat` 内 emit 顺序为 `Message → ChatTrigger`（顺序正确性是 §2.2 的契约）
- `tests/unit/life/test_proactive.py` + `tests/unit/life/test_glimpse.py` + `tests/wiring/test_chat_wiring.py` + `tests/nodes/test_route_chat_node.py` + `tests/nodes/test_chat_node.py` green

### 第三刀
- `apps/agent-service/app/data/queries.py` 文件不存在
- `apps/agent-service/app/data/queries/` package 存在，每个文件 < 300 行（memory 若超就细拆，最终所有文件 < 300）
- `from app.data.queries import X` 在 44 个 import site 全部仍 import success（`pytest --collect-only` 通过）
- `find apps/agent-service/app/data/queries -type f -name "*.py" -exec wc -l {} +` 总行数 ≈ 1283 ± 5%（允许 import / module docstring 重复带来的小幅膨胀）
- 调用方文件无任何改动（`git diff --stat HEAD~1 -- apps/agent-service/app/` 中调用方 0 命中）
- **`__all__` 完整 + 无重名硬验收**（reviewer v2 #5）：新加测试 `tests/unit/data/test_queries_split.py`，断言两条不变量：
  1. `app.data.queries.__all__` 与硬编码已知函数清单（spec §3.3 列的 67 函数）集合**完全相等**（无遗漏、无多余）。
  2. 7 个 domain 文件的 `__all__` 两两交集为空（无重名）—— `from X import *` 重名后者覆盖不报错，测试是唯一保险。
  实现示意：
  ```python
  EXPECTED_FUNCTIONS = {  # 硬编码 spec §3.3 全 67 项
      "parse_model_id", "find_model_mapping", ...
  }
  def test_queries_all_complete():
      from app.data import queries
      assert set(queries.__all__) == EXPECTED_FUNCTIONS
  def test_queries_no_duplicate_names():
      from app.data.queries import (agent_response, life, memory, messages,
                                     model_provider, persona, schedule)
      modules = [agent_response, life, memory, messages, model_provider, persona, schedule]
      seen: dict[str, str] = {}
      for m in modules:
          for name in m.__all__:
              assert name not in seen, f"{name} in both {seen[name]} and {m.__name__}"
              seen[name] = m.__name__
  ```

### 整体
- `compile_graph()` 通过
- 全量 `pytest apps/agent-service/` green
- `ruff check apps/agent-service/` 与 main 一致 0 新增
- 飞书 dev 泳道 e2e 通过

## 5. 实施切法

两刀打**同一个 PR**，分两个 commit（按时间顺序、各自可独立 verify）：

1. **commit 1**: 第一刀 proactive emit ChatTrigger（小，先合并）
2. **commit 2**: 第三刀 queries.py 拆 package（大但纯机械）

PR title: `Phase 6 — proactive ChatTrigger emit + queries.py domain split`

部署铁律：**先 dev 泳道验证再上 prod**。

## 6. Out of Scope

- **第二刀（vectorize emit）**：runtime emit 跨进程能力（emit Data → 反查 wire 的 `Source.mq(...)` → 当 consumer 不在本进程时自动 publish 到 mq queue）—— 单独立项 Phase 6.5
- **第四刀（workers/ 死代码）**：state_sync_worker / arq_settings 都是活的（被 update_schedule tool 使用），无清扫空间
- agent tool 副作用进 wire（commit_abstract → emit AbstractMemorySaved 等）—— Phase 7+
- chat 部分段后中断、新 Pod 不续传 —— 长期 epic
- ka 群 P0/P1 体验问题 —— 与 dataflow 无关，独立线
