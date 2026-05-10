# 赤尾 Notes 体验重设计 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 重设计赤尾 Notes 的 CRUD 工具集 + context 注入策略，解决"无日期/无法更新/全量注入"三个体验问题。

**Architecture:** SQLAlchemy `Note` 表加 `deleted_at` / `delete_reason` 软删字段；queries 层用 `upsert_note` / `delete_note` / `list_active_notes` / `select_notes_for_context` 替代旧 `insert_note` / `get_active_notes`；tool 层用 `upsert_note` / `list_note` / `delete_note` 替代 `write_note`；context 注入按 3 天 / 7 天窗口 + 15 条上限过滤，显示相对时间。

**Tech Stack:** Python 3.12 + SQLAlchemy async + LangChain `@tool` + pytest + asyncio。

**Spec:** `docs/superpowers/specs/2026-05-10-chiwei-notes-redesign-design.md`

---

## File Structure

| 文件 | 改动类型 | 责任 |
|------|---------|------|
| `apps/agent-service/app/data/models.py` | Modify (lines 359-377) | `Note` 类加 `deleted_at` / `delete_reason` |
| `apps/agent-service/app/data/queries/memory_edges.py` | Modify (lines 98-129) | 删 `insert_note` / `get_active_notes`，新增 4 个 query |
| `apps/agent-service/app/agent/tools/notes.py` | Rewrite | 删 `write_note` / `_write_note_impl`，新增 `upsert_note` / `list_note` / `delete_note`，改 `resolve_note` description |
| `apps/agent-service/app/agent/tools/__init__.py` | Modify | 工具注册更新 |
| `apps/agent-service/app/memory/sections/active_notes.py` | Rewrite | 改造注入逻辑 + 时间格式化 |
| `apps/agent-service/app/memory/notes_format.py` | Create | 公共 `format_when_label` helper（section + tool 共享） |
| `apps/agent-service/tests/unit/data/test_v4_queries.py` | Modify | 替换 note 相关测试 |
| `apps/agent-service/tests/unit/data/test_queries_split.py` | Modify (lines 44-46) | 更新 `EXPECTED_FUNCTIONS` |
| `apps/agent-service/tests/unit/agent/tools/test_notes.py` | Rewrite | 新工具的测试 |
| `apps/agent-service/tests/unit/memory/sections/test_active_notes.py` | Rewrite | section 改造测试 |
| `apps/agent-service/tests/unit/memory/test_notes_format.py` | Create | format helper 测试 |

---

## Task 1: DB schema — 加 `deleted_at` + `delete_reason` 字段

**Files:**
- Modify: `apps/agent-service/app/data/models.py:359-377`
- DDL: 通过 `/ops-db submit @chiwei` 提交

- [ ] **Step 1: 改 ORM model**

编辑 `apps/agent-service/app/data/models.py`，在 `Note` 类（359-377 行）末尾追加两个字段：

```python
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
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delete_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
```

- [ ] **Step 2: 跑现有测试确认 model 改动不破坏**

Run: `cd apps/agent-service && uv run pytest tests/unit/data/test_v4_queries.py -v`
Expected: 全部 PASS（model 加字段是 additive，旧测试应仍通过）。

- [ ] **Step 3: 提交 DDL**

Run（不要带前导 `--` 注释，会被 ops-db submit 吞掉）：

```
/ops-db submit @chiwei ALTER TABLE notes ADD COLUMN deleted_at TIMESTAMPTZ NULL, ADD COLUMN delete_reason TEXT NULL;
```

Expected: 提交成功，等用户审批后执行。

- [ ] **Step 4: Commit**

```bash
git add apps/agent-service/app/data/models.py
git commit -m "feat(notes): add deleted_at and delete_reason columns to Note"
```

---

## Task 2: queries 层 — 新增 `upsert_note`

**Files:**
- Modify: `apps/agent-service/app/data/queries/memory_edges.py`
- Test: `apps/agent-service/tests/unit/data/test_v4_queries.py`

- [ ] **Step 1: 写失败测试 — create 路径**

在 `apps/agent-service/tests/unit/data/test_v4_queries.py` 末尾追加（替换原 `test_insert_note_adds_to_session`，因为 `insert_note` 将被 `upsert_note` 替代；保留为新测试）：

```python
@pytest.mark.asyncio
async def test_upsert_note_create_when_no_id():
    session = AsyncMock()
    session.add = lambda obj: setattr(session, "_added", obj)
    session.flush = AsyncMock()
    patches = _patch_module("app.data.queries.memory_edges", session)
    try:
        from app.data.queries import upsert_note
        added = await upsert_note(
            persona_id="chiwei",
            content="周五看电影",
        )
        assert isinstance(added, Note)
        assert added.id.startswith("n_")
        assert added.content == "周五看电影"
        assert added.when_at is None
    finally:
        _stop(patches)
```

- [ ] **Step 2: Run test — 确认 fail**

Run: `cd apps/agent-service && uv run pytest tests/unit/data/test_v4_queries.py::test_upsert_note_create_when_no_id -v`
Expected: FAIL `ImportError: cannot import name 'upsert_note'`

- [ ] **Step 3: 实现 `upsert_note`（create 分支）**

编辑 `apps/agent-service/app/data/queries/memory_edges.py`：

文件顶部 `from sqlalchemy import func, update` 后追加 import（如果尚未引入）：

```python
from app.data.ids import new_id
```

并在 `__all__` 列表里追加 `"upsert_note"`（保留 `insert_note` 和 `get_active_notes` 直到 Task 9 删除）：

```python
__all__ = [
    "insert_memory_edge",
    "delete_edge",
    "list_edges_to",
    "list_edges_from",
    "insert_note",
    "upsert_note",
    "get_active_notes",
    "resolve_note",
]
```

在文件末尾、`resolve_note` 之前追加 `_UNSET` 哨兵和 `upsert_note`：

```python
_UNSET: object = object()


async def upsert_note(
    *,
    persona_id: str,
    content: str,
    when_at: datetime | None | object = _UNSET,
    note_id: str | None = None,
) -> Note:
    """Create or update a Note.

    - ``note_id is None`` → create new note (id auto-generated as ``n_<hex>``)
    - ``note_id`` provided + ``when_at is _UNSET`` → update content only
    - ``note_id`` provided + ``when_at is None`` → clear when_at column
    - ``note_id`` provided + ``when_at`` is datetime → update when_at column

    Returns the persisted Note. Raises ``LookupError`` if updating an unknown id.
    """
    async with auto_tx():
        s = current_session()
        if note_id is None:
            nid = new_id("n")
            when_value = None if when_at is _UNSET else when_at
            n = Note(
                id=nid,
                persona_id=persona_id,
                content=content,
                when_at=when_value,
            )
            s.add(n)
            await s.flush()
            return n

        result = await s.execute(select(Note).where(Note.id == note_id))
        existing = result.scalar_one_or_none()
        if existing is None:
            raise LookupError(f"note not found: {note_id}")

        existing.content = content
        if when_at is not _UNSET:
            existing.when_at = when_at  # type: ignore[assignment]
        await s.flush()
        return existing
```

- [ ] **Step 4: Run test — 确认 pass**

Run: `cd apps/agent-service && uv run pytest tests/unit/data/test_v4_queries.py::test_upsert_note_create_when_no_id -v`
Expected: PASS

- [ ] **Step 5: 写 update 路径测试**

在同一文件追加：

