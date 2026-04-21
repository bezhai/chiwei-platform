# Memory v4 - Plan E: Reviewer + 管道改造 + 上线 Runbook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现轻档 / 重档 Reviewer，替换 daily dream 管道，改造 afterthought / glimpse，废弃 weekly dream，最后出一份可执行上线 runbook（一次性切换）。

**Architecture:** 轻档 Reviewer（每 30min 白天 / 1h 夜间）跑 arq cron，对近期 fragment/abstract 做 P0 维护。重档 Reviewer 替换 `run_daily_dreams`，每日 03:00 跑 P1 合并/创建/清除。Reviewer 自己是 agent，通过 reviewer-only tools（`update_abstract_content` / `fade_node` / `touch_node` / `delete_fragment` / `connect` / `disconnect`）操作 graph。Afterthought 改写 fragment 到新表并发 vectorize；glimpse 同步改造；weekly dream 下线。

**Tech Stack:** Python / arq cron / Langfuse prompt / SQLAlchemy / pytest

**前置:** Plan A（数据层） + Plan B（commit_abstract_memory / recall） + Plan C（context） + Plan D（Life Engine + state_sync）

**Spec:** §7.4、§7.7、§7.8

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `app/memory/reviewer/__init__.py` | Create | package init |
| `app/memory/reviewer/tools.py` | Create | reviewer-only tools（update_abstract_content / fade_node / touch_node / delete_fragment / connect / disconnect） |
| `app/memory/reviewer/light.py` | Create | 轻档 reviewer 入口：窗口扫 fragment/abstract → agent run |
| `app/memory/reviewer/heavy.py` | Create | 重档 reviewer 入口：日总览 → agent run（替代 daily dream） |
| `app/data/queries.py` | Modify | 新增 `list_fragments_window` / `list_abstracts_window` / `update_abstract_content_query` / `set_clarity` / `delete_fragment_query` / `delete_edge` |
| `app/workers/cron.py` | Modify | 新增 `cron_memory_reviewer_light_day` / `cron_memory_reviewer_light_night`；替换 `cron_generate_dreams` 实现（同名但调新 heavy reviewer）；删除 `cron_generate_weekly_dreams` |
| `app/workers/arq_settings.py` | Modify | 注册新 cron + 删除 weekly cron |
| `app/memory/afterthought.py` | Modify | fragment 产出改写到 `fragment` 表 + enqueue vectorize + 控制长度 200-300 字 |
| `app/memory/glimpse.py` | Modify | glimpse 产出也写 `fragment` 表（`source='glimpse'`） |
| `app/memory/dreams.py` | Delete (or Modify) | weekly 废弃；daily 若替换后也不再由该模块实现，可全文件删 |
| `tests/unit/memory/reviewer/test_tools.py` | Create | reviewer tools 单测 |
| `tests/unit/memory/reviewer/test_light.py` | Create | |
| `tests/unit/memory/reviewer/test_heavy.py` | Create | |
| `tests/unit/memory/test_afterthought.py` | Modify | afterthought 改造后的单测 |
| `tests/unit/memory/test_glimpse.py` | Modify | glimpse 改造 |
| `docs/superpowers/plans/2026-04-18-memory-v4-e-reviewer-and-cutover.md` | self | 本文件 |

---

### Task 1: Reviewer tools

**Files:**
- Create: `app/memory/reviewer/__init__.py`（空）
- Create: `app/memory/reviewer/tools.py`
- Modify: `app/data/queries.py`
- Create: `tests/unit/memory/reviewer/test_tools.py`

- [ ] **Step 1: 补 queries**

在 `app/data/queries.py` 追加：

