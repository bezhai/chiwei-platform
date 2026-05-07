# Dataflow Phase 6 Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `proactive.py` 的 mq.publish 收敛到 `emit(ChatTrigger)` 让 chat 入口单一化，并把 1283 行的 `data/queries.py` 拆成 7 个 domain 文件让单文件 < 300 行。

**Architecture:** 第一刀：`proactive.submit_proactive_chat()` 第三步从 `mq.publish(CHAT_REQUEST, body)` 替换成 `emit(ChatTrigger(...))`，配套升级 `glimpse.py` 的 try/except 监控（保留 catch + `logger.exception` + state_observation 标记）。第三刀：`apps/agent-service/app/data/queries.py` 单 module 改成 `apps/agent-service/app/data/queries/` package（7 domain），`__init__.py` 用 `from X import *` 重导出保持 44 个调用方 import path 零改动。

**Tech Stack:** Python 3.12 / pytest-asyncio / SQLAlchemy AsyncSession / 项目自有 dataflow runtime（`app.runtime.emit`、`Source.mq`、`wire`、`bind`）。

**前置 spec：** `docs/superpowers/specs/2026-05-07-dataflow-phase-6-cleanup-design.md`（v3）

---

## Task 顺序与提交策略

5 个 task 打**同一个 PR**，按下序合并到 `refactor/flow-parse-6` 分支：

| Task | 主题 | 改动量 | 风险 |
|---|---|---|---|
| 1 | glimpse 升级监控 | ~10 行 | 极低 |
| 2 | chat placement 硬验收 | ~30 行 test | 0（纯测试） |
| 3 | proactive emit ChatTrigger | ~30 行 + 测试改写 | 低 |
| 4 | queries.py 拆 7 domain（含 test_queries_split） | 大但纯机械 | 低（调用方 0 改动） |
| 5 | 部署 dev 泳道 + e2e 验证 | 0 代码 | 中 |

**每个 task 完成后 commit。Task 1-4 完成后开 PR；Task 5 部署验证通过后才 ship。**

---

## Task 1: glimpse 升级监控（保留 catch）

**Files:**
- Modify: `apps/agent-service/app/life/glimpse.py:277-287`
- Modify: `apps/agent-service/tests/unit/life/test_glimpse.py`（如已有 want_to_speak 测试就改造，否则新增一个失败路径测试）

### 子步

- [ ] **Step 1.1: 找现有 want_to_speak 路径测试**

Run: `grep -n "want_to_speak\|submit_proactive_chat" apps/agent-service/tests/unit/life/test_glimpse.py`
预期：找到至少一个 `submit_proactive_chat` patch 的测试。

- [ ] **Step 1.2: 加失败路径测试 — submit raise 时 state_observation 标记 [chat_submit_failed] 且 glimpse_state 仍落盘**

文件：`apps/agent-service/tests/unit/life/test_glimpse.py`，加一个测试函数：

```python
@pytest.mark.asyncio
async def test_run_glimpse_chat_submit_failure_marks_state_and_persists():
    """submit_proactive_chat raise 时:
      - logger.exception 记 traceback
      - state_observation 拼接 [chat_submit_failed]
      - insert_glimpse_state 仍执行，last_seen 推进
    """
    from app.life.glimpse import run_glimpse

    decision = {
        "want_to_speak": True,
        "speak_reason": "user mentioned",
        "stimulus": "我想接一句",
        "target_message_id": None,
        "observation": "群里在聊摄影",
    }

    inserted_states: list[dict] = []

    async def _fake_insert_glimpse_state(s, **kwargs):
        inserted_states.append(kwargs)

    with (
        patch(f"{MODULE}.find_unseen_messages", AsyncMock(return_value=[])),
        patch(f"{MODULE}._observe", AsyncMock(return_value=decision)),
        patch(f"{MODULE}.insert_fragment", AsyncMock()),
        patch(f"{MODULE}.enqueue_fragment_vectorize", AsyncMock()),
        patch(f"{MODULE}.submit_proactive_chat", AsyncMock(side_effect=RuntimeError("boom"))),
        patch(f"{MODULE}.Q.insert_glimpse_state", _fake_insert_glimpse_state),
        patch(f"{MODULE}.get_recent_proactive_records", AsyncMock(return_value=[])),
        patch(f"{MODULE}.get_session", return_value=_mock_session_cm(MagicMock())),
    ):
        await run_glimpse("akao-001", "oc_test")

    assert inserted_states, "insert_glimpse_state must still run after submit failure"
    obs = inserted_states[-1]["observation"]
    assert "[chat_submit_failed]" in obs, f"missing failure tag in {obs!r}"
    assert "RuntimeError" in obs, f"missing exception type tag in {obs!r}"
```

注：测试里 patch 对象的具体路径（`{MODULE}.find_unseen_messages` 等）按 test_glimpse.py 已有 patch 风格走；如已有 fixture/帮助器就复用，不要新建。

- [ ] **Step 1.3: 跑测试验证它失败**

Run: `cd apps/agent-service && uv run pytest tests/unit/life/test_glimpse.py::test_run_glimpse_chat_submit_failure_marks_state_and_persists -v`
预期：FAIL，因为现有 except 块只 `logger.error` + state_observation 不带 `[chat_submit_failed]`。

- [ ] **Step 1.4: 改 `glimpse.py:277-287`**

把这段：

```python
            try:
                await submit_proactive_chat(
                    chat_id=chat_id,
                    persona_id=persona_id,
                    target_message_id=target,
                    stimulus=stimulus,
                )
            except Exception as exc:
                logger.error(
                    "[%s] Glimpse proactive submit failed: %s", persona_id, exc
                )
```

改为：

