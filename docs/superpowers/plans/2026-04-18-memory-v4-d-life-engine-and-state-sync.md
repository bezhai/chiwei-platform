# Memory v4 - Plan D: Life Engine 重构 + State Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Life Engine tick 改造成 tool-based（`commit_life_state`）、落地 `state_end_at` 硬约束替代 `skip_until` 语义；实现 `update_schedule` 触发的异步 state sync。

**Architecture:** 新增 `commit_life_state` tool（只给 Life Engine 用），tool 层做 §9.5 五条硬校验。Life Engine tick 改成"跑 agent → 收 tool call → 持久化"。新增 `state_only_refresh` 纯函数。`update_schedule` 触发 arq job `sync_life_state_after_schedule`，后台异步跑 state_only_refresh。

**Tech Stack:** Python / langchain tool / arq / SQLAlchemy / Langfuse / pytest

**前置:** Plan A（数据层）+ Plan B（tool 体系框架 + `enqueue_state_sync`）

**Spec:** `docs/superpowers/specs/2026-04-16-memory-v4-design.md` §9.3-9.6、§7.5

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `app/data/models.py` | Modify | `LifeEngineState` 新增 `state_end_at` 列 |
| `app/life/tool.py` | Create | `commit_life_state` tool + 五条校验逻辑 |
| `app/life/engine.py` | Modify | tick 改为 tool-based 流程 |
| `app/life/state_sync.py` | Create | `state_only_refresh(persona_id, schedule_content)` 纯函数 |
| `app/workers/state_sync_worker.py` | Create | arq job `sync_life_state_after_schedule` |
| `app/workers/arq_settings.py` | Modify | 注册 `sync_life_state_after_schedule` 到 `functions` |
| `app/data/queries.py` | Modify | `get_latest_life_state_full` 返回带 `state_end_at` |
| `tests/unit/life/test_tool.py` | Create | commit_life_state 校验单测 |
| `tests/unit/life/test_state_sync.py` | Create | state_only_refresh 单测 |
| `tests/unit/workers/test_state_sync_worker.py` | Create | arq job 单测 |
| `tests/unit/life/test_engine.py` | Modify | tick 改造后的单测 |

---

### Task 1: DB 加 `state_end_at` 列

**Files:**
- ops-db submit

- [ ] **Step 1: 提交 DDL**

```
/ops-db submit @chiwei
ALTER TABLE life_engine_state ADD COLUMN state_end_at TIMESTAMPTZ;
-- reason: Memory v4 §9.5 硬约束字段，替代 skip_until 作为状态结束边界
```

- [ ] **Step 2: 验证**

```
/ops-db @chiwei SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'life_engine_state' AND column_name = 'state_end_at'
```

期望输出一行：`state_end_at | timestamp with time zone`

- [ ] **Step 3: 修改 `LifeEngineState` ORM model**

在 `app/data/models.py` 中 `class LifeEngineState` 内，`skip_until` 下面追加：