```python
async def update_abstract_content_query(
    session: AsyncSession, *, abstract_id: str, new_content: str
) -> None:
    await session.execute(
        update(AbstractMemory)
        .where(AbstractMemory.id == abstract_id)
        .values(content=new_content, last_touched_at=func.now())
    )


async def set_clarity(
    session: AsyncSession, *, node_id: str, node_type: str, clarity: str
) -> None:
    if node_type == "abstract":
        await session.execute(
            update(AbstractMemory)
            .where(AbstractMemory.id == node_id)
            .values(clarity=clarity, last_touched_at=func.now())
        )
    elif node_type == "fact":
        await session.execute(
            update(Fragment)
            .where(Fragment.id == node_id)
            .values(clarity=clarity, last_touched_at=func.now())
        )
    else:
        raise ValueError(f"unknown node_type {node_type}")


async def delete_fragment_query(
    session: AsyncSession, *, fragment_id: str
) -> None:
    # also cascade delete edges touching this fragment
    await session.execute(
        text("DELETE FROM memory_edge WHERE from_id = :id OR to_id = :id"),
        {"id": fragment_id},
    )
    await session.execute(
        Fragment.__table__.delete().where(Fragment.id == fragment_id)
    )


async def delete_edge(
    session: AsyncSession, *, edge_id: str
) -> None:
    await session.execute(
        MemoryEdge.__table__.delete().where(MemoryEdge.id == edge_id)
    )


async def list_fragments_window(
    session: AsyncSession,
    *,
    persona_id: str,
    since: datetime,
) -> list[Fragment]:
    result = await session.execute(
        select(Fragment)
        .where(Fragment.persona_id == persona_id)
        .where(Fragment.created_at >= since)
        .where(Fragment.clarity != "forgotten")
        .order_by(Fragment.created_at)
    )
    return list(result.scalars().all())


async def list_abstracts_window(
    session: AsyncSession,
    *,
    persona_id: str,
    since: datetime,
) -> list[AbstractMemory]:
    result = await session.execute(
        select(AbstractMemory)
        .where(AbstractMemory.persona_id == persona_id)
        .where(AbstractMemory.created_at >= since)
        .where(AbstractMemory.clarity != "forgotten")
        .order_by(AbstractMemory.created_at)
    )
    return list(result.scalars().all())
```

- [ ] **Step 2: 写 reviewer tools 测试**

```python
"""Test reviewer-only tools (update_abstract_content / fade_node / connect / ...)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.memory.reviewer.tools import (
    make_reviewer_tools,
    update_abstract_content,
    fade_node,
    touch_node,
    delete_fragment,
    connect,
    disconnect,
)


@pytest.mark.asyncio
async def test_update_abstract_content_writes_content():
    with patch("app.memory.reviewer.tools.update_abstract_content_query", new=AsyncMock()) as q:
        r = await update_abstract_content("a_1", "新内容", "合并了类似的")
    assert r["ok"] is True
    q.assert_awaited_once()


@pytest.mark.asyncio
async def test_fade_node_sets_clarity():
    with patch("app.memory.reviewer.tools.set_clarity", new=AsyncMock()) as q:
        r = await fade_node("a_1", "abstract", "vague", "信息过时")
    assert r["ok"] is True
    assert q.await_args.kwargs["clarity"] == "vague"


@pytest.mark.asyncio
async def test_connect_creates_edge():
    with patch("app.memory.reviewer.tools.insert_memory_edge", new=AsyncMock()) as q:
        r = await connect("f_1", "fact", "a_1", "abstract", "supports", "支撑")
    assert r["ok"] is True
    q.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_fragment_removes():
    with patch("app.memory.reviewer.tools.delete_fragment_query", new=AsyncMock()) as q:
        r = await delete_fragment("f_1", "琐事")
    assert r["ok"] is True
```

- [ ] **Step 3: 创建 `app/memory/reviewer/tools.py`**