```python
            try:
                await submit_proactive_chat(
                    chat_id=chat_id,
                    persona_id=persona_id,
                    target_message_id=target,
                    stimulus=stimulus,
                )
            except Exception as exc:
                logger.exception(
                    "[%s] Glimpse proactive submit failed: %s", persona_id, exc
                )
                state_observation = (
                    f"{state_observation}\n[chat_submit_failed] "
                    f"reason={type(exc).__name__}"
                )
```

- [ ] **Step 1.5: 跑测试验证通过**

Run: `cd apps/agent-service && uv run pytest tests/unit/life/test_glimpse.py -v`
预期：所有 test 全 PASS（含新加的 chat_submit_failure 测试和已有 want_to_speak 测试）。

- [ ] **Step 1.6: 跑全量 life 单元测试**

Run: `cd apps/agent-service && uv run pytest tests/unit/life/ -v`
预期：全部 PASS。

- [ ] **Step 1.7: Commit**

```bash
git add apps/agent-service/app/life/glimpse.py apps/agent-service/tests/unit/life/test_glimpse.py
git commit -m "feat(life): glimpse upgrade chat_submit failure observability

logger.error -> logger.exception (captures traceback)
state_observation tags [chat_submit_failed] reason=<exc_type> alongside
existing [want_to_speak:throttled] / [no_speak] markers, so glimpse_state
history shows which tick failed to submit. last_seen still advances.

Prerequisite for Phase 6 knife 1: emit(ChatTrigger) replaces fire-and-
forget mq.publish, errors will start propagating to glimpse; without
this upgrade those errors lose context.

Spec: docs/superpowers/specs/2026-05-07-dataflow-phase-6-cleanup-design.md §2.3"
```

---

## Task 2: chat placement 硬验收

**Files:**
- Modify: `apps/agent-service/tests/wiring/test_chat_wiring.py`

### 子步

- [ ] **Step 2.1: 加 placement 测试**

文件：`apps/agent-service/tests/wiring/test_chat_wiring.py` 末尾新增：

```python
def test_chat_nodes_placement_under_agent_service():
    """route_chat_node + chat_node 必须落在 agent-service 默认 app。

    emit() 用 nodes_for_app(APP_NAME) 过滤 in-process consumer：
    proactive 在 agent-service 进程 emit ChatTrigger 后，route_chat_node
    必须在 agent-service 这一组 nodes 里，否则 fan-out 会被静默跳过。
    chat_node 同理（route_chat_node fan-out 出 ChatRequest，链路下半段
    必须在同进程命中或走 durable mq）。

    deployment.py 没显式 bind 这两个节点 → fall through 到 default app
    "agent-service"。如果未来一改 bind（误绑到 vectorize-worker），
    这条测试会立刻挂掉。
    """
    _fresh_import()
    # deployment.py 也要 reload 让 bind 注册重新生效
    import importlib

    import app.deployment as d

    importlib.reload(d)

    from app.nodes.chat_node import chat_node, route_chat_node
    from app.runtime.placement import nodes_for_app

    agent_service_nodes = nodes_for_app("agent-service")
    assert route_chat_node in agent_service_nodes, (
        "route_chat_node must be in agent-service app; emit(ChatTrigger) "
        "from same process won't reach it otherwise"
    )
    assert chat_node in agent_service_nodes, (
        "chat_node must be in agent-service app"
    )
```

- [ ] **Step 2.2: 跑测试验证它通过**

Run: `cd apps/agent-service && uv run pytest tests/wiring/test_chat_wiring.py::test_chat_nodes_placement_under_agent_service -v`
预期：PASS（当前没 bind 落 default agent-service，测试应当直接通过）。

- [ ] **Step 2.3: 跑全量 wiring 测试**

Run: `cd apps/agent-service && uv run pytest tests/wiring/ -v`
预期：全部 PASS。

- [ ] **Step 2.4: Commit**

```bash
git add apps/agent-service/tests/wiring/test_chat_wiring.py
git commit -m "test(wiring): hard placement assertion for chat nodes

emit() filters in-process consumers by APP_NAME; deployment.py uses
explicit bind (vectorize-worker has 5 binds; chat nodes fall through
to default agent-service). Lock that contract so a future bind change
fails loudly instead of silently dropping fan-out.

Spec: docs/superpowers/specs/2026-05-07-dataflow-phase-6-cleanup-design.md §4 (reviewer #2)"
```

---

## Task 3: proactive emit ChatTrigger

**Files:**
- Modify: `apps/agent-service/app/life/proactive.py:142-167`
- Modify: `apps/agent-service/tests/unit/life/test_proactive.py`

### 子步

- [ ] **Step 3.1: 改造 test_proactive.py 现有两个测试 — 删 mq.publish patch，加 emit 顺序断言**

读现有：`grep -n "mock_publish\|patch.*mq.publish\|patch.*emit\|test_submit_proactive_chat" apps/agent-service/tests/unit/life/test_proactive.py`

把每个用 `patch(f"{MODULE}.mq.publish", AsyncMock())` 的测试改成：
- 删 `patch(f"{MODULE}.mq.publish", AsyncMock()) as mock_publish` 这行
- 删 `patch("app.infra.rabbitmq.current_lane", return_value="prod")` 改成 `patch(f"{MODULE}.current_lane", return_value="prod")`（current_lane 是从 `app.infra.rabbitmq` import 进 proactive 模块的）
- 加 `patch(f"{MODULE}.emit", AsyncMock()) as mock_emit`
- 把 `payload = mock_publish.await_args.args[1]; assert payload["..."] == ...` 这种断言改成对 `mock_emit.await_args_list` 做 emit 顺序断言

具体改造模板（替换 `test_submit_proactive_chat_uses_existing_lark_target_root` 的 patch + assert 段）：

