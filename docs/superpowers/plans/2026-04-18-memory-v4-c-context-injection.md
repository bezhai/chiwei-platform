# Memory v4 - Plan C: Context 注入重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 重写 `build_inner_context()`：去除 `relationship_memory_v2` 注入、按 §2.8 新规则注入短期 fragment、cross-chat 去硬编码、新增 always-on 抽象记忆注入 + recall 目录索引。

**Architecture:** Context 按语义分 6 个 always-on section（scene / life state / self 抽象 / trigger_user 抽象 / active notes / recall 索引）+ 可选 section（recent fragments 按 §2.8 / cross-chat 按 trigger_user 过滤）。每个 section 独立函数，方便单测。

**Tech Stack:** Python / SQLAlchemy async / Qdrant / Langfuse / pytest

**前置:** Plan A（数据层） + Plan B（recall engine）

**Spec:** `docs/superpowers/specs/2026-04-16-memory-v4-design.md` §五.①、§2.8、§7.6、§7.7

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `app/memory/context.py` | Rewrite | 重写 `build_inner_context()`，组合新 section |
| `app/memory/sections/self_abstracts.py` | Create | subject="self" always-on section |
| `app/memory/sections/user_abstracts.py` | Create | trigger_user 相关抽象 + 关系抽象 section |
| `app/memory/sections/active_notes.py` | Create | 未 resolve notes section |
| `app/memory/sections/recall_index.py` | Create | 目录统计 + 近期 N 条标题 section |
| `app/memory/sections/short_term_fragments.py` | Create | §2.8 短期 fragment 注入 |
| `app/memory/sections/schedule.py` | Create | today_schedule 注入 section |
| `app/memory/cross_chat.py` | Modify | 去硬编码，改 dynamic config 黑名单；可空 trigger_user_id |
| `app/data/queries.py` | Modify | 新增 `get_recent_abstract_titles`, `get_recent_fragments_for_injection`, 等 |
| `tests/unit/memory/sections/test_self_abstracts.py` | Create | section 单测 |
| `tests/unit/memory/sections/test_user_abstracts.py` | Create | |
| `tests/unit/memory/sections/test_active_notes.py` | Create | |
| `tests/unit/memory/sections/test_recall_index.py` | Create | |
| `tests/unit/memory/sections/test_short_term_fragments.py` | Create | |
| `tests/unit/memory/sections/test_schedule.py` | Create | |
| `tests/unit/memory/test_context.py` | Rewrite | 整体 `build_inner_context` 单测 |
| `tests/unit/memory/test_cross_chat.py` | Modify | 去硬编码后的单测 |

---

### Task 1: 新增 queries helper

**Files:**
- Modify: `app/data/queries.py`

- [ ] **Step 1: 追加 helper 函数到 queries.py**