```python
@pytest.mark.asyncio
async def test_upsert_note_update_content_only():
    existing = Note(id="n_abc", persona_id="chiwei", content="old", when_at=None)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult(existing))
    session.flush = AsyncMock()
    patches = _patch_module("app.data.queries.memory_edges", session)
    try:
        from app.data.queries import upsert_note
        out = await upsert_note(
            persona_id="chiwei",
            content="new content",
            note_id="n_abc",
        )
        assert out.content == "new content"
        assert out.when_at is None  # untouched (was None, stays None; _UNSET means don't change)
    finally:
        _stop(patches)


@pytest.mark.asyncio
async def test_upsert_note_update_when_at():
    from datetime import UTC, datetime as _dt
    existing = Note(id="n_abc", persona_id="chiwei", content="old", when_at=None)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult(existing))
    session.flush = AsyncMock()
    patches = _patch_module("app.data.queries.memory_edges", session)
    try:
        from app.data.queries import upsert_note
        new_when = _dt(2026, 5, 17, 12, 0, tzinfo=UTC)
        out = await upsert_note(
            persona_id="chiwei",
            content="old",
            when_at=new_when,
            note_id="n_abc",
        )
        assert out.when_at == new_when
    finally:
        _stop(patches)


@pytest.mark.asyncio
async def test_upsert_note_clear_when_at_with_explicit_none():
    from datetime import UTC, datetime as _dt
    existing = Note(
        id="n_abc",
        persona_id="chiwei",
        content="old",
        when_at=_dt(2026, 5, 1, 12, 0, tzinfo=UTC),
    )
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult(existing))
    session.flush = AsyncMock()
    patches = _patch_module("app.data.queries.memory_edges", session)
    try:
        from app.data.queries import upsert_note
        out = await upsert_note(
            persona_id="chiwei",
            content="old",
            when_at=None,  # explicit None = clear
            note_id="n_abc",
        )
        assert out.when_at is None
    finally:
        _stop(patches)


@pytest.mark.asyncio
async def test_upsert_note_unknown_id_raises():
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult(None))
    patches = _patch_module("app.data.queries.memory_edges", session)
    try:
        from app.data.queries import upsert_note
        with pytest.raises(LookupError, match="note not found"):
            await upsert_note(
                persona_id="chiwei",
                content="x",
                note_id="n_does_not_exist",
            )
    finally:
        _stop(patches)
```

- [ ] **Step 6: Run all upsert tests**

Run: `cd apps/agent-service && uv run pytest tests/unit/data/test_v4_queries.py -k upsert_note -v`
Expected: 4 个测试全部 PASS

- [ ] **Step 7: 同步 `EXPECTED_FUNCTIONS` 基线**

编辑 `apps/agent-service/tests/unit/data/test_queries_split.py:44-46`，把 memory_edges 段从 7 改成 8（加 `upsert_note`）：

```python
    # memory_edges (8) — edges + notes
    "insert_memory_edge", "delete_edge", "list_edges_to", "list_edges_from",
    "insert_note", "upsert_note", "get_active_notes", "resolve_note",
```

并把文件第 9 行注释 "硬编码作为期望基线（80 函数）" 改成 "（81 函数）"。

Run: `cd apps/agent-service && uv run pytest tests/unit/data/test_queries_split.py -v`
Expected: 3 个测试全部 PASS

- [ ] **Step 8: Commit**

```bash
git add apps/agent-service/app/data/queries/memory_edges.py \
        apps/agent-service/tests/unit/data/test_v4_queries.py \
        apps/agent-service/tests/unit/data/test_queries_split.py
git commit -m "feat(notes): add upsert_note query (create + update)"
```

---

## Task 3: queries 层 — 新增 `delete_note`

**Files:**
- Modify: `apps/agent-service/app/data/queries/memory_edges.py`
- Test: `apps/agent-service/tests/unit/data/test_v4_queries.py`

- [ ] **Step 1: 写失败测试**

在 `tests/unit/data/test_v4_queries.py` 追加：

```python
@pytest.mark.asyncio
async def test_delete_note_soft_deletes():
    session = AsyncMock()
    session.execute = AsyncMock()
    patches = _patch_module("app.data.queries.memory_edges", session)
    try:
        from app.data.queries import delete_note
        await delete_note(note_id="n_abc", reason="改主意了")
        # _stop verified the patch ran; assert that execute() was called once
        # and the SQL is an UPDATE setting deleted_at + delete_reason
        session.execute.assert_awaited_once()
        stmt = session.execute.await_args.args[0]
        compiled = str(stmt)
        assert "UPDATE notes" in compiled
        assert "deleted_at" in compiled
        assert "delete_reason" in compiled
    finally:
        _stop(patches)
```

- [ ] **Step 2: Run test — 确认 fail**

Run: `cd apps/agent-service && uv run pytest tests/unit/data/test_v4_queries.py::test_delete_note_soft_deletes -v`
Expected: FAIL `ImportError: cannot import name 'delete_note'`

- [ ] **Step 3: 实现 `delete_note`**

在 `apps/agent-service/app/data/queries/memory_edges.py` 末尾追加，并在 `__all__` 加 `"delete_note"`：

```python
async def delete_note(*, note_id: str, reason: str) -> None:
    """Soft-delete a note (sets deleted_at + delete_reason).

    Does not raise if note_id does not exist; the UPDATE simply affects 0 rows.
    The tool layer is responsible for verifying existence if needed.
    """
    async with auto_tx():
        await current_session().execute(
            update(Note)
            .where(Note.id == note_id)
            .values(deleted_at=func.now(), delete_reason=reason)
        )
```

`__all__` 现在应为：

```python
__all__ = [
    "insert_memory_edge",
    "delete_edge",
    "list_edges_to",
    "list_edges_from",
    "insert_note",
    "upsert_note",
    "delete_note",
    "get_active_notes",
    "resolve_note",
]
```

- [ ] **Step 4: Run test — 确认 pass**

Run: `cd apps/agent-service && uv run pytest tests/unit/data/test_v4_queries.py::test_delete_note_soft_deletes -v`
Expected: PASS

- [ ] **Step 5: 同步 `EXPECTED_FUNCTIONS` 基线**

编辑 `apps/agent-service/tests/unit/data/test_queries_split.py`，把 memory_edges 段从 8 改成 9（加 `delete_note`）：

```python
    # memory_edges (9) — edges + notes
    "insert_memory_edge", "delete_edge", "list_edges_to", "list_edges_from",
    "insert_note", "upsert_note", "delete_note", "get_active_notes", "resolve_note",
```

把文件顶部注释 "（81 函数）" 改成 "（82 函数）"。

Run: `cd apps/agent-service && uv run pytest tests/unit/data/test_queries_split.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add apps/agent-service/app/data/queries/memory_edges.py \
        apps/agent-service/tests/unit/data/test_v4_queries.py \
        apps/agent-service/tests/unit/data/test_queries_split.py
git commit -m "feat(notes): add delete_note query (soft delete)"
```

---

## Task 4: queries 层 — 新增 `list_active_notes`

**Files:**
- Modify: `apps/agent-service/app/data/queries/memory_edges.py`
- Test: `apps/agent-service/tests/unit/data/test_v4_queries.py`

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.asyncio
async def test_list_active_notes_filters_resolved_and_deleted():
    n_active = Note(id="n_active", persona_id="chiwei", content="alive")
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_IterResult([n_active]))
    patches = _patch_module("app.data.queries.memory_edges", session)
    try:
        from app.data.queries import list_active_notes
        result = await list_active_notes(persona_id="chiwei")
        assert result == [n_active]
        # Assert WHERE clause filters both resolved_at and deleted_at IS NULL.
        stmt = session.execute.await_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "resolved_at IS NULL" in compiled
        assert "deleted_at IS NULL" in compiled
    finally:
        _stop(patches)
```

- [ ] **Step 2: Run test — 确认 fail**

Run: `cd apps/agent-service && uv run pytest tests/unit/data/test_v4_queries.py::test_list_active_notes_filters_resolved_and_deleted -v`
Expected: FAIL `ImportError`

- [ ] **Step 3: 实现 `list_active_notes`**

在 `apps/agent-service/app/data/queries/memory_edges.py` 追加（同时把 `"list_active_notes"` 加入 `__all__`）：

```python
async def list_active_notes(persona_id: str) -> list[Note]:
    """Return all notes that are neither resolved nor deleted.

    Ordered: notes with ``when_at`` first (ascending — soonest first),
    then notes without ``when_at`` (most recently created first).
    """
    async with auto_tx():
        result = await current_session().execute(
            select(Note)
            .where(Note.persona_id == persona_id)
            .where(Note.resolved_at.is_(None))
            .where(Note.deleted_at.is_(None))
            .order_by(
                Note.when_at.asc().nulls_last(),
                Note.created_at.desc(),
            )
        )
        return list(result.scalars().all())