```python
    with (
        patch(f"{MODULE}.get_session", return_value=_mock_session_cm(session)),
        patch("app.data.queries.find_message_by_id", AsyncMock(return_value=target)),
        patch(
            "app.data.queries.resolve_bot_name_for_persona",
            AsyncMock(return_value="akao"),
        ),
        patch(f"{MODULE}.time.time", return_value=1234.567),
        patch(f"{MODULE}.uuid.uuid4", return_value="session-1"),
        patch(f"{MODULE}.emit", AsyncMock()) as mock_emit,
        patch(f"{MODULE}.current_lane", return_value="prod"),
    ):
        session_id = await submit_proactive_chat(
            chat_id="oc_test",
            persona_id="akao-001",
            target_message_id="om_target",
            stimulus="想接一句",
        )

    assert session_id == "session-1"
    added = session.add.call_args.args[0]
    assert added.message_id == "proactive_1234567"
    assert added.root_message_id == "om_root"
    assert added.reply_message_id == "om_target"

    # emit order: Message first (line 147), then ChatTrigger (line 153 replacement)
    from app.domain.chat_dataflow import ChatTrigger
    from app.domain.message import Message

    assert mock_emit.await_count == 2, "expect emit called twice: Message then ChatTrigger"
    first_arg = mock_emit.await_args_list[0].args[0]
    second_arg = mock_emit.await_args_list[1].args[0]
    assert isinstance(first_arg, Message), f"first emit must be Message, got {type(first_arg).__name__}"
    assert isinstance(second_arg, ChatTrigger), f"second emit must be ChatTrigger, got {type(second_arg).__name__}"
    assert second_arg.message_id == "proactive_1234567"
    assert second_arg.root_id == "om_target"
    assert second_arg.is_proactive is True
    assert second_arg.bot_name == "akao"
    assert second_arg.lane == "prod"
    assert second_arg.session_id == "session-1"
    assert second_arg.user_id == "__proactive__"
```

第二个测试 `test_submit_proactive_chat_resolves_numeric_target_row_id` 用同样方式改造：替换 patch + 加 emit 断言；该测试主要 assert `resolve_message_id_by_row_id` 被调用 + payload.message_id 正确，新版改成断言 `mock_resolve_row.await_count == 1` + ChatTrigger 字段。

如还有其它 proactive 测试用 mq.publish，按同样 pattern 全部改造。

- [ ] **Step 3.2: 跑测试验证它失败**

Run: `cd apps/agent-service && uv run pytest tests/unit/life/test_proactive.py -v`
预期：所有 emit 断言 FAIL，因为 proactive.py 还在用 mq.publish。

- [ ] **Step 3.3: 改 proactive.py — 用 emit ChatTrigger 替换 mq.publish**

文件：`apps/agent-service/app/life/proactive.py`

把第 142-167 行（emit Message 之后、return 之前的整段）：

```python
    # get_session() commits on block exit; emit AFTER commit so downstream
    # consumers querying pg will see the row.
    from app.domain.message import Message
    from app.runtime import emit  # local import to avoid boot cycles

    await emit(Message.from_cm(msg))

    # Publish to chat_request queue
    from app.infra.rabbitmq import current_lane

    lane = current_lane()
    await mq.publish(
        CHAT_REQUEST,
        {
            "session_id": session_id,
            "message_id": message_id,
            "chat_id": chat_id,
            "is_p2p": False,
            "root_id": target_lark_id or "",
            "user_id": PROACTIVE_USER_ID,
            "bot_name": bot_name,
            "is_proactive": True,
            "lane": lane,
            "enqueued_at": now_ms,
        },
    )

    logger.info(
        "Proactive request submitted: session_id=%s, target=%s",
        session_id,
        target_lark_id,
    )
    return session_id
```

替换为：

```python
    # get_session() commits on block exit; emit AFTER commit so downstream
    # consumers querying pg will see the row.
    from app.domain.chat_dataflow import ChatTrigger
    from app.domain.message import Message
    from app.infra.rabbitmq import current_lane
    from app.runtime import emit  # local import to avoid boot cycles

    await emit(Message.from_cm(msg))

    await emit(
        ChatTrigger(
            message_id=message_id,
            session_id=session_id,
            chat_id=chat_id,
            is_p2p=False,
            root_id=target_lark_id or None,
            user_id=PROACTIVE_USER_ID,
            bot_name=bot_name,
            is_proactive=True,
            lane=current_lane(),
            enqueued_at=now_ms,
        )
    )

    logger.info(
        "Proactive request submitted: session_id=%s, target=%s",
        session_id,
        target_lark_id,
    )
    return session_id
```

- [ ] **Step 3.4: 删 proactive.py 顶部的 CHAT_REQUEST / mq import**

文件顶部当前是 `from app.infra.rabbitmq import CHAT_REQUEST, mq`（line 19）。grep 验 mq / CHAT_REQUEST 在文件其他地方是否还有用：

Run: `grep -n "CHAT_REQUEST\|mq\." apps/agent-service/app/life/proactive.py`
预期：除了第 19 行 import 外其他命中应当为 0。

如果 0 命中，删除这行 import：

```python
# 删除：
from app.infra.rabbitmq import CHAT_REQUEST, mq
```

如果 grep 还有 `mq.something()` 命中（比如 mq.declare 之类），保留 `from app.infra.rabbitmq import mq`，只删 `CHAT_REQUEST` 部分。

- [ ] **Step 3.5: 跑 proactive 单元测试验证通过**

Run: `cd apps/agent-service && uv run pytest tests/unit/life/test_proactive.py -v`
预期：全部 PASS。

- [ ] **Step 3.6: 跑 chat / wiring / glimpse 全部相关测试**

Run: `cd apps/agent-service && uv run pytest tests/unit/life/ tests/wiring/ tests/nodes/test_chat_node.py tests/nodes/test_route_chat_node.py -v`
预期：全部 PASS。

- [ ] **Step 3.7: 跑全量 agent-service 测试**

Run: `cd apps/agent-service && uv run pytest -v 2>&1 | tail -60`
预期：全部 PASS。

- [ ] **Step 3.8: ruff 检查**