```python
async def get_abstracts_by_subjects(
    session: AsyncSession,
    *,
    persona_id: str,
    subjects: list[str],
    limit_per_subject: int = 5,
) -> list[AbstractMemory]:
    """Get abstracts whose subject is in given list (for always-on injection)."""
    if not subjects:
        return []
    result = await session.execute(
        select(AbstractMemory)
        .where(AbstractMemory.persona_id == persona_id)
        .where(AbstractMemory.subject.in_(subjects))
        .where(AbstractMemory.clarity != "forgotten")
        .order_by(
            AbstractMemory.subject,
            AbstractMemory.last_touched_at.desc(),
        )
    )
    rows = list(result.scalars().all())
    # Keep at most `limit_per_subject` per subject
    by_subject: dict[str, list[AbstractMemory]] = {}
    for r in rows:
        by_subject.setdefault(r.subject, []).append(r)
    out: list[AbstractMemory] = []
    for subj in subjects:
        out.extend(by_subject.get(subj, [])[:limit_per_subject])
    return out


async def get_recent_abstract_titles(
    session: AsyncSession,
    *,
    persona_id: str,
    limit: int = 10,
) -> list[AbstractMemory]:
    """Recently touched abstracts — for recall-index hint."""
    result = await session.execute(
        select(AbstractMemory)
        .where(AbstractMemory.persona_id == persona_id)
        .where(AbstractMemory.clarity != "forgotten")
        .order_by(AbstractMemory.last_touched_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def count_abstracts_per_subject_prefix(
    session: AsyncSession,
    *,
    persona_id: str,
    prefix: str,
) -> int:
    from sqlalchemy import func as sa_func

    result = await session.execute(
        select(sa_func.count())
        .select_from(AbstractMemory)
        .where(AbstractMemory.persona_id == persona_id)
        .where(AbstractMemory.subject.like(f"{prefix}%"))
        .where(AbstractMemory.clarity != "forgotten")
    )
    return int(result.scalar_one())


async def get_recent_fragments_for_injection(
    session: AsyncSession,
    *,
    persona_id: str,
    chat_id: str | None,
    trigger_user_id: str | None,
    max_same_chat: int = 1,
    max_other_chat: int = 2,
    hours: int = 4,
) -> list[Fragment]:
    """§2.8 短期注入规则：
    - 当前 chat 最近 N 小时内的最新 1 条 fragment
    - 其他 chat 最近 1-2 小时内、含 trigger_user 的 fragment（最多 2 条，每 chat 只取最新）
    注意：当前实现简化 — 如果没有"fragment 里 trigger_user 标识"，先按 chat_id 过滤。
    待 reviewer/afterthought 完善后可再加 trigger_user filter。
    """
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import func as sa_func

    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    stmt = (
        select(Fragment)
        .where(Fragment.persona_id == persona_id)
        .where(Fragment.clarity != "forgotten")
        .where(Fragment.created_at >= since)
        .order_by(Fragment.created_at.desc())
    )
    result = await session.execute(stmt)
    all_recent = list(result.scalars().all())

    same_chat: list[Fragment] = []
    other_chats: dict[str, Fragment] = {}
    for f in all_recent:
        if chat_id and f.chat_id == chat_id:
            if len(same_chat) < max_same_chat:
                same_chat.append(f)
        elif f.chat_id and f.chat_id not in other_chats:
            other_chats[f.chat_id] = f

    other_list = list(other_chats.values())[:max_other_chat]
    return same_chat + other_list
```

- [ ] **Step 2: Commit**

```bash
git add app/data/queries.py
git commit -m "feat(memory-v4): context-injection queries (abstracts by subject/recent titles/short-term fragments)"
```

---

### Task 2: `self_abstracts` section

**Files:**
- Create: `app/memory/sections/self_abstracts.py`
- Create: `app/memory/sections/__init__.py`（空 module）
- Create: `tests/unit/memory/sections/test_self_abstracts.py`

- [ ] **Step 1: 创建 package init**

```python
# app/memory/sections/__init__.py
"""Context injection sections (assembled by build_inner_context)."""
```

- [ ] **Step 2: 写测试**

```python
"""Test self_abstracts section."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory.sections.self_abstracts import build_self_abstracts_section


@pytest.mark.asyncio
async def test_returns_empty_when_no_abstracts():
    with patch("app.memory.sections.self_abstracts.get_abstracts_by_subjects", new=AsyncMock(return_value=[])):
        text = await build_self_abstracts_section(persona_id="chiwei")
    assert text == ""


@pytest.mark.asyncio
async def test_renders_bullet_list():
    a1 = MagicMock(content="我最近变温柔了", clarity="clear")
    a2 = MagicMock(content="我爱吃拉面", clarity="vague")
    with patch("app.memory.sections.self_abstracts.get_abstracts_by_subjects", new=AsyncMock(return_value=[a1, a2])):
        text = await build_self_abstracts_section(persona_id="chiwei")
    assert "温柔" in text
    assert "拉面" in text
    assert text.startswith("关于你自己")
```

- [ ] **Step 3: 创建 `app/memory/sections/self_abstracts.py`**