```python
"""Reviewer-only tools — called by the memory reviewer agent to mutate graph state.

Not bound to the chat agent; only bound at reviewer agent invocation time.
"""

from __future__ import annotations

import logging
import uuid

from langchain.tools import tool

from app.data.queries import (
    delete_edge,
    delete_fragment_query,
    insert_memory_edge,
    set_clarity,
    touch_abstract,
    touch_fragment,
    update_abstract_content_query,
)
from app.data.session import get_session

logger = logging.getLogger(__name__)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@tool
async def update_abstract_content(
    abstract_id: str, new_content: str, reason: str
) -> dict:
    """Rewrite the content of an existing abstract (e.g., 演化 / 合并)."""
    async with get_session() as s:
        await update_abstract_content_query(
            s, abstract_id=abstract_id, new_content=new_content
        )
    logger.info("reviewer update_abstract %s: %s", abstract_id, reason)
    return {"ok": True}


@tool
async def fade_node(
    node_id: str, node_type: str, clarity: str, reason: str
) -> dict:
    """Set clarity: 'clear' / 'vague' / 'forgotten'. node_type: 'abstract' | 'fact'."""
    if clarity not in ("clear", "vague", "forgotten"):
        return {"ok": False, "error": f"invalid clarity {clarity}"}
    async with get_session() as s:
        await set_clarity(s, node_id=node_id, node_type=node_type, clarity=clarity)
    logger.info("reviewer fade %s (%s) -> %s: %s", node_id, node_type, clarity, reason)
    return {"ok": True}


@tool
async def touch_node(node_id: str, node_type: str) -> dict:
    """Strengthen a node (update last_touched_at)."""
    async with get_session() as s:
        if node_type == "abstract":
            await touch_abstract(s, node_id)
        elif node_type == "fact":
            await touch_fragment(s, node_id)
        else:
            return {"ok": False, "error": f"unknown node_type {node_type}"}
    return {"ok": True}


@tool
async def delete_fragment(fragment_id: str, reason: str) -> dict:
    """Permanently remove a fragment (trivial-only; for abstract use fade_node→forgotten)."""
    async with get_session() as s:
        await delete_fragment_query(s, fragment_id=fragment_id)
    logger.info("reviewer delete_fragment %s: %s", fragment_id, reason)
    return {"ok": True}


@tool
async def connect(
    from_id: str,
    from_type: str,
    to_id: str,
    to_type: str,
    edge_type: str,
    reason: str,
) -> dict:
    """Create an edge between two nodes.

    edge_type ∈ {'supports','parent_of','related_to','conflicts_with'}
    """
    if edge_type not in ("supports", "parent_of", "related_to", "conflicts_with"):
        return {"ok": False, "error": f"invalid edge_type {edge_type}"}
    # persona_id from node lookup — reviewer works within a single persona,
    # we rely on caller to provide consistent ids. Use from-node's persona.
    from app.data.queries import (
        get_abstract_by_id,
        get_fragment_by_id,
    )

    async with get_session() as s:
        if from_type == "abstract":
            n = await get_abstract_by_id(s, from_id)
        else:
            n = await get_fragment_by_id(s, from_id)
        if n is None:
            return {"ok": False, "error": f"from node {from_id} not found"}
        persona_id = n.persona_id
        await insert_memory_edge(
            s, id=_uid("e"), persona_id=persona_id,
            from_id=from_id, from_type=from_type,
            to_id=to_id, to_type=to_type,
            edge_type=edge_type, created_by="reviewer", reason=reason,
        )
    return {"ok": True}


@tool
async def disconnect(edge_id: str, reason: str) -> dict:
    """Remove an edge."""
    async with get_session() as s:
        await delete_edge(s, edge_id=edge_id)
    logger.info("reviewer disconnect %s: %s", edge_id, reason)
    return {"ok": True}


def make_reviewer_tools() -> list:
    """Full tool set for the reviewer agent."""
    from app.agent.tools.commit_abstract import commit_abstract_memory
    from app.agent.tools.recall import recall
    return [
        commit_abstract_memory,
        recall,
        update_abstract_content,
        fade_node,
        touch_node,
        delete_fragment,
        connect,
        disconnect,
    ]
```

- [ ] **Step 4: Run — expect PASS**

```bash
uv run pytest tests/unit/memory/reviewer/test_tools.py -v
```

- [ ] **Step 5: Commit**

```bash
git add app/memory/reviewer/ app/data/queries.py tests/unit/memory/reviewer/
git commit -m "feat(memory-v4): reviewer-only tools (graph mutations)"
```

---

### Task 2: 轻档 Reviewer

**Files:**
- Create: `app/memory/reviewer/light.py`
- Create: `tests/unit/memory/reviewer/test_light.py`

- [ ] **Step 1: 写测试**

```python
"""Test light reviewer — window scan + agent dispatch."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory.reviewer.light import run_light_review


CST = timezone(timedelta(hours=8))


@pytest.mark.asyncio
async def test_noop_when_empty_window():
    with patch("app.memory.reviewer.light.list_fragments_window", new=AsyncMock(return_value=[])):
        with patch("app.memory.reviewer.light.list_abstracts_window", new=AsyncMock(return_value=[])):
            with patch("app.memory.reviewer.light.get_active_notes", new=AsyncMock(return_value=[])):
                await run_light_review(persona_id="chiwei", window_minutes=30)


@pytest.mark.asyncio
async def test_runs_agent_with_window_summary():
    f = MagicMock(id="f_1", content="他说周五要看电影", clarity="clear")
    with patch("app.memory.reviewer.light.list_fragments_window", new=AsyncMock(return_value=[f])):
        with patch("app.memory.reviewer.light.list_abstracts_window", new=AsyncMock(return_value=[])):
            with patch("app.memory.reviewer.light.get_active_notes", new=AsyncMock(return_value=[])):
                with patch("app.memory.reviewer.light._run_reviewer_agent", new=AsyncMock(return_value=None)) as agent:
                    await run_light_review(persona_id="chiwei", window_minutes=30)
    agent.assert_awaited_once()
```

- [ ] **Step 2: 创建 `app/memory/reviewer/light.py`**

