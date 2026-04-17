# Memory v4 - Plan A: 数据层 + 迁移脚本 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 v4 记忆系统的底层数据基础：PG 5 张新表、Qdrant 2 个 collections、memory 向量化流、历史数据迁移脚本。

**Architecture:** PG 存结构化字段和 graph edges，Qdrant 存向量（dense-only 1024 维 COSINE）。向量写入走异步 vectorize-worker（复用现有 worker + 新增 payload type）。迁移脚本一次性跑，把 617 条 relationship_memory_v2 拆成 fact+abstract+supports edges；最近 7 天 conversation_fragment 平迁成新 fragment 表。

**Tech Stack:** Python / SQLAlchemy 2.0 async / PostgreSQL / Qdrant / Volcengine Ark embedding / RabbitMQ (aio_pika) / pytest / uv

**Spec:** `docs/superpowers/specs/2026-04-16-memory-v4-design.md`（§七.1、§七.2、§七.8）

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `app/data/models.py` | Modify | 新增 5 个 ORM model（fragment / abstract_memory / memory_edge / notes / schedule_revision） |
| `app/data/queries.py` | Modify | 新增基础 CRUD（insert/get by id/list active notes/get current schedule） |
| `app/infra/qdrant.py` | Modify | `init_collections` 新增 `memory_abstract` / `memory_fragment` 两个 dense collection |
| `app/memory/vectorize_memory.py` | Create | 向量化 fragment / abstract 节点（读 DB → embed_dense → upsert Qdrant） |
| `app/workers/vectorize.py` | Modify | 消费新增的 `memory_vectorize` 消息类型（payload 带 `kind: 'fragment'` / `'abstract'` + `id`） |
| `app/infra/rabbitmq.py` | Modify | 新增 `MEMORY_VECTORIZE` Route |
| `scripts/migrate_relationship_to_abstract.py` | Create | 617 条 relationship_memory_v2 拆成 fact + abstract + supports edges（LLM 改写 impression） |
| `scripts/migrate_fragment_to_fragment.py` | Create | 最近 7 天 experience_fragment（conversation 类型）平迁到新 fragment 表 |
| `tests/unit/data/test_v4_models.py` | Create | ORM model 单元测试 |
| `tests/unit/data/test_v4_queries.py` | Create | queries 单元测试 |
| `tests/unit/memory/test_vectorize_memory.py` | Create | 向量化单元测试 |
| `tests/unit/scripts/test_migrate_relationship.py` | Create | 迁移脚本单测（mock LLM） |

---

### Task 1: 提交 DB Schema DDL

**Files:**
- 无代码改动，通过 `/ops-db submit @chiwei` 提交 DDL

- [ ] **Step 1: 提交 5 张表 + 索引的 DDL**

```
/ops-db submit @chiwei
-- Memory v4 schema
CREATE TABLE fragment (
    id              TEXT PRIMARY KEY,
    persona_id      TEXT NOT NULL,
    content         TEXT NOT NULL,
    source          TEXT NOT NULL,
    chat_id         TEXT,
    clarity         TEXT NOT NULL DEFAULT 'clear',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_touched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_fragment_persona_created ON fragment(persona_id, created_at DESC);
CREATE INDEX idx_fragment_persona_clarity ON fragment(persona_id, clarity);

CREATE TABLE abstract_memory (
    id              TEXT PRIMARY KEY,
    persona_id      TEXT NOT NULL,
    subject         TEXT NOT NULL,
    content         TEXT NOT NULL,
    created_by      TEXT NOT NULL,
    clarity         TEXT NOT NULL DEFAULT 'clear',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_touched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_abstract_persona_subject ON abstract_memory(persona_id, subject);
CREATE INDEX idx_abstract_persona_clarity ON abstract_memory(persona_id, clarity);

CREATE TABLE memory_edge (
    id          TEXT PRIMARY KEY,
    persona_id  TEXT NOT NULL,
    from_id     TEXT NOT NULL,
    from_type   TEXT NOT NULL,
    to_id       TEXT NOT NULL,
    to_type     TEXT NOT NULL,
    edge_type   TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    reason      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_edge_persona_from ON memory_edge(persona_id, from_id);
CREATE INDEX idx_edge_persona_to ON memory_edge(persona_id, to_id);

CREATE TABLE notes (
    id          TEXT PRIMARY KEY,
    persona_id  TEXT NOT NULL,
    content     TEXT NOT NULL,
    when_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    resolution  TEXT
);
CREATE INDEX idx_notes_active ON notes(persona_id, resolved_at) WHERE resolved_at IS NULL;
CREATE INDEX idx_notes_when ON notes(persona_id, when_at);

CREATE TABLE schedule_revision (
    id          TEXT PRIMARY KEY,
    persona_id  TEXT NOT NULL,
    content     TEXT NOT NULL,
    reason      TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_schedule_revision_latest ON schedule_revision(persona_id, created_at DESC);

-- reason: Memory v4 数据基础层，v4 上线前必须建好
```

- [ ] **Step 2: 等待审批通过后验证**

```
/ops-db @chiwei SELECT tablename FROM pg_tables WHERE tablename IN ('fragment','abstract_memory','memory_edge','notes','schedule_revision') ORDER BY tablename
```

期望输出 5 行，全部表名命中。

- [ ] **Step 3: 验证索引**

```
/ops-db @chiwei SELECT indexname FROM pg_indexes WHERE tablename IN ('fragment','abstract_memory','memory_edge','notes','schedule_revision') ORDER BY indexname
```

期望 ≥10 个索引（5 张表每张 ≥2 个索引）。

---

### Task 2: 新增 SQLAlchemy ORM Models

**Files:**
- Modify: `apps/agent-service/app/data/models.py`（尾部追加）

- [ ] **Step 1: 写测试 `tests/unit/data/test_v4_models.py`**