```python
    state_end_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

- [ ] **Step 4: Commit**

```bash
git add app/data/models.py
git commit -m "feat(memory-v4): add state_end_at column to life_engine_state"
```

---

### Task 2: `commit_life_state` tool + 校验逻辑

**Files:**
- Create: `app/life/tool.py`
- Create: `tests/unit/life/test_tool.py`

- [ ] **Step 1: 写测试（Spec §9.5 五条校验逐条覆盖）**

```python
"""Test commit_life_state tool — v4 §9.5 hard validations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.life.tool import CommitResult, commit_life_state_impl


CST = timezone(timedelta(hours=8))


def _now() -> datetime:
    return datetime.now(CST)


@pytest.mark.asyncio
async def test_validates_nonempty_fields():
    r = await commit_life_state_impl(
        persona_id="chiwei",
        activity_type="",
        current_state="walking",
        response_mood="calm",
        state_end_at=_now() + timedelta(minutes=30),
        skip_until=None,
        reasoning=None,
        now=_now(),
        prev_state=None,
    )
    assert r.ok is False
    assert "activity_type" in r.error


@pytest.mark.asyncio
async def test_rejects_past_state_end_at():
    now = _now()
    r = await commit_life_state_impl(
        persona_id="chiwei",
        activity_type="transit", current_state="walking",
        response_mood="calm",
        state_end_at=now - timedelta(minutes=1),
        skip_until=None, reasoning=None,
        now=now, prev_state=None,
    )
    assert r.ok is False
    assert "state_end_at" in r.error


@pytest.mark.asyncio
async def test_rejects_skip_until_outside_range():
    now = _now()
    # skip_until 超过 state_end_at
    r = await commit_life_state_impl(
        persona_id="chiwei",
        activity_type="transit", current_state="walking", response_mood="calm",
        state_end_at=now + timedelta(minutes=30),
        skip_until=now + timedelta(minutes=45),  # invalid
        reasoning=None, now=now, prev_state=None,
    )
    assert r.ok is False


@pytest.mark.asyncio
async def test_prev_not_expired_only_allows_refresh_not_new_activity():
    now = _now()
    prev = MagicMock(
        activity_type="study", state_end_at=now + timedelta(minutes=30),
    )
    r = await commit_life_state_impl(
        persona_id="chiwei",
        activity_type="transit",  # trying to switch
        current_state="walking", response_mood="calm",
        state_end_at=now + timedelta(minutes=30),
        skip_until=None, reasoning=None,
        now=now, prev_state=prev,
    )
    assert r.ok is False
    assert "refresh" in r.error.lower() or "prev" in r.error.lower()


@pytest.mark.asyncio
async def test_prev_not_expired_allows_in_segment_refresh():
    now = _now()
    prev_end = now + timedelta(minutes=30)
    prev = MagicMock(activity_type="study", state_end_at=prev_end)
    with patch("app.life.tool.insert_life_state", new=AsyncMock()) as ins:
        r = await commit_life_state_impl(
            persona_id="chiwei",
            activity_type="study",
            current_state="reading more focused",
            response_mood="calm",
            state_end_at=prev_end,  # must equal prev
            skip_until=now + timedelta(minutes=10),
            reasoning=None, now=now, prev_state=prev,
        )
    assert r.ok is True
    assert r.is_refresh is True
    ins.assert_awaited_once()


@pytest.mark.asyncio
async def test_prev_expired_allows_new_activity():
    now = _now()
    prev = MagicMock(
        activity_type="study",
        state_end_at=now - timedelta(minutes=5),  # expired
    )
    with patch("app.life.tool.insert_life_state", new=AsyncMock()) as ins:
        r = await commit_life_state_impl(
            persona_id="chiwei",
            activity_type="transit",
            current_state="walking home",
            response_mood="calm",
            state_end_at=now + timedelta(minutes=30),
            skip_until=None, reasoning=None,
            now=now, prev_state=prev,
        )
    assert r.ok is True
    assert r.is_refresh is False
    ins.assert_awaited_once()
```

- [ ] **Step 2: Run — expect ImportError**

```bash
uv run pytest tests/unit/life/test_tool.py -v
```

- [ ] **Step 3: 创建 `app/life/tool.py`**

```python
"""Life Engine v4 — commit_life_state tool + §9.5 hard validations.

This tool is called by the Life Engine's LLM via a langchain tool binding. It's
NOT exposed to the chat agent — only internal to the life pipeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.data.queries import find_latest_life_state, insert_life_state
from app.data.session import get_session

logger = logging.getLogger(__name__)


@dataclass
class CommitResult:
    ok: bool
    error: str = ""
    is_refresh: bool = False
    life_state_id: int | None = None