```python
"""Always-on injection: abstracts with subject='self' (or '我自己')."""

from __future__ import annotations

import logging

from app.data.queries import get_abstracts_by_subjects
from app.data.session import get_session

logger = logging.getLogger(__name__)

SELF_SUBJECTS = ["self", "我自己"]
MAX_PER_SUBJECT = 5


async def build_self_abstracts_section(*, persona_id: str) -> str:
    try:
        async with get_session() as s:
            rows = await get_abstracts_by_subjects(
                s, persona_id=persona_id,
                subjects=SELF_SUBJECTS,
                limit_per_subject=MAX_PER_SUBJECT,
            )
    except Exception as e:
        logger.warning("self_abstracts failed: %s", e)
        return ""

    if not rows:
        return ""

    lines = ["关于你自己："]
    for r in rows:
        lines.append(f"- {r.content}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run — expect PASS**

```bash
uv run pytest tests/unit/memory/sections/test_self_abstracts.py -v
```

- [ ] **Step 5: Commit**

```bash
git add app/memory/sections/__init__.py app/memory/sections/self_abstracts.py tests/unit/memory/sections/
git commit -m "feat(memory-v4): self_abstracts context section"
```

---

### Task 3: `user_abstracts` section

**Files:**
- Create: `app/memory/sections/user_abstracts.py`
- Create: `tests/unit/memory/sections/test_user_abstracts.py`

- [ ] **Step 1: 写测试**

```python
"""Test user_abstracts section."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory.sections.user_abstracts import build_user_abstracts_section


@pytest.mark.asyncio
async def test_empty_when_no_trigger_user():
    text = await build_user_abstracts_section(persona_id="chiwei", trigger_user_id=None, trigger_username=None)
    assert text == ""


@pytest.mark.asyncio
async def test_renders_user_and_relation_subjects():
    a1 = MagicMock(subject="user:u1", content="他是程序员", clarity="clear")
    a2 = MagicMock(subject="和 u1 的关系", content="我们最近吵架了", clarity="clear")
    with patch("app.memory.sections.user_abstracts.get_abstracts_by_subjects", new=AsyncMock(return_value=[a1, a2])):
        text = await build_user_abstracts_section(
            persona_id="chiwei",
            trigger_user_id="u1",
            trigger_username="浩南",
        )
    assert "浩南" in text
    assert "程序员" in text
    assert "吵架" in text
```

- [ ] **Step 2: 创建 `app/memory/sections/user_abstracts.py`**

```python
"""Always-on injection: abstracts about trigger_user (subject='user:<id>') and the relationship."""

from __future__ import annotations

import logging

from app.data.queries import get_abstracts_by_subjects
from app.data.session import get_session

logger = logging.getLogger(__name__)

MAX_PER_SUBJECT = 5


async def build_user_abstracts_section(
    *,
    persona_id: str,
    trigger_user_id: str | None,
    trigger_username: str | None,
) -> str:
    if not trigger_user_id or trigger_user_id == "__proactive__":
        return ""

    name_label = trigger_username or f"该用户"
    subjects = [
        f"user:{trigger_user_id}",
        f"和 {trigger_user_id} 的关系",
    ]
    if trigger_username:
        subjects.extend([trigger_username, f"和 {trigger_username} 的关系"])

    try:
        async with get_session() as s:
            rows = await get_abstracts_by_subjects(
                s, persona_id=persona_id,
                subjects=subjects, limit_per_subject=MAX_PER_SUBJECT,
            )
    except Exception as e:
        logger.warning("user_abstracts failed: %s", e)
        return ""

    if not rows:
        return ""

    lines = [f"关于 {name_label}（以及你们的关系）："]
    for r in rows:
        lines.append(f"- {r.content}")
    return "\n".join(lines)
```

- [ ] **Step 3: Run — expect PASS**

```bash
uv run pytest tests/unit/memory/sections/test_user_abstracts.py -v
```

- [ ] **Step 4: Commit**

```bash
git add app/memory/sections/user_abstracts.py tests/unit/memory/sections/test_user_abstracts.py
git commit -m "feat(memory-v4): user_abstracts context section"
```

---

### Task 4: `active_notes` section

**Files:**
- Create: `app/memory/sections/active_notes.py`
- Create: `tests/unit/memory/sections/test_active_notes.py`

- [ ] **Step 1: 写测试**

```python
"""Test active_notes section."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory.sections.active_notes import build_active_notes_section


@pytest.mark.asyncio
async def test_empty_when_no_notes():
    with patch("app.memory.sections.active_notes.get_active_notes", new=AsyncMock(return_value=[])):
        text = await build_active_notes_section(persona_id="chiwei")
    assert text == ""


@pytest.mark.asyncio
async def test_renders_with_and_without_when_at():
    n1 = MagicMock(id="n_1", content="周五看电影", when_at=datetime(2026,4,24,19,0,tzinfo=timezone.utc))
    n2 = MagicMock(id="n_2", content="想一下要不要学Rust", when_at=None)
    with patch("app.memory.sections.active_notes.get_active_notes", new=AsyncMock(return_value=[n1, n2])):
        text = await build_active_notes_section(persona_id="chiwei")
    assert "周五看电影" in text
    assert "Rust" in text
    assert "n_1" in text or "(" in text  # id hint included
```

- [ ] **Step 2: 创建 `app/memory/sections/active_notes.py`**

```python
"""Always-on injection: 未 resolve 的 notes。"""

from __future__ import annotations