```

- [ ] **Step 4: Run test — 确认 pass**

Run: `cd apps/agent-service && uv run pytest tests/unit/data/test_v4_queries.py::test_list_active_notes_filters_resolved_and_deleted -v`
Expected: PASS

- [ ] **Step 5: 同步 `EXPECTED_FUNCTIONS` 基线**

编辑 `apps/agent-service/tests/unit/data/test_queries_split.py`，memory_edges 段 (9) → (10)，加 `"list_active_notes"`；顶部注释 "（82 函数）" → "（83 函数）"。

```python
    # memory_edges (10) — edges + notes
    "insert_memory_edge", "delete_edge", "list_edges_to", "list_edges_from",
    "insert_note", "upsert_note", "delete_note", "list_active_notes",
    "get_active_notes", "resolve_note",
```

Run: `cd apps/agent-service && uv run pytest tests/unit/data/test_queries_split.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add apps/agent-service/app/data/queries/memory_edges.py \
        apps/agent-service/tests/unit/data/test_v4_queries.py \
        apps/agent-service/tests/unit/data/test_queries_split.py
git commit -m "feat(notes): add list_active_notes query"
```

---

## Task 5: queries 层 — 新增 `select_notes_for_context`

**Files:**
- Modify: `apps/agent-service/app/data/queries/memory_edges.py`
- Test: `apps/agent-service/tests/unit/data/test_v4_queries.py`

阈值常量：
- `_RECENT_OVERDUE_DAYS = 3`（带 `when_at` 的，过期 ≤3 天还注入）
- `_NEW_MEMO_DAYS = 7`（无 `when_at` 的，创建 ≤7 天还注入）
- `_CONTEXT_NOTES_LIMIT = 15`

- [ ] **Step 1: 写失败测试 — 窗口过滤**

```python
@pytest.mark.asyncio
async def test_select_notes_for_context_window_and_limit():
    """Verify SQL has the 3-day / 7-day window + 15-row limit."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_IterResult([]))
    patches = _patch_module("app.data.queries.memory_edges", session)
    try:
        from app.data.queries import select_notes_for_context
        await select_notes_for_context(persona_id="chiwei")
        stmt = session.execute.await_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        # Window cutoffs are computed in Python, so SQL contains literal timestamps;
        # what matters is LIMIT and the OR-shape with when_at + created_at.
        assert "LIMIT 15" in compiled
        assert "when_at" in compiled
        assert "created_at" in compiled
    finally:
        _stop(patches)


@pytest.mark.asyncio
async def test_select_notes_for_context_returns_results():
    n = Note(id="n_1", persona_id="chiwei", content="x")
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_IterResult([n]))
    patches = _patch_module("app.data.queries.memory_edges", session)
    try:
        from app.data.queries import select_notes_for_context
        result = await select_notes_for_context(persona_id="chiwei")
        assert result == [n]
    finally:
        _stop(patches)
```

- [ ] **Step 2: Run test — 确认 fail**

Run: `cd apps/agent-service && uv run pytest tests/unit/data/test_v4_queries.py -k select_notes_for_context -v`
Expected: FAIL `ImportError`

- [ ] **Step 3: 实现 `select_notes_for_context`**

在 `apps/agent-service/app/data/queries/memory_edges.py`：

文件顶部 import 区把 `from datetime import datetime` 改为：

```python
from datetime import datetime, timedelta, timezone
```

把 `"select_notes_for_context"` 加入 `__all__`（最终 5 个旧 + 4 个新 + 2 个待删 = 11 个）。

在文件末尾追加常量和函数：

```python
_CONTEXT_RECENT_OVERDUE_DAYS = 3
_CONTEXT_NEW_MEMO_DAYS = 7
_CONTEXT_NOTES_LIMIT = 15


async def select_notes_for_context(persona_id: str) -> list[Note]:
    """Return notes appropriate for live context injection.

    Filters:
    - Notes with ``when_at`` are kept if ``when_at >= now - 3 days``
      (upcoming + recently overdue).
    - Notes without ``when_at`` are kept if ``created_at >= now - 7 days``
      (fresh memos).

    Order: notes with ``when_at`` first (ascending — soonest first), then
    notes without ``when_at`` (most recent created first). Capped at 15 rows.
    """
    now = datetime.now(timezone.utc)
    overdue_cutoff = now - timedelta(days=_CONTEXT_RECENT_OVERDUE_DAYS)
    memo_cutoff = now - timedelta(days=_CONTEXT_NEW_MEMO_DAYS)

    async with auto_tx():
        result = await current_session().execute(
            select(Note)
            .where(Note.persona_id == persona_id)
            .where(Note.resolved_at.is_(None))
            .where(Note.deleted_at.is_(None))
            .where(
                # (when_at IS NOT NULL AND when_at >= overdue_cutoff)
                # OR (when_at IS NULL AND created_at >= memo_cutoff)
                (Note.when_at.is_not(None) & (Note.when_at >= overdue_cutoff))
                | (Note.when_at.is_(None) & (Note.created_at >= memo_cutoff))
            )
            .order_by(
                Note.when_at.asc().nulls_last(),
                Note.created_at.desc(),
            )
            .limit(_CONTEXT_NOTES_LIMIT)
        )
        return list(result.scalars().all())
```

- [ ] **Step 4: Run test — 确认 pass**

Run: `cd apps/agent-service && uv run pytest tests/unit/data/test_v4_queries.py -k select_notes_for_context -v`
Expected: PASS（2 个测试）

- [ ] **Step 5: 同步 `EXPECTED_FUNCTIONS` 基线**

编辑 `apps/agent-service/tests/unit/data/test_queries_split.py`，memory_edges 段 (10) → (11)，加 `"select_notes_for_context"`；顶部注释 "（83 函数）" → "（84 函数）"。

```python
    # memory_edges (11) — edges + notes
    "insert_memory_edge", "delete_edge", "list_edges_to", "list_edges_from",
    "insert_note", "upsert_note", "delete_note", "list_active_notes",
    "select_notes_for_context", "get_active_notes", "resolve_note",
```

Run: `cd apps/agent-service && uv run pytest tests/unit/data/test_queries_split.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add apps/agent-service/app/data/queries/memory_edges.py \
        apps/agent-service/tests/unit/data/test_v4_queries.py \
        apps/agent-service/tests/unit/data/test_queries_split.py
git commit -m "feat(notes): add select_notes_for_context with 3d/7d window + 15 cap"
```

---

## Task 6: 公共 helper — `format_when_label`

**Files:**
- Create: `apps/agent-service/app/memory/notes_format.py`
- Test: `apps/agent-service/tests/unit/memory/test_notes_format.py`

`format_when_label(when_at, created_at, now)` 接收 3 个 datetime 参数（when_at 可选），返回中文相对时间标签字符串。

- [ ] **Step 1: 写失败测试**

创建 `apps/agent-service/tests/unit/memory/test_notes_format.py`：

```python
"""Test format_when_label helper for Notes context injection / list_note tool."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.memory.notes_format import format_when_label


_NOW = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)


def _at(days: int, hour: int = 12) -> datetime:
    return _NOW + timedelta(days=days, hours=hour - 12)