async def commit_life_state_impl(
    *,
    persona_id: str,
    activity_type: str,
    current_state: str,
    response_mood: str,
    state_end_at: datetime,
    skip_until: datetime | None,
    reasoning: str | None,
    now: datetime,
    prev_state: Any | None,
) -> CommitResult:
    """Persist a Life State after the 5 hard validations from spec §9.5.

    Rules:
      1. activity_type / current_state / response_mood 非空
      2. state_end_at > now
      3. skip_until 为空，或 now < skip_until < state_end_at
      4. prev 关系：
         - now < prev.state_end_at → 只允许段内刷新 (activity_type=prev, state_end_at=prev)
         - now >= prev.state_end_at → 允许换 activity_type，必须给新 state_end_at
      5. state_end_at 必须代表对"这段活动完整时长"的承诺 — 由 LLM 层自律，tool 层不可机械校验。
    """
    if not activity_type.strip():
        return CommitResult(ok=False, error="activity_type 不能为空")
    if not current_state.strip():
        return CommitResult(ok=False, error="current_state 不能为空")
    if not response_mood.strip():
        return CommitResult(ok=False, error="response_mood 不能为空")

    if state_end_at <= now:
        return CommitResult(ok=False, error="state_end_at 必须大于 now")

    if skip_until is not None:
        if not (now < skip_until < state_end_at):
            return CommitResult(
                ok=False,
                error=f"skip_until 必须满足 now < skip_until < state_end_at",
            )

    is_refresh = False
    if prev_state is not None and prev_state.state_end_at is not None:
        if now < prev_state.state_end_at:
            # prev still active → only in-segment refresh
            is_refresh = True
            if activity_type != prev_state.activity_type:
                return CommitResult(
                    ok=False,
                    error=(
                        f"prev state 仍未到期（{prev_state.state_end_at}），"
                        f"只允许段内 refresh，activity_type 必须等于 prev ({prev_state.activity_type})"
                    ),
                )
            if state_end_at != prev_state.state_end_at:
                return CommitResult(
                    ok=False,
                    error=(
                        "prev state 仍未到期，段内 refresh 不允许改 state_end_at"
                    ),
                )

    async with get_session() as s:
        life_state_id = await insert_life_state(
            s,
            persona_id=persona_id,
            current_state=current_state,
            activity_type=activity_type,
            response_mood=response_mood,
            reasoning=reasoning,
            skip_until=skip_until,
            state_end_at=state_end_at,
        )

    return CommitResult(ok=True, is_refresh=is_refresh, life_state_id=life_state_id)
```

- [ ] **Step 4: 在 `app/data/queries.py` 追加 `insert_life_state`**（如果现有没有这个 helper）

```python
async def insert_life_state(
    session: AsyncSession,
    *,
    persona_id: str,
    current_state: str,
    activity_type: str,
    response_mood: str,
    reasoning: str | None,
    skip_until: datetime | None,
    state_end_at: datetime | None,
) -> int:
    row = LifeEngineState(
        persona_id=persona_id,
        current_state=current_state,
        activity_type=activity_type,
        response_mood=response_mood,
        reasoning=reasoning,
        skip_until=skip_until,
        state_end_at=state_end_at,
    )
    session.add(row)
    await session.flush()
    return row.id
```

（如果已存在同名函数，追加 `state_end_at` 参数到签名并写入。）

- [ ] **Step 5: Run — expect PASS**

```bash
uv run pytest tests/unit/life/test_tool.py -v
```

- [ ] **Step 6: Commit**

```bash
git add app/life/tool.py app/data/queries.py tests/unit/life/test_tool.py
git commit -m "feat(memory-v4): commit_life_state tool with §9.5 hard validations"
```

---

### Task 3: Life Engine tick 改造为 tool-based

**Files:**
- Modify: `app/life/engine.py`
- Modify: `tests/unit/life/test_engine.py`

**改造策略**：保留 tick() 入口不变，改动 `_think` 的 LLM 调用从 JSON parse 改为 tool call：

- [ ] **Step 1: 梳理现有 `_think` 的输入输出（阅读 engine.py）**

当前 `_think` 返回 `(new_state_dict, schedule_text, duration_minutes)`。

新版改为：
1. Agent 通过 langchain tool binding 暴露 `commit_life_state` tool
2. Agent 跑一轮 → 收 tool call → tool 层校验 → 返回 CommitResult
3. `_think` 返回 `(CommitResult, schedule_text, duration_minutes)`

- [ ] **Step 2: 修改 `_think` 使用 tool**

在 `app/life/engine.py` 改：

```python
# at top of file
from langchain.tools import tool
from app.life.tool import commit_life_state_impl, CommitResult

# Agent config gains the tool binding. Existing _TICK_AGENT_CFG 构造处追加:
#   tools=[_commit_life_state_binding]  # depending on project Agent abstraction
```

具体做法要看 `app/agent/core.py` 的 `Agent` class 怎么注入 tool。如果是 langgraph 的 `bind_tools` 模式，写一个 shim：

```python
# in app/life/engine.py