```python
"""Test v4 memory schema ORM models."""

from __future__ import annotations

from datetime import datetime, timezone

from app.data.models import (
    AbstractMemory,
    Fragment,
    MemoryEdge,
    Note,
    ScheduleRevision,
)


def test_fragment_defaults():
    f = Fragment(id="f_test", persona_id="chiwei", content="hello", source="manual")
    assert f.id == "f_test"
    assert f.persona_id == "chiwei"
    # clarity default set at DB level, not python; instantiated value is None until flush
    assert f.chat_id is None


def test_abstract_memory_instantiation():
    a = AbstractMemory(
        id="a_test",
        persona_id="chiwei",
        subject="user:u1",
        content="他是程序员",
        created_by="chiwei",
    )
    assert a.subject == "user:u1"
    assert a.created_by == "chiwei"


def test_memory_edge_instantiation():
    e = MemoryEdge(
        id="e_test",
        persona_id="chiwei",
        from_id="f_1",
        from_type="fact",
        to_id="a_1",
        to_type="abstract",
        edge_type="supports",
        created_by="chiwei",
    )
    assert e.edge_type == "supports"


def test_note_active_state():
    n = Note(id="n_test", persona_id="chiwei", content="周五看电影")
    assert n.resolved_at is None
    assert n.resolution is None


def test_schedule_revision_instantiation():
    sr = ScheduleRevision(
        id="sr_test",
        persona_id="chiwei",
        content="今天...",
        reason="first draft",
        created_by="cron_morning",
    )
    assert sr.created_by == "cron_morning"
```

- [ ] **Step 2: Run test — expect ImportError**

```bash
cd apps/agent-service
uv run pytest tests/unit/data/test_v4_models.py -v
```

期望：失败，`ImportError: cannot import name 'Fragment' from 'app.data.models'`

- [ ] **Step 3: 追加 5 个 ORM class 到 `app/data/models.py`**

在 `app/data/models.py` 文件末尾追加：

```python
# ---------------------------------------------------------------------------
# Memory v4
# ---------------------------------------------------------------------------


class Fragment(Base):
    """事实碎片 — v4 短期/长期记忆中的原子事实。"""

    __tablename__ = "fragment"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    persona_id: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    chat_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    clarity: Mapped[str] = mapped_column(Text, nullable=False, server_default="clear")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_touched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AbstractMemory(Base):
    """抽象记忆 — v4 subject + content 模型（不分类型）。"""

    __tablename__ = "abstract_memory"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    persona_id: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    clarity: Mapped[str] = mapped_column(Text, nullable=False, server_default="clear")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_touched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MemoryEdge(Base):
    """统一边表 — 连接 fragment / abstract_memory 节点。"""

    __tablename__ = "memory_edge"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    persona_id: Mapped[str] = mapped_column(Text, nullable=False)
    from_id: Mapped[str] = mapped_column(Text, nullable=False)
    from_type: Mapped[str] = mapped_column(Text, nullable=False)
    to_id: Mapped[str] = mapped_column(Text, nullable=False)
    to_type: Mapped[str] = mapped_column(Text, nullable=False)
    edge_type: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Note(Base):
    """赤尾主动清单 — 她自己决定记下来的事。"""

    __tablename__ = "notes"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    persona_id: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    when_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)


class ScheduleRevision(Base):
    """today_schedule 的 append-only 历史版本。"""

    __tablename__ = "schedule_revision"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    persona_id: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```

同时在 `models.py` 顶部的 docstring Tables 列表加上这 5 张新表名。

- [ ] **Step 4: Run test — expect PASS**

```bash
uv run pytest tests/unit/data/test_v4_models.py -v
```

期望：5 个 test 全部通过。

- [ ] **Step 5: 类型和 lint 检查**

```bash
uv run ruff check app/data/models.py tests/unit/data/test_v4_models.py
uv run basedpyright app/data/models.py
```

期望：无 error。

- [ ] **Step 6: Commit**

```bash
git add app/data/models.py tests/unit/data/test_v4_models.py
git commit -m "feat(memory-v4): add ORM models for fragment/abstract/edge/note/schedule_revision"
```

---

### Task 3: 基础 CRUD queries

**Files:**
- Modify: `apps/agent-service/app/data/queries.py`（追加新函数）
- Create: `apps/agent-service/tests/unit/data/test_v4_queries.py`

- [ ] **Step 1: 写测试 `tests/unit/data/test_v4_queries.py`**

```python
"""Test v4 basic CRUD queries."""

from __future__ import annotations

import pytest
from datetime import datetime, timezone

from app.data.models import AbstractMemory, Fragment, MemoryEdge, Note
from app.data.queries import (
    count_abstracts_by_persona,
    get_abstract_by_id,
    get_active_notes,
    get_current_schedule,
    get_fragment_by_id,
    insert_abstract_memory,
    insert_fragment,
    insert_memory_edge,
    insert_note,
    insert_schedule_revision,
    resolve_note as resolve_note_query,
    touch_abstract,
    touch_fragment,
)
from app.data.session import get_session


@pytest.mark.asyncio
async def test_insert_and_get_fragment():
    fid = "f_unit_test_1"
    async with get_session() as s:
        await insert_fragment(
            s, id=fid, persona_id="test_persona",
            content="test content", source="manual", chat_id="test_chat"
        )
    async with get_session() as s:
        f = await get_fragment_by_id(s, fid)
    assert f is not None
    assert f.content == "test content"
    # cleanup
    async with get_session() as s:
        await s.execute(Fragment.__table__.delete().where(Fragment.id == fid))


@pytest.mark.asyncio
async def test_insert_and_get_abstract():
    aid = "a_unit_test_1"
    async with get_session() as s:
        await insert_abstract_memory(
            s, id=aid, persona_id="test_persona",
            subject="test_subj", content="test", created_by="chiwei",
        )
    async with get_session() as s:
        a = await get_abstract_by_id(s, aid)
    assert a is not None
    assert a.subject == "test_subj"
    async with get_session() as s:
        await s.execute(AbstractMemory.__table__.delete().where(AbstractMemory.id == aid))


@pytest.mark.asyncio
async def test_insert_edge():
    eid = "e_unit_test_1"
    async with get_session() as s:
        await insert_memory_edge(
            s, id=eid, persona_id="test_persona",
            from_id="f_x", from_type="fact",
            to_id="a_y", to_type="abstract",
            edge_type="supports", created_by="chiwei", reason="test",
        )
    async with get_session() as s:
        await s.execute(MemoryEdge.__table__.delete().where(MemoryEdge.id == eid))


@pytest.mark.asyncio
async def test_insert_and_resolve_note():
    nid = "n_unit_test_1"
    async with get_session() as s:
        await insert_note(
            s, id=nid, persona_id="test_persona",
            content="周五看电影", when_at=None,
        )
    async with get_session() as s:
        active = await get_active_notes(s, persona_id="test_persona")
    assert any(n.id == nid for n in active)

    async with get_session() as s:
        await resolve_note_query(s, note_id=nid, resolution="done")
    async with get_session() as s:
        active2 = await get_active_notes(s, persona_id="test_persona")
    assert not any(n.id == nid for n in active2)
    async with get_session() as s:
        await s.execute(Note.__table__.delete().where(Note.id == nid))


@pytest.mark.asyncio
async def test_schedule_revision_and_current():
    async with get_session() as s:
        await insert_schedule_revision(
            s, id="sr_test_1", persona_id="test_persona",
            content="today1", reason="init", created_by="cron_morning",
        )
        await insert_schedule_revision(
            s, id="sr_test_2", persona_id="test_persona",
            content="today2", reason="update", created_by="chiwei",
        )
    async with get_session() as s:
        cur = await get_current_schedule(s, persona_id="test_persona")
    assert cur is not None
    assert cur.content == "today2"
    # cleanup
    async with get_session() as s:
        await s.execute(
            "DELETE FROM schedule_revision WHERE id IN ('sr_test_1','sr_test_2')"
        )
```