Run: `cd apps/agent-service && uv run ruff check app/life/proactive.py tests/unit/life/test_proactive.py`
预期：0 报错（或只有原有未触动的报错）。

- [ ] **Step 3.9: Commit**

```bash
git add apps/agent-service/app/life/proactive.py apps/agent-service/tests/unit/life/test_proactive.py
git commit -m "feat(dataflow): proactive submit emits ChatTrigger instead of mq.publish

Same-process publisher and consumer (proactive runs in agent-service main,
route_chat_node binds default agent-service via deployment.py fall-through),
so emit() in-process dispatch hits route_chat_node directly. lark-server
keeps mq.publish(chat_request) for cross-process publishing; runtime mq
source loop decodes both into ChatTrigger and feeds the same route_chat_node
— the chat entry now has a single Data type as its sole ingress.

CHAT_REQUEST routing constant stays in app/infra/rabbitmq.py because
lark-server still uses it. proactive.py's import of CHAT_REQUEST/mq is
removed.

Observability tradeoff explicitly accepted (spec §2.5): old path's mq
source DLQ for route_chat_node failures is replaced by glimpse.py
[chat_submit_failed] tag + logger.exception (Task 1).

Spec: docs/superpowers/specs/2026-05-07-dataflow-phase-6-cleanup-design.md §2"
```

---

## Task 4: queries.py 拆 7 个 domain

**Files:**
- Delete: `apps/agent-service/app/data/queries.py`
- Create: `apps/agent-service/app/data/queries/__init__.py`
- Create: `apps/agent-service/app/data/queries/model_provider.py`
- Create: `apps/agent-service/app/data/queries/persona.py`
- Create: `apps/agent-service/app/data/queries/messages.py`
- Create: `apps/agent-service/app/data/queries/agent_response.py`
- Create: `apps/agent-service/app/data/queries/schedule.py`
- Create: `apps/agent-service/app/data/queries/life.py`
- Create: `apps/agent-service/app/data/queries/memory.py`（如超 300 行再细拆 `memory_edges.py` / `memory_search.py`）
- Create: `apps/agent-service/tests/unit/data/test_queries_split.py`

### 函数归类（spec §3.3，搬运索引）

| domain | 函数（按 queries.py 现有顺序） |
|---|---|
| **model_provider** (3) | `parse_model_id` / `find_model_mapping` / `find_provider_by_name` |
| **persona** (6) | `find_persona` / `list_all_persona_ids` / `resolve_persona_id` / `resolve_bot_name_for_persona` / `resolve_mentioned_personas` / `find_bot_names_for_persona` |
| **messages** (12) | `find_cross_chat_messages` / `find_message_content` / `find_messages_in_range` / `find_username` / `find_group_name` / `find_group_download_permission` / `find_message_by_id` / `resolve_message_id_by_row_id` / `find_last_bot_reply_time` / `find_context_messages_for_anchors` / `find_group_members` / `find_gray_config` |
| **agent_response** (4) | `set_agent_response_bot` / `is_chat_request_completed` / `get_safety_status` / `set_safety_status` |
| **schedule** (11) | `find_active_schedules_for_date` / `find_latest_plan` / `find_plan_for_period` / `find_daily_entries` / `list_schedules` / `upsert_schedule` / `delete_schedule` / `insert_schedule_revision` / `get_current_schedule` / `get_schedule_revision_by_id` / `list_recent_schedule_revisions` |
| **life** (9) | `find_latest_life_state` / `insert_life_state` / `find_today_activity_states` / `find_life_states_in_range` / `find_latest_glimpse_state` / `insert_glimpse_state` / `insert_reply_style` / `find_latest_reply_style` / `list_recent_life_states` |
| **memory** (29) | `list_today_fragments` / `find_fragments_since` / `get_fragment_by_id` / `get_abstract_by_id` / `insert_fragment` / `touch_fragment` / `get_fragments_by_ids` / `touch_fragments_bulk` / `insert_abstract_memory` / `touch_abstract` / `touch_abstracts_bulk` / `count_abstracts_by_persona` / `insert_memory_edge` / `insert_note` / `get_active_notes` / `resolve_note` / `update_abstract_content_query` / `set_clarity` / `delete_fragment_query` / `delete_edge` / `list_fragments_window` / `list_abstracts_window` / `list_edges_to` / `list_edges_from` / `get_abstracts_by_subject` / `get_abstracts_by_subjects` / `get_recent_abstract_titles` / `count_abstracts_per_subject_prefix` / `get_recent_fragments_for_injection` |

合计 74 个 public 函数（含 `parse_model_id` 这种纯函数）。

> 注：`get_schedule_revision_by_id` 在源文件出现在 line 1252，按表归 schedule；`update_abstract_content_query` 在 line 1003，按表归 memory。两个函数在源文件的位置乍看离 domain section 远，归类按表不按位置。

### 子步

- [ ] **Step 4.1: 验证 queries.py 公开函数集合（建立 source-of-truth）**

Run（确认所有 def 数量 + 名字）：

```bash
grep -E "^(async )?def [a-z_]+" apps/agent-service/app/data/queries.py | \
  sed 's/async def //; s/def //; s/(.*//' | sort
```

预期：74 个函数名。把这个列表存到剪贴板/临时文件作为后续验收 fixture。

- [ ] **Step 4.2: 创建 `app/data/queries/` package 目录 + 7 个 domain 文件**

按归类索引把函数从原 `queries.py` 一比一搬到各 domain 文件，函数体 0 字节修改。

**完整示例 — `app/data/queries/model_provider.py`**（按此模板做其它 6 个 domain）：