_CAPTURED_COMMIT: dict[str, Any] = {}


def _make_commit_tool(persona_id: str, now, prev_state):
    @tool
    async def commit_life_state(
        activity_type: str,
        current_state: str,
        response_mood: str,
        state_end_at: str,  # ISO 8601
        skip_until: str | None = None,
        reasoning: str | None = None,
    ) -> str:
        """Commit your current life state to memory."""
        from datetime import datetime
        end = datetime.fromisoformat(state_end_at)
        skip = datetime.fromisoformat(skip_until) if skip_until else None
        result = await commit_life_state_impl(
            persona_id=persona_id,
            activity_type=activity_type,
            current_state=current_state,
            response_mood=response_mood,
            state_end_at=end,
            skip_until=skip,
            reasoning=reasoning,
            now=now,
            prev_state=prev_state,
        )
        _CAPTURED_COMMIT[persona_id] = result
        if not result.ok:
            return f"校验失败：{result.error}"
        return f"状态已提交。id={result.life_state_id} is_refresh={result.is_refresh}"
    return commit_life_state
```

在 `_think` 里：
1. 清空 `_CAPTURED_COMMIT.pop(persona_id, None)`
2. 构造 agent with `tools=[_make_commit_tool(persona_id, now, prev_state_row)]`
3. 调用 agent 跑
4. 从 `_CAPTURED_COMMIT.get(persona_id)` 拿 result
5. 如果 `result is None` → LLM 没调 tool，当失败
6. 如果 `result.ok is False` → 返回 None 让上层 retry 或 skip

- [ ] **Step 3: 修改 prompt** 告诉 LLM 用 tool

找到 `_TICK_AGENT_CFG` 关联的 Langfuse prompt（可能是 `life_engine_tick`），更新 prompt 指令强调必须通过 `commit_life_state` tool 输出结果，不要输出 JSON 或纯文本。

**通过 langfuse skill 更新：**

```
/langfuse get-prompt life_engine_tick
# 记录当前内容，加一段：
"输出：必须通过调用 `commit_life_state` tool 来提交你的状态。不要输出 JSON 或纯文本。
字段：
- activity_type: 简短活动类型（"transit"/"study"/"eating"/...）
- current_state: 一段话，描述你当下在做什么 / 感受
- response_mood: 一个词描述心情
- state_end_at: 这个状态预计什么时候结束（ISO 8601），过了这个时间必须切新状态
- skip_until: 可选，段内刷新时间点（必须 > now 且 < state_end_at）
- reasoning: 可选，为什么这么定"

# 通过 langfuse skill 写回
/langfuse update-prompt life_engine_tick ...
```

- [ ] **Step 4: 修改 tick() 读取 prev_state 时带上 state_end_at**

在 `tick()` 函数中读 `row = await Q.find_latest_life_state(s, persona_id)` 后，确保 row 包含 `state_end_at` 字段（ORM 已加，自动带）。

Skip check 改为（优先用 state_end_at）：

```python
# NEW: skip if inside skip_until (in-segment pause)
if not dry_run and not force and row and row.skip_until and now < row.skip_until:
    return None