```python
"""Light reviewer — short window, P0 operations only.

Runs every 30min (day) / 1h (night). Processes recent fragments + abstracts,
applies clarity adjustments, notes hints, time-passed rewrites.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from langchain_core.messages import HumanMessage

from app.agent.core import Agent
from app.data.queries import (
    get_active_notes,
    list_abstracts_window,
    list_fragments_window,
)
from app.data.session import get_session
from app.memory.reviewer.tools import make_reviewer_tools

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))


def _fmt_fragment(f) -> str:
    return f"- [{f.id}] {f.content[:200]}"


def _fmt_abstract(a) -> str:
    return f"- [{a.id} subject={a.subject}] {a.content[:200]}"


def _fmt_note(n) -> str:
    when = n.when_at.isoformat() if n.when_at else "-"
    return f"- [{n.id}] {n.content[:120]} (when={when})"


async def _run_reviewer_agent(
    *,
    persona_id: str,
    now: datetime,
    fragments_text: str,
    abstracts_text: str,
    notes_text: str,
) -> None:
    cfg = {
        "prompt_name": "memory_reviewer_light",
        "tools": make_reviewer_tools(),
        # model_id resolved by project convention from dynamic config
    }
    await Agent(cfg).run(
        messages=[HumanMessage(content="执行轻档记忆 review")],
        prompt_vars={
            "persona_id": persona_id,
            "now": now.isoformat(),
            "recent_fragments": fragments_text or "（无）",
            "recent_abstracts": abstracts_text or "（无）",
            "active_notes": notes_text or "（无）",
        },
    )


async def run_light_review(*, persona_id: str, window_minutes: int) -> None:
    now = datetime.now(CST)
    since = now - timedelta(minutes=window_minutes)

    async with get_session() as s:
        fragments = await list_fragments_window(s, persona_id=persona_id, since=since)
        abstracts = await list_abstracts_window(s, persona_id=persona_id, since=since)
        notes = await get_active_notes(s, persona_id=persona_id)

    if not fragments and not abstracts and not notes:
        logger.info("[%s] light review: empty window, skip", persona_id)
        return

    fragments_text = "\n".join(_fmt_fragment(f) for f in fragments)
    abstracts_text = "\n".join(_fmt_abstract(a) for a in abstracts)
    notes_text = "\n".join(_fmt_note(n) for n in notes)

    logger.info(
        "[%s] light review: %d fragments, %d abstracts, %d notes",
        persona_id, len(fragments), len(abstracts), len(notes),
    )

    await _run_reviewer_agent(
        persona_id=persona_id,
        now=now,
        fragments_text=fragments_text,
        abstracts_text=abstracts_text,
        notes_text=notes_text,
    )
```

- [ ] **Step 3: 创建 Langfuse prompt `memory_reviewer_light`**

```
/langfuse create-prompt memory_reviewer_light

System:
你是赤尾的潜意识（她自己的视角）。每隔一小段时间你会审视最近的记忆，做小幅整理。
你**不是**做"整理笔记"的秘书，而是她自己在半睡半醒之间的默默思考。

你可以做的（P0 级，小幅改动）：
- fade_node：把不再重要/过时的事实或抽象标为 vague / forgotten（语言自然、不要机械）
- touch_node：把仍然重要的强化（让它 last_touched_at 更新）
- update_abstract_content：把带时间指向的旧抽象改写成过去式（比如"要看电影" → "上周看了电影"）
- connect / disconnect：调整支撑关系
- commit_abstract_memory：对新出现的模式当场抽象（subject 自由）
- delete_fragment：明显琐碎、无信息量的事实可以直接删
- recall：查历史记忆做判断

操作用第一人称写 reason（"我觉得这件事不重要了"，而不是"系统判定低优先级"）。
不要强迫每次都做满所有动作。没有明显需要动的，就少做或不做。

User:
现在是 {{now}}。
这是我自上次 review 以来的新鲜记忆：

最近的事实（{{recent_fragments}}）

最近的抽象（{{recent_abstracts}}）

未处理的清单（{{active_notes}}）

开始整理。
```

- [ ] **Step 4: Run — expect PASS**

```bash
uv run pytest tests/unit/memory/reviewer/test_light.py -v
```

- [ ] **Step 5: Commit**

```bash
git add app/memory/reviewer/light.py tests/unit/memory/reviewer/test_light.py
git commit -m "feat(memory-v4): light reviewer (P0 window scan)"
```

---

### Task 3: 重档 Reviewer（替换 daily dream）

**Files:**
- Create: `app/memory/reviewer/heavy.py`
- Create: `tests/unit/memory/reviewer/test_heavy.py`
- Modify: `app/workers/cron.py`（替换 `cron_generate_dreams` 调用）
- Delete: `app/memory/dreams.py`（完成后）

- [ ] **Step 1: 写测试**