@pytest.mark.parametrize(
    ("when_at", "created_at", "expected"),
    [
        # when_at present
        (_at(0), _at(0), "今天"),
        (_at(1), _at(0), "明天"),
        (_at(2), _at(0), "还有 2 天"),
        (_at(7), _at(0), "还有 7 天"),
        (_at(-1), _at(0), "昨天就该做"),
        (_at(-2), _at(0), "已过期 2 天"),
        (_at(-5), _at(0), "已过期 5 天"),
        # when_at None
        (None, _NOW, "今天记的，没说时间"),
        (None, _NOW - timedelta(days=1), "1 天前记的，没说时间"),
        (None, _NOW - timedelta(days=14), "14 天前记的，没说时间"),
    ],
)
def test_format_when_label(when_at, created_at, expected):
    assert format_when_label(when_at=when_at, created_at=created_at, now=_NOW) == expected
```

- [ ] **Step 2: Run test — 确认 fail**

Run: `cd apps/agent-service && uv run pytest tests/unit/memory/test_notes_format.py -v`
Expected: FAIL `ModuleNotFoundError: No module named 'app.memory.notes_format'`

- [ ] **Step 3: 实现 `format_when_label`**

创建 `apps/agent-service/app/memory/notes_format.py`：

```python
"""Format a Note's when_at / created_at into a human-readable Chinese label.

Used both by ``list_note`` tool output and ``build_active_notes_section``
context injection. Day boundaries are computed in CST (UTC+8) so "今天" /
"明天" align with the user's lived day, not UTC.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

_CST = timezone(timedelta(hours=8))


def _to_local_date(dt: datetime) -> datetime:
    return dt.astimezone(_CST).replace(hour=0, minute=0, second=0, microsecond=0)


def format_when_label(
    *,
    when_at: datetime | None,
    created_at: datetime,
    now: datetime,
) -> str:
    """Return a Chinese relative-time label.

    - ``when_at`` set: "今天" / "明天" / "还有 N 天" / "昨天就该做" / "已过期 N 天"
    - ``when_at`` None: "今天记的，没说时间" / "N 天前记的，没说时间"
    """
    today = _to_local_date(now)

    if when_at is not None:
        target = _to_local_date(when_at)
        delta_days = (target - today).days
        if delta_days == 0:
            return "今天"
        if delta_days == 1:
            return "明天"
        if delta_days >= 2:
            return f"还有 {delta_days} 天"
        if delta_days == -1:
            return "昨天就该做"
        return f"已过期 {-delta_days} 天"

    created_local = _to_local_date(created_at)
    delta_days = (today - created_local).days
    if delta_days <= 0:
        return "今天记的，没说时间"
    return f"{delta_days} 天前记的，没说时间"
```

- [ ] **Step 4: Run test — 确认 pass**

Run: `cd apps/agent-service && uv run pytest tests/unit/memory/test_notes_format.py -v`
Expected: 10 个 parametrize case 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/memory/notes_format.py \
        apps/agent-service/tests/unit/memory/test_notes_format.py
git commit -m "feat(notes): add format_when_label helper for relative-time display"
```

---

## Task 7: section 层 — 改造 `build_active_notes_section`

**Files:**
- Rewrite: `apps/agent-service/app/memory/sections/active_notes.py`
- Rewrite: `apps/agent-service/tests/unit/memory/sections/test_active_notes.py`

新逻辑：
1. 从 `select_notes_for_context` 拿"活跃"列表（已经按窗口 + 上限过滤）
2. 同时调 `list_active_notes` 拿全量数量（用于截断提示）
3. 渲染成"清单 + 数量提示"

- [ ] **Step 1: 写失败测试**

完全重写 `apps/agent-service/tests/unit/memory/sections/test_active_notes.py`：

```python
"""Test active_notes section after windowed-injection redesign."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.data.models import Note
from app.memory.sections.active_notes import build_active_notes_section


def _note(*, id: str, content: str, when_at=None, created_at=None) -> Note:
    n = Note(
        id=id,
        persona_id="chiwei",
        content=content,
        when_at=when_at,
    )
    if created_at is not None:
        n.created_at = created_at
    return n


_NOW = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)


def _patch_now(monkeypatch):
    """Pin datetime.now in active_notes module to _NOW."""
    import app.memory.sections.active_notes as mod

    class _FixedDt:
        @staticmethod
        def now(tz=None):
            return _NOW.astimezone(tz) if tz else _NOW

    monkeypatch.setattr(mod, "datetime", _FixedDt)


@pytest.mark.asyncio
async def test_section_empty_when_no_active(monkeypatch):
    _patch_now(monkeypatch)
    with patch(
        "app.memory.sections.active_notes.select_notes_for_context",
        new=AsyncMock(return_value=[]),
    ):
        with patch(
            "app.memory.sections.active_notes.list_active_notes",
            new=AsyncMock(return_value=[]),
        ):
            text = await build_active_notes_section(persona_id="chiwei")
    assert text == ""


@pytest.mark.asyncio
async def test_section_renders_active_with_when_label(monkeypatch):
    _patch_now(monkeypatch)
    n1 = _note(
        id="n_1",
        content="周五和浩南看电影",
        when_at=_NOW + timedelta(days=2),
        created_at=_NOW - timedelta(hours=1),
    )
    n2 = _note(
        id="n_2",
        content="想问妈妈那件事",
        when_at=None,
        created_at=_NOW - timedelta(days=1),
    )
    with patch(
        "app.memory.sections.active_notes.select_notes_for_context",
        new=AsyncMock(return_value=[n1, n2]),
    ):
        with patch(
            "app.memory.sections.active_notes.list_active_notes",
            new=AsyncMock(return_value=[n1, n2]),
        ):
            text = await build_active_notes_section(persona_id="chiwei")
    assert "周五和浩南看电影 [还有 2 天] (id: n_1)" in text
    assert "想问妈妈那件事 [1 天前记的，没说时间] (id: n_2)" in text
    assert text.startswith("你的清单")


@pytest.mark.asyncio
async def test_section_appends_remainder_when_truncated(monkeypatch):
    """active_total > injected_count → append truncation hint."""
    _patch_now(monkeypatch)
    injected = [
        _note(id=f"n_{i}", content=f"事 {i}", when_at=None,
              created_at=_NOW - timedelta(hours=i))
        for i in range(15)
    ]
    all_active = injected + [
        _note(id="n_old", content="老事", when_at=None,
              created_at=_NOW - timedelta(days=30)),
        _note(id="n_old2", content="更老的", when_at=None,
              created_at=_NOW - timedelta(days=40)),
    ]
    with patch(
        "app.memory.sections.active_notes.select_notes_for_context",
        new=AsyncMock(return_value=injected),
    ):
        with patch(
            "app.memory.sections.active_notes.list_active_notes",
            new=AsyncMock(return_value=all_active),
        ):
            text = await build_active_notes_section(persona_id="chiwei")
    assert "（清单里还有 2 条更老的没列出来，用 list_note 看全部。）" in text


@pytest.mark.asyncio
async def test_section_only_remainder_hint_when_all_old(monkeypatch):
    """Active notes exist but none meet injection window → show only remainder hint."""
    _patch_now(monkeypatch)
    old = [
        _note(id="n_old", content="老事", when_at=None,
              created_at=_NOW - timedelta(days=30)),
        _note(id="n_old2", content="更老", when_at=None,
              created_at=_NOW - timedelta(days=40)),
        _note(id="n_old3", content="最老",
              when_at=_NOW - timedelta(days=10),
              created_at=_NOW - timedelta(days=12)),
    ]
    with patch(
        "app.memory.sections.active_notes.select_notes_for_context",
        new=AsyncMock(return_value=[]),
    ):
        with patch(
            "app.memory.sections.active_notes.list_active_notes",
            new=AsyncMock(return_value=old),
        ):
            text = await build_active_notes_section(persona_id="chiwei")
    assert text == "你的清单里还有 3 条没动的事（用 list_note 看）。"


@pytest.mark.asyncio
async def test_section_swallows_query_errors(monkeypatch):
    """Section must not crash if query layer fails (resilience)."""
    _patch_now(monkeypatch)
    with patch(
        "app.memory.sections.active_notes.select_notes_for_context",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    ):
        text = await build_active_notes_section(persona_id="chiwei")
    assert text == ""