# NEW: NO skip if past state_end_at — must switch
# (the LLM will see prev_state and decide)
```

- [ ] **Step 5: 去掉旧的 JSON parse 逻辑**

删除 `_think` 里对 LLM 输出 `tick_output = json.loads(...)` 或类似的代码 — 全部改走 tool。

- [ ] **Step 6: 保留 reviewer？**

现有 `_review_tick` 是第二层审核（检查 plausibility）。v4 里由 `commit_life_state` 校验 + LLM 自觉负责；可以**保留** reviewer 作为二次兜底，但改成 reviewer 不再拒绝 JSON 而是对已 commit 的 state 做"放弃/保留"判断（或直接简化掉）。

**建议：先保留 reviewer 逻辑不动，仅在 LLM 最终没有调用 tool 时走 retry 流程；review 本身的 LLM 调用留着。如果发现校验够强 reviewer 冗余，可后续 Plan E 清理。**

- [ ] **Step 7: 更新测试 `tests/unit/life/test_engine.py`**

原有测试如果 mock `_think` 返回 JSON dict，改成 mock `_think` 返回 `(CommitResult(ok=True, ...), schedule_text, 30)`。

新增测试：
- LLM 没调 tool → tick 返回 None
- commit_life_state 校验失败 → tick 返回 None 或进 retry

- [ ] **Step 8: Run**

```bash
uv run pytest tests/unit/life/test_engine.py tests/unit/life/test_tool.py -v
```

- [ ] **Step 9: Commit**

```bash
git add app/life/engine.py tests/unit/life/test_engine.py
git commit -m "feat(memory-v4): tick uses commit_life_state tool with state_end_at"
```

---

### Task 4: `state_only_refresh` 纯函数

**Files:**
- Create: `app/life/state_sync.py`
- Create: `tests/unit/life/test_state_sync.py`

- [ ] **Step 1: 写测试**

```python
"""Test state_only_refresh — replay Life Engine logic for schedule-triggered resync."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.life.state_sync import state_only_refresh


CST = timezone(timedelta(hours=8))


@pytest.mark.asyncio
async def test_noop_when_no_prev_state():
    with patch("app.life.state_sync.find_latest_life_state", new=AsyncMock(return_value=None)):
        result = await state_only_refresh(
            persona_id="chiwei", new_schedule_content="today..."
        )
    assert result is None


@pytest.mark.asyncio
async def test_in_segment_refresh_updates_current_state():
    now = datetime.now(CST)
    prev = MagicMock(
        activity_type="study",
        current_state="doing homework",
        state_end_at=now + timedelta(minutes=30),
        response_mood="calm",
    )
    with patch("app.life.state_sync.find_latest_life_state", new=AsyncMock(return_value=prev)):
        with patch("app.life.state_sync._run_refresh_agent", new=AsyncMock(return_value=MagicMock(ok=True, is_refresh=True))):
            result = await state_only_refresh(
                persona_id="chiwei", new_schedule_content="冰淇淋到了"
            )
    assert result is not None
    assert result.is_refresh is True


@pytest.mark.asyncio
async def test_agent_no_tool_call_returns_none():
    now = datetime.now(CST)
    prev = MagicMock(
        activity_type="study", current_state="...",
        state_end_at=now + timedelta(minutes=30), response_mood="calm",
    )
    with patch("app.life.state_sync.find_latest_life_state", new=AsyncMock(return_value=prev)):
        with patch("app.life.state_sync._run_refresh_agent", new=AsyncMock(return_value=None)):
            result = await state_only_refresh(
                persona_id="chiwei", new_schedule_content="x"
            )
    assert result is None
```

- [ ] **Step 2: 创建 `app/life/state_sync.py`**

```python
"""State-only refresh — lighter Life Engine replay triggered by schedule changes.

Called by the state_sync arq job after an update_schedule event.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.data.queries import find_latest_life_state
from app.data.session import get_session
from app.life.tool import CommitResult, commit_life_state_impl

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))


async def _run_refresh_agent(
    *,
    persona_id: str,
    prev_state,
    new_schedule_content: str,
    now: datetime,
) -> CommitResult | None:
    """Run the refresh LLM agent with commit_life_state tool binding.

    Returns CommitResult from the tool call, or None if the LLM didn't call it.
    """
    from langchain.tools import tool

    captured: dict = {}

    @tool
    async def commit_life_state(
        activity_type: str,
        current_state: str,
        response_mood: str,
        state_end_at: str,
        skip_until: str | None = None,
        reasoning: str | None = None,
    ) -> str:
        """Commit refreshed life state."""
        end = datetime.fromisoformat(state_end_at)
        skip = datetime.fromisoformat(skip_until) if skip_until else None
        r = await commit_life_state_impl(
            persona_id=persona_id,
            activity_type=activity_type,
            current_state=current_state,
            response_mood=response_mood,
            state_end_at=end,
            skip_until=skip,
            reasoning=reasoning,
            now=now,
            prev_state=prev_state,
        )
        captured["result"] = r
        if not r.ok:
            return f"校验失败：{r.error}"
        return f"已刷新。is_refresh={r.is_refresh}"

    from app.agent.core import Agent
    from langchain_core.messages import HumanMessage

    # Use Langfuse prompt `life_engine_state_refresh`
    cfg = {
        "prompt_name": "life_engine_state_refresh",
        "tools": [commit_life_state],
    }
    await Agent(cfg).run(
        messages=[HumanMessage(content="按新 schedule 重新评估状态")],
        prompt_vars={
            "prev_activity": prev_state.activity_type,
            "prev_current_state": prev_state.current_state,
            "prev_state_end_at": prev_state.state_end_at.isoformat() if prev_state.state_end_at else "",
            "new_schedule": new_schedule_content,
            "now": now.isoformat(),
        },
    )
    return captured.get("result")