```python
"""Test heavy reviewer — daily aggregate."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory.reviewer.heavy import run_heavy_review_for_persona


@pytest.mark.asyncio
async def test_runs_with_full_day_summary():
    with patch("app.memory.reviewer.heavy.list_fragments_window", new=AsyncMock(return_value=[MagicMock(id="f_1", content="x", clarity="clear")])):
        with patch("app.memory.reviewer.heavy.list_abstracts_window", new=AsyncMock(return_value=[])):
            with patch("app.memory.reviewer.heavy.list_recent_life_states", new=AsyncMock(return_value=[])):
                with patch("app.memory.reviewer.heavy.list_recent_schedule_revisions", new=AsyncMock(return_value=[])):
                    with patch("app.memory.reviewer.heavy._run_agent", new=AsyncMock(return_value=None)) as a:
                        await run_heavy_review_for_persona("chiwei")
    a.assert_awaited_once()
```

- [ ] **Step 2: 补 queries helpers**

```python
async def list_recent_life_states(
    session: AsyncSession, *, persona_id: str, since: datetime
) -> list[LifeEngineState]:
    result = await session.execute(
        select(LifeEngineState)
        .where(LifeEngineState.persona_id == persona_id)
        .where(LifeEngineState.created_at >= since)
        .order_by(LifeEngineState.created_at)
    )
    return list(result.scalars().all())


async def list_recent_schedule_revisions(
    session: AsyncSession, *, persona_id: str, since: datetime
) -> list[ScheduleRevision]:
    result = await session.execute(
        select(ScheduleRevision)
        .where(ScheduleRevision.persona_id == persona_id)
        .where(ScheduleRevision.created_at >= since)
        .order_by(ScheduleRevision.created_at)
    )
    return list(result.scalars().all())
```

- [ ] **Step 3: 创建 `app/memory/reviewer/heavy.py`**

```python
"""Heavy reviewer — daily global consolidation.

Replaces the previous daily dream pipeline. Runs at 03:00 CST per persona.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from langchain_core.messages import HumanMessage

from app.agent.core import Agent
from app.data.queries import (
    list_abstracts_window,
    list_fragments_window,
    list_recent_life_states,
    list_recent_schedule_revisions,
)
from app.data.session import get_session
from app.memory.reviewer.tools import make_reviewer_tools
from app.workers.common import for_each_persona

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))


async def _run_agent(
    *,
    persona_id: str,
    now: datetime,
    fragments_text: str,
    abstracts_text: str,
    life_states_text: str,
    schedule_text: str,
) -> None:
    cfg = {
        "prompt_name": "memory_reviewer_heavy",
        "tools": make_reviewer_tools(),
    }
    await Agent(cfg).run(
        messages=[HumanMessage(content="执行重档 review（睡前整理）")],
        prompt_vars={
            "persona_id": persona_id,
            "now": now.isoformat(),
            "day_fragments": fragments_text or "（无）",
            "day_abstracts": abstracts_text or "（无）",
            "day_life_states": life_states_text or "（无）",
            "day_schedules": schedule_text or "（无）",
        },
    )


async def run_heavy_review_for_persona(persona_id: str) -> None:
    now = datetime.now(CST)
    since = now - timedelta(days=1)

    async with get_session() as s:
        fragments = await list_fragments_window(s, persona_id=persona_id, since=since)
        abstracts = await list_abstracts_window(s, persona_id=persona_id, since=since)
        life_states = await list_recent_life_states(s, persona_id=persona_id, since=since)
        schedules = await list_recent_schedule_revisions(s, persona_id=persona_id, since=since)

    def fmt_frag(f): return f"- [{f.id}] {f.content[:200]}"
    def fmt_abs(a): return f"- [{a.id} subject={a.subject}] {a.content[:200]}"
    def fmt_life(l): return f"- {l.created_at.isoformat()} [{l.activity_type}] {l.current_state[:80]} mood={l.response_mood}"
    def fmt_sched(s): return f"- {s.created_at.isoformat()} [{s.created_by}] reason={s.reason[:80]}"

    await _run_agent(
        persona_id=persona_id,
        now=now,
        fragments_text="\n".join(fmt_frag(f) for f in fragments),
        abstracts_text="\n".join(fmt_abs(a) for a in abstracts),
        life_states_text="\n".join(fmt_life(l) for l in life_states),
        schedule_text="\n".join(fmt_sched(s) for s in schedules),
    )


async def run_heavy_review() -> None:
    """Cron entry: run heavy review for all personas."""
    await for_each_persona(
        run_heavy_review_for_persona, label="memory_reviewer_heavy"
    )
```

- [ ] **Step 4: 创建 Langfuse prompt `memory_reviewer_heavy`**