- [ ] **Step 2: Run — expect ImportError**

```bash
uv run pytest tests/unit/data/test_v4_queries.py -v
```

期望：失败，`ImportError: cannot import name 'insert_fragment'`

- [ ] **Step 3: 追加 CRUD 函数到 `app/data/queries.py`**

在 `queries.py` 文件末尾追加：

```python
# ---------------------------------------------------------------------------
# Memory v4 — fragment / abstract / edge / note / schedule_revision
# ---------------------------------------------------------------------------

from datetime import datetime
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.models import (
    AbstractMemory,
    Fragment,
    MemoryEdge,
    Note,
    ScheduleRevision,
)


async def insert_fragment(
    session: AsyncSession,
    *,
    id: str,
    persona_id: str,
    content: str,
    source: str,
    chat_id: str | None = None,
    clarity: str = "clear",
    created_at: datetime | None = None,
) -> None:
    f = Fragment(
        id=id,
        persona_id=persona_id,
        content=content,
        source=source,
        chat_id=chat_id,
        clarity=clarity,
    )
    if created_at is not None:
        f.created_at = created_at
        f.last_touched_at = created_at
    session.add(f)


async def get_fragment_by_id(
    session: AsyncSession, fragment_id: str
) -> Fragment | None:
    result = await session.execute(
        select(Fragment).where(Fragment.id == fragment_id)
    )
    return result.scalar_one_or_none()


async def touch_fragment(session: AsyncSession, fragment_id: str) -> None:
    await session.execute(
        update(Fragment)
        .where(Fragment.id == fragment_id)
        .values(last_touched_at=func.now())
    )


async def insert_abstract_memory(
    session: AsyncSession,
    *,
    id: str,
    persona_id: str,
    subject: str,
    content: str,
    created_by: str,
    clarity: str = "clear",
) -> None:
    a = AbstractMemory(
        id=id,
        persona_id=persona_id,
        subject=subject,
        content=content,
        created_by=created_by,
        clarity=clarity,
    )
    session.add(a)


async def get_abstract_by_id(
    session: AsyncSession, abstract_id: str
) -> AbstractMemory | None:
    result = await session.execute(
        select(AbstractMemory).where(AbstractMemory.id == abstract_id)
    )
    return result.scalar_one_or_none()


async def touch_abstract(session: AsyncSession, abstract_id: str) -> None:
    await session.execute(
        update(AbstractMemory)
        .where(AbstractMemory.id == abstract_id)
        .values(last_touched_at=func.now())
    )


async def count_abstracts_by_persona(
    session: AsyncSession, persona_id: str
) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(AbstractMemory)
        .where(AbstractMemory.persona_id == persona_id)
    )
    return int(result.scalar_one())


async def insert_memory_edge(
    session: AsyncSession,
    *,
    id: str,
    persona_id: str,
    from_id: str,
    from_type: str,
    to_id: str,
    to_type: str,
    edge_type: str,
    created_by: str,
    reason: str | None = None,
) -> None:
    e = MemoryEdge(
        id=id,
        persona_id=persona_id,
        from_id=from_id,
        from_type=from_type,
        to_id=to_id,
        to_type=to_type,
        edge_type=edge_type,
        created_by=created_by,
        reason=reason,
    )
    session.add(e)


async def insert_note(
    session: AsyncSession,
    *,
    id: str,
    persona_id: str,
    content: str,
    when_at: datetime | None = None,
) -> None:
    n = Note(id=id, persona_id=persona_id, content=content, when_at=when_at)
    session.add(n)


async def get_active_notes(
    session: AsyncSession, persona_id: str
) -> list[Note]:
    result = await session.execute(
        select(Note)
        .where(Note.persona_id == persona_id)
        .where(Note.resolved_at.is_(None))
        .order_by(Note.created_at.desc())
    )
    return list(result.scalars().all())


async def resolve_note(
    session: AsyncSession, *, note_id: str, resolution: str
) -> None:
    await session.execute(
        update(Note)
        .where(Note.id == note_id)
        .values(resolved_at=func.now(), resolution=resolution)
    )


async def insert_schedule_revision(
    session: AsyncSession,
    *,
    id: str,
    persona_id: str,
    content: str,
    reason: str,
    created_by: str,
) -> None:
    sr = ScheduleRevision(
        id=id,
        persona_id=persona_id,
        content=content,
        reason=reason,
        created_by=created_by,
    )
    session.add(sr)


async def get_current_schedule(
    session: AsyncSession, persona_id: str
) -> ScheduleRevision | None:
    result = await session.execute(
        select(ScheduleRevision)
        .where(ScheduleRevision.persona_id == persona_id)
        .order_by(ScheduleRevision.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()
```