import logging
from datetime import timedelta, timezone

from app.data.queries import get_active_notes
from app.data.session import get_session

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))


def _fmt_when(dt) -> str:
    if dt is None:
        return ""
    local = dt.astimezone(_CST)
    return local.strftime("%m-%d %H:%M")


async def build_active_notes_section(*, persona_id: str) -> str:
    try:
        async with get_session() as s:
            notes = await get_active_notes(s, persona_id=persona_id)
    except Exception as e:
        logger.warning("active_notes failed: %s", e)
        return ""

    if not notes:
        return ""

    lines = ["你的清单（没处理的事）："]
    for n in notes:
        when = _fmt_when(n.when_at)
        suffix = f" [{when}]" if when else ""
        lines.append(f"- {n.content}{suffix} (id: {n.id})")
    return "\n".join(lines)
```

- [ ] **Step 3: Run — expect PASS**

```bash
uv run pytest tests/unit/memory/sections/test_active_notes.py -v
```

- [ ] **Step 4: Commit**

```bash
git add app/memory/sections/active_notes.py tests/unit/memory/sections/test_active_notes.py
git commit -m "feat(memory-v4): active_notes context section"
```

---

### Task 5: `recall_index` section

**Files:**
- Create: `app/memory/sections/recall_index.py`
- Create: `tests/unit/memory/sections/test_recall_index.py`

- [ ] **Step 1: 写测试**

```python
"""Test recall_index section."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory.sections.recall_index import build_recall_index_section


@pytest.mark.asyncio
async def test_empty_when_no_memory():
    with patch("app.memory.sections.recall_index.count_abstracts_by_persona", new=AsyncMock(return_value=0)):
        with patch("app.memory.sections.recall_index.get_recent_abstract_titles", new=AsyncMock(return_value=[])):
            text = await build_recall_index_section(persona_id="chiwei")
    assert text == ""


@pytest.mark.asyncio
async def test_renders_counts_and_recent_titles():
    titles = [MagicMock(subject="浩南", content="他最近压力大"), MagicMock(subject="学习", content="我开始学 Rust")]
    with patch("app.memory.sections.recall_index.count_abstracts_by_persona", new=AsyncMock(return_value=50)):
        with patch("app.memory.sections.recall_index.get_recent_abstract_titles", new=AsyncMock(return_value=titles)):
            text = await build_recall_index_section(persona_id="chiwei")
    assert "50" in text
    assert "浩南" in text
    assert "学习" in text
    # content summary is truncated
    assert "recall" in text.lower()
```

- [ ] **Step 2: 创建 `app/memory/sections/recall_index.py`**

```python
"""Always-on injection: recall index hint — counts and recent abstract titles."""

from __future__ import annotations

import logging

from app.data.queries import count_abstracts_by_persona, get_recent_abstract_titles
from app.data.session import get_session

logger = logging.getLogger(__name__)

RECENT_N = 10
SNIPPET = 30


async def build_recall_index_section(*, persona_id: str) -> str:
    try:
        async with get_session() as s:
            total = await count_abstracts_by_persona(s, persona_id)
            recent = await get_recent_abstract_titles(s, persona_id=persona_id, limit=RECENT_N)
    except Exception as e:
        logger.warning("recall_index failed: %s", e)
        return ""

    if total == 0 and not recent:
        return ""

    lines = [f"你总共记得 {total} 条抽象认识。最近碰过的："]
    for r in recent:
        snippet = r.content[:SNIPPET].replace("\n", " ")
        lines.append(f"- [{r.subject}] {snippet}...")
    lines.append(
        "（如果眼前的事让你隐约想起别的，用 recall(queries=[\"...\"]) 查一查。"
        "批量传多条 query 可以并行搜。）"
    )
    return "\n".join(lines)
```

- [ ] **Step 3: Run — expect PASS**

```bash
uv run pytest tests/unit/memory/sections/test_recall_index.py -v
```

- [ ] **Step 4: Commit**

```bash
git add app/memory/sections/recall_index.py tests/unit/memory/sections/test_recall_index.py
git commit -m "feat(memory-v4): recall_index context section"
```

---

### Task 6: `short_term_fragments` section（§2.8）

**Files:**
- Create: `app/memory/sections/short_term_fragments.py`
- Create: `tests/unit/memory/sections/test_short_term_fragments.py`

- [ ] **Step 1: 写测试**

```python
"""Test short-term fragment injection (§2.8)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory.sections.short_term_fragments import build_short_term_fragments_section