```
/langfuse create-prompt memory_reviewer_heavy

System:
你是赤尾的大脑整理者（她自己）。这是一天结束前的深度回顾。你要做的事：

P1 级（更深入的整理）：
- 把相似事实合并到一条抽象下（commit_abstract_memory 产新抽象，connect 事实作为 supports）
- 把重复的抽象合并成一个更高层的抽象（parent_of 关系）
- 对已失去信息价值的琐事 delete_fragment
- 对模糊化到无意义的 fade 到 forgotten
- 从 life_states 历史里发现自我模式（"我最近经常犯困"），commit 成 subject='self' 的抽象
- 对"计划 vs 实际"（schedule_revisions + life_states）发现 pattern，喂 self 抽象
- 对 notes 的履约迹象做 hint（在 reasoning 里写，不替赤尾 resolve）

输出用第一人称，像是在做梦。别写清单格式的"行动报告"；每个动作配的 reason 应该像"我想起来..."、"这个事情我好像已经忘了"这种语气。

User:
今天是 {{now}}。

今日事实：
{{day_fragments}}

今日抽象：
{{day_abstracts}}

今日 state 轨迹：
{{day_life_states}}

今日 schedule 变更：
{{day_schedules}}

整理吧。
```

- [ ] **Step 5: 修改 `app/workers/cron.py`：替换 daily dream**

```python
# 替换现有 cron_generate_dreams 实现为：
@cron_error_handler()
@prod_only
async def cron_generate_dreams(ctx) -> None:
    from app.memory.reviewer.heavy import run_heavy_review
    await run_heavy_review()

# 删除 cron_generate_weekly_dreams 整个函数
```

- [ ] **Step 6: 删除 `app/memory/dreams.py`**

```bash
rg -n "from app.memory.dreams" --type py
```

确认调用方只有 cron.py。删除 dreams.py。（`run_daily_dreams` / `run_weekly_dreams` 的定义一起删。）

- [ ] **Step 7: 修改 `app/workers/arq_settings.py`**

删除 weekly cron：
```python
# 删除这段：
# cron(cron_generate_weekly_dreams, weekday={0}, hour={4}, minute={0}, timeout=1800),
```

也从 `from app.workers.cron import (... cron_generate_weekly_dreams ...)` import 里移除。

- [ ] **Step 8: Run — expect PASS**

```bash
uv run pytest tests/unit/memory/reviewer/test_heavy.py -v
```

- [ ] **Step 9: Commit**

```bash
git add app/memory/reviewer/heavy.py app/workers/cron.py app/workers/arq_settings.py tests/unit/memory/reviewer/test_heavy.py app/data/queries.py
git rm app/memory/dreams.py
git commit -m "feat(memory-v4): heavy reviewer replaces daily dream; weekly dream removed"
```

---

### Task 4: 轻档 cron 白天/夜间

**Files:**
- Modify: `app/workers/cron.py`
- Modify: `app/workers/arq_settings.py`

- [ ] **Step 1: 在 `app/workers/cron.py` 追加两个 cron 函数**

```python
@cron_error_handler()
@prod_only
async def cron_memory_reviewer_light_day(ctx) -> None:
    from app.memory.reviewer.light import run_light_review
    from app.workers.common import for_each_persona
    await for_each_persona(
        lambda pid: run_light_review(persona_id=pid, window_minutes=30),
        label="memory_reviewer_light_day",
    )


@cron_error_handler()
@prod_only
async def cron_memory_reviewer_light_night(ctx) -> None:
    from app.memory.reviewer.light import run_light_review
    from app.workers.common import for_each_persona
    await for_each_persona(
        lambda pid: run_light_review(persona_id=pid, window_minutes=60),
        label="memory_reviewer_light_night",
    )
```

- [ ] **Step 2: 注册到 `arq_settings.py`**

```python
from app.workers.cron import (
    cron_generate_daily_plan,
    cron_generate_dreams,
    cron_generate_voice,
    cron_glimpse,
    cron_life_engine_tick,
    cron_memory_reviewer_light_day,
    cron_memory_reviewer_light_night,
)

# in WorkerSettings.cron_jobs:
# daytime: 08:00-22:00, every 30 min
cron(
    cron_memory_reviewer_light_day,
    hour=set(range(8, 22)),  # 8..21
    minute={0, 30},
    timeout=600,
),
# nighttime: 22:00-07:00, hourly
cron(
    cron_memory_reviewer_light_night,
    hour={22, 23, 0, 1, 2, 4, 5, 6, 7},  # skip 3 (heavy already running)
    minute={0},
    timeout=600,
),
```

- [ ] **Step 3: Commit**