- [ ] **Step 4: Run — expect PASS**

```bash
uv run pytest tests/unit/data/test_v4_queries.py -v
```

期望：5 个 test 全部通过。

⚠️ 这些测试需要真实 DB 连接，跑之前确认 `.env`（或 `uv run` 能读到）配了正确的 `postgres_*`。CI 流程应该有测试库。如果本地跑不了就在泳道里 `make deploy` 后观察启动日志确认。

- [ ] **Step 5: Commit**

```bash
git add app/data/queries.py tests/unit/data/test_v4_queries.py
git commit -m "feat(memory-v4): add CRUD queries for new memory tables"
```

---

### Task 4: Qdrant 新增 memory collections

**Files:**
- Modify: `apps/agent-service/app/infra/qdrant.py`（`init_collections` 函数）
- Create: `apps/agent-service/tests/unit/infra/test_qdrant_init_memory.py`

- [ ] **Step 1: 写测试**

```python
"""Test that init_collections creates v4 memory collections."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.infra.qdrant import init_collections


@pytest.mark.asyncio
async def test_init_collections_creates_memory_fragment_and_abstract():
    with patch("app.infra.qdrant.qdrant") as mock_qdrant:
        mock_qdrant.create_collection = AsyncMock(return_value=True)
        mock_qdrant.create_hybrid_collection = AsyncMock(return_value=True)

        await init_collections()

        created_names = [
            call.kwargs.get("collection_name")
            or call.args[0]  # positional fallback
            for call in mock_qdrant.create_collection.call_args_list
        ]
        assert "memory_fragment" in created_names
        assert "memory_abstract" in created_names
```

- [ ] **Step 2: Run — expect FAIL（collection name 不存在）**

```bash
uv run pytest tests/unit/infra/test_qdrant_init_memory.py -v
```

- [ ] **Step 3: 修改 `init_collections` 追加两个 collection**

找到 `app/infra/qdrant.py` 中的 `init_collections` 函数，在现有 `messages_recall` / `messages_cluster` 创建逻辑之后追加：

```python
        # v4 memory collections — dense only, 1024d COSINE
        for name in ("memory_fragment", "memory_abstract"):
            ok = await qdrant.create_collection(
                collection_name=name, vector_size=1024
            )
            if ok:
                logger.info("Qdrant v4 collection %s created", name)
            else:
                logger.warning("Qdrant v4 collection %s may already exist", name)
```

- [ ] **Step 4: Run — expect PASS**

```bash
uv run pytest tests/unit/infra/test_qdrant_init_memory.py -v
```

- [ ] **Step 5: Commit**

```bash
git add app/infra/qdrant.py tests/unit/infra/test_qdrant_init_memory.py
git commit -m "feat(memory-v4): init memory_fragment and memory_abstract qdrant collections"
```

---

### Task 5: memory 向量化函数

**Files:**
- Create: `apps/agent-service/app/memory/vectorize_memory.py`
- Create: `apps/agent-service/tests/unit/memory/test_vectorize_memory.py`

- [ ] **Step 1: 写测试**

```python
"""Test memory node vectorization functions."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory.vectorize_memory import vectorize_abstract, vectorize_fragment


@pytest.mark.asyncio
async def test_vectorize_fragment_upserts_to_qdrant():
    mock_fragment = MagicMock()
    mock_fragment.id = "f_1"
    mock_fragment.persona_id = "chiwei"
    mock_fragment.content = "他说明天要下雨"
    mock_fragment.source = "afterthought"
    mock_fragment.chat_id = "oc_xxx"
    mock_fragment.clarity = "clear"

    with patch("app.memory.vectorize_memory.get_fragment_by_id", new=AsyncMock(return_value=mock_fragment)):
        with patch("app.memory.vectorize_memory.embed_dense", new=AsyncMock(return_value=[0.1] * 1024)) as emb:
            with patch("app.memory.vectorize_memory.qdrant") as q:
                q.upsert_vectors = AsyncMock(return_value=True)
                ok = await vectorize_fragment("f_1")
    assert ok is True
    emb.assert_awaited_once()
    q.upsert_vectors.assert_awaited_once()
    # payload carries persona_id and clarity
    upsert_call = q.upsert_vectors.call_args
    payloads = upsert_call.kwargs.get("payloads") or upsert_call.args[3]
    assert payloads[0]["persona_id"] == "chiwei"
    assert payloads[0]["clarity"] == "clear"


@pytest.mark.asyncio
async def test_vectorize_fragment_missing_returns_false():
    with patch("app.memory.vectorize_memory.get_fragment_by_id", new=AsyncMock(return_value=None)):
        ok = await vectorize_fragment("f_missing")
    assert ok is False


@pytest.mark.asyncio
async def test_vectorize_abstract_upserts_to_qdrant():
    mock_a = MagicMock()
    mock_a.id = "a_1"
    mock_a.persona_id = "chiwei"
    mock_a.subject = "user:u1"
    mock_a.content = "他是程序员"
    mock_a.created_by = "chiwei"
    mock_a.clarity = "clear"

    with patch("app.memory.vectorize_memory.get_abstract_by_id", new=AsyncMock(return_value=mock_a)):
        with patch("app.memory.vectorize_memory.embed_dense", new=AsyncMock(return_value=[0.2] * 1024)):
            with patch("app.memory.vectorize_memory.qdrant") as q:
                q.upsert_vectors = AsyncMock(return_value=True)
                ok = await vectorize_abstract("a_1")
    assert ok is True
    payloads = q.upsert_vectors.call_args.kwargs.get("payloads") or q.upsert_vectors.call_args.args[3]
    assert payloads[0]["subject"] == "user:u1"
    assert payloads[0]["persona_id"] == "chiwei"
```

- [ ] **Step 2: Run — expect ImportError**