```

- [ ] **Step 2: Run test — 确认 fail**

Run: `cd apps/agent-service && uv run pytest tests/unit/memory/sections/test_active_notes.py -v`
Expected: FAIL — current implementation imports `get_active_notes`，新测试 import `select_notes_for_context` / `list_active_notes`

- [ ] **Step 3: 实现新 section**

完全重写 `apps/agent-service/app/memory/sections/active_notes.py`：

```python
"""Always-on injection: active notes (windowed + capped + remainder hint).

The section pulls a limited set of "live-ish" notes via
``select_notes_for_context`` (3-day overdue / 7-day memo window, 15 rows max)
plus the total active count via ``list_active_notes`` so we can append a
truncation hint pointing the agent at ``list_note`` for the full picture.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.data.queries import list_active_notes, select_notes_for_context
from app.memory.notes_format import format_when_label

logger = logging.getLogger(__name__)


async def build_active_notes_section(*, persona_id: str) -> str:
    try:
        injected = await select_notes_for_context(persona_id=persona_id)
        all_active = await list_active_notes(persona_id=persona_id)
    except Exception as e:
        logger.warning("active_notes failed: %s", e)
        return ""

    if not all_active:
        return ""

    total = len(all_active)
    shown = len(injected)

    # All active notes are old enough that none made the injection window.
    if shown == 0:
        return f"你的清单里还有 {total} 条没动的事（用 list_note 看）。"

    now = datetime.now(timezone.utc)
    lines = ["你的清单（最近活跃，全部用 list_note 查）："]
    for n in injected:
        label = format_when_label(when_at=n.when_at, created_at=n.created_at, now=now)
        lines.append(f"- {n.content} [{label}] (id: {n.id})")

    remainder = total - shown
    if remainder > 0:
        lines.append(
            f"（清单里还有 {remainder} 条更老的没列出来，用 list_note 看全部。）"
        )

    return "\n".join(lines)
```

- [ ] **Step 4: Run test — 确认 pass**

Run: `cd apps/agent-service && uv run pytest tests/unit/memory/sections/test_active_notes.py -v`
Expected: 5 个测试全部 PASS

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/memory/sections/active_notes.py \
        apps/agent-service/tests/unit/memory/sections/test_active_notes.py
git commit -m "feat(notes): redesign active_notes section with windowed injection + remainder hint"
```

---

## Task 8: tool 层 — 重写 `notes.py`（4 个工具）

**Files:**
- Rewrite: `apps/agent-service/app/agent/tools/notes.py`
- Rewrite: `apps/agent-service/tests/unit/agent/tools/test_notes.py`
- Modify: `apps/agent-service/app/agent/tools/__init__.py`

工具集：
- `upsert_note(content, when_at?, note_id?)` — 替代 `write_note`
- `list_note()` — 新增
- `delete_note(note_id, reason)` — 新增
- `resolve_note(note_id, resolution)` — description 调整，逻辑不变

- [ ] **Step 1: 写失败测试**

完全重写 `apps/agent-service/tests/unit/agent/tools/test_notes.py`：

```python
"""Test upsert_note / list_note / resolve_note / delete_note tool implementations."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.tools.notes import (
    _delete_note_impl,
    _list_note_impl,
    _resolve_note_impl,
    _upsert_note_impl,
)


@asynccontextmanager
async def _fake_tx():
    yield


def _make_emit_tx_mock():
    captured: list = []

    async def _fake_emit_tx(ev):
        captured.append(ev)

    return _fake_emit_tx, captured


# ----- upsert_note: create -----

@pytest.mark.asyncio
async def test_upsert_note_create_emits_note_created():
    from app.domain.agent_tool_events import NoteCreated

    created = MagicMock(
        id="n_new", content="周五看电影", when_at=None,
        created_at=datetime(2026, 5, 10, tzinfo=UTC),
    )
    fake_emit, captured = _make_emit_tx_mock()
    with patch("app.agent.tools.notes.upsert_note_query",
               new=AsyncMock(return_value=created)) as up:
        with patch("app.agent.tools.notes.tx", _fake_tx):
            with patch("app.agent.tools.notes.emit_tx", fake_emit):
                out = await _upsert_note_impl(
                    persona_id="chiwei",
                    content="周五看电影",
                    when_at_raw=None,
                    note_id=None,
                )
    assert out["id"] == "n_new"
    up.assert_awaited_once()
    assert len(captured) == 1
    ev = captured[0]
    assert isinstance(ev, NoteCreated)
    assert ev.note_id == "n_new"


@pytest.mark.asyncio
async def test_upsert_note_rejects_empty_content():
    fake_emit, captured = _make_emit_tx_mock()
    with patch("app.agent.tools.notes.upsert_note_query",
               new=AsyncMock()) as up:
        with patch("app.agent.tools.notes.tx", _fake_tx):
            with patch("app.agent.tools.notes.emit_tx", fake_emit):
                out = await _upsert_note_impl(
                    persona_id="chiwei",
                    content="  ",
                    when_at_raw=None,
                    note_id=None,
                )
    assert "error" in out
    up.assert_not_awaited()
    assert captured == []


@pytest.mark.asyncio
async def test_upsert_note_parses_iso_when_at():
    when_iso = "2026-05-15T19:00:00+08:00"
    created = MagicMock(id="n_x", content="x", when_at=None,
                        created_at=datetime(2026, 5, 10, tzinfo=UTC))
    fake_emit, _ = _make_emit_tx_mock()
    with patch("app.agent.tools.notes.upsert_note_query",
               new=AsyncMock(return_value=created)) as up:
        with patch("app.agent.tools.notes.tx", _fake_tx):
            with patch("app.agent.tools.notes.emit_tx", fake_emit):
                await _upsert_note_impl(
                    persona_id="chiwei", content="x",
                    when_at_raw=when_iso, note_id=None,
                )
    kwargs = up.await_args.kwargs
    assert kwargs["when_at"] == datetime.fromisoformat(when_iso)


@pytest.mark.asyncio
async def test_upsert_note_rejects_bad_when_at():
    fake_emit, _ = _make_emit_tx_mock()
    with patch("app.agent.tools.notes.upsert_note_query",
               new=AsyncMock()) as up:
        with patch("app.agent.tools.notes.tx", _fake_tx):
            with patch("app.agent.tools.notes.emit_tx", fake_emit):
                out = await _upsert_note_impl(
                    persona_id="chiwei", content="x",
                    when_at_raw="not-a-date", note_id=None,
                )
    assert "error" in out
    up.assert_not_awaited()


# ----- upsert_note: update -----

@pytest.mark.asyncio
async def test_upsert_note_update_passes_note_id_and_does_not_emit():
    updated = MagicMock(
        id="n_abc", content="改后", when_at=None,
        created_at=datetime(2026, 5, 10, tzinfo=UTC),
    )
    fake_emit, captured = _make_emit_tx_mock()
    with patch("app.agent.tools.notes.upsert_note_query",
               new=AsyncMock(return_value=updated)) as up:
        with patch("app.agent.tools.notes.tx", _fake_tx):
            with patch("app.agent.tools.notes.emit_tx", fake_emit):
                out = await _upsert_note_impl(
                    persona_id="chiwei", content="改后",
                    when_at_raw=None, note_id="n_abc",
                )
    assert out["id"] == "n_abc"
    kwargs = up.await_args.kwargs
    assert kwargs["note_id"] == "n_abc"
    # update path: when_at_raw is None (no instruction) → query gets _UNSET
    assert "when_at" in kwargs
    # NoteCreated must NOT fire on update
    assert captured == []