async def state_only_refresh(
    *,
    persona_id: str,
    new_schedule_content: str,
    now: datetime | None = None,
) -> CommitResult | None:
    """Re-evaluate current state given a new schedule. Returns the CommitResult if
    state was refreshed/switched, else None (no prev state / LLM decided unchanged /
    validation failed).
    """
    now = now or datetime.now(CST)

    async with get_session() as s:
        prev = await find_latest_life_state(s, persona_id)
    if prev is None:
        logger.info("[%s] no prev state, skip refresh", persona_id)
        return None

    return await _run_refresh_agent(
        persona_id=persona_id,
        prev_state=prev,
        new_schedule_content=new_schedule_content,
        now=now,
    )
```

- [ ] **Step 3: 创建 Langfuse prompt `life_engine_state_refresh`**

```
/langfuse create-prompt life_engine_state_refresh

System:
你是赤尾的内部状态评估器。刚刚她的日程有了更新，你需要判断当前状态是否需要刷新。

规则（§9.5 硬约束）：
- 如果 `now >= prev_state_end_at` → 可以切新的 activity_type，必须给出新的 state_end_at
- 如果 `now < prev_state_end_at` → 只允许段内刷新（activity_type 和 state_end_at 必须等于 prev），只能改 current_state 文案和心情
- 如果看完新 schedule 你觉得当前状态完全合理无需动 → 不要调用 tool，直接说明即可

务必通过调用 `commit_life_state` tool 产出结果；不要输出 JSON。

User:
当前时间: {{now}}

上一个状态：
- activity_type: {{prev_activity}}
- current_state: {{prev_current_state}}
- state_end_at: {{prev_state_end_at}}

新的 schedule:
{{new_schedule}}