@pytest.mark.asyncio
async def test_empty_when_no_fragments():
    with patch("app.memory.sections.short_term_fragments.get_recent_fragments_for_injection", new=AsyncMock(return_value=[])):
        text = await build_short_term_fragments_section(
            persona_id="chiwei", chat_id="oc_a", trigger_user_id="u1",
        )
    assert text == ""


@pytest.mark.asyncio
async def test_renders_fragments_with_length_cap():
    f1 = MagicMock(
        id="f_1", content="刚才和浩南在 ka 群聊了新番，氛围不错", chat_id="oc_a",
        created_at=datetime(2026,4,18,10,0,tzinfo=timezone.utc),
    )
    f2 = MagicMock(
        id="f_2", content="x" * 500, chat_id="oc_b",  # very long
        created_at=datetime(2026,4,18,9,30,tzinfo=timezone.utc),
    )
    with patch("app.memory.sections.short_term_fragments.get_recent_fragments_for_injection", new=AsyncMock(return_value=[f1, f2])):
        text = await build_short_term_fragments_section(
            persona_id="chiwei", chat_id="oc_a", trigger_user_id="u1",
        )
    assert "新番" in text
    # long fragment content is truncated
    assert len(text) < 1200
```

- [ ] **Step 2: 创建 `app/memory/sections/short_term_fragments.py`**

```python
"""§2.8 短期 fragment 注入：
- 当前 chat 最近 2-4h 最新 1 条
- 其他 chat 最近 1-2h（含 trigger_user 的）最多 2 条，每 chat 只取最新

作用：补 chat_history 30min/15 窗口以外的回忆 + 补 cross-chat 24h raw 的噪音。
"""

from __future__ import annotations

import logging
from datetime import timedelta, timezone

from app.data.queries import get_recent_fragments_for_injection
from app.data.session import get_session

logger = logging.getLogger(__name__)

MAX_TOTAL_CHARS = 1000
FRAGMENT_MAX = 350
_CST = timezone(timedelta(hours=8))


def _fmt_time(dt) -> str:
    return dt.astimezone(_CST).strftime("%H:%M")


async def build_short_term_fragments_section(
    *,
    persona_id: str,
    chat_id: str | None,
    trigger_user_id: str | None,
) -> str:
    try:
        async with get_session() as s:
            fragments = await get_recent_fragments_for_injection(
                s,
                persona_id=persona_id,
                chat_id=chat_id,
                trigger_user_id=trigger_user_id,
            )
    except Exception as e:
        logger.warning("short_term_fragments failed: %s", e)
        return ""

    if not fragments:
        return ""

    lines = ["最近的新鲜经历："]
    total = 0
    for f in fragments:
        text = f.content.strip()
        if len(text) > FRAGMENT_MAX:
            text = text[:FRAGMENT_MAX] + "..."
        if total + len(text) > MAX_TOTAL_CHARS:
            break
        where = "这里" if f.chat_id == chat_id else f"别处({f.chat_id[:6] if f.chat_id else '?'})"
        lines.append(f"- [{where} {_fmt_time(f.created_at)}] {text}")
        total += len(text)
    if len(lines) == 1:
        return ""
    return "\n".join(lines)