@pytest.mark.asyncio
async def test_upsert_note_clear_when_at_translates_to_explicit_none():
    """when_at_raw='clear' → query receives when_at=None (not _UNSET)."""
    from app.data.queries.memory_edges import _UNSET
    updated = MagicMock(id="n_abc", content="x", when_at=None,
                        created_at=datetime(2026, 5, 10, tzinfo=UTC))
    fake_emit, _ = _make_emit_tx_mock()
    with patch("app.agent.tools.notes.upsert_note_query",
               new=AsyncMock(return_value=updated)) as up:
        with patch("app.agent.tools.notes.tx", _fake_tx):
            with patch("app.agent.tools.notes.emit_tx", fake_emit):
                await _upsert_note_impl(
                    persona_id="chiwei", content="x",
                    when_at_raw="clear", note_id="n_abc",
                )
    assert up.await_args.kwargs["when_at"] is None
    assert up.await_args.kwargs["when_at"] is not _UNSET


@pytest.mark.asyncio
async def test_upsert_note_unknown_id_returns_error():
    fake_emit, _ = _make_emit_tx_mock()
    with patch(
        "app.agent.tools.notes.upsert_note_query",
        new=AsyncMock(side_effect=LookupError("note not found: n_x")),
    ):
        with patch("app.agent.tools.notes.tx", _fake_tx):
            with patch("app.agent.tools.notes.emit_tx", fake_emit):
                out = await _upsert_note_impl(
                    persona_id="chiwei", content="x",
                    when_at_raw=None, note_id="n_x",
                )
    assert "error" in out
    assert "n_x" in out["error"]


# ----- list_note -----

@pytest.mark.asyncio
async def test_list_note_returns_items_with_when_label():
    rows = [
        MagicMock(
            id="n_1", content="周五看电影",
            when_at=datetime(2026, 5, 15, 19, 0, tzinfo=UTC),
            created_at=datetime(2026, 5, 9, tzinfo=UTC),
        ),
        MagicMock(
            id="n_2", content="想问妈妈那件事",
            when_at=None,
            created_at=datetime(2026, 5, 8, tzinfo=UTC),
        ),
    ]
    with patch("app.agent.tools.notes.list_active_notes_query",
               new=AsyncMock(return_value=rows)):
        out = await _list_note_impl(persona_id="chiwei")
    assert len(out["items"]) == 2
    assert out["items"][0]["note_id"] == "n_1"
    assert "when_label" in out["items"][0]
    assert out["items"][1]["when_at"] is None


@pytest.mark.asyncio
async def test_list_note_empty():
    with patch("app.agent.tools.notes.list_active_notes_query",
               new=AsyncMock(return_value=[])):
        out = await _list_note_impl(persona_id="chiwei")
    assert out == {"items": []}


# ----- delete_note -----

@pytest.mark.asyncio
async def test_delete_note_passes_reason():
    with patch("app.agent.tools.notes.delete_note_query",
               new=AsyncMock()) as dn:
        out = await _delete_note_impl(
            persona_id="chiwei", note_id="n_abc", reason="改主意了",
        )
    assert out == {"ok": True}
    dn.assert_awaited_once_with(note_id="n_abc", reason="改主意了")


@pytest.mark.asyncio
async def test_delete_note_rejects_empty_reason():
    with patch("app.agent.tools.notes.delete_note_query",
               new=AsyncMock()) as dn:
        out = await _delete_note_impl(
            persona_id="chiwei", note_id="n_abc", reason="  ",
        )
    assert "error" in out
    dn.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_note_rejects_empty_note_id():
    with patch("app.agent.tools.notes.delete_note_query",
               new=AsyncMock()) as dn:
        out = await _delete_note_impl(
            persona_id="chiwei", note_id="", reason="改主意了",
        )
    assert "error" in out
    dn.assert_not_awaited()


# ----- resolve_note (logic unchanged) -----

@pytest.mark.asyncio
async def test_resolve_note_calls_query():
    with patch("app.agent.tools.notes.resolve_note_query", new=AsyncMock()) as rn:
        out = await _resolve_note_impl(
            persona_id="chiwei", note_id="n_1", resolution="看完了",
        )
    assert out == {"ok": True}
    rn.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_note_rejects_whitespace_resolution():
    with patch("app.agent.tools.notes.resolve_note_query", new=AsyncMock()) as rn:
        out = await _resolve_note_impl(
            persona_id="chiwei", note_id="n_1", resolution="  ",
        )
    assert "error" in out
    rn.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_note_rejects_empty_note_id():
    with patch("app.agent.tools.notes.resolve_note_query", new=AsyncMock()) as rn:
        out = await _resolve_note_impl(
            persona_id="chiwei", note_id="", resolution="看完了",
        )
    assert "error" in out
    rn.assert_not_awaited()
```

- [ ] **Step 2: Run test — 确认 fail**

Run: `cd apps/agent-service && uv run pytest tests/unit/agent/tools/test_notes.py -v`
Expected: FAIL `ImportError` （`_upsert_note_impl` / `_list_note_impl` / `_delete_note_impl` 未定义）

- [ ] **Step 3: 重写 `apps/agent-service/app/agent/tools/notes.py`**

```python
"""Notes tool set — 赤尾自己的主动清单 (CRUD)。

Tools exposed:
- ``upsert_note`` (create / update content / update when_at / clear when_at)
- ``list_note``  (full active list, with when_label)
- ``resolve_note`` (mark as completed)
- ``delete_note`` (soft delete with mandatory reason)
"""

from __future__ import annotations

from datetime import datetime, timezone

from langchain.tools import tool
from langgraph.runtime import get_runtime

from app.agent.context import AgentContext
from app.agent.tools._common import tool_error
from app.data.queries import list_active_notes as list_active_notes_query
from app.data.queries import upsert_note as upsert_note_query
from app.data.queries import delete_note as delete_note_query
from app.data.queries import resolve_note as resolve_note_query
from app.data.queries.memory_edges import _UNSET
from app.domain.agent_tool_events import NoteCreated
from app.memory.notes_format import format_when_label
from app.runtime.db import emit_tx, tx


def _serialize(n) -> dict:
    return {
        "note_id": n.id,
        "content": n.content,
        "when_at": n.when_at.isoformat() if n.when_at else None,
        "created_at": n.created_at.isoformat() if n.created_at else None,
        "when_label": format_when_label(
            when_at=n.when_at,
            created_at=n.created_at,
            now=datetime.now(timezone.utc),
        ),
    }


# ---------------------------------------------------------------------------
# upsert_note
# ---------------------------------------------------------------------------

async def _upsert_note_impl(
    *,
    persona_id: str,
    content: str,
    when_at_raw: str | None,
    note_id: str | None,
) -> dict:
    content = (content or "").strip()
    if not content:
        return {"error": "content 不能为空"}

    # Translate when_at_raw into the query-layer sentinel pattern:
    # - None       → _UNSET (don't touch column on update; default NULL on create)
    # - "clear"    → None   (explicit clear on update)
    # - ISO string → datetime
    if when_at_raw is None:
        when_at: datetime | None | object = _UNSET
    elif when_at_raw.strip().lower() == "clear":
        when_at = None
    else:
        try:
            when_at = datetime.fromisoformat(when_at_raw)
        except ValueError:
            return {"error": f"when_at 格式无效: {when_at_raw}"}

    is_create = note_id is None
    async with tx():
        try:
            row = await upsert_note_query(
                persona_id=persona_id,
                content=content,
                when_at=when_at,
                note_id=note_id,
            )
        except LookupError as e:
            return {"error": str(e)}
        if is_create:
            await emit_tx(NoteCreated(note_id=row.id, persona_id=persona_id))

    return {"id": row.id, "note": _serialize(row)}


# ---------------------------------------------------------------------------
# list_note
# ---------------------------------------------------------------------------

async def _list_note_impl(*, persona_id: str) -> dict:
    rows = await list_active_notes_query(persona_id=persona_id)
    return {"items": [_serialize(n) for n in rows]}


# ---------------------------------------------------------------------------
# resolve_note
# ---------------------------------------------------------------------------