请判断并通过 commit_life_state tool 输出新状态（或说明无需变更）。
```

- [ ] **Step 4: Run — expect PASS**

```bash
uv run pytest tests/unit/life/test_state_sync.py -v
```

- [ ] **Step 5: Commit**

```bash
git add app/life/state_sync.py tests/unit/life/test_state_sync.py
git commit -m "feat(memory-v4): state_only_refresh for schedule-triggered resync"
```

---

### Task 5: arq job `sync_life_state_after_schedule`

**Files:**
- Create: `app/workers/state_sync_worker.py`
- Modify: `app/workers/arq_settings.py`
- Create: `tests/unit/workers/test_state_sync_worker.py`

- [ ] **Step 1: 写测试**

```python
"""Test state_sync arq job."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.workers.state_sync_worker import sync_life_state_after_schedule


@pytest.mark.asyncio
async def test_reads_revision_and_calls_refresh():
    rev = MagicMock(persona_id="chiwei", content="new plan")
    with patch("app.workers.state_sync_worker.get_schedule_revision_by_id", new=AsyncMock(return_value=rev)):
        with patch("app.workers.state_sync_worker.state_only_refresh", new=AsyncMock(return_value=None)) as sref:
            await sync_life_state_after_schedule(ctx={}, revision_id="sr_1")
    sref.assert_awaited_once()
    assert sref.await_args.kwargs["persona_id"] == "chiwei"
    assert sref.await_args.kwargs["new_schedule_content"] == "new plan"


@pytest.mark.asyncio
async def test_missing_revision_no_op():
    with patch("app.workers.state_sync_worker.get_schedule_revision_by_id", new=AsyncMock(return_value=None)):
        with patch("app.workers.state_sync_worker.state_only_refresh", new=AsyncMock()) as sref:
            await sync_life_state_after_schedule(ctx={}, revision_id="sr_missing")
    sref.assert_not_awaited()
```

- [ ] **Step 2: 在 `app/data/queries.py` 补 `get_schedule_revision_by_id`**

```python
async def get_schedule_revision_by_id(
    session: AsyncSession, revision_id: str
) -> ScheduleRevision | None:
    result = await session.execute(
        select(ScheduleRevision).where(ScheduleRevision.id == revision_id)
    )
    return result.scalar_one_or_none()
```

- [ ] **Step 3: 创建 `app/workers/state_sync_worker.py`**

```python
"""arq job: sync_life_state_after_schedule — consumes schedule update events."""

from __future__ import annotations

import logging
from typing import Any

from app.data.queries import get_schedule_revision_by_id
from app.data.session import get_session
from app.life.state_sync import state_only_refresh

logger = logging.getLogger(__name__)


async def sync_life_state_after_schedule(ctx: dict[str, Any], revision_id: str) -> None:
    async with get_session() as s:
        rev = await get_schedule_revision_by_id(s, revision_id)
    if rev is None:
        logger.warning("state_sync: revision %s not found, skip", revision_id)
        return
    logger.info("state_sync: refreshing for persona=%s revision=%s", rev.persona_id, revision_id)
    result = await state_only_refresh(
        persona_id=rev.persona_id,
        new_schedule_content=rev.content,
    )
    if result is None:
        logger.info("state_sync: no refresh (either LLM decided unchanged or no prev state)")
    elif result.ok:
        logger.info("state_sync: committed life state id=%s refresh=%s", result.life_state_id, result.is_refresh)
    else:
        logger.warning("state_sync: commit failed: %s", result.error)
```

- [ ] **Step 4: 注册到 `arq_settings.py` functions**

```python
# at top
from app.workers.state_sync_worker import sync_life_state_after_schedule

# in class WorkerSettings:
functions: list = [sync_life_state_after_schedule]
```

- [ ] **Step 5: 验证 `enqueue_state_sync`（Plan B Task 5）能调用成功**

在 Plan B 的 update_schedule tool 里 `enqueue_state_sync` 用的是 `arq_pool()`。如果项目缺这个 helper，添加：

```python
# app/workers/arq_settings.py 底部
from arq import create_pool

_pool = None
async def arq_pool():
    global _pool  # noqa: PLW0603
    if _pool is None:
        _pool = await create_pool(WorkerSettings.redis_settings)
    return _pool
```

- [ ] **Step 6: Run — expect PASS**

```bash
uv run pytest tests/unit/workers/test_state_sync_worker.py -v
```

- [ ] **Step 7: Commit**

```bash
git add app/workers/state_sync_worker.py app/workers/arq_settings.py app/data/queries.py tests/unit/workers/test_state_sync_worker.py
git commit -m "feat(memory-v4): sync_life_state_after_schedule arq job"
```

---

### Task 6: 合并自检

- [ ] **Step 1: 全量测试**

```bash
cd apps/agent-service
uv run pytest tests/unit/life/ tests/unit/workers/test_state_sync_worker.py tests/unit/workers/test_memory_vectorize.py -v
```

- [ ] **Step 2: lint + 类型**

```bash
uv run ruff check app tests
uv run basedpyright app tests
```

- [ ] **Step 3: 确认 Langfuse 两个 prompt 存在**

```
/langfuse get-prompt life_engine_tick
/langfuse get-prompt life_engine_state_refresh
```

都返回内容则 OK。

- [ ] **Step 4: Final commit**

```bash
git commit --allow-empty -m "chore(memory-v4): Plan D life engine + state sync ready"
```

---

## Self-Review

- ✅ `state_end_at` 列加到 DB 和 ORM（Task 1）
- ✅ `commit_life_state` tool + §9.5 五条校验（Task 2）
- ✅ tick 改造成 tool-based（Task 3）
- ✅ `state_only_refresh` + Langfuse prompt（Task 4）
- ✅ arq job + `arq_pool` helper（Task 5）
- ⚠️ Task 3 的 engine.py 改造**侵入性较大**，建议执行时先在 dev 泳道跑一轮 tick 观察 Langfuse trace
- ⚠️ `Agent` 类的 tool binding 语法取决于项目实现，如果和计划里的 pseudo-code 不符需要对齐

## Execution Handoff

Plan D 完成标志：tick 跑一轮能在 Langfuse trace 里看到 `commit_life_state` tool call + DB 里新 row 带 `state_end_at` 字段。