```python
"""Model provider / model mapping queries.

Operates on tables: ``ModelProvider``, ``ModelMapping``.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.data.models import ModelMapping, ModelProvider

__all__ = [
    "parse_model_id",
    "find_model_mapping",
    "find_provider_by_name",
]


def parse_model_id(model_id: str) -> tuple[str, str]:
    """Split ``provider/model_name`` into a tuple. (queries.py:51-60 1:1)"""
    if "/" not in model_id:
        raise ValueError(f"model_id must be 'provider/model', got {model_id!r}")
    provider, _, name = model_id.partition("/")
    if not provider or not name:
        raise ValueError(f"model_id has empty provider or name: {model_id!r}")
    return provider, name


async def find_model_mapping(session: AsyncSession, alias: str) -> ModelMapping | None:
    """Find model mapping by alias. (queries.py:62-69 1:1)"""
    result = await session.execute(
        select(ModelMapping).where(ModelMapping.alias == alias)
    )
    return result.scalar_one_or_none()


async def find_provider_by_name(
    session: AsyncSession, provider_name: str
) -> ModelProvider | None:
    """Find provider by name. (queries.py:70-82 1:1)"""
    result = await session.execute(
        select(ModelProvider).where(ModelProvider.name == provider_name)
    )
    return result.scalar_one_or_none()
```

**搬运规则**（每个 domain 都遵守）：

1. **Module docstring 顶部写明 "Operates on tables: ..."** 列该 domain 主操作表。
2. **只 import 该 domain 实际用到的 models / SQLAlchemy 类型**。原 queries.py 顶部 import 了 17 个 model 类，不要原样复制 —— 每个 domain 只 import 自己 def 体里出现的类。例：persona.py 只 import `BotPersona` + `ConversationMessage`（resolve 系列要 join messages）+ 必要 SQL 类型；不要 import `Fragment` / `AkaoSchedule` 等无关类。
3. **`__all__` 显式列函数名**（与该文件实际 def 的函数集合相等，无遗漏无多余）。
4. **函数体 1:1 从 queries.py 搬运** —— 不调签名、不调 SQL、不调 docstring、不重命名变量。如果原函数依赖 queries.py 顶部的模块级常量（如 `_CST = timezone(...)`），把该常量也搬到对应 domain 文件（例：messages.py / life.py / schedule.py 用到 `_CST`，三个文件各自独立定义一份；这是 domain 自包含原则，不算重复定义因为是模块私有常量）。
5. **`from __future__ import annotations` 每个 domain 文件都加**（与原 queries.py 一致，让类型注解延迟求值）。

**搬运索引**（按归类表 + 原 queries.py 行号区间）：

| 文件 | 函数（行号区间） |
|---|---|
| `model_provider.py` | parse_model_id (51-60) / find_model_mapping (62-69) / find_provider_by_name (70-82) |
| `persona.py` | find_persona (83-87) / list_all_persona_ids (88-92) / resolve_persona_id (107-116) / resolve_bot_name_for_persona (117-133) / resolve_mentioned_personas (134-149) / find_bot_names_for_persona (150-163) |
| `messages.py` | find_cross_chat_messages (164-201) / find_message_content (202-209) / find_messages_in_range (210-228) / find_username (229-236) / find_group_name (237-244) / find_group_download_permission (245-256) / find_message_by_id (257-266) / resolve_message_id_by_row_id (267-280) / find_last_bot_reply_time (363-376) / find_context_messages_for_anchors (710-750) / find_group_members (751-779) / find_gray_config (94-105) |
| `agent_response.py` | set_agent_response_bot (281-295) / is_chat_request_completed (297-322) / get_safety_status (324-338) / set_safety_status (340-361) |
| `schedule.py` | find_active_schedules_for_date (377-393) / find_latest_plan (394-412) / find_plan_for_period (413-430) / find_daily_entries (431-445) / list_schedules (446-468) / upsert_schedule (469-503) / delete_schedule (504-518) / insert_schedule_revision (969-987) / get_current_schedule (988-1001) / get_schedule_revision_by_id (1252-1261) / list_recent_schedule_revisions (1274-1283) |
| `life.py` | find_latest_life_state (519-531) / insert_life_state (532-557) / find_today_activity_states (558-575) / find_life_states_in_range (619-638) / find_latest_glimpse_state (639-654) / insert_glimpse_state (655-676) / insert_reply_style (677-695) / find_latest_reply_style (696-709) / list_recent_life_states (1262-1273) |
| `memory.py` | list_today_fragments (576-596) / find_fragments_since (597-618) / get_fragment_by_id (780-787) / get_abstract_by_id (788-800) / insert_fragment (801-825) / touch_fragment (826-833) / get_fragments_by_ids (834-845) / touch_fragments_bulk (846-856) / insert_abstract_memory (857-877) / touch_abstract (878-885) / touch_abstracts_bulk (886-896) / count_abstracts_by_persona (897-907) / insert_memory_edge (908-934) / insert_note (935-946) / get_active_notes (947-958) / resolve_note (959-968) / update_abstract_content_query (1003-1012) / set_clarity (1013-1031) / delete_fragment_query (1032-1045) / delete_edge (1046-1053) / list_fragments_window (1054-1066) / list_abstracts_window (1067-1082) / list_edges_to (1083-1101) / list_edges_from (1102-1120) / get_abstracts_by_subject (1121-1142) / get_abstracts_by_subjects (1143-1173) / get_recent_abstract_titles (1174-1190) / count_abstracts_per_subject_prefix (1191-1207) / get_recent_fragments_for_injection (1208-1251) |