```bash
uv run pytest tests/unit/memory/test_vectorize_memory.py -v
```

- [ ] **Step 3: 创建 `app/memory/vectorize_memory.py`**

```python
"""Memory v4 vectorization — embed and upsert fragments/abstracts to Qdrant.

Called by vectorize-worker when consuming memory_vectorize tasks.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agent.embedding import embed_dense
from app.data.queries import get_abstract_by_id, get_fragment_by_id
from app.data.session import get_session
from app.infra.dynamic_config import get_dynamic_config
from app.infra.qdrant import qdrant

logger = logging.getLogger(__name__)

COLLECTION_FRAGMENT = "memory_fragment"
COLLECTION_ABSTRACT = "memory_abstract"


async def _embed_model_id() -> str:
    """Resolve embedding model id from dynamic config, fallback to project default."""
    try:
        cfg = await get_dynamic_config()
        return cfg.get("memory.embedding.model_id") or "embedding-model"
    except Exception:
        return "embedding-model"


async def vectorize_fragment(fragment_id: str) -> bool:
    async with get_session() as s:
        fragment = await get_fragment_by_id(s, fragment_id)
    if fragment is None:
        logger.warning("Fragment %s not found for vectorize", fragment_id)
        return False
    if not fragment.content.strip():
        logger.warning("Fragment %s has empty content", fragment_id)
        return False

    model_id = await _embed_model_id()
    vector = await embed_dense(model_id, text=fragment.content)

    payload: dict[str, Any] = {
        "persona_id": fragment.persona_id,
        "source": fragment.source,
        "chat_id": fragment.chat_id,
        "clarity": fragment.clarity,
    }
    ok = await qdrant.upsert_vectors(
        collection=COLLECTION_FRAGMENT,
        vectors=[vector],
        ids=[fragment.id],
        payloads=[payload],
    )
    if not ok:
        logger.error("Qdrant upsert failed for fragment %s", fragment_id)
    return ok


async def vectorize_abstract(abstract_id: str) -> bool:
    async with get_session() as s:
        a = await get_abstract_by_id(s, abstract_id)
    if a is None:
        logger.warning("Abstract %s not found for vectorize", abstract_id)
        return False
    if not a.content.strip():
        logger.warning("Abstract %s has empty content", abstract_id)
        return False

    model_id = await _embed_model_id()
    # Concatenate subject + content so subject terms contribute to embedding signal
    text = f"[{a.subject}] {a.content}"
    vector = await embed_dense(model_id, text=text)

    payload: dict[str, Any] = {
        "persona_id": a.persona_id,
        "subject": a.subject,
        "created_by": a.created_by,
        "clarity": a.clarity,
    }
    ok = await qdrant.upsert_vectors(
        collection=COLLECTION_ABSTRACT,
        vectors=[vector],
        ids=[a.id],
        payloads=[payload],
    )
    if not ok:
        logger.error("Qdrant upsert failed for abstract %s", abstract_id)
    return ok
```

- [ ] **Step 4: Run — expect PASS**

```bash
uv run pytest tests/unit/memory/test_vectorize_memory.py -v
```

- [ ] **Step 5: Commit**

```bash
git add app/memory/vectorize_memory.py tests/unit/memory/test_vectorize_memory.py
git commit -m "feat(memory-v4): add vectorize_fragment and vectorize_abstract"
```

---

### Task 6: vectorize-worker 消费 memory_vectorize 队列

**Files:**
- Modify: `apps/agent-service/app/infra/rabbitmq.py`（新增 Route）
- Modify: `apps/agent-service/app/workers/vectorize.py`（新增 consumer）
- Create: `apps/agent-service/tests/unit/workers/test_memory_vectorize.py`

- [ ] **Step 1: 新增 MEMORY_VECTORIZE Route**

在 `app/infra/rabbitmq.py` 找到 `VECTORIZE = Route("vectorize", "task.vectorize")` 那一行附近，追加：

```python
MEMORY_VECTORIZE = Route("memory_vectorize", "task.memory_vectorize")
```

并加入 Route 列表（`_ROUTES` 或类似常量，按现有代码风格同步）。

- [ ] **Step 2: 写 worker 消费者测试**

`tests/unit/workers/test_memory_vectorize.py`：

```python
"""Test memory_vectorize queue consumer routing."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.workers.vectorize import handle_memory_vectorize


@pytest.mark.asyncio
async def test_handle_memory_fragment_task_calls_vectorize_fragment():
    msg = MagicMock()
    msg.body = json.dumps({"kind": "fragment", "id": "f_1"}).encode()
    msg.process = MagicMock()
    # async context manager
    msg.process.return_value.__aenter__ = AsyncMock()
    msg.process.return_value.__aexit__ = AsyncMock()

    with patch("app.workers.vectorize.vectorize_fragment", new=AsyncMock(return_value=True)) as vf:
        await handle_memory_vectorize(msg)
    vf.assert_awaited_once_with("f_1")


@pytest.mark.asyncio
async def test_handle_memory_abstract_task_calls_vectorize_abstract():
    msg = MagicMock()
    msg.body = json.dumps({"kind": "abstract", "id": "a_1"}).encode()
    msg.process = MagicMock()
    msg.process.return_value.__aenter__ = AsyncMock()
    msg.process.return_value.__aexit__ = AsyncMock()

    with patch("app.workers.vectorize.vectorize_abstract", new=AsyncMock(return_value=True)) as va:
        await handle_memory_vectorize(msg)
    va.assert_awaited_once_with("a_1")


@pytest.mark.asyncio
async def test_handle_memory_unknown_kind_logs_and_acks():
    msg = MagicMock()
    msg.body = json.dumps({"kind": "???", "id": "x"}).encode()
    msg.process = MagicMock()
    msg.process.return_value.__aenter__ = AsyncMock()
    msg.process.return_value.__aexit__ = AsyncMock()

    # should not raise
    await handle_memory_vectorize(msg)
```

- [ ] **Step 3: Run — expect ImportError**

```bash
uv run pytest tests/unit/workers/test_memory_vectorize.py -v
```