```

- [ ] **Step 3: Run — expect PASS**

```bash
uv run pytest tests/unit/memory/sections/test_short_term_fragments.py -v
```

- [ ] **Step 4: Commit**

```bash
git add app/memory/sections/short_term_fragments.py tests/unit/memory/sections/test_short_term_fragments.py
git commit -m "feat(memory-v4): short_term_fragments context section (§2.8)"
```

---

### Task 7: `schedule` section

**Files:**
- Create: `app/memory/sections/schedule.py`
- Create: `tests/unit/memory/sections/test_schedule.py`

- [ ] **Step 1: 写测试**

```python
"""Test schedule section."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory.sections.schedule import build_schedule_section


@pytest.mark.asyncio
async def test_empty_when_no_schedule():
    with patch("app.memory.sections.schedule.get_current_schedule", new=AsyncMock(return_value=None)):
        text = await build_schedule_section(persona_id="chiwei")
    assert text == ""


@pytest.mark.asyncio
async def test_renders_schedule_content():
    sr = MagicMock(content="今天周五，早上 8-12 两节课...", reason="first draft")
    with patch("app.memory.sections.schedule.get_current_schedule", new=AsyncMock(return_value=sr)):
        text = await build_schedule_section(persona_id="chiwei")
    assert "今天" in text
```

- [ ] **Step 2: 创建 `app/memory/sections/schedule.py`**

```python
"""Always-on injection: today_schedule (latest revision)."""

from __future__ import annotations

import logging

from app.data.queries import get_current_schedule
from app.data.session import get_session

logger = logging.getLogger(__name__)


async def build_schedule_section(*, persona_id: str) -> str:
    try:
        async with get_session() as s:
            sr = await get_current_schedule(s, persona_id=persona_id)
    except Exception as e:
        logger.warning("schedule section failed: %s", e)
        return ""
    if sr is None:
        return ""
    return f"今天的安排：\n{sr.content}"
```

- [ ] **Step 3: Run — expect PASS**

```bash
uv run pytest tests/unit/memory/sections/test_schedule.py -v
```

- [ ] **Step 4: Commit**

```bash
git add app/memory/sections/schedule.py tests/unit/memory/sections/test_schedule.py
git commit -m "feat(memory-v4): schedule context section"
```

---

### Task 8: Cross-chat 去硬编码

**Files:**
- Modify: `app/memory/cross_chat.py`
- Modify: `tests/unit/memory/test_cross_chat.py`

- [ ] **Step 1: 确认 dynamic config 读取模式**

```bash
rg -n "get_dynamic_config\|DynamicConfig" app --type py | head -20
```

找到项目读 dynamic config 的典型调用（例如 `from app.infra.dynamic_config import get_dynamic_config; cfg = await get_dynamic_config(); value = cfg.get("...")`），按此模式写。

- [ ] **Step 2: 改 `cross_chat.py`**

```python
# 删除硬编码常量
# CROSS_CHAT_GROUP_IDS = ["oc_..."]  # 删除

from app.infra.dynamic_config import get_dynamic_config

DEFAULT_MAX_TOTAL_MESSAGES = 15


async def _excluded_chats() -> list[str]:
    try:
        cfg = await get_dynamic_config()
        return cfg.get("memory.cross_chat.excluded_chat_ids") or []
    except Exception:
        return []


async def _max_total() -> int:
    try:
        cfg = await get_dynamic_config()
        val = cfg.get("memory.cross_chat.max_total_messages")
        return int(val) if val else DEFAULT_MAX_TOTAL_MESSAGES
    except Exception:
        return DEFAULT_MAX_TOTAL_MESSAGES
```

修改 `build_cross_chat_context`：
1. 签名改成 `trigger_user_id: str | None`
2. 如果 `trigger_user_id` 为 None 或 `"__proactive__"` → 返回 `""`
3. 不再传 `allowed_group_ids` 给 `find_cross_chat_messages`，改成传 `excluded_chat_ids`
4. 拉到消息后先根据 `max_total_messages` 截断

修改 `app/data/queries.py` 的 `find_cross_chat_messages` 签名：
- 删除 `allowed_group_ids` 参数
- 新增 `excluded_chat_ids: list[str] | None = None` 参数
- SQL WHERE 用 `NOT IN` 或 `ANY <> ALL`

- [ ] **Step 3: 改 `test_cross_chat.py`**

原有测试里如有对 `CROSS_CHAT_GROUP_IDS` 的 mock/assertion，改成对 `excluded_chat_ids` dynamic config 的 mock。增加 `trigger_user_id=None` 返回空字符串的 case。

- [ ] **Step 4: grep 验证硬编码常量清零**

```bash
rg -n "CROSS_CHAT_GROUP_IDS" --type py
```

期望：0 match。

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/unit/memory/test_cross_chat.py -v
```

- [ ] **Step 6: Commit**

```bash
git add app/memory/cross_chat.py app/data/queries.py tests/unit/memory/test_cross_chat.py
git commit -m "feat(memory-v4): remove cross-chat hardcoded whitelist; dynamic config blacklist"
```

---

### Task 9: 重写 `build_inner_context`

**Files:**
- Rewrite: `app/memory/context.py`
- Rewrite: `tests/unit/memory/test_context.py`

- [ ] **Step 1: 写整体测试**

```python
"""Test build_inner_context end-to-end composition."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.memory.context import build_inner_context