**边搬边量行**：每文件搬完跑 `wc -l apps/agent-service/app/data/queries/<file>.py`，确认 < 300 行。memory.py 预估 ~440 行超 300 行，搬完确认超了之后再细拆为：
- `memory.py` ≈ fragments + abstracts CRUD（`get_fragment_by_id` / `get_abstract_by_id` / `insert_fragment` / `touch_fragment` / `get_fragments_by_ids` / `touch_fragments_bulk` / `insert_abstract_memory` / `touch_abstract` / `touch_abstracts_bulk` / `count_abstracts_by_persona` / `update_abstract_content_query` / `set_clarity` / `delete_fragment_query`）
- `memory_edges.py` ≈ edges + notes（`insert_memory_edge` / `delete_edge` / `list_edges_to` / `list_edges_from` / `insert_note` / `get_active_notes` / `resolve_note`）
- `memory_search.py` ≈ 查询助手（`list_today_fragments` / `find_fragments_since` / `list_fragments_window` / `list_abstracts_window` / `get_abstracts_by_subject` / `get_abstracts_by_subjects` / `get_recent_abstract_titles` / `count_abstracts_per_subject_prefix` / `get_recent_fragments_for_injection`）

`__init__.py`（Step 4.3）和 `test_queries_split.py`（Step 4.5）需要相应同步加 `from app.data.queries.memory_edges import *` / `from app.data.queries.memory_search import *` + 对应 modules dict 条目。

- [ ] **Step 4.3: 创建 `app/data/queries/__init__.py`**

```python
"""Data queries — split per domain. 调用方 import 不变 (`from app.data.queries import X`)."""
from app.data.queries.agent_response import *  # noqa: F401,F403
from app.data.queries.life import *  # noqa: F401,F403
from app.data.queries.memory import *  # noqa: F401,F403
from app.data.queries.messages import *  # noqa: F401,F403
from app.data.queries.model_provider import *  # noqa: F401,F403
from app.data.queries.persona import *  # noqa: F401,F403
from app.data.queries.schedule import *  # noqa: F401,F403

# Export aggregated for downstream introspection (test_queries_split asserts on this).
from app.data.queries.agent_response import __all__ as _ar
from app.data.queries.life import __all__ as _life
from app.data.queries.memory import __all__ as _memory
from app.data.queries.messages import __all__ as _messages
from app.data.queries.model_provider import __all__ as _mp
from app.data.queries.persona import __all__ as _persona
from app.data.queries.schedule import __all__ as _schedule

__all__ = [
    *_ar, *_life, *_memory, *_messages, *_mp, *_persona, *_schedule,
]
```

如 memory 拆成 `memory.py` / `memory_edges.py` / `memory_search.py` 三个，再加 `from app.data.queries.memory_edges import *` / `from app.data.queries.memory_search import *` + `_memory_edges` / `_memory_search` 加入 `__all__`。

- [ ] **Step 4.4: 删除 `app/data/queries.py`**

```bash
git rm apps/agent-service/app/data/queries.py
```

注意：`git rm` 之后 Python import 才会找到 package。删之前不能直接 import 测试（package 与 module 同名冲突）。

- [ ] **Step 4.5: 创建 `tests/unit/data/test_queries_split.py`**

```python
"""Phase 6 第三刀验收：queries package 拆分完整性 + 无重名。"""
from __future__ import annotations


# 来自 spec §3.3，硬编码作为期望基线。Step 4.1 grep 出来的 74 个名字必须与此完全相等。
EXPECTED_FUNCTIONS = {
    # model_provider (3)
    "parse_model_id", "find_model_mapping", "find_provider_by_name",
    # persona (6)
    "find_persona", "list_all_persona_ids", "resolve_persona_id",
    "resolve_bot_name_for_persona", "resolve_mentioned_personas",
    "find_bot_names_for_persona",
    # messages (12)
    "find_cross_chat_messages", "find_message_content", "find_messages_in_range",
    "find_username", "find_group_name", "find_group_download_permission",
    "find_message_by_id", "resolve_message_id_by_row_id", "find_last_bot_reply_time",
    "find_context_messages_for_anchors", "find_group_members", "find_gray_config",
    # agent_response (4)
    "set_agent_response_bot", "is_chat_request_completed",
    "get_safety_status", "set_safety_status",
    # schedule (11)
    "find_active_schedules_for_date", "find_latest_plan", "find_plan_for_period",
    "find_daily_entries", "list_schedules", "upsert_schedule", "delete_schedule",
    "insert_schedule_revision", "get_current_schedule", "get_schedule_revision_by_id",
    "list_recent_schedule_revisions",
    # life (9)
    "find_latest_life_state", "insert_life_state", "find_today_activity_states",
    "find_life_states_in_range", "find_latest_glimpse_state", "insert_glimpse_state",
    "insert_reply_style", "find_latest_reply_style", "list_recent_life_states",
    # memory (29)
    "list_today_fragments", "find_fragments_since", "get_fragment_by_id",
    "get_abstract_by_id", "insert_fragment", "touch_fragment", "get_fragments_by_ids",
    "touch_fragments_bulk", "insert_abstract_memory", "touch_abstract",
    "touch_abstracts_bulk", "count_abstracts_by_persona", "insert_memory_edge",
    "insert_note", "get_active_notes", "resolve_note", "update_abstract_content_query",
    "set_clarity", "delete_fragment_query", "delete_edge", "list_fragments_window",
    "list_abstracts_window", "list_edges_to", "list_edges_from",
    "get_abstracts_by_subject", "get_abstracts_by_subjects",
    "get_recent_abstract_titles", "count_abstracts_per_subject_prefix",
    "get_recent_fragments_for_injection",
}


def test_queries_all_complete():
    """app.data.queries.__all__ 与 spec §3.3 函数集合完全相等。"""
    from app.data import queries

    actual = set(queries.__all__)
    missing = EXPECTED_FUNCTIONS - actual
    extra = actual - EXPECTED_FUNCTIONS
    assert not missing, f"missing in queries.__all__: {sorted(missing)}"
    assert not extra, f"unexpected in queries.__all__: {sorted(extra)}"


def test_queries_no_duplicate_names():
    """7 个 domain 文件的 __all__ 两两交集为空 — `from X import *` 重名后者覆盖不报错，靠这条测试兜底。"""
    from app.data.queries import (
        agent_response,
        life,
        memory,
        messages,
        model_provider,
        persona,
        schedule,
    )

    modules = {
        "agent_response": agent_response,
        "life": life,
        "memory": memory,
        "messages": messages,
        "model_provider": model_provider,
        "persona": persona,
        "schedule": schedule,
    }

    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for mod_name, mod in modules.items():
        for name in mod.__all__:
            if name in seen:
                duplicates.append(f"{name}: {seen[name]} & {mod_name}")
            else:
                seen[name] = mod_name
    assert not duplicates, f"duplicate names across domains: {duplicates}"


def test_queries_each_function_callable():
    """每个 export 都是 callable（防止 __all__ 列了不存在的名字）。"""
    from app.data import queries

    for name in queries.__all__:
        attr = getattr(queries, name, None)
        assert callable(attr), f"queries.{name} is not callable (got {type(attr).__name__})"
```