- [ ] **Step 4: 在 `app/workers/vectorize.py` 追加消费者**

在文件末尾追加：

```python
# ---------------------------------------------------------------------------
# v4 memory vectorization consumer
# ---------------------------------------------------------------------------

from app.memory.vectorize_memory import vectorize_abstract, vectorize_fragment


@mq_error_handler
async def handle_memory_vectorize(message: AbstractIncomingMessage) -> None:
    """Consume memory_vectorize queue.

    Payload: {"kind": "fragment"|"abstract", "id": "<pk>"}
    """
    async with message.process(ignore_processed=True):
        data = json.loads(message.body.decode())
        kind = data.get("kind")
        node_id = data.get("id")
        if not node_id:
            logger.warning("memory_vectorize missing id: %s", data)
            return
        if kind == "fragment":
            await vectorize_fragment(node_id)
        elif kind == "abstract":
            await vectorize_abstract(node_id)
        else:
            logger.warning("memory_vectorize unknown kind %s", kind)
```

- [ ] **Step 5: 绑定 worker 启动订阅**

找到 worker 启动逻辑（可能在 `app/main.py` 或 `app/workers/__init__.py` 里通过 `mq.subscribe(VECTORIZE, handle_vectorize)` 绑定），追加：

```python
from app.infra.rabbitmq import MEMORY_VECTORIZE
from app.workers.vectorize import handle_memory_vectorize

await mq.subscribe(MEMORY_VECTORIZE, handle_memory_vectorize)
```

（具体路径看现有 VECTORIZE 订阅的代码风格 — grep `subscribe(VECTORIZE` 找到位置同步加）。

- [ ] **Step 6: Run — expect PASS**

```bash
uv run pytest tests/unit/workers/test_memory_vectorize.py -v
```

- [ ] **Step 7: 提供 enqueue helper**

在 `app/memory/vectorize_memory.py` 末尾追加：

```python
async def enqueue_fragment_vectorize(fragment_id: str) -> None:
    from app.infra.rabbitmq import MEMORY_VECTORIZE, mq
    await mq.publish(MEMORY_VECTORIZE, {"kind": "fragment", "id": fragment_id})


async def enqueue_abstract_vectorize(abstract_id: str) -> None:
    from app.infra.rabbitmq import MEMORY_VECTORIZE, mq
    await mq.publish(MEMORY_VECTORIZE, {"kind": "abstract", "id": abstract_id})
```

- [ ] **Step 8: Commit**

```bash
git add app/infra/rabbitmq.py app/workers/vectorize.py app/memory/vectorize_memory.py tests/unit/workers/test_memory_vectorize.py
git commit -m "feat(memory-v4): memory_vectorize queue + consumer wiring"
```

---

### Task 7: 迁移脚本 A — relationship_memory_v2 → abstract + fact + edges

**Files:**
- Create: `apps/agent-service/scripts/migrate_relationship_to_abstract.py`
- Create: `apps/agent-service/tests/unit/scripts/test_migrate_relationship.py`

- [ ] **Step 1: 写测试（mock DB + LLM）**

```python
"""Test migrate_relationship_to_abstract core logic (batch processor)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from scripts.migrate_relationship_to_abstract import process_one_row


@pytest.mark.asyncio
async def test_process_one_row_creates_fact_abstract_and_edges():
    row = SimpleNamespace(
        persona_id="chiwei",
        user_id="u1",
        core_facts="事实1\n事实2",
        impression="他很认真",
    )
    with patch("scripts.migrate_relationship_to_abstract.llm_rewrite_impression", new=AsyncMock(return_value="他做事认真")):
        with patch("scripts.migrate_relationship_to_abstract.insert_fragment", new=AsyncMock()) as ins_f:
            with patch("scripts.migrate_relationship_to_abstract.insert_abstract_memory", new=AsyncMock()) as ins_a:
                with patch("scripts.migrate_relationship_to_abstract.insert_memory_edge", new=AsyncMock()) as ins_e:
                    with patch("scripts.migrate_relationship_to_abstract.enqueue_fragment_vectorize", new=AsyncMock()):
                        with patch("scripts.migrate_relationship_to_abstract.enqueue_abstract_vectorize", new=AsyncMock()):
                            ok = await process_one_row(row, dry_run=False)
    assert ok is True
    assert ins_f.await_count == 2  # two facts
    assert ins_a.await_count == 1  # one abstract
    assert ins_e.await_count == 2  # two supports edges


@pytest.mark.asyncio
async def test_process_one_row_llm_failure_skips():
    row = SimpleNamespace(
        persona_id="chiwei", user_id="u1",
        core_facts="事实1", impression="他很认真",
    )
    with patch("scripts.migrate_relationship_to_abstract.llm_rewrite_impression", new=AsyncMock(side_effect=RuntimeError("llm down"))):
        with patch("scripts.migrate_relationship_to_abstract.insert_fragment", new=AsyncMock()) as ins_f:
            ok = await process_one_row(row, dry_run=False)
    assert ok is False
    ins_f.assert_not_awaited()  # didn't write partial data
```

- [ ] **Step 2: Run — expect ImportError**

- [ ] **Step 3: 创建 `scripts/migrate_relationship_to_abstract.py`**