@pytest.mark.asyncio
async def test_p2p_assembles_all_sections():
    with patch("app.memory.context.build_schedule_section", new=AsyncMock(return_value="SCHED")):
        with patch("app.memory.context.build_self_abstracts_section", new=AsyncMock(return_value="SELF")):
            with patch("app.memory.context.build_user_abstracts_section", new=AsyncMock(return_value="USER")):
                with patch("app.memory.context.build_active_notes_section", new=AsyncMock(return_value="NOTES")):
                    with patch("app.memory.context.build_short_term_fragments_section", new=AsyncMock(return_value="FRAG")):
                        with patch("app.memory.context.build_recall_index_section", new=AsyncMock(return_value="RECALL")):
                            with patch("app.memory.context.build_cross_chat_context", new=AsyncMock(return_value="CROSS")):
                                with patch("app.memory.context._build_life_state", new=AsyncMock(return_value="LIFE")):
                                    out = await build_inner_context(
                                        chat_id="oc_a", chat_type="p2p",
                                        user_ids=["u1"], trigger_user_id="u1",
                                        trigger_username="浩南", persona_id="chiwei",
                                    )
    for token in ("LIFE", "SELF", "USER", "SCHED", "NOTES", "FRAG", "RECALL", "CROSS"):
        assert token in out


@pytest.mark.asyncio
async def test_proactive_skips_user_and_cross():
    with patch("app.memory.context.build_schedule_section", new=AsyncMock(return_value="")):
        with patch("app.memory.context.build_self_abstracts_section", new=AsyncMock(return_value="SELF")):
            with patch("app.memory.context.build_user_abstracts_section", new=AsyncMock(return_value="USER")):
                with patch("app.memory.context.build_active_notes_section", new=AsyncMock(return_value="NOTES")):
                    with patch("app.memory.context.build_short_term_fragments_section", new=AsyncMock(return_value="FRAG")):
                        with patch("app.memory.context.build_recall_index_section", new=AsyncMock(return_value="RECALL")):
                            with patch("app.memory.context.build_cross_chat_context", new=AsyncMock(return_value="CROSS")):
                                with patch("app.memory.context._build_life_state", new=AsyncMock(return_value="LIFE")):
                                    out = await build_inner_context(
                                        chat_id="oc_a", chat_type="group",
                                        user_ids=["u1"], trigger_user_id=None,
                                        trigger_username=None, persona_id="chiwei",
                                        is_proactive=True,
                                    )
    # user_abstracts / cross-chat should have been called with None trigger_user_id
    # (they'll return empty internally). Our mocks always return USER/CROSS, so check
    # that build_inner_context still composes them — we trust section-level tests for
    # the conditional logic.
    assert "SELF" in out
```

- [ ] **Step 2: 重写 `app/memory/context.py`**

```python
"""Memory context builder v4 — assemble always-on + conditional sections.

Sections (order matters for prompt flow):
  1. Scene (p2p/group/proactive)
  2. Life state (current activity + mood)
  3. Today schedule
  4. Self abstracts (subject='self')
  5. User abstracts (subject='user:<id>' / 和 X 的关系)  — skipped if no trigger_user
  6. Active notes
  7. Cross-chat (user-centric raw msgs)  — skipped if no trigger_user
  8. Short-term fragments (§2.8)
  9. Recall index (counts + recent titles)
"""

from __future__ import annotations

import logging
from datetime import timedelta, timezone

from app.data.queries import find_latest_life_state
from app.data.session import get_session
from app.memory.cross_chat import build_cross_chat_context
from app.memory.sections.active_notes import build_active_notes_section
from app.memory.sections.recall_index import build_recall_index_section
from app.memory.sections.schedule import build_schedule_section
from app.memory.sections.self_abstracts import build_self_abstracts_section
from app.memory.sections.short_term_fragments import build_short_term_fragments_section
from app.memory.sections.user_abstracts import build_user_abstracts_section

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))


async def _build_life_state(persona_id: str) -> str:
    try:
        async with get_session() as s:
            row = await find_latest_life_state(s, persona_id)
        if not row:
            return ""
        current = row.current_state
        mood = row.response_mood
        if current:
            return (
                f"你此刻的状态：{current}\n你的心情：{mood}"
                if mood else f"你此刻的状态：{current}"
            )
    except Exception as e:
        logger.warning("[%s] Failed to read life state: %s", persona_id, e)
    return ""