如 memory 被细拆，把 `from app.data.queries import memory_edges, memory_search` 加进 `test_queries_no_duplicate_names` 的 modules dict。

- [ ] **Step 4.6: pytest --collect-only 验证 import 完整**

Run: `cd apps/agent-service && uv run pytest --collect-only 2>&1 | tail -30`
预期：0 import error。如果有 ImportError 说明某个调用方 `from app.data.queries import X` 找不到 X，回到 Step 4.2 检查该函数有没有漏搬或写错 `__all__`。

- [ ] **Step 4.7: 跑 test_queries_split**

Run: `cd apps/agent-service && uv run pytest tests/unit/data/test_queries_split.py -v`
预期：3 个 test 全 PASS。任何失败按其错误信息回去补/改。

- [ ] **Step 4.8: 跑全量 agent-service 测试**

Run: `cd apps/agent-service && uv run pytest -v 2>&1 | tail -80`
预期：全 PASS。如果某测试 `from app.data.queries import Y` 失败，说明 Y 漏搬。

- [ ] **Step 4.9: ruff 检查**

Run: `cd apps/agent-service && uv run ruff check app/data/queries/`
预期：0 报错。ruff 对 `from X import *` 通常会警告 unused，但 `# noqa: F401,F403` 已经压制；如有其它真错按错改。

- [ ] **Step 4.10: 量行验证**

Run: `wc -l apps/agent-service/app/data/queries/*.py`
预期：每个文件 < 300 行；总行数 ≈ 1283 ± 5%（允许 import / docstring 重复带来的 < 70 行膨胀）。

- [ ] **Step 4.11: grep 旧 pattern 验证零残留**

Run: `find apps/agent-service -name "queries.py" -path "*/data/*"`
预期：无输出（旧文件已删，package 下不允许同名 queries.py）。

Run: `grep -rn "from app.data.queries import\|from app.data import queries" apps/agent-service/ | wc -l`
预期：和 main 分支同样数量（44 左右），调用方零改动。

Run: `git diff --stat HEAD~3 -- apps/agent-service/app/ | grep -v "data/queries"`
预期：调用方文件无改动（只有 `app/data/queries.py` 删 + `app/data/queries/*.py` 新增 + Task 1-3 的 glimpse.py / proactive.py / wiring 改动）。

- [ ] **Step 4.12: Commit**

```bash
git add apps/agent-service/app/data/queries/ apps/agent-service/tests/unit/data/test_queries_split.py
git rm apps/agent-service/app/data/queries.py 2>/dev/null || true  # 已在 Step 4.4 git rm 过
git commit -m "refactor(data): split queries.py 1283-line god module into 7 domains

Per CLAUDE.md (single file <300 lines + single responsibility), split:
  - model_provider.py (3 fns)
  - persona.py (6 fns)
  - messages.py (12 fns)
  - agent_response.py (4 fns: agent_responses table)
  - schedule.py (11 fns)
  - life.py (9 fns)
  - memory.py (29 fns)

Domain assigned by primary table operated on (not by call site):
  - list_today_fragments lives in life module by usage but operates on
    fragments table -> memory.py
  - find_gray_config joins via message_id but reads LarkBaseChatInfo
    chat-dimension config -> messages.py (not persona)
  - is_chat_request_completed / set_agent_response_bot / *_safety_status
    all primarily on agent_responses -> agent_response.py (carved out
    from messages.py)

queries/__init__.py re-exports via from-X-import-*; __all__ aggregates
the 7 domains. queries.py file deleted (no compat shim).

Hard test test_queries_split.py asserts:
  1. queries.__all__ equals 74-function set from spec §3.3
  2. domain __all__ pairwise-disjoint (from-import-* name collision is
     silent; ruff/mypy do not catch it)
  3. each exported name is callable

Caller import sites: 44 files unchanged (verified via git diff --stat).

Spec: docs/superpowers/specs/2026-05-07-dataflow-phase-6-cleanup-design.md §3"
```

---

## Task 5: dev 泳道部署 + e2e 验证

**Files:** 0（纯部署 / 验证）

> 部署 / e2e 命令需要用户参与（`make deploy` 触发构建、绑定 dev bot 后人手在飞书发消息）。AI agent 完成 Task 1-4 + 推送到远端 + 开 PR 后停下，由用户跑 Task 5。

### 子步

- [ ] **Step 5.1: 推送到远端**

```bash
git push -u origin refactor/flow-parse-6
```

- [ ] **Step 5.2: 开 PR**