async def _resolve_note_impl(
    *,
    persona_id: str,
    note_id: str,
    resolution: str,
) -> dict:
    resolution = (resolution or "").strip()
    if not note_id or not resolution:
        return {"error": "note_id 和 resolution 都不能为空"}

    await resolve_note_query(note_id=note_id, resolution=resolution)
    return {"ok": True}


# ---------------------------------------------------------------------------
# delete_note
# ---------------------------------------------------------------------------

async def _delete_note_impl(
    *,
    persona_id: str,
    note_id: str,
    reason: str,
) -> dict:
    reason = (reason or "").strip()
    if not note_id:
        return {"error": "note_id 不能为空"}
    if not reason:
        return {"error": "reason 不能为空，请说明为什么删"}

    await delete_note_query(note_id=note_id, reason=reason)
    return {"ok": True}


# ---------------------------------------------------------------------------
# @tool wrappers — exposed to the LLM
# ---------------------------------------------------------------------------

@tool
@tool_error("笔记保存失败")
async def upsert_note(
    content: str,
    when_at: str | None = None,
    note_id: str | None = None,
) -> dict:
    """把一件你觉得必须记住的事写进清单，或者更新已有的一条。

    这是你自己的清单，不是系统强加的承诺列表。只有你觉得"不能忘"、"需要专门记住"的
    事才写。

    什么时候用：
    - 第一次提到一件想记住的事 → 不传 note_id，会创建新的一条
    - 用户重复提到同一件事（比如"那家餐厅改成下周去了"）→ 传清单里看到的 note_id，会更新

    Args:
        content: 笔记内容（必填）。
        when_at: ISO 8601 时间戳（"2026-05-15T19:00:00+08:00"）。**如果这件事和某个时间相关
            （"明天"/"周五"/"下个月"/具体日子），强烈建议填**。没明确时间线索就别硬填。
            想清空已有的 when_at 传 "clear"（仅在更新场景有意义）。
        note_id: 已有 note 的 id（形如 "n_xxx"，从清单里看到）；不传则新建。
    """
    context = get_runtime(AgentContext).context
    return await _upsert_note_impl(
        persona_id=context.persona_id,
        content=content,
        when_at_raw=when_at,
        note_id=note_id,
    )


@tool
@tool_error("清单查询失败")
async def list_note() -> dict:
    """列出你目前的全部清单（没完成、没删除的）。

    什么时候用：
    - 用户问起"你都记了啥"
    - 你想盘点一下有没有重复的、可以合并的
    - 你想看看有没有挂了很久该处理的事

    注意：context 里通常已经有"最近活跃"的几条了。需要看全量、找特定 id、
    清盘的时候才用这个。

    Returns:
        ``{"items": [{"note_id": "n_xxx", "content": "...", "when_at": "...|null",
        "created_at": "...", "when_label": "还有 2 天"}, ...]}``
    """
    context = get_runtime(AgentContext).context
    return await _list_note_impl(persona_id=context.persona_id)


@tool
@tool_error("清单更新失败")
async def resolve_note(note_id: str, resolution: str) -> dict:
    """把一条已经完结的笔记划掉。

    比如电影看了、想法落实了。resolution 写一句话说明结果（"看完了"/"做完了"）。

    这是"完成"，不是"删除"。如果是改主意了 / 记错了 / 重复了，用 delete_note。

    Args:
        note_id: 笔记 id（形如 "n_xxxxxx"）
        resolution: 结果描述（必填）
    """
    context = get_runtime(AgentContext).context
    return await _resolve_note_impl(
        persona_id=context.persona_id,
        note_id=note_id,
        resolution=resolution,
    )


@tool
@tool_error("清单删除失败")
async def delete_note(note_id: str, reason: str) -> dict:
    """真删除一条清单项。

    和 resolve 不同 —— resolve 是"做完了"留个痕，delete 是"这条本来就不该存在"。

    什么时候用：
    - 改主意了，不打算做这件事了
    - 当时记错了，根本不是这件事
    - 发现是重复的（已经有一模一样的另一条）

    Args:
        note_id: 笔记 id（形如 "n_xxx"）
        reason: 必填，写明为什么删（"改主意了" / "记错了" / "和 n_xyz 重复"）
    """
    context = get_runtime(AgentContext).context
    return await _delete_note_impl(
        persona_id=context.persona_id,
        note_id=note_id,
        reason=reason,
    )
```

- [ ] **Step 4: 更新工具注册**

编辑 `apps/agent-service/app/agent/tools/__init__.py`：

```python
"""Agent tool sets.

Exports two tool lists:

- ``BASE_TOOLS`` — available to all agents including sub-agents.
- ``ALL_TOOLS`` — only for the main agent.
"""

from app.agent.tools.commit_abstract import commit_abstract_memory
from app.agent.tools.delegation import deep_research
from app.agent.tools.history import (
    check_chat_history,
    list_group_members,
    search_group_history,
)
from app.agent.tools.image import generate_image, read_images
from app.agent.tools.image_search import search_images
from app.agent.tools.notes import delete_note, list_note, resolve_note, upsert_note
from app.agent.tools.recall import recall
from app.agent.tools.sandbox import sandbox_bash
from app.agent.tools.search import search_web
from app.agent.tools.skill import load_skill
from app.agent.tools.update_schedule import update_schedule

# Base tools: available to all agents (including sub-agents like research)
BASE_TOOLS = [
    search_web,
    search_images,
    generate_image,
    read_images,
    recall,
    commit_abstract_memory,
    upsert_note,
    list_note,
    resolve_note,
    delete_note,
    update_schedule,
]

# All tools: only for the main agent.
# History search tools are intentionally excluded: current-chat and cross-chat
# context should be injected up front rather than searched ad hoc at reply time.
ALL_TOOLS = [
    *BASE_TOOLS,
    list_group_members,
    deep_research,
    load_skill,
    sandbox_bash,
]

__all__ = [
    "BASE_TOOLS",
    "ALL_TOOLS",
    # Individual tools (for callers that need fine-grained control)
    "search_web",
    "search_images",
    "generate_image",
    "read_images",
    "recall",
    "commit_abstract_memory",
    "upsert_note",
    "list_note",
    "resolve_note",
    "delete_note",
    "update_schedule",
    "check_chat_history",
    "search_group_history",
    "list_group_members",
    "deep_research",
    "load_skill",
    "sandbox_bash",
]
```

- [ ] **Step 5: Run tool tests — 确认 pass**

Run: `cd apps/agent-service && uv run pytest tests/unit/agent/tools/test_notes.py -v`
Expected: 14 个测试全部 PASS

- [ ] **Step 6: Commit**

```bash
git add apps/agent-service/app/agent/tools/notes.py \
        apps/agent-service/app/agent/tools/__init__.py \
        apps/agent-service/tests/unit/agent/tools/test_notes.py
git commit -m "feat(notes): replace write_note with upsert_note + list_note + delete_note"
```

---

## Task 9: 切除旧 query — 删 `insert_note` / `get_active_notes` 并清理 `__all__` / `EXPECTED_FUNCTIONS`

**Files:**
- Modify: `apps/agent-service/app/data/queries/memory_edges.py`
- Modify: `apps/agent-service/tests/unit/data/test_queries_split.py`
- Modify: `apps/agent-service/tests/unit/data/test_v4_queries.py`（删旧测试）

前置条件：Task 8 完成（tool 层已切到新 query），且 `app/memory/sections/active_notes.py` 已切到 `select_notes_for_context` + `list_active_notes`（Task 7）。也就是 `insert_note` 和 `get_active_notes` 已无业务调用方。

- [ ] **Step 1: 删除 `memory_edges.py` 里的 `insert_note` / `get_active_notes` 函数定义并更新 `__all__`**

编辑 `apps/agent-service/app/data/queries/memory_edges.py`：

1. 删除 `async def insert_note(...)` 函数体
2. 删除 `async def get_active_notes(...)` 函数体
3. `__all__` 改成最终态（9 项，删去 `"insert_note"` 和 `"get_active_notes"`）：

```python
__all__ = [
    "insert_memory_edge",
    "delete_edge",
    "list_edges_to",
    "list_edges_from",
    "upsert_note",
    "delete_note",
    "list_active_notes",
    "select_notes_for_context",
    "resolve_note",
]
```

- [ ] **Step 2: 更新 `test_queries_split.py` 的 `EXPECTED_FUNCTIONS`**

编辑 `apps/agent-service/tests/unit/data/test_queries_split.py`，把 memory_edges 段从 (11) 改回 (9)（删 `"insert_note"` 和 `"get_active_notes"`）；顶部注释 "（84 函数）" → "（82 函数）"。

```python
    # memory_edges (9) — edges + notes
    "insert_memory_edge", "delete_edge", "list_edges_to", "list_edges_from",
    "upsert_note", "delete_note", "list_active_notes", "select_notes_for_context",
    "resolve_note",