```python
"""Migration: relationship_memory_v2 → v4 fragment + abstract + supports edges.

For each relationship_memory_v2 row:
  - Split core_facts into individual fragments (one per non-empty line)
  - LLM-rewrite impression into a clean abstract content
  - Create abstract_memory with subject=f"user:{user_id}"
  - Connect each fragment to the abstract via supports edges

Idempotent: re-runs delete prior migration=source rows before reprocessing.

Usage:
    python scripts/migrate_relationship_to_abstract.py [--dry-run] [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import uuid
from typing import Any

from sqlalchemy import select, text

from app.data.models import RelationshipMemoryV2
from app.data.queries import (
    insert_abstract_memory,
    insert_fragment,
    insert_memory_edge,
)
from app.data.session import get_session
from app.memory.vectorize_memory import (
    enqueue_abstract_vectorize,
    enqueue_fragment_vectorize,
)

logger = logging.getLogger("migrate_relationship")

MIGRATION_SOURCE = "migration"


async def llm_rewrite_impression(
    core_facts: str, impression: str
) -> str:
    """Rewrite facts + impression into one clean abstract content via haiku.

    Uses Langfuse prompt `memory_migrate_relationship`.
    """
    from app.agent.core import call_llm_with_prompt

    result = await call_llm_with_prompt(
        prompt_name="memory_migrate_relationship",
        variables={"facts": core_facts, "impression": impression},
    )
    return result.strip()


def _uid(prefix: str) -> str:
    return f"{prefix}_mig_{uuid.uuid4().hex[:12]}"


async def process_one_row(row: Any, *, dry_run: bool) -> bool:
    """Process a single relationship_memory_v2 row. Return True on success."""
    try:
        abstract_content = await llm_rewrite_impression(
            row.core_facts, row.impression
        )
    except Exception as e:
        logger.warning(
            "LLM rewrite failed for persona=%s user=%s: %s",
            row.persona_id, row.user_id, e,
        )
        return False

    fact_lines = [ln.strip() for ln in row.core_facts.splitlines() if ln.strip()]
    aid = _uid("a")
    fact_ids = [_uid("f") for _ in fact_lines]

    if dry_run:
        logger.info(
            "[DRY] persona=%s user=%s facts=%d abstract=%s",
            row.persona_id, row.user_id, len(fact_lines), abstract_content[:60],
        )
        return True

    async with get_session() as s:
        for fid, content in zip(fact_ids, fact_lines):
            await insert_fragment(
                s, id=fid, persona_id=row.persona_id,
                content=content, source=MIGRATION_SOURCE,
            )
        await insert_abstract_memory(
            s, id=aid, persona_id=row.persona_id,
            subject=f"user:{row.user_id}", content=abstract_content,
            created_by=MIGRATION_SOURCE,
        )
        for fid in fact_ids:
            await insert_memory_edge(
                s, id=_uid("e"), persona_id=row.persona_id,
                from_id=fid, from_type="fact",
                to_id=aid, to_type="abstract",
                edge_type="supports", created_by=MIGRATION_SOURCE,
                reason="migrated from relationship_memory_v2",
            )

    # enqueue vectorize tasks after commit
    for fid in fact_ids:
        await enqueue_fragment_vectorize(fid)
    await enqueue_abstract_vectorize(aid)
    return True


async def clear_prior_migration() -> None:
    """Idempotency: delete rows created by migration before re-running."""
    async with get_session() as s:
        await s.execute(
            text("DELETE FROM memory_edge WHERE created_by = :src"),
            {"src": MIGRATION_SOURCE},
        )
        await s.execute(
            text("DELETE FROM abstract_memory WHERE created_by = :src"),
            {"src": MIGRATION_SOURCE},
        )
        await s.execute(
            text("DELETE FROM fragment WHERE source = :src"),
            {"src": MIGRATION_SOURCE},
        )


async def main(dry_run: bool, limit: int | None) -> None:
    logging.basicConfig(level=logging.INFO)
    logger.info("Migration starting (dry_run=%s limit=%s)", dry_run, limit)

    if not dry_run:
        await clear_prior_migration()
        logger.info("Cleared prior migration rows")

    async with get_session() as s:
        q = select(RelationshipMemoryV2).order_by(RelationshipMemoryV2.id)
        if limit:
            q = q.limit(limit)
        result = await s.execute(q)
        rows = list(result.scalars().all())

    logger.info("Loaded %d relationship_memory_v2 rows", len(rows))

    success = 0
    failed = 0
    for i, row in enumerate(rows, 1):
        ok = await process_one_row(row, dry_run=dry_run)
        if ok:
            success += 1
        else:
            failed += 1
        if i % 50 == 0:
            logger.info("Progress %d/%d (success=%d failed=%d)", i, len(rows), success, failed)

    logger.info("Done. success=%d failed=%d", success, failed)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()
    asyncio.run(main(args.dry_run, args.limit))
```

- [ ] **Step 4: 创建 Langfuse prompt `memory_migrate_relationship`**

（通过 langfuse skill）：

```
/langfuse create-prompt memory_migrate_relationship

System: 你是赤尾（一个 AI persona）的潜意识整理者。你看到关于某个用户的历史事实和印象，
用第一人称（赤尾视角）把它们合并改写成一条简洁流畅的"关于这个人"的抽象认识，保留关键信息。
输出格式：一段话，不超过 200 字，不要加标题或列表。

User:
关于 TA 的事实：
{{facts}}

之前的印象：
{{impression}}

请用一两句话写出你现在对 TA 的整体认识：
```

- [ ] **Step 5: Run — expect PASS**

```bash
uv run pytest tests/unit/scripts/test_migrate_relationship.py -v
```

- [ ] **Step 6: dry-run 在本地（连线上 DB + Langfuse dev 环境）**

```bash
cd apps/agent-service
uv run python scripts/migrate_relationship_to_abstract.py --dry-run --limit 5
```

期望日志：5 条 `[DRY] persona=... user=... facts=N abstract=...`，不报错。

- [ ] **Step 7: Commit**

```bash
git add scripts/migrate_relationship_to_abstract.py tests/unit/scripts/test_migrate_relationship.py
git commit -m "feat(memory-v4): relationship_memory_v2 → abstract+fact+edges migration"
```

---

### Task 8: 迁移脚本 B — experience_fragment（conversation 类）平迁

**Files:**
- Create: `apps/agent-service/scripts/migrate_fragment_to_fragment.py`
- Create: `apps/agent-service/tests/unit/scripts/test_migrate_fragment.py`

- [ ] **Step 1: 写测试**