```bash
ghc pr create --title "Phase 6 — proactive ChatTrigger emit + queries.py domain split" \
  --body "$(cat <<'EOF'
## Summary

Phase 6 cleanup, two knives in one PR:

- **Knife 1**: `proactive.submit_proactive_chat()` switches from `mq.publish(CHAT_REQUEST, body)` to `emit(ChatTrigger(...))`. Same-process publisher/consumer, so emit() in-process dispatch hits route_chat_node directly. Chat ingress now has a single Data type.
- **Knife 3**: `data/queries.py` 1283-line god module split into 7 domain files (`model_provider` / `persona` / `messages` / `agent_response` / `schedule` / `life` / `memory`). `queries/__init__.py` re-exports so 44 caller import sites stay zero-touch.

Knife 2 (vectorize emit) deferred to Phase 6.5 — current emit() does not support cross-process dispatch via Source.mq lookup; vectorize publisher (agent-service) and consumer (vectorize-worker) are different pods, switching to emit would lose messages.

Knife 4 (workers/ dead code) cancelled — state_sync_worker is enqueued by name from update_schedule tool, not actually dead.

Companion changes that ship together:
- `glimpse.py` keeps the proactive-submit try/except (deleting it would propagate route_chat_node errors past insert_fragment + enqueue_vectorize, and DLQ replay is deduped by insert_idempotent → next cron tick re-runs glimpse with un-advanced last_seen → duplicate fragments). `logger.error` upgraded to `logger.exception`; `state_observation` tags `[chat_submit_failed] reason=<exc_type>` alongside existing `[want_to_speak:throttled]`.
- Hard placement test asserts `route_chat_node` and `chat_node` are in `nodes_for_app("agent-service")`. `deployment.py` falls through to default agent-service today; this locks the contract so a future bind change fails loudly.
- Hard split test asserts `queries.__all__` equals the 74-function spec set, and the 7 domain `__all__` are pairwise-disjoint.

## Test plan

- [ ] `pytest apps/agent-service/` green (all unit/integration tests)
- [ ] `ruff check apps/agent-service/` clean
- [ ] dev lane deployed; bind dev bot
- [ ] Lark dev bot: group chat + p2p basic conversation works
- [ ] Lark dev bot: glimpse triggers proactive — proactive message appears in group
- [ ] Lark dev bot: simulate route_chat_node failure (e.g. force a transient DB error) and verify glimpse_state observation contains `[chat_submit_failed]`
- [ ] grep `mq.publish` in apps/agent-service/app/ shows count = main - 1
- [ ] grep `from app.data.queries` shows 44 caller files unchanged

Spec: docs/superpowers/specs/2026-05-07-dataflow-phase-6-cleanup-design.md (v3)
EOF
)"
```

- [ ] **Step 5.3: 部署到 dev 泳道**

按用户实际泳道命名（如 `phase6-cleanup`）：

```bash
make deploy APP=agent-service LANE=phase6-cleanup GIT_REF=refactor/flow-parse-6
```

agent-service 一镜像多服务，根据 CLAUDE.md 部署铁律 #4，同步 release `arq-worker` + `vectorize-worker`：

```bash
make release APP=arq-worker LANE=phase6-cleanup VERSION=<上一步 build 出的 version>
make release APP=vectorize-worker LANE=phase6-cleanup VERSION=<同上>
```

- [ ] **Step 5.4: 绑定 dev bot 到泳道**

```
/ops bind TYPE=bot KEY=dev LANE=phase6-cleanup
```

- [ ] **Step 5.5: 飞书 e2e 验证**

在飞书 dev bot：
1. 群聊发"@chiwei 你好"，确认正常回复（验证 chat 主入口未受影响）
2. 私聊发消息，确认正常回复（验证 p2p）
3. 等 ~5 分钟（glimpse 5min 周期）或在群里制造 browsing 转移触发即时 glimpse；观察是否有主动消息（验证第一刀 emit ChatTrigger 链路）
4. （可选）人为制造 route_chat_node 异常（如改 dynamic config 让某 persona 不存在，触发 resolve_persona_id raise），观察 glimpse_state.observation 是否含 `[chat_submit_failed]`，pod 日志有 traceback

- [ ] **Step 5.6: 验证 grep 不变量**

```bash
# 第一刀：proactive 不再 mq.publish
git checkout main -- apps/agent-service && \
  git diff --stat HEAD -- apps/agent-service/app/life/proactive.py
# 切回当前 branch
git checkout refactor/flow-parse-6 -- apps/agent-service

grep -rn "mq.publish" apps/agent-service/app/ | wc -l
# 应当 = main 上的命中数 - 1（proactive 那一处）
```

```bash
# 第三刀：caller 0 改动
git diff main..HEAD --stat -- apps/agent-service/app/ | grep -v "data/queries\|life/proactive\|life/glimpse"
# 应当无输出（除 wiring/test 的新增以外，调用方 0 改动）
```

- [ ] **Step 5.7: 解绑 + 下泳道（验证完成后）**

```
/ops unbind TYPE=bot KEY=dev
```

```bash
make undeploy APP=agent-service LANE=phase6-cleanup
make undeploy APP=arq-worker LANE=phase6-cleanup
make undeploy APP=vectorize-worker LANE=phase6-cleanup
```

- [ ] **Step 5.8: 等用户决定是否合码 / ship**

不要自行 `ghc pr merge`。等用户明确说"合"或"ship"再走 `/ship` skill。

---

## 总验收（全 task 完成后）

跑一遍以下命令确认：

```bash
# 1. 全量测试
cd apps/agent-service && uv run pytest -v 2>&1 | tail -20

# 2. ruff 干净
cd apps/agent-service && uv run ruff check .

# 3. queries.py 已删除、package 存在
ls apps/agent-service/app/data/queries.py 2>/dev/null
ls apps/agent-service/app/data/queries/

# 4. 单文件行数
wc -l apps/agent-service/app/data/queries/*.py

# 5. mq.publish 减少
grep -rn "mq.publish" apps/agent-service/app/ | wc -l

# 6. proactive 不再 import CHAT_REQUEST
grep -n "CHAT_REQUEST" apps/agent-service/app/life/proactive.py
```

期望：
1. tests 全 PASS
2. ruff 0 报错
3. queries.py 不存在、queries/ 存在
4. 每文件 < 300 行
5. 命中数比 main 少 1
6. 无命中