```

- [ ] **Step 3: 删除 `test_v4_queries.py` 里的旧 note 测试**

删除 `tests/unit/data/test_v4_queries.py` 里的 `test_insert_note_adds_to_session` 和 `test_get_active_notes_returns_list` 两个测试函数（保留 `test_resolve_note_executes_update`）。同时把文件顶部 import：

```python
from app.data.queries import (
    count_abstracts_by_persona,
    get_active_notes,
    get_current_schedule,
    insert_abstract_memory,
    insert_fragment,
    insert_memory_edge,
    insert_note,
    insert_schedule_revision,
    resolve_note,
    touch_abstract,
    touch_fragment,
)
```

改成（删除 `get_active_notes` 和 `insert_note`）：

```python
from app.data.queries import (
    count_abstracts_by_persona,
    get_current_schedule,
    insert_abstract_memory,
    insert_fragment,
    insert_memory_edge,
    insert_schedule_revision,
    resolve_note,
    touch_abstract,
    touch_fragment,
)
```

- [ ] **Step 4: Run all queries tests**

Run: `cd apps/agent-service && uv run pytest tests/unit/data/ -v`
Expected: 全部 PASS（`test_queries_all_complete` / `test_queries_no_duplicate_names` / `test_queries_each_function_callable` 都过）

- [ ] **Step 5: Grep 验证 `insert_note` / `get_active_notes` 零残留**

Run: `grep -rn 'insert_note\|get_active_notes' apps/agent-service/app/ apps/agent-service/tests/`
Expected: 没有输出（除注释、文档外）。如果有命中，逐一切换或删除。

- [ ] **Step 6: Commit**

```bash
git add apps/agent-service/app/data/queries/memory_edges.py \
        apps/agent-service/tests/unit/data/test_queries_split.py \
        apps/agent-service/tests/unit/data/test_v4_queries.py
git commit -m "refactor(notes): remove insert_note/get_active_notes (replaced by upsert/list/select)"
```

---

## Task 10: 全量回归测试 + grep 清扫

**Files:** 全仓

- [ ] **Step 1: 跑 agent-service 全量单测**

Run: `cd apps/agent-service && uv run pytest tests/unit -x -q`
Expected: 全部 PASS。如有 fail，修复后回到对应 Task。

- [ ] **Step 2: Grep 清扫旧符号**

Run:

```bash
grep -rn 'write_note\|_write_note_impl\|insert_note\|get_active_notes' apps/agent-service/
```

Expected: 仅 spec / plan / changelog 等文档/历史文件命中；`apps/agent-service/app/` 和 `apps/agent-service/tests/` 下零业务命中。

- [ ] **Step 3: 跑 ruff / mypy（如项目有）**

Run:

```bash
cd apps/agent-service && uv run ruff check app/ tests/
cd apps/agent-service && uv run mypy app/agent/tools/notes.py app/data/queries/memory_edges.py app/memory/sections/active_notes.py app/memory/notes_format.py
```

Expected: 0 ruff issue, 0 mypy error。

- [ ] **Step 4: Commit（如有 lint / 清扫修复）**

```bash
git add -A
git commit -m "chore(notes): cleanup leftover symbols / fix lint"
```

如无改动则跳过。

---

## Task 11: 端到端验证（部署到泳道）

**等用户确认部署许可后执行。** 不要擅自部署 prod。

- [ ] **Step 1: 推到远端**

```bash
git push origin fix/chiwei-todo-delete
```

- [ ] **Step 2: 部署到泳道（需用户确认 lane 名）**

请用户指定泳道名 `<LANE>`，然后：

```bash
make deploy APP=agent-service LANE=<LANE> GIT_REF=fix/chiwei-todo-delete
```

注意：agent-service 镜像产出 `agent-service` + `vectorize-worker` 两个 deployment，但 vectorize-worker 不依赖 notes 工具集，本次只需 release `agent-service`。

- [ ] **Step 3: 绑定 dev bot 到泳道**

```
/ops bind TYPE=bot KEY=dev LANE=<LANE>
```

- [ ] **Step 4: 飞书 dev bot 实测三个场景**

| 场景 | 输入 | 期望 |
|------|------|------|
| 创建带日期 | "周五和浩南看电影" | 赤尾应调 `upsert_note(content="...", when_at="2026-05-15T...")`，回复确认 |
| 更新已存在 | （在上一条基础上）"那个改成下周吧" | 赤尾从 context 拿到 id，调 `upsert_note(content=..., when_at=..., note_id="n_xxx")`，回复确认 |
| 删除 | "算了不去了，把那条删了" | 赤尾应调 `delete_note(note_id="n_xxx", reason="改主意了")`，回复确认 |

每个场景去 Langfuse 看 trace，确认：
- tool 调用参数正确
- DB 里 `notes` 表对应行的字段符合预期（用 `/ops-db @chiwei SELECT id, content, when_at, deleted_at, delete_reason FROM notes ORDER BY created_at DESC LIMIT 5;`）

- [ ] **Step 5: Context 注入验证**

下一轮对话开始时，看 Langfuse trace 里注入到 LLM 的 prompt 是否包含：
- "你的清单（最近活跃，全部用 list_note 查）：" 段落
- 时间标签是相对时间（"还有 N 天" / "已过期 N 天"）
- 已 delete 的 note 不出现
- 如有 >15 条 active，末尾出现"清单里还有 M 条更老的"提示

- [ ] **Step 6: 验收完，等用户决定是否合码 / 下泳道**

不要擅自 merge / undeploy。报告验证结果给用户，等指令。

---

## Self-Review

跑这些检查（已在写 plan 时验证，落地者无需重做，仅作记录）：

**1. Spec coverage**：
- 设计目标 1（引导填日期）→ Task 8 `upsert_note` description ✓
- 设计目标 2（CRUD 完整）→ Task 2-5 + Task 8 ✓
- 设计目标 3（窗口注入）→ Task 5 + Task 7 ✓
- 设计目标 4（相对时间显示）→ Task 6 + Task 7 ✓
- 设计目标 5（不引入状态机）→ 无新字段 / 无自动隐藏，spec 决策已贯彻 ✓
- DB schema → Task 1 ✓
- 4 个 tool → Task 8 ✓
- 公共 helper → Task 6 ✓

**2. Placeholder scan**：无 TBD/TODO/省略；每个 step 都有完整代码或具体命令。

**3. Type / 命名一致性**：
- `_UNSET` 哨兵在 query 层定义、tool 层 import ✓
- `upsert_note_query` / `list_active_notes_query` / `delete_note_query` / `resolve_note_query` 在 tool 层使用 `as` 别名，避免和 `@tool` 装饰器后的同名冲突 ✓
- `format_when_label` 在两处使用同一函数 ✓
- `EXPECTED_FUNCTIONS` 总数从 80 加到 82（memory_edges 7 → 9） — 注意 spec 文件 `test_queries_split.py:9` 的注释"硬编码作为期望基线（80 函数）"也要同步 → 但 plan 里 Task 9 Step 1 只改 EXPECTED_FUNCTIONS 块，注释里的 80 数字也要改成 82。落地者自查时同步改注释。