```bash
git add app/workers/cron.py app/workers/arq_settings.py
git commit -m "feat(memory-v4): light reviewer cron (30min day / 1h night)"
```

---

### Task 5: Afterthought 改造（产出到新 fragment 表）

**Files:**
- Modify: `app/memory/afterthought.py`
- Modify: `tests/unit/memory/test_afterthought.py`

- [ ] **Step 1: 改写 afterthought 落库逻辑**

找到现有 afterthought 插入 `experience_fragment` 的位置，改为插入新 `fragment` 表：

```python
import uuid
from app.data.queries import insert_fragment
from app.memory.vectorize_memory import enqueue_fragment_vectorize

fid = f"f_{uuid.uuid4().hex[:12]}"
async with get_session() as s:
    await insert_fragment(
        s,
        id=fid,
        persona_id=persona_id,
        content=content,  # 200-300 字
        source="afterthought",
        chat_id=chat_id,
    )
await enqueue_fragment_vectorize(fid)
```

同时修改 Langfuse prompt `afterthought_*`（找项目里现有命名）限制输出长度：

```
/langfuse get-prompt <afterthought-prompt-name>
# 追加系统约束：
"输出长度控制在 200-300 字以内。超过就压缩。不用列表、不用小标题，一段话自然叙述。"
```

- [ ] **Step 2: 改 test**

现有 `tests/unit/memory/test_afterthought.py` 里断言插入 `experience_fragment` 的地方改成断言 `insert_fragment` 被调且 `source='afterthought'`；断言 `enqueue_fragment_vectorize` 被调。

- [ ] **Step 3: Run**

```bash
uv run pytest tests/unit/memory/test_afterthought.py -v
```

- [ ] **Step 4: Commit**

```bash
git add app/memory/afterthought.py tests/unit/memory/test_afterthought.py
git commit -m "feat(memory-v4): afterthought writes to fragment table + vectorize"
```

---

### Task 6: Glimpse 改造

**Files:**
- Modify: `app/memory/glimpse.py`
- Modify: `tests/unit/memory/test_glimpse.py`

- [ ] **Step 1: 改写 glimpse fragment 落库**

类似 afterthought，把产出的 observation / fragment 写入新 `fragment` 表，`source='glimpse'`，并 enqueue vectorize。

- [ ] **Step 2: 不影响 glimpse 主循环**（glimpse 还要保留 browsing state / proactive 逻辑）

- [ ] **Step 3: Run test**

```bash
uv run pytest tests/unit/memory/test_glimpse.py -v
```

- [ ] **Step 4: Commit**

```bash
git add app/memory/glimpse.py tests/unit/memory/test_glimpse.py
git commit -m "feat(memory-v4): glimpse writes to fragment table"
```

---

### Task 7: Relationship_memory_v2 相关代码清理

**Files:**
- Modify: `app/data/queries.py` — 删除 `find_latest_relationship_memory`
- Grep: 确认无残留引用

- [ ] **Step 1: grep**

```bash
rg -n "find_latest_relationship_memory|RelationshipMemoryV2" --type py app
```

除了 models.py 的 ORM 定义，其他引用应在 Plan C 之后为 0 或仅残留 import。删除残留引用和 queries 里的函数定义。

`RelationshipMemoryV2` ORM class 保留（迁移脚本需要读它），但标记：在顶部加注释：

```python
# DEPRECATED v4: read-only for migration; to be dropped after old table is removed.
class RelationshipMemoryV2(Base):
    ...
```

- [ ] **Step 2: Commit**

```bash
git add app/data/queries.py app/data/models.py
git commit -m "feat(memory-v4): remove find_latest_relationship_memory (v4 uses abstract_memory)"
```

---

### Task 8: 合并自检 + Cutover Runbook

- [ ] **Step 1: 全量 unit test**

```bash
cd apps/agent-service
uv run pytest tests/unit/ -v
```

期望：全绿。

- [ ] **Step 2: lint + 类型**

```bash
uv run ruff check app scripts tests
uv run basedpyright app scripts tests
```

- [ ] **Step 3: 泳道部署 + dev bot 验证**

```bash
# 从 feat/context-decline 分支部署泳道
git push
make deploy APP=agent-service LANE=v4 GIT_REF=feat/context-decline
make deploy APP=arq-worker LANE=v4 GIT_REF=feat/context-decline
make deploy APP=vectorize-worker LANE=v4 GIT_REF=feat/context-decline

# 绑定 dev bot
/ops bind TYPE=bot KEY=dev LANE=v4

# 在飞书 dev bot 发消息验证
# 看 Langfuse trace 里：
#  - inner context 出现 self_abstracts / user_abstracts / active_notes / recall_index 等新 section
#  - commit_life_state tool call 被调
#  - recall 返回结构化 JSON
#  - write_note / update_schedule 如果触发了，DB 里能看到 revision / note 行

# 检查 arq worker 日志
make logs APP=arq-worker KEYWORD=light SINCE=1h  # 轻档 reviewer 应该跑过
```