```python
"""Test migrate_fragment_to_fragment core logic."""

from __future__ import annotations

from types import SimpleNamespace
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from scripts.migrate_fragment_to_fragment import copy_one_row


@pytest.mark.asyncio
async def test_copy_one_row_preserves_timestamps():
    row = SimpleNamespace(
        id="oldid-1",
        persona_id="chiwei",
        content="啊今天和浩南聊了很久",
        chat_id="oc_xxx",
        created_at=datetime(2026, 4, 11, 10, 0, tzinfo=timezone.utc),
    )
    with patch("scripts.migrate_fragment_to_fragment.insert_fragment", new=AsyncMock()) as ins:
        with patch("scripts.migrate_fragment_to_fragment.enqueue_fragment_vectorize", new=AsyncMock()):
            ok = await copy_one_row(row, dry_run=False)
    assert ok is True
    call_kwargs = ins.await_args.kwargs
    assert call_kwargs["id"] == "f_mig_oldid-1"
    assert call_kwargs["source"] == "afterthought"
    assert call_kwargs["created_at"] == row.created_at
```

- [ ] **Step 2: Run — expect ImportError**

- [ ] **Step 3: 创建 `scripts/migrate_fragment_to_fragment.py`**

```python
"""Migration: experience_fragment (最近 7 天, granularity='conversation') → v4 fragment.

Pure data copy — no LLM rewrite (old content is often too long; reviewer heavy
will consolidate on day 2).

Idempotent: re-runs delete rows with id starting with `f_mig_` before reprocessing.

Usage:
    python scripts/migrate_fragment_to_fragment.py [--dry-run] [--limit N] [--days N]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, text

from app.data.models import ExperienceFragment
from app.data.queries import insert_fragment
from app.data.session import get_session
from app.memory.vectorize_memory import enqueue_fragment_vectorize

logger = logging.getLogger("migrate_fragment")

MIG_ID_PREFIX = "f_mig_"


async def copy_one_row(row: Any, *, dry_run: bool) -> bool:
    new_id = f"{MIG_ID_PREFIX}{row.id}"
    if dry_run:
        logger.info(
            "[DRY] %s → %s persona=%s chat=%s content_len=%d",
            row.id, new_id, row.persona_id, row.chat_id, len(row.content or ""),
        )
        return True
    try:
        async with get_session() as s:
            await insert_fragment(
                s, id=new_id, persona_id=row.persona_id,
                content=row.content, source="afterthought",
                chat_id=row.chat_id, clarity="clear",
                created_at=row.created_at,
            )
        await enqueue_fragment_vectorize(new_id)
        return True
    except Exception as e:
        logger.warning("Copy failed for %s: %s", row.id, e)
        return False


async def clear_prior_migration() -> None:
    async with get_session() as s:
        await s.execute(
            text("DELETE FROM fragment WHERE id LIKE :p"),
            {"p": f"{MIG_ID_PREFIX}%"},
        )


async def main(dry_run: bool, limit: int | None, days: int) -> None:
    logging.basicConfig(level=logging.INFO)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    logger.info(
        "Migrating experience_fragment (granularity='conversation') since %s (dry_run=%s)",
        since, dry_run,
    )

    if not dry_run:
        await clear_prior_migration()
        logger.info("Cleared prior migrated fragments")

    async with get_session() as s:
        q = (
            select(ExperienceFragment)
            .where(ExperienceFragment.granularity == "conversation")
            .where(ExperienceFragment.created_at >= since)
            .order_by(ExperienceFragment.created_at)
        )
        if limit:
            q = q.limit(limit)
        rows = list((await s.execute(q)).scalars().all())

    logger.info("Loaded %d rows", len(rows))
    success = failed = 0
    for i, row in enumerate(rows, 1):
        ok = await copy_one_row(row, dry_run=dry_run)
        success += int(ok)
        failed += int(not ok)
        if i % 50 == 0:
            logger.info("Progress %d/%d success=%d failed=%d", i, len(rows), success, failed)

    logger.info("Done. success=%d failed=%d", success, failed)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--days", type=int, default=7)
    args = p.parse_args()
    asyncio.run(main(args.dry_run, args.limit, args.days))
```

- [ ] **Step 4: Run — expect PASS**

```bash
uv run pytest tests/unit/scripts/test_migrate_fragment.py -v
```

- [ ] **Step 5: dry-run**

```bash
uv run python scripts/migrate_fragment_to_fragment.py --dry-run --limit 5
```

- [ ] **Step 6: Commit**

```bash
git add scripts/migrate_fragment_to_fragment.py tests/unit/scripts/test_migrate_fragment.py
git commit -m "feat(memory-v4): experience_fragment (conversation, 7d) → fragment migration"
```

---

### Task 9: 合并自检 + 文档补充

- [ ] **Step 1: 全量测试**

```bash
cd apps/agent-service
uv run pytest tests/unit/data/ tests/unit/memory/test_vectorize_memory.py tests/unit/workers/test_memory_vectorize.py tests/unit/scripts/ tests/unit/infra/test_qdrant_init_memory.py -v
```

期望：全部通过。

- [ ] **Step 2: 类型/lint 全部通过**

```bash
uv run ruff check app scripts tests
uv run basedpyright app scripts
```

- [ ] **Step 3: 新增 runbook 片段到 spec 文档**

在 `docs/superpowers/specs/2026-04-16-memory-v4-design.md` 的 §7.8 节末追加"上线当天执行顺序"（如果 spec 里缺失）。

- [ ] **Step 4: Final commit**

```bash
git commit --allow-empty -m "chore(memory-v4): Plan A data layer ready"
```

---

## Self-Review

- ✅ DB schema 5 张表全部覆盖 spec §7.1（task 1）
- ✅ ORM model / CRUD / Qdrant 初始化都有 TDD 循环
- ✅ 向量化 + enqueue + worker 订阅连起来（task 5-6）
- ✅ 两个迁移脚本幂等 + dry-run（task 7-8）
- ⚠️ Langfuse prompt `memory_migrate_relationship` 需要在执行 task 7 之前手动建好（step 4 提示了）
- ⚠️ 迁移的真实执行（非 dry-run）放到 Plan E 最后的"上线当天"章节统一做，不在 Plan A 里跑

## Execution Handoff

**Plan A 完成后不立即跑迁移（只跑 dry-run 验证脚本），等 Plan E 最后一起上线。**

Plan A 完成的标志：所有 8 个 task 都绿灯 + dry-run 输出正确。