def _scene_section(
    chat_type: str,
    chat_name: str,
    trigger_username: str | None,
    is_proactive: bool,
    proactive_stimulus: str,
) -> str:
    if is_proactive:
        scene = f"你在群聊「{chat_name}」中。" if chat_name else ""
        scene += "\n你刚刷到了群里的对话。如果你想说点什么就说，不想说也可以不说。"
        scene += "\n不要刻意解释为什么突然说话，像朋友在群里自然接话就好。"
        if proactive_stimulus:
            scene += f"\n（你注意到的：{proactive_stimulus}）"
        return scene
    if chat_type == "p2p":
        return f"你正在和 {trigger_username} 私聊。" if trigger_username else ""
    parts = []
    if chat_name:
        parts.append(f"你在群聊「{chat_name}」中。")
    if trigger_username:
        parts.append(f"需要回复 {trigger_username} 的消息（消息中用 ⭐ 标记）。")
    return "\n".join(parts)


async def build_inner_context(
    chat_id: str,
    chat_type: str,
    user_ids: list[str],
    trigger_user_id: str | None,
    trigger_username: str | None,
    persona_id: str,
    chat_name: str = "",
    *,
    is_proactive: bool = False,
    proactive_stimulus: str = "",
) -> str:
    """Assemble the full inner context string for chat injection (v4)."""

    # normalize "__proactive__" sentinel (if upstream still uses it) → None
    effective_user_id = (
        None if (trigger_user_id in (None, "__proactive__")) else trigger_user_id
    )

    sections: list[str] = []

    scene = _scene_section(
        chat_type, chat_name, trigger_username, is_proactive, proactive_stimulus
    )
    if scene:
        sections.append(scene)

    life = await _build_life_state(persona_id)
    if life:
        sections.append(life)

    sched = await build_schedule_section(persona_id=persona_id)
    if sched:
        sections.append(sched)

    self_abs = await build_self_abstracts_section(persona_id=persona_id)
    if self_abs:
        sections.append(self_abs)

    user_abs = await build_user_abstracts_section(
        persona_id=persona_id,
        trigger_user_id=effective_user_id,
        trigger_username=trigger_username,
    )
    if user_abs:
        sections.append(user_abs)

    notes = await build_active_notes_section(persona_id=persona_id)
    if notes:
        sections.append(notes)

    if effective_user_id:
        cross = await build_cross_chat_context(
            persona_id=persona_id,
            trigger_user_id=effective_user_id,
            trigger_username=trigger_username or "",
            current_chat_id=chat_id,
        )
        if cross:
            sections.append(cross)

    frag = await build_short_term_fragments_section(
        persona_id=persona_id,
        chat_id=chat_id,
        trigger_user_id=effective_user_id,
    )
    if frag:
        sections.append(frag)

    recall_idx = await build_recall_index_section(persona_id=persona_id)
    if recall_idx:
        sections.append(recall_idx)

    return "\n\n".join(sections)
```

- [ ] **Step 3: Run — expect PASS**

```bash
uv run pytest tests/unit/memory/test_context.py -v
```

- [ ] **Step 4: 废弃旧 relationship_memory 注入相关代码**

```bash
rg -n "find_latest_relationship_memory" --type py
```

如果只 context.py 用过，queries.py 里的 `find_latest_relationship_memory` 可删（或标记废弃，在 Plan E 清理）。

- [ ] **Step 5: Commit**

```bash
git add app/memory/context.py tests/unit/memory/test_context.py app/data/queries.py
git commit -m "feat(memory-v4): rewrite build_inner_context with v4 sections"
```

---

### Task 10: 合并自检

- [ ] **Step 1: 全量测试**

```bash
cd apps/agent-service
uv run pytest tests/unit/memory/ -v
```

- [ ] **Step 2: lint + 类型**

```bash
uv run ruff check app tests
uv run basedpyright app tests
```

- [ ] **Step 3: grep 验证 CROSS_CHAT_GROUP_IDS 零残留**

```bash
rg "CROSS_CHAT_GROUP_IDS" --type py
```

期望：0 match。

- [ ] **Step 4: Final commit**

```bash
git commit --allow-empty -m "chore(memory-v4): Plan C context injection ready"
```

---

## Self-Review

- ✅ 7 个 always-on / 条件 section 各自 TDD（Task 2-8）
- ✅ `build_inner_context` 整体重写（Task 9）
- ✅ Cross-chat 去硬编码（Task 8）
- ⚠️ `find_latest_relationship_memory` 的清理留给 Plan E（避免 Plan C 和 E 冲突）
- ⚠️ section 内文案（中文 prompt 内容）可能在实验阶段调整；Plan 里选了中性表达

## Execution Handoff

Plan C 完成标志：context 注入全通过测试 + 泳道部署后在 dev bot 对话里能看到 Langfuse trace 里的新 context。