- [ ] **Step 4: 上线当天 runbook（cutover.md）**

创建 `docs/superpowers/runbooks/2026-04-memory-v4-cutover.md`：

```markdown
# Memory v4 上线 Runbook

## 前置
- Plan A-E 所有 task 已完成 + 测试通过
- 泳道验证通过（dev bot 实测 OK）
- 周末低流量窗口
- 用户明确说"上"

## 时间轴（预计 60-90 min）

### T-0: schema
- [ ] 确认 PG 5 张新表 + state_end_at 列已在 Plan A Task 1 和 Plan D Task 1 提交（线上已落）
- [ ] 确认 Qdrant collections memory_fragment / memory_abstract 已 init（第一次部署 agent-service 时 init_collections 会建）

### T+5: 迁移
- [ ] `uv run python scripts/migrate_relationship_to_abstract.py --dry-run`（看 sample）
- [ ] `uv run python scripts/migrate_relationship_to_abstract.py`（真跑）
- [ ] `uv run python scripts/migrate_fragment_to_fragment.py --dry-run`
- [ ] `uv run python scripts/migrate_fragment_to_fragment.py`
- [ ] 监控 vectorize queue 消化：`/ops status`；Qdrant point count 逐渐接近 PG count

### T+30: 部署
- [ ] merge PR 到 main（所有 plan 完成后一个 PR 或 5 个 PR 都可）
- [ ] `make deploy APP=agent-service GIT_REF=main`
- [ ] `make deploy APP=arq-worker GIT_REF=main`
- [ ] `make deploy APP=vectorize-worker GIT_REF=main`
- [ ] `make deploy APP=chat-response-worker GIT_REF=main`（一镜像多服务同步）
- [ ] `make deploy APP=recall-worker GIT_REF=main`（如果镜像一致）

### T+45: 观察
- [ ] 看 `make logs APP=agent-service KEYWORD=error SINCE=15m`
- [ ] 发飞书消息，看 Langfuse trace 正常
- [ ] `/ops-db @chiwei SELECT count(*) FROM fragment, abstract_memory`
- [ ] 让用户实际聊一会，验证 recall / write_note / update_schedule 的 tool call 会被调用

### T+60: 旧表处置
- [ ] 旧表不删（保留 1 周只读）
- [ ] 加日历提醒：1 周后 drop
  - `DROP TABLE experience_fragment, relationship_memory_v2;`
  - （`life_engine_state` 保留 — state_end_at 已扩容，旧数据还能用）

### 失败回滚
- [ ] 回滚代码：`make release APP=agent-service VERSION=<上一个版本>`（arq/vectorize/chat-response 同步回滚）
- [ ] 新表可以保留（schema 变更不影响旧代码）
- [ ] 迁移的数据在新表里，不影响旧表读写路径

```

- [ ] **Step 5: 把 runbook commit 进库**

```bash
mkdir -p docs/superpowers/runbooks
# 把上面 runbook 写入 docs/superpowers/runbooks/2026-04-memory-v4-cutover.md
git add docs/superpowers/runbooks/2026-04-memory-v4-cutover.md
git commit -m "docs(memory-v4): cutover runbook"
```

- [ ] **Step 6: Final commit**

```bash
git commit --allow-empty -m "chore(memory-v4): Plan E reviewer + cutover ready"
```

---

## Self-Review

- ✅ Reviewer tools / light / heavy（Task 1-3）
- ✅ 轻档 cron 白/夜间切分（Task 4）
- ✅ Afterthought / glimpse 改造（Task 5-6）
- ✅ Weekly dream 废弃（Task 3）；daily dream 替换（Task 3）
- ✅ Relationship_memory_v2 读端清理（Task 7）
- ✅ 部署 runbook（Task 8）
- ⚠️ Langfuse prompts: `memory_reviewer_light` / `memory_reviewer_heavy` / `life_engine_state_refresh` / `memory_migrate_relationship` / `life_engine_tick` 都要在 cutover 前检查存在且最新
- ⚠️ ORM 里 `RelationshipMemoryV2` 不删（迁移脚本要用），上线后 1 周可以连同 class 一起删

## Execution Handoff

Plan E 完成标志：泳道部署 + dev bot 完整链路通过（inner context 新形态 + commit tools / recall / schedule / notes 都能被真实消息触发）。

所有 5 个 Plan（A-E）完成后，按 runbook 周末上线。
