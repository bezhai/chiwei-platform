# Dataflow Phase 7d Implementation Plan — DB / Redis / HTTP capability

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 业务区彻底不见 session / Redis / httpx，业务作者只回答「这几行原子吗 / 这是哪个域 capability」。

**Architecture:** session 走 contextvar，业务用 `async with tx():` 表达原子边界；`emit_tx` 强制 in-tx；single_flight + banned_words + ImageRegistry 三类 Redis 收敛；HTTPClient 按错误粒度区分 retry，业务只见域 capability（web_search / image_search / sandbox / read_webpage / rerank）。

**Tech Stack:** SQLAlchemy AsyncSession, ContextVar, asyncpg, redis.asyncio, httpx, pytest.

**Spec:** `docs/superpowers/specs/2026-05-09-dataflow-phase-7d-design.md`

**Branch:** `feat/dataflow-phase7d-parse`

---

## 共用约定

- **业务区** = `apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/`
- **测试运行**：所有 unit / contract test 用 `cd apps/agent-service && uv run pytest tests/...`
- **lint**：`cd apps/agent-service && uv run ruff check . && uv run ruff format --check .`
- **每 commit 验收**：(a) 单元 / contract 测试 green (b) ruff 通过 (c) 本 commit 关闭的 grep gate 没新增违规
- **commit message 格式**：conventional commits（`feat(...)` / `refactor(...)` / `chore(...)`）
- **小步提交**：每个 task 一个独立 commit，不要攒批

---

## Task 1 — Spec & Plan 落盘

**Files:**
- 已存在：`docs/superpowers/specs/2026-05-09-dataflow-phase-7d-design.md`
- 已存在：`docs/superpowers/plans/2026-05-09-dataflow-phase-7d.md`（本文件）

- [ ] **Step 1: 确认两份文档存在**

```bash
ls docs/superpowers/specs/2026-05-09-dataflow-phase-7d-design.md docs/superpowers/plans/2026-05-09-dataflow-phase-7d.md
```

- [ ] **Step 2: 提交 spec + plan**

```bash
git add docs/superpowers/specs/2026-05-09-dataflow-phase-7d-design.md docs/superpowers/plans/2026-05-09-dataflow-phase-7d.md
git commit -m "docs(spec): Phase 7d gap 13/14/16 design + plan"
```

---

## Task 2 — `runtime/db.py` capability

**Files:**
- Create: `apps/agent-service/app/runtime/db.py`
- Test: `apps/agent-service/tests/unit/test_runtime_db.py`

- [ ] **Step 1: 写测试 — `tx()` / `current_session()` / `emit_tx` / `auto_tx`**

`apps/agent-service/tests/unit/test_runtime_db.py`：

```python
"""Unit + integration tests for runtime.db capability (Gap 13)."""
from __future__ import annotations

import asyncio
import pytest
from sqlalchemy import text

from app.runtime.db import tx, current_session, emit_tx, auto_tx
from app.runtime.data import Data


class _ProbeData(Data):
    """Test-only Data class — concrete name comes from runtime migrator setup."""
    chat_id: str
    note: str

    class Meta:
        dedup_target = "chat_id"


@pytest.mark.asyncio
async def test_current_session_outside_tx_raises():
    with pytest.raises(RuntimeError, match="outside tx"):
        current_session()


@pytest.mark.asyncio
async def test_tx_opens_session():
    async with tx():
        s = current_session()
        result = await s.execute(text("SELECT 1"))
        assert result.scalar_one() == 1


@pytest.mark.asyncio
async def test_emit_tx_outside_raises():
    with pytest.raises(RuntimeError, match="outside tx"):
        await emit_tx(_ProbeData(chat_id="c1", note="n"))


@pytest.mark.asyncio
async def test_emit_tx_in_tx_writes_outbox():
    async with tx():
        await emit_tx(_ProbeData(chat_id="c1", note="hello"))
        s = current_session()
        # outbox row visible within same tx via SELECT
        r = await s.execute(text(
            "SELECT data_type FROM runtime_outbox WHERE payload_json->>'chat_id'='c1'"
        ))
        rows = r.scalars().all()
        assert any("_ProbeData" in row for row in rows)


@pytest.mark.asyncio
async def test_nested_tx_savepoint():
    """Inner raise rolls back inner write; outer continues + commits."""
    async with tx():
        s = current_session()
        await s.execute(text(
            "CREATE TEMP TABLE _phase7d_probe (val TEXT) ON COMMIT DROP"
        ))
        await s.execute(text("INSERT INTO _phase7d_probe VALUES ('outer-A')"))
        try:
            async with tx():  # nested → SAVEPOINT
                await s.execute(text("INSERT INTO _phase7d_probe VALUES ('inner-B')"))
                raise ValueError("intentional")
        except ValueError:
            pass
        await s.execute(text("INSERT INTO _phase7d_probe VALUES ('outer-C')"))
        r = await s.execute(text("SELECT val FROM _phase7d_probe ORDER BY val"))
        rows = [row[0] for row in r.all()]
        assert rows == ["outer-A", "outer-C"]


@pytest.mark.asyncio
async def test_auto_tx_opens_when_outside():
    async with auto_tx():
        s = current_session()
        result = await s.execute(text("SELECT 2"))
        assert result.scalar_one() == 2


@pytest.mark.asyncio
async def test_auto_tx_reuses_when_inside():
    async with tx():
        outer = current_session()
        async with auto_tx():
            inner = current_session()
        assert outer is inner


@pytest.mark.asyncio
async def test_concurrent_branches_each_in_own_tx():
    """gather called from outside tx — each branch gets its own session."""
    async def branch(label: str) -> str:
        async with tx():
            s = current_session()
            r = await s.execute(text("SELECT :v"), {"v": label})
            return r.scalar_one()

    a, b = await asyncio.gather(branch("A"), branch("B"))
    assert {a, b} == {"A", "B"}
```

- [ ] **Step 2: 跑测试确认 fail**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_runtime_db.py -v
```

Expected: 全部 fail，`ImportError: cannot import name 'tx' from 'app.runtime.db'`

- [ ] **Step 3: 实现 `runtime/db.py`**

```python
"""DB session capability — Phase 7d Gap 13.

业务永远不直接拿 session。三个对外 API：

  - ``async with tx():``  — 表达「这几行原子」
  - ``await emit_tx(data)``  — 在 tx 内追加 outbox row（强制：tx 外调用 raise）
  - ``current_session()``  — query 函数内部用；业务区禁止 import

session 走 contextvar。**AsyncSession 单 session 单 connection 不支持并发使用，
所以同一 tx 内 DB 操作只能顺序 await — 在 tx 内塞 ``asyncio.gather`` 跑多条
query 没有并发收益（最终被 SQLAlchemy 锁串成顺序），并且会触发
``InvalidRequestError``。** 想并发查就让每个分支自己进独立 tx，**前提是调用方
自身不在外层 tx 里**（ContextVar 继承会让嵌套 tx 走 SAVEPOINT 复用外层 session）。
"""
from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.data.session import get_session as _get_session_internal
from app.runtime.data import Data
from app.runtime.outbox import OutboxEmitter

logger = logging.getLogger(__name__)

_session_var: ContextVar[AsyncSession | None] = ContextVar("_session", default=None)

_TX_SLOW_THRESHOLD_S = 5.0


def current_session() -> AsyncSession:
    """Return the AsyncSession bound to the current tx context.

    Caller MUST be inside ``async with tx():`` (or auto_tx fallback).
    Raises ``RuntimeError`` otherwise.
    """
    s = _session_var.get()
    if s is None:
        raise RuntimeError(
            "current_session() called outside tx() — wrap your DB calls in "
            "`async with tx():` or rely on a query function's auto_tx fallback"
        )
    return s


@asynccontextmanager
async def tx() -> AsyncIterator[None]:
    """Open or reuse a DB transaction. Nestable via SAVEPOINT.

    - Outermost call: opens fresh AsyncSession + transaction; commits on
      exit, rolls back on exception.
    - Nested call: opens a SAVEPOINT inside the existing session; inner
      raise rolls back inner only when caller catches the exception
      outside the nested with.
    """
    existing = _session_var.get()
    if existing is not None:
        async with existing.begin_nested():
            yield
        return

    started = time.monotonic()
    async with _get_session_internal() as s:
        token = _session_var.set(s)
        try:
            yield
        finally:
            _session_var.reset(token)
            elapsed = time.monotonic() - started
            if elapsed > _TX_SLOW_THRESHOLD_S:
                logger.warning(
                    "tx() held for %.2fs (threshold=%.1fs) — review for "
                    "external IO inside tx block",
                    elapsed, _TX_SLOW_THRESHOLD_S,
                )


async def emit_tx(data: Data) -> None:
    """Append an outbox row in the current tx. Raises if not in a tx.

    Why strict: outbox MUST commit atomically with business writes.
    Allowing ``emit_tx`` outside tx would let the row sneak into a
    one-shot transaction that doesn't include the caller's business
    writes — exactly the bug Gap 8 outbox closed.
    """
    s = current_session()
    emitter = OutboxEmitter(s)
    await emitter.append(data)


@asynccontextmanager
async def auto_tx() -> AsyncIterator[None]:
    """Internal helper for query functions: if not in tx, open one for
    this single call. Allows business code to call a single query
    (e.g. ``await find_persona(pid)``) without an explicit ``with tx():``
    while still working inside an explicit tx block.
    """
    if _session_var.get() is not None:
        yield
        return
    async with tx():
        yield
```

- [ ] **Step 4: 跑测试确认 pass**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_runtime_db.py -v
```

Expected: 8 passed

- [ ] **Step 5: ruff 通过**

```bash
cd apps/agent-service && uv run ruff check app/runtime/db.py tests/unit/test_runtime_db.py
```

- [ ] **Step 6: commit**

```bash
git add apps/agent-service/app/runtime/db.py apps/agent-service/tests/unit/test_runtime_db.py
git commit -m "feat(runtime): db capability — tx() / current_session() / emit_tx()"
```

---

## Task 3 — `data/queries/*.py` 删 session 参数（mechanical）

**Files modified (9 个 query 模块):**
- `apps/agent-service/app/data/queries/agent_response.py`
- `apps/agent-service/app/data/queries/life.py`
- `apps/agent-service/app/data/queries/memory.py`
- `apps/agent-service/app/data/queries/memory_edges.py`
- `apps/agent-service/app/data/queries/memory_search.py`
- `apps/agent-service/app/data/queries/messages.py`
- `apps/agent-service/app/data/queries/model_provider.py`
- `apps/agent-service/app/data/queries/persona.py`
- `apps/agent-service/app/data/queries/schedule.py`

**Pattern**：每个函数

```python
# Before
async def find_persona(session: AsyncSession, persona_id: str) -> BotPersona | None:
    result = await session.execute(text("SELECT ... WHERE id=:id"), {"id": persona_id})
    return ...

# After
from app.runtime.db import auto_tx, current_session

async def find_persona(persona_id: str) -> BotPersona | None:
    async with auto_tx():
        result = await current_session().execute(
            text("SELECT ... WHERE id=:id"), {"id": persona_id}
        )
        return ...
```

- [ ] **Step 1: 改一个文件做样本：`data/queries/persona.py`**

按 pattern 改全部 6 个函数（`find_persona / list_all_persona_ids / resolve_persona_id / resolve_bot_name_for_persona / resolve_mentioned_personas / find_bot_names_for_persona`）：
1. 顶部 import 改成 `from app.runtime.db import auto_tx, current_session`，删 `from sqlalchemy.ext.asyncio import AsyncSession`
2. 每个 `async def fn(session: AsyncSession, ...)` 删第一参数
3. 函数体在原最外层 `await session.execute(...)` 之前包 `async with auto_tx():`，把 `session.` 改成 `current_session().`

- [ ] **Step 2: 写 quick test — 老调用方还能通过（这一步先不通过，下一 commit 改调用方）**

跑现有 query 单测查它们目前依赖什么：

```bash
cd apps/agent-service && uv run pytest tests/unit/test_data_queries_persona.py -v 2>/dev/null || echo "no existing test"
```

如果有现有测试，会因为签名改变 fail —— 这是预期的，下一 commit 修复。

- [ ] **Step 3: 同样 pattern 改剩余 8 个文件**

按真实文件用 grep 列出所有 query 函数：

```bash
for f in apps/agent-service/app/data/queries/*.py; do
  echo "--- $(basename $f) ---"
  grep -E "^async def" "$f"
done
```

每个函数走 Step 1 的 pattern。

- [ ] **Step 4: 验证：业务区调用方编译失败（预期，下一 commit 修）**

```bash
cd apps/agent-service && uv run ruff check app/data/queries/
```

ruff 应该过；mypy/import 错误会出现在调用方但本 commit 不修。

- [ ] **Step 5: 写一个 query 函数的 unit test 验证 auto_tx 兜底**

`apps/agent-service/tests/unit/test_data_queries_persona.py`（如不存在则新建）：

```python
import pytest
from app.data.queries import find_persona


@pytest.mark.asyncio
async def test_find_persona_works_without_explicit_tx():
    """auto_tx fallback opens a one-shot tx when caller is outside tx."""
    # 假设测试 fixture 已经准备了一个 persona
    result = await find_persona("test_persona_id")
    # 不存在或存在都可以，关键是不抛 RuntimeError("outside tx")
    assert result is None or result.id == "test_persona_id"
```

- [ ] **Step 6: 跑这个 test 确认 auto_tx 兜底正常**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_data_queries_persona.py -v
```

- [ ] **Step 7: commit**

```bash
git add apps/agent-service/app/data/queries/ apps/agent-service/tests/unit/test_data_queries_persona.py
git commit -m "refactor(data/queries): drop session arg from 9 query modules

70+ query functions now use current_session() via auto_tx fallback.
Business callers still pass session in this commit — fixed in next."
```

注意：本 commit 完成后，业务侧 `await find_persona(s, pid)` 会编译错（多了个参数）。下一 commit 立即修。**两 commit 之间不要 push**——保持本地连续。

---

## Task 4 — 业务侧切换：`get_session` / `transactional_emit` → `tx` / `emit_tx`

**Files modified (38 业务文件):**

完整列表来自 spec §2.4：

```
agent/tools/{commit_abstract,history,notes,update_schedule}.py
agent/models.py
chat/{persona_filter,agent_stream,_context_images}.py
life/{glimpse,proactive,schedule,engine,state_sync,tool}.py
memory/{conflict,context,cross_chat,_persona,recall_engine,vectorize_memory,voice,_timeline}.py
memory/sections/{active_notes,recall_index,schedule,self_abstracts,short_term_fragments,user_abstracts}.py
memory/reviewer/{heavy,light,tools}.py
nodes/{admin,chat_node,hydrate_message,life_dataflow,memory_pipelines,safety,sync_life_state,vectorize}.py
```

**Pattern A — `get_session` + 多个 query**：

```python
# Before
from app.data.session import get_session

async def some_node(req):
    async with get_session() as s:
        result = await find_xxx(s, ...)
        await insert_yyy(s, ...)

# After
from app.runtime.db import tx

async def some_node(req):
    async with tx():
        result = await find_xxx(...)         # query 自管 session
        await insert_yyy(...)
```

**Pattern B — `get_session` + `transactional_emit`**：

```python
# Before
from app.data.session import get_session
from app.runtime import transactional_emit

async def commit_abstract(...):
    async with get_session() as s:
        await insert_abstract_memory(s, ...)
        async with transactional_emit(s) as emitter:
            await emitter.append(SomeData(...))

# After
from app.runtime.db import tx, emit_tx

async def commit_abstract(...):
    async with tx():
        await insert_abstract_memory(...)
        await emit_tx(SomeData(...))
```

**Pattern C — 只读单条 query**：

```python
# Before
async def get_something(...):
    async with get_session() as s:
        return await find_xxx(s, ...)

# After
async def get_something(...):
    return await find_xxx(...)               # auto_tx 兜底
```

- [ ] **Step 1: 改一个文件作为样本：`agent/tools/commit_abstract.py`**

按 Pattern B 改：
1. 删 `from app.data.session import get_session`
2. 删 `from app.runtime import transactional_emit`
3. 加 `from app.runtime.db import tx, emit_tx`
4. `async with get_session() as s:` → `async with tx():`
5. 所有 `await fn(s, ...)` → `await fn(...)`
6. `async with transactional_emit(s) as emitter: await emitter.append(d)` → `await emit_tx(d)`

- [ ] **Step 2: 跑这个文件相关的测试**

```bash
cd apps/agent-service && uv run pytest tests/ -k "commit_abstract" -v
```

- [ ] **Step 3: 按业务域分组改剩余文件，每组一组提交内审视**

建议分组顺序（按依赖少 → 多）：
1. `chat/` 3 文件
2. `agent/tools/` + `agent/models.py` 5 文件
3. `memory/` 顶层 8 文件
4. `memory/sections/` 6 文件 + `memory/reviewer/` 3 文件
5. `life/` 6 文件
6. `nodes/` 8 文件

每组改完跑：

```bash
cd apps/agent-service && uv run pytest tests/ -k "<group_keyword>" -v
```

- [ ] **Step 4: 全量编译 + lint 检查**

```bash
cd apps/agent-service && uv run ruff check app/
cd apps/agent-service && uv run python -c "import app.main"  # import 时 catch syntax error
```

- [ ] **Step 5: 跑全量 unit tests**

```bash
cd apps/agent-service && uv run pytest tests/unit/ -v
```

- [ ] **Step 6: grep 验证业务区 `get_session(` 命中下降**

```bash
grep -rn "get_session(" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/ | wc -l
```

Expected: 0（不含 `chat/quick_search.py` / `nodes/persist_tos_files.py` —— 它们用 `async_session()` 不是 `get_session()`，下一 commit 处理）

- [ ] **Step 7: grep 验证 `transactional_emit` 业务区清零**

```bash
grep -rn "transactional_emit" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/ | wc -l
```

Expected: 0

- [ ] **Step 8: commit**

```bash
git add apps/agent-service/app/{nodes,agent,chat,life,memory}/
git commit -m "refactor(business): switch get_session/transactional_emit to tx/emit_tx

38 files. Mechanical: get_session() → tx(), transactional_emit(s) → emit_tx().
Query functions no longer take session arg."
```

---

## Task 5 — ORM-style 业务区改造（`async_session` + `session.execute/.add/.commit`）

**Files:**
- Modify: `apps/agent-service/app/chat/quick_search.py`（line 65, 89, 125）
- Modify: `apps/agent-service/app/nodes/persist_tos_files.py`（line 49, 60）
- Modify: `apps/agent-service/app/life/proactive.py`（line 51, 145, 197）
- Modify: `apps/agent-service/app/data/queries/messages.py`（加新 query 函数）
- Modify: `apps/agent-service/app/data/queries/life.py`（加新 query 函数；如改的是 life 数据）

**目标**：把这 3 个文件里的 inline SQL 抽到 `data/queries/*.py` 加新 query 函数；业务侧改用 query 函数 + `tx()`。

- [ ] **Step 1: 读 `chat/quick_search.py:60-130` 看具体在做啥**

```bash
sed -n '60,135p' apps/agent-service/app/chat/quick_search.py
```

根据它跑的 SQL 决定加什么 query：可能是 `find_messages_with_anchors_and_context` 之类。命名按现有 `data/queries/messages.py` 风格。

- [ ] **Step 2: 在 `data/queries/messages.py` 加新 query 函数**

```python
# 顶部已有
from app.runtime.db import auto_tx, current_session

# 新增（具体函数名 + SQL 视 quick_search.py 实际逻辑而定）
async def quick_search_messages(
    *, message_id: str, limit: int, ...
) -> list[MessageRow]:
    async with auto_tx():
        s = current_session()
        # 1. root query
        root = await s.execute(text("SELECT ... root SQL"), {...})
        # 2. additional context query
        additional = await s.execute(text("SELECT ... additional SQL"), {...})
        # 合并 + 返回
        ...
```

- [ ] **Step 3: 改 `chat/quick_search.py`**

```python
# Before
from app.data.session import async_session
async def quick_search(message_id, limit):
    async with async_session() as session:
        root_result = await session.execute(...)
        additional_result = await session.execute(...)
        ...

# After
from app.data.queries import quick_search_messages
async def quick_search(message_id: str, limit: int):
    return await quick_search_messages(message_id=message_id, limit=limit, ...)
```

- [ ] **Step 4: 同 pattern 改 `nodes/persist_tos_files.py`**

读 line 45-65 看在做啥（应该是写 TOS 文件元数据），加 `data/queries/messages.py` 的 `insert_tos_file_records(...)` 之类，业务侧调用即可。

- [ ] **Step 5: 同 pattern 改 `life/proactive.py`**

`life/proactive.py:51 / 145 / 197` 三处。读真实代码，加 `data/queries/life.py` 或 `messages.py` 对应 query：
- line 51: SELECT
- line 145: `session.add(msg)` ← INSERT 到 messages 表，加 `insert_proactive_message(...)`
- line 197: SELECT

- [ ] **Step 6: 跑相关测试**

```bash
cd apps/agent-service && uv run pytest tests/unit/ -k "quick_search or persist_tos or proactive" -v
```

- [ ] **Step 7: grep 验证业务区 `async_session(` / `session.execute|.commit|.add|.merge|.flush` 全部清零**

```bash
grep -rn "async_session(\|session\.execute\|session\.commit\|session\.add\|session\.merge\|session\.flush" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/ | wc -l
```

Expected: 0

- [ ] **Step 8: commit**

```bash
git add apps/agent-service/app/{chat,nodes,life}/ apps/agent-service/app/data/queries/
git commit -m "refactor(business): replace async_session/direct session.* in 3 files

Hoist inline SQL from quick_search.py / persist_tos_files.py / proactive.py
into data/queries/* domain functions; business uses tx() + query API only."
```

---

## Task 6 — `runtime/__init__.py` 公共 surface 调整

**Files:**
- Modify: `apps/agent-service/app/runtime/__init__.py`

- [ ] **Step 1: 改 import + `__all__`**

```python
# Before
from app.runtime.outbox import transactional_emit

__all__ = [..., "transactional_emit", ...]

# After
from app.runtime.db import emit_tx, tx
# transactional_emit 仍在 outbox.py，但不 re-export

__all__ = [..., "emit_tx", "tx", ...]
```

具体：

```python
from app.runtime.data import AdminOnly, Data, DedupKey, Key, Version
from app.runtime.db import emit_tx, tx
from app.runtime.emit import emit, emit_at, emit_delayed
from app.runtime.errors import DuplicateData, NeedsReview
from app.runtime.node import node
from app.runtime.placement import bind
from app.runtime.query import query
from app.runtime.sink import Sink
from app.runtime.source import Source
from app.runtime.wire import wire

__all__ = [
    "AdminOnly",
    "Data",
    "DedupKey",
    "DuplicateData",
    "Key",
    "NeedsReview",
    "Version",
    "Sink",
    "Source",
    "bind",
    "emit",
    "emit_at",
    "emit_delayed",
    "emit_tx",
    "node",
    "query",
    "tx",
    "wire",
]
```

- [ ] **Step 2: 全量编译验证**

```bash
cd apps/agent-service && uv run python -c "from app.runtime import tx, emit_tx; print('OK')"
```

- [ ] **Step 3: grep 验证业务区不再 import `transactional_emit`**

```bash
grep -rn "transactional_emit" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/
```

Expected: 0 命中

- [ ] **Step 4: grep 验证业务区不直接 import `app.runtime.outbox`**

```bash
grep -rn "from app\.runtime\.outbox import" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/
```

Expected: 0 命中

- [ ] **Step 5: 跑全量 unit tests**

```bash
cd apps/agent-service && uv run pytest tests/unit/ -v
```

- [ ] **Step 6: commit**

```bash
git add apps/agent-service/app/runtime/__init__.py
git commit -m "chore(runtime): swap public surface — drop transactional_emit, export tx + emit_tx

OutboxEmitter / transactional_emit remain in app/runtime/outbox.py for
framework-internal use (e.g. emit_tx() builds an OutboxEmitter inside
runtime/db.py); business code is now blocked from importing them."
```

---

## Task 7 — `runtime/single_flight.py`

**Files:**
- Create: `apps/agent-service/app/runtime/single_flight.py`
- Test: `apps/agent-service/tests/unit/test_runtime_single_flight.py`

- [ ] **Step 1: 写测试**

`apps/agent-service/tests/unit/test_runtime_single_flight.py`：

```python
"""Tests for runtime.single_flight (Gap 14)."""
from __future__ import annotations

import asyncio
import pytest

from app.runtime.single_flight import single_flight, SingleFlightConflict


@pytest.mark.asyncio
async def test_acquire_releases_on_exit():
    async with single_flight("test:sf:basic", ttl=10):
        pass
    # 离开后能再次拿到
    async with single_flight("test:sf:basic", ttl=10):
        pass


@pytest.mark.asyncio
async def test_concurrent_acquire_raises():
    async def hold(latch: asyncio.Event):
        async with single_flight("test:sf:contend", ttl=10):
            latch.set()
            await asyncio.sleep(0.5)

    latch = asyncio.Event()
    holder = asyncio.create_task(hold(latch))
    await latch.wait()

    with pytest.raises(SingleFlightConflict, match="test:sf:contend"):
        async with single_flight("test:sf:contend", ttl=10):
            pass

    await holder


@pytest.mark.asyncio
async def test_token_compare_prevents_misdelete():
    """Slow holder past TTL doesn't delete a new holder's lock."""
    # 用极短 TTL 模拟过期
    async def slow():
        async with single_flight("test:sf:ttl", ttl=1):
            await asyncio.sleep(2)  # 超过 TTL

    holder = asyncio.create_task(slow())
    await asyncio.sleep(1.2)  # 等 TTL 到期

    # 现在新 holder 能进
    async with single_flight("test:sf:ttl", ttl=10):
        # 在 holder finally 之前，这把锁是新 token
        await asyncio.sleep(1)

    # holder 退出（finally Lua DEL 比较 token 失败，不会删新锁）
    await holder
    # 验证：新 holder 释放后能再拿（说明 holder 没误删）
    async with single_flight("test:sf:ttl", ttl=10):
        pass
```

- [ ] **Step 2: 跑测试确认 fail**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_runtime_single_flight.py -v
```

Expected: ImportError

- [ ] **Step 3: 实现 `runtime/single_flight.py`**

```python
"""Single-flight lock — Phase 7d Gap 14.

Idiom:
    async with single_flight(f"drift:{chat}:{persona}", ttl=600):
        await _do_work()

**语义**：ttl 时间窗内 single-flight，**不是任务存活期严格互斥**。
- 进入：SETNX + uuid token，已被持有 → raise SingleFlightConflict
- 离开：Lua 比较 token 后 DEL（防误删别人持有的锁）
- TTL 到期前：保护 single-flight
- TTL 到期后：哪怕原 holder 还在跑，新 holder 可以进入；原 holder finally 时
  Lua 比较 token 失败、不会误删新 holder 的锁

调用方负责选择「比业务最坏耗时更大的 ttl」。
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.infra.redis import get_redis

_RELEASE_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""


class SingleFlightConflict(Exception):
    """Raised when another holder already owns the key."""

    def __init__(self, key: str) -> None:
        super().__init__(f"single-flight conflict on key={key!r}")
        self.key = key


@asynccontextmanager
async def single_flight(key: str, *, ttl: int) -> AsyncIterator[None]:
    """Acquire single-flight lock; raise SingleFlightConflict if held."""
    redis = await get_redis()
    token = uuid.uuid4().hex
    if not await redis.set(key, token, nx=True, ex=ttl):
        raise SingleFlightConflict(key)
    try:
        yield
    finally:
        await redis.eval(_RELEASE_LUA, 1, key, token)
```

- [ ] **Step 4: 跑测试确认 pass**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_runtime_single_flight.py -v
```

Expected: 3 passed

- [ ] **Step 5: ruff + commit**

```bash
cd apps/agent-service && uv run ruff check app/runtime/single_flight.py tests/unit/test_runtime_single_flight.py
git add apps/agent-service/app/runtime/single_flight.py apps/agent-service/tests/unit/test_runtime_single_flight.py
git commit -m "feat(runtime): single_flight capability"
```

---

## Task 8 — `capabilities/banned_words.py`

**Files:**
- Create: `apps/agent-service/app/capabilities/banned_words.py`
- Test: `apps/agent-service/tests/unit/test_capabilities_banned_words.py`

- [ ] **Step 1: 写测试**

```python
"""Tests for capabilities.banned_words (Gap 14)."""
from __future__ import annotations

import pytest
from app.capabilities import banned_words
from app.infra.redis import get_redis


@pytest.fixture
async def populated_set():
    redis = await get_redis()
    await redis.sadd("banned_words", "badword1", "另一个屏蔽词")
    yield
    await redis.delete("banned_words")


@pytest.mark.asyncio
async def test_contains_clean_text(populated_set):
    assert await banned_words.contains("hello world") is None


@pytest.mark.asyncio
async def test_contains_hit(populated_set):
    assert await banned_words.contains("this contains badword1 inside") == "badword1"


@pytest.mark.asyncio
async def test_contains_chinese_normalization(populated_set):
    assert await banned_words.contains("中间有 另一个屏蔽词 的句子") == "另一个屏蔽词"


@pytest.mark.asyncio
async def test_contains_empty_set():
    redis = await get_redis()
    await redis.delete("banned_words")
    assert await banned_words.contains("anything") is None
```

- [ ] **Step 2: 实现 `capabilities/banned_words.py`**

```python
"""Banned-words set — Phase 7d Gap 14."""
from __future__ import annotations

from app.infra.redis import get_redis

_KEY = "banned_words"


async def contains(text: str) -> str | None:
    """Return the matched banned word, or None if clean.

    Matches case-insensitively after stripping whitespace.
    """
    redis = await get_redis()
    words = await redis.smembers(_KEY)
    if not words:
        return None
    normalized = text.replace(" ", "").lower()
    for w in words:
        if isinstance(w, bytes):
            w = w.decode("utf-8")
        if w.lower() in normalized or w in text:
            return w
    return None
```

- [ ] **Step 3: 跑测试 + commit**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_capabilities_banned_words.py -v
cd apps/agent-service && uv run ruff check app/capabilities/banned_words.py
git add apps/agent-service/app/capabilities/banned_words.py apps/agent-service/tests/unit/test_capabilities_banned_words.py
git commit -m "feat(capabilities): banned_words"
```

---

## Task 9 — `infra/image.py` `ImageRegistry` self-fetches redis

**Files:**
- Modify: `apps/agent-service/app/infra/image.py:250+`

- [ ] **Step 1: 读现有 `ImageRegistry` 类完整实现**

```bash
sed -n '245,320p' apps/agent-service/app/infra/image.py
```

- [ ] **Step 2: 改构造签名 + 内部自取 redis**

```python
# Before
class ImageRegistry:
    def __init__(self, message_id: str, redis: Redis) -> None:
        self._message_id = message_id
        self._redis = redis

    async def register(self, ...):
        ... await self._redis.hset(...) ...

# After
class ImageRegistry:
    def __init__(self, message_id: str) -> None:
        self._message_id = message_id

    async def _redis(self) -> Redis:
        from app.infra.redis import get_redis
        return await get_redis()

    async def register(self, ...):
        redis = await self._redis()
        await redis.hset(...)
```

把所有 `self._redis.xxx` 改成 `(await self._redis()).xxx`，或者方法开头 `redis = await self._redis()` 然后用 `redis.xxx`。后者更清楚，推荐。

- [ ] **Step 3: 跑现有 image 相关测试**

```bash
cd apps/agent-service && uv run pytest tests/ -k "image" -v
```

如有测试构造 `ImageRegistry(msg_id, redis)`，改成 `ImageRegistry(msg_id)`。

- [ ] **Step 4: commit**

```bash
git add apps/agent-service/app/infra/image.py apps/agent-service/tests/
git commit -m "refactor(infra/image): ImageRegistry self-fetches redis

Constructor no longer takes Redis; internal methods call get_redis() directly.
Caller no longer needs to import redis to use ImageRegistry."
```

---

## Task 10 — 业务侧切换：SETNX/Lua/SMEMBERS/ImageRegistry → capability

**Files:**
- Modify: `apps/agent-service/app/nodes/memory_pipelines.py`
- Modify: `apps/agent-service/app/nodes/safety.py`
- Modify: `apps/agent-service/app/chat/context.py`

- [ ] **Step 1: 改 `nodes/memory_pipelines.py`**

```python
# Before — 顶部 imports
import uuid
from app.infra.redis import get_redis
from app.runtime.debounce import DebounceReschedule

_LOCK_RELEASE_LUA = """..."""

# After
from app.runtime.single_flight import single_flight, SingleFlightConflict
from app.runtime.debounce import DebounceReschedule
# 删除 _LOCK_RELEASE_LUA 常量、import uuid、import get_redis
```

每个 `@node`：

```python
# Before
@node
async def drift_check(trigger: DriftTrigger) -> None:
    lock_key = f"phase2:drift:{trigger.chat_id}:{trigger.persona_id}"
    token = uuid.uuid4().hex
    redis = await get_redis()
    if not await redis.set(lock_key, token, nx=True, ex=600):
        raise DebounceReschedule(DriftTrigger(...))
    try:
        await _run_drift(trigger.chat_id, trigger.persona_id)
    finally:
        await redis.eval(_LOCK_RELEASE_LUA, 1, lock_key, token)

# After
@node
async def drift_check(trigger: DriftTrigger) -> None:
    try:
        async with single_flight(
            f"phase2:drift:{trigger.chat_id}:{trigger.persona_id}", ttl=600
        ):
            await _run_drift(trigger.chat_id, trigger.persona_id)
    except SingleFlightConflict:
        raise DebounceReschedule(DriftTrigger(
            chat_id=trigger.chat_id, persona_id=trigger.persona_id,
        ))
```

`afterthought_check` 同 pattern（ttl=900）。

- [ ] **Step 2: 改 `nodes/safety.py`**

```python
# Before — 顶部
from app.infra.redis import get_redis
_BANNED_WORDS_KEY = "banned_words"

async def _check_banned_word(text: str) -> str | None:
    redis = await get_redis()
    banned_words = await redis.smembers(_BANNED_WORDS_KEY)
    if not banned_words:
        return None
    normalized = text.replace(" ", "").lower()
    for word in banned_words:
        if word in normalized:
            return word
    return None

# After
from app.capabilities import banned_words as _banned_words_capability
# 删除 _BANNED_WORDS_KEY 常量、import get_redis

async def _check_banned_word(text: str) -> str | None:
    return await _banned_words_capability.contains(text)
```

- [ ] **Step 3: 改 `chat/context.py`**

```python
# Before — line 33-34, 127-128
from app.infra.image import ImageRegistry
from app.infra.redis import get_redis

# ... 函数体内
redis = await get_redis()
registry = ImageRegistry(message_id, redis)

# After — line 33-34, 127-128
from app.infra.image import ImageRegistry
# 删除 from app.infra.redis import get_redis

# ... 函数体内
registry = ImageRegistry(message_id)
# 删除 redis = await get_redis()
```

- [ ] **Step 4: 跑相关测试**

```bash
cd apps/agent-service && uv run pytest tests/ -k "memory_pipeline or safety or chat_context or drift or afterthought" -v
```

- [ ] **Step 5: grep 验证业务区 redis 调用清零**

```bash
grep -rnE "redis\.set\(.*nx=True\|redis\.eval\(\|redis\.smembers\(\|redis\.sadd\(" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/ | wc -l
grep -rn "from app\.infra\.redis import\|from app\.infra import redis" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/ | wc -l
```

Expected: 0 / 0

- [ ] **Step 6: commit**

```bash
git add apps/agent-service/app/{nodes,chat}/
git commit -m "refactor(business): switch SETNX/Lua/SMEMBERS/ImageRegistry to capabilities

- nodes/memory_pipelines.py: drift_check / afterthought_check use single_flight
- nodes/safety.py: _check_banned_word uses banned_words.contains
- chat/context.py: ImageRegistry no longer needs raw redis

Business code zero redis import."
```

---

## Task 11 — `capabilities/http.py` retry 策略升级 + 域 capability 模块

**Files:**
- Modify: `apps/agent-service/app/capabilities/http.py`（升级 retry）
- Create: `apps/agent-service/app/capabilities/web_search.py`
- Create: `apps/agent-service/app/capabilities/image_search.py`
- Create: `apps/agent-service/app/capabilities/sandbox.py`
- Test: `apps/agent-service/tests/unit/test_capabilities_http_retry.py`
- Test: `apps/agent-service/tests/unit/test_capabilities_sandbox.py`

- [ ] **Step 1: HTTPClient retry 策略升级**

`apps/agent-service/app/capabilities/http.py`：

```python
"""HTTPClient — lane-aware httpx adapter with method-aware retry (Phase 7d Gap 16).

Retry 决策矩阵（见 spec §4.2.1）：
- ConnectError / ConnectTimeout / DNS / TLS：所有方法 retry（请求未到达）
- 429 Too Many Requests：所有方法 retry（合规）
- ReadTimeout / WriteTimeout / 5xx：仅 GET/HEAD retry；POST 不 retry（服务端可能已执行）
- POST + idempotency_key：注入 Idempotency-Key header，按 retry_post 次数 retry，但仍仅
  对 connect 阶段错误 + 429 重试（ReadTimeout/5xx 仍不重试，让服务端自己用 key 去重）
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.api.middleware import trace_id_var
from app.infra.lane import lane_router

logger = logging.getLogger(__name__)


class HTTPClient:
    def __init__(
        self,
        service: str | None = None,
        *,
        timeout: float = 30.0,
        retries: int = 3,
        retry_post: int = 0,
        retry_backoff: float = 0.5,
        retry_on_status_get: frozenset[int] = frozenset({429, 500, 502, 503, 504}),
        retry_on_status_post: frozenset[int] = frozenset({429}),
    ) -> None:
        self._service = service
        self._client = httpx.AsyncClient(timeout=timeout)
        self._retries_get = retries
        self._retries_post = retry_post
        self._backoff = retry_backoff
        self._status_get = retry_on_status_get
        self._status_post = retry_on_status_post

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h: dict[str, str] = dict(extra) if extra else {}
        h.update(lane_router.get_headers())
        if tid := trace_id_var.get():
            h["X-Trace-Id"] = tid
        return h

    def _url(self, path: str) -> str:
        if not self._service or path.startswith(("http://", "https://")):
            return path
        return lane_router.base_url(self._service) + path

    async def get(self, path: str, **kw: Any) -> httpx.Response:
        return await self._request("GET", path, **kw)

    async def post(
        self,
        path: str,
        *,
        idempotency_key: str | None = None,
        **kw: Any,
    ) -> httpx.Response:
        if idempotency_key:
            headers = kw.pop("headers", None) or {}
            headers["Idempotency-Key"] = idempotency_key
            kw["headers"] = headers
        return await self._request("POST", path, _idempotency=bool(idempotency_key), **kw)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        _idempotency: bool = False,
        **kw: Any,
    ) -> httpx.Response:
        is_get_like = method in ("GET", "HEAD")
        retries = self._retries_get if is_get_like else (self._retries_post if _idempotency else 0)
        retry_status = self._status_get if is_get_like else self._status_post

        url = self._url(path)
        headers = self._headers(kw.pop("headers", None))

        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = await self._client.request(method, url, headers=headers, **kw)
                if resp.status_code in retry_status and attempt < retries:
                    delay = self._backoff * (2 ** attempt)
                    logger.warning(
                        "HTTP %s %s status=%d, retrying in %.2fs (%d/%d)",
                        method, url, resp.status_code, delay, attempt + 1, retries,
                    )
                    await asyncio.sleep(delay)
                    continue
                return resp
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                # 请求未到达 — 所有方法可安全重试
                last_exc = e
                if attempt >= retries:
                    raise
                delay = self._backoff * (2 ** attempt)
                logger.warning(
                    "HTTP %s %s connect error %s, retrying in %.2fs (%d/%d)",
                    method, url, type(e).__name__, delay, attempt + 1, retries,
                )
                await asyncio.sleep(delay)
            except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError) as e:
                # 请求已发出 — 仅 GET/HEAD retry，POST（即使 idempotency_key）不 retry
                if not is_get_like:
                    raise
                last_exc = e
                if attempt >= retries:
                    raise
                delay = self._backoff * (2 ** attempt)
                logger.warning(
                    "HTTP %s %s read/write timeout %s, retrying in %.2fs (%d/%d)",
                    method, url, type(e).__name__, delay, attempt + 1, retries,
                )
                await asyncio.sleep(delay)

        # 不该到这里
        if last_exc:
            raise last_exc
        raise RuntimeError("HTTPClient retry loop exited unexpectedly")

    async def close(self) -> None:
        await self._client.aclose()
```

- [ ] **Step 2: 写 retry 矩阵单测**

`apps/agent-service/tests/unit/test_capabilities_http_retry.py`：

```python
"""Tests for HTTPClient method-aware retry (Phase 7d Gap 16)."""
from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from app.capabilities.http import HTTPClient


@pytest.mark.asyncio
async def test_get_retries_on_5xx(httpx_mock: HTTPXMock):
    httpx_mock.add_response(method="GET", url="https://x/y", status_code=503)
    httpx_mock.add_response(method="GET", url="https://x/y", status_code=200, json={"ok": True})

    client = HTTPClient(retries=3, retry_backoff=0.0)
    resp = await client.get("https://x/y")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_post_does_not_retry_on_5xx(httpx_mock: HTTPXMock):
    httpx_mock.add_response(method="POST", url="https://x/y", status_code=503)

    client = HTTPClient(retries=3, retry_post=3, retry_backoff=0.0)
    # 没传 idempotency_key → retries 强制为 0
    resp = await client.post("https://x/y", json={})
    assert resp.status_code == 503  # 不重试，直接返回


@pytest.mark.asyncio
async def test_post_with_idempotency_key_still_skips_5xx_retry(httpx_mock: HTTPXMock):
    """Even with idempotency_key, POST does not retry 5xx (only connect + 429)."""
    httpx_mock.add_response(method="POST", url="https://x/y", status_code=502)

    client = HTTPClient(retries=3, retry_post=3, retry_backoff=0.0)
    resp = await client.post("https://x/y", idempotency_key="k1", json={})
    assert resp.status_code == 502  # 不重试


@pytest.mark.asyncio
async def test_post_with_idempotency_key_retries_429(httpx_mock: HTTPXMock):
    httpx_mock.add_response(method="POST", url="https://x/y", status_code=429)
    httpx_mock.add_response(method="POST", url="https://x/y", status_code=200, json={"ok": True})

    client = HTTPClient(retries=3, retry_post=3, retry_backoff=0.0)
    resp = await client.post("https://x/y", idempotency_key="k1", json={})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_get_retries_on_read_timeout(httpx_mock: HTTPXMock):
    httpx_mock.add_exception(httpx.ReadTimeout("timeout"))
    httpx_mock.add_response(method="GET", url="https://x/y", status_code=200, json={"ok": True})

    client = HTTPClient(retries=3, retry_backoff=0.0)
    resp = await client.get("https://x/y")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_post_does_not_retry_read_timeout(httpx_mock: HTTPXMock):
    httpx_mock.add_exception(httpx.ReadTimeout("timeout"))

    client = HTTPClient(retries=3, retry_post=3, retry_backoff=0.0)
    with pytest.raises(httpx.ReadTimeout):
        await client.post("https://x/y", idempotency_key="k1", json={})


@pytest.mark.asyncio
async def test_connect_error_retries_for_post(httpx_mock: HTTPXMock):
    """ConnectError = request never reached server, safe to retry POST."""
    httpx_mock.add_exception(httpx.ConnectError("dns"))
    httpx_mock.add_response(method="POST", url="https://x/y", status_code=200, json={"ok": True})

    client = HTTPClient(retries=3, retry_post=3, retry_backoff=0.0)
    resp = await client.post("https://x/y", idempotency_key="k1", json={})
    assert resp.status_code == 200
```

- [ ] **Step 3: 跑 HTTPClient 测试**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_capabilities_http_retry.py -v
```

- [ ] **Step 4: 实现 `capabilities/web_search.py`**

读 `agent/tools/search.py` 现有逻辑（_you_search / _google_search / _read_webpage / _rerank）→ 抽 capability：

```python
"""Web search + read webpage + rerank — Phase 7d Gap 16."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.capabilities.http import HTTPClient
from app.infra.config import settings

logger = logging.getLogger(__name__)


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str


_GET_CLIENT = HTTPClient(timeout=10.0)            # 默认 GET retry
_POST_CLIENT = HTTPClient(timeout=15.0)           # POST retry_post=0


async def web_search(query: str, *, count: int = 10) -> list[SearchHit]:
    """Route to You Search / Google CSE based on settings."""
    if settings.you_search_host and settings.you_search_api_key:
        return await _you_search(query, count)
    if settings.google_search_host and settings.google_search_api_key:
        return await _google_search(query, count)
    return []


async def _you_search(query: str, count: int) -> list[SearchHit]:
    url = f"{settings.you_search_host}/v1/search"
    headers = {"X-API-Key": settings.you_search_api_key}
    params = {"query": query, "num_web_results": count, "country": "CN", "safesearch": "off"}
    resp = await _GET_CLIENT.get(url, params=params, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return [
        SearchHit(
            title=r.get("title", ""),
            url=r.get("url", ""),
            snippet=r.get("description", ""),
        )
        for r in data.get("results", {}).get("web", [])
    ]


async def _google_search(query: str, count: int) -> list[SearchHit]:
    params = {
        "q": query,
        "num": count,
        "key": settings.google_search_api_key,
        "cx": settings.google_search_cx,
    }
    resp = await _GET_CLIENT.get(settings.google_search_host, params=params)
    resp.raise_for_status()
    data = resp.json()
    return [
        SearchHit(
            title=item.get("title", ""),
            url=item.get("link", ""),
            snippet=item.get("snippet", ""),
        )
        for item in data.get("items", [])
    ]


async def read_webpage(url: str) -> str:
    """Fetch + html→markdown via You Contents API."""
    if not settings.you_search_host or not settings.you_search_api_key:
        return ""
    api_url = f"{settings.you_search_host}/v1/contents"
    headers = {
        "X-API-Key": settings.you_search_api_key,
        "Content-Type": "application/json",
    }
    payload = {"urls": [url], "formats": ["markdown", "html"]}
    resp = await _POST_CLIENT.post(api_url, headers=headers, json=payload)
    resp.raise_for_status()
    data = resp.json()
    contents = data.get("contents") or data.get("results") or []
    if not contents:
        return ""
    item = contents[0]
    return item.get("markdown") or item.get("html") or ""


async def rerank(
    query: str, docs: list[str], *, top_k: int = 5
) -> list[tuple[int, float]]:
    """Rerank docs against query via SiliconFlow API; return [(idx, score), ...]."""
    if not settings.rerank_host or not settings.rerank_api_key:
        # 无配置 → 返回原顺序前 top_k
        return [(i, 1.0) for i in range(min(top_k, len(docs)))]
    headers = {
        "Authorization": f"Bearer {settings.rerank_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "Qwen/Qwen3-Reranker-4B",
        "query": query,
        "documents": docs,
        "top_n": top_k,
    }
    resp = await _POST_CLIENT.post(
        f"{settings.rerank_host}/v1/rerank", headers=headers, json=payload
    )
    resp.raise_for_status()
    data = resp.json()
    return [
        (item["index"], item.get("relevance_score", 0))
        for item in data.get("results", [])
    ]
```

- [ ] **Step 5: 实现 `capabilities/image_search.py`**

读 `agent/tools/image_search.py:55-110` 看真实形态，抽：

```python
"""Image search — Phase 7d Gap 16."""
from __future__ import annotations

from dataclasses import dataclass

from app.capabilities.http import HTTPClient
from app.infra.config import settings

_CLIENT = HTTPClient(timeout=10.0)


@dataclass
class ImageHit:
    image_url: str
    title: str
    source_url: str


async def image_search(query: str, *, count: int = 5) -> list[ImageHit]:
    if not settings.you_search_host or not settings.you_search_api_key:
        return []
    url = f"{settings.you_search_host}/v1/images"
    headers = {"X-API-Key": settings.you_search_api_key}
    params = {"query": query, "num": count, "country": "CN", "safesearch": "off"}
    resp = await _CLIENT.get(url, params=params, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    raw = data.get("images", [])
    if isinstance(raw, dict):
        raw = raw.get("results", [])
    return [
        ImageHit(
            image_url=img.get("image_url") or img.get("url", ""),
            title=img.get("title", ""),
            source_url=img.get("source_url", ""),
        )
        for img in raw
        if img.get("image_url") or img.get("url")
    ]
```

- [ ] **Step 6: 实现 `capabilities/sandbox.py`**

```python
"""Sandbox skill execution — Phase 7d Gap 16.

Calls sandbox-worker /exec endpoint. Non-idempotent: retries=0.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.capabilities.http import HTTPClient
from app.infra.config import settings

# service="sandbox-worker" → LaneRouter resolves lane; trace + lane headers auto-injected
# retries=0 + retry_post=0: /exec is non-idempotent (executes a bash command)
_CLIENT = HTTPClient(service="sandbox-worker", timeout=45.0)


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str


async def run(
    *,
    command: str,
    skill_name: str = "",
    envs: dict[str, str] | None = None,
    timeout: int = 30,
) -> SandboxResult:
    """Execute command in sandbox; non-idempotent, no retry on failure."""
    headers: dict[str, str] = {}
    if settings.inner_http_secret:
        headers["Authorization"] = f"Bearer {settings.inner_http_secret}"
    resp = await _CLIENT.post(
        "/exec",
        json={
            "command": command,
            "skill_name": skill_name,
            "envs": envs or {},
            "timeout_sec": timeout,
        },
        headers=headers,
        # 不传 idempotency_key — POST 不重试
    )
    resp.raise_for_status()
    data = resp.json()
    return SandboxResult(
        exit_code=data["exit_code"],
        stdout=data["stdout"],
        stderr=data["stderr"],
    )
```

- [ ] **Step 7: 写 sandbox capability 单测**

`apps/agent-service/tests/unit/test_capabilities_sandbox.py`：

```python
"""Tests for capabilities.sandbox (Phase 7d Gap 16)."""
from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from app.capabilities.sandbox import run, SandboxResult


@pytest.mark.asyncio
async def test_run_returns_structured_result(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        method="POST",
        json={"exit_code": 0, "stdout": "hello\n", "stderr": ""},
    )
    result = await run(command="echo hello")
    assert isinstance(result, SandboxResult)
    assert result.exit_code == 0
    assert result.stdout == "hello\n"


@pytest.mark.asyncio
async def test_run_does_not_retry_on_500(httpx_mock: HTTPXMock):
    httpx_mock.add_response(method="POST", status_code=500)
    with pytest.raises(Exception):  # raise_for_status
        await run(command="anything")
    # 验证：只调用了 1 次（无 retry）
    assert len(httpx_mock.get_requests()) == 1
```

- [ ] **Step 8: 跑全部 capability 测试**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_capabilities_http_retry.py tests/unit/test_capabilities_sandbox.py -v
```

- [ ] **Step 9: ruff + commit**

```bash
cd apps/agent-service && uv run ruff check app/capabilities/
git add apps/agent-service/app/capabilities/ apps/agent-service/tests/unit/test_capabilities_http_retry.py apps/agent-service/tests/unit/test_capabilities_sandbox.py
git commit -m "feat(capabilities): http method-aware retry + web_search / image_search / sandbox

HTTPClient: retry by error class (connect/timeout/status) and method.
POST defaults retries=0; idempotency_key opts in but only retries connect+429.
New domain capabilities: web_search / read_webpage / rerank / image_search / sandbox.run."
```

---

## Task 12 — 业务侧切换 + 删 `skills/sandbox_client.py`

**Files:**
- Modify: `apps/agent-service/app/agent/tools/search.py`
- Modify: `apps/agent-service/app/agent/tools/image_search.py`
- Modify: `apps/agent-service/app/agent/tools/sandbox.py`
- Modify: `apps/agent-service/app/agent/image_gen.py`
- Modify: `apps/agent-service/app/skills/renderer.py`
- Delete: `apps/agent-service/app/skills/sandbox_client.py`
- Modify: `apps/agent-service/tests/unit/test_skill_renderer.py`

- [ ] **Step 1: 改 `agent/tools/search.py`**

```python
# Before — 顶部
import httpx
from app.infra.config import settings

# 函数体内自己 httpx.AsyncClient + 拼 url + headers + retry

# After — 顶部
from app.capabilities.web_search import web_search, read_webpage, rerank, SearchHit
from app.infra.config import settings

# 删除：所有 _you_search / _google_search / _brave_search / _read_webpage / _rerank inline
# 业务 tool 函数直接调 capability
```

具体保留 `@tool` 装饰函数（如 `web_search_tool`），内部调 `web_search(query, count)`：

```python
@tool
@tool_error("搜索失败")
async def web_search_tool(query: str, num: int = 10) -> list[dict]:
    hits = await web_search(query, count=num)
    return [{"title": h.title, "link": h.url, "snippet": h.snippet} for h in hits]
```

`read_webpage_tool` / `rerank_tool` 同 pattern。

- [ ] **Step 2: 改 `agent/tools/image_search.py`**

```python
# Before — 顶部
import httpx

# After
from app.capabilities.image_search import image_search

@tool
@tool_error("图片搜索失败")
async def search_images(query: str) -> list[dict]:
    hits = await image_search(query, count=IMAGE_MAX_RESULTS)
    # 后续 upload_and_register 逻辑保留
    ...
```

- [ ] **Step 3: 改 `agent/tools/sandbox.py`**

```python
# Before
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from app.skills.sandbox_client import SandboxClient

_sandbox_client: SandboxClient | None = None

def _get_sandbox_client() -> SandboxClient: ...

@tool
@tool_error("沙箱执行失败")
async def sandbox_bash(command: str) -> str:
    client = _get_sandbox_client()
    result = await client.execute(command)
    ...

# After
from app.capabilities.sandbox import run as _sandbox_run
# 删除 _sandbox_client / _get_sandbox_client / TYPE_CHECKING block

# 保留 module-level reference 让 test patch
run = _sandbox_run

@tool
@tool_error("沙箱执行失败")
async def sandbox_bash(command: str) -> str:
    result = await run(command=command)
    if result.exit_code != 0:
        return (
            f"命令退出码 {result.exit_code}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result.stdout
```

- [ ] **Step 4: 改 `agent/image_gen.py:115` 删 inline httpx**

```bash
sed -n '110,125p' apps/agent-service/app/agent/image_gen.py
```

把 `import httpx` 改成模块顶层 `from app.capabilities.http import HTTPClient`，复用一个 `_CLIENT = HTTPClient()` 实例。

- [ ] **Step 5: 改 `skills/renderer.py`**

```python
# Before
if TYPE_CHECKING:
    from app.skills.sandbox_client import SandboxClient

sandbox_client: SandboxClient | None = None

def _get_sandbox_client() -> SandboxClient:
    global sandbox_client
    if sandbox_client is None:
        from app.skills.sandbox_client import sandbox_client as _client
        sandbox_client = _client
    return sandbox_client

# inside run_directive
client = _get_sandbox_client()
result = await client.execute(directive.command, skill.name)

# After
from app.capabilities.sandbox import run as _sandbox_run
# 保留 module-level 让 test patch（虽然 test 现在 patch capabilities.sandbox.run）
run = _sandbox_run

# inside run_directive
result = await run(command=directive.command, skill_name=skill.name)
output = (
    f"命令退出码 {result.exit_code}\n"
    f"stdout:\n{result.stdout}\n"
    f"stderr:\n{result.stderr}"
) if result.exit_code != 0 else result.stdout
```

- [ ] **Step 6: 删 `skills/sandbox_client.py`**

```bash
git rm apps/agent-service/app/skills/sandbox_client.py
```

- [ ] **Step 7: 改 `tests/unit/test_skill_renderer.py`**

```python
# Before — 三处 patch
@patch("app.skills.renderer.sandbox_client")

# After
@patch("app.skills.renderer.run")
async def test_xxx(mock_run, ...):
    from app.capabilities.sandbox import SandboxResult
    mock_run.return_value = SandboxResult(exit_code=0, stdout="output", stderr="")
    ...
```

按现有 test 内容调整 mock 返回的 SandboxResult 字段。

- [ ] **Step 8: 跑全量测试**

```bash
cd apps/agent-service && uv run pytest tests/ -v
```

- [ ] **Step 9: grep gate 全套验证**

```bash
echo "--- Gap 13 ---"
grep -rnE "from app\.data\.session import|from app\.data import session" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/ | wc -l
grep -rnE "from app\.runtime\.db import.*current_session|from app\.runtime import.*current_session" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/ | wc -l
grep -rnE "get_session\(|async_session\(|AsyncSessionLocal|current_session\(" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/ | wc -l
grep -rn "AsyncSession" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/ | wc -l
grep -rnE "\bsession\.(execute|commit|add|merge|flush|rollback)\(" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/ | wc -l
grep -rnE "\bs\.(execute|commit|add|merge|flush|rollback)\(" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/ | wc -l
grep -rn "transactional_emit" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/ | wc -l
grep -rn "from app\.runtime\.outbox import" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/ | wc -l

echo "--- Gap 14 ---"
grep -rnE "from app\.infra\.redis import|from app\.infra import redis" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/ | wc -l
grep -rnE "redis\.set\(.*nx=True|redis\.eval\(|redis\.smembers\(|redis\.sadd\(" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/ | wc -l

echo "--- Gap 16 ---"
grep -rnE "import httpx|from httpx" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/ | wc -l
grep -rnE "from app\.capabilities\.http import|from app\.capabilities import http" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/ | wc -l
```

Expected: 全部 0

- [ ] **Step 10: commit**

```bash
git add apps/agent-service/app/{agent,skills}/ apps/agent-service/tests/unit/test_skill_renderer.py
git rm apps/agent-service/app/skills/sandbox_client.py 2>/dev/null  # 已 rm 过则 noop
git commit -m "refactor(agent/tools+skills): use web_search / image_search / sandbox capability

- agent/tools/search.py / image_search.py / sandbox.py / image_gen.py: drop httpx
- skills/renderer.py: use sandbox capability directly
- delete skills/sandbox_client.py (73 lines)
- update test_skill_renderer.py patch targets

Business code zero httpx import; HTTPClient hidden behind domain capabilities."
```

---

## Task 13 — CI grep gate

**Files:**
- Modify: `.github/workflows/grep-gate.yml`（添加 closed-gap-zero job 的新 grep）
- Modify: `.github/grep-baselines.json`（删 gap_13/14/16 三行）

- [ ] **Step 1: 看现有 grep-gate.yml 结构**

```bash
cat .github/workflows/grep-gate.yml
```

定位 closed-gap-zero job 的 grep 列表。Phase 7c 已有的 closed-gap-zero 是 Gap 7+8+9+11+12+15+18，现在加上 Gap 13+14+16。

- [ ] **Step 2: 在 closed-gap-zero job 加入新 grep（按 spec §6.1）**

`.github/workflows/grep-gate.yml`，在 closed-gap-zero job 的现有 grep 列表后追加：

```yaml
      # Gap 13 — DB session
      - name: Gap 13 — no session import in business
        run: |
          c=$(grep -rnE "from app\.data\.session import|from app\.data import session" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/ | wc -l) || true
          if [ "$c" != "0" ]; then echo "FAIL: $c hit(s)"; exit 1; fi

      - name: Gap 13 — no current_session import in business
        run: |
          c=$(grep -rnE "from app\.runtime\.db import.*current_session|from app\.runtime import.*current_session" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/ | wc -l) || true
          if [ "$c" != "0" ]; then echo "FAIL: $c hit(s)"; exit 1; fi

      - name: Gap 13 — no session factory call in business
        run: |
          c=$(grep -rnE "get_session\(|async_session\(|AsyncSessionLocal|current_session\(" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/ | wc -l) || true
          if [ "$c" != "0" ]; then echo "FAIL: $c hit(s)"; exit 1; fi

      - name: Gap 13 — no AsyncSession type in business
        run: |
          c=$(grep -rn "AsyncSession" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/ | wc -l) || true
          if [ "$c" != "0" ]; then echo "FAIL: $c hit(s)"; exit 1; fi

      - name: Gap 13 — no direct session.* in business (belt-and-suspenders)
        run: |
          c1=$(grep -rnE "\bsession\.(execute|commit|add|merge|flush|rollback)\(" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/ | wc -l) || true
          c2=$(grep -rnE "\bs\.(execute|commit|add|merge|flush|rollback)\(" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/ | wc -l) || true
          c=$((c1 + c2))
          if [ "$c" != "0" ]; then echo "FAIL: $c hit(s)"; exit 1; fi

      - name: Gap 13 — no transactional_emit in business
        run: |
          c=$(grep -rn "transactional_emit" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/ | wc -l) || true
          if [ "$c" != "0" ]; then echo "FAIL: $c hit(s)"; exit 1; fi

      - name: Gap 13 — no app.runtime.outbox import in business
        run: |
          c=$(grep -rn "from app\.runtime\.outbox import" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/ | wc -l) || true
          if [ "$c" != "0" ]; then echo "FAIL: $c hit(s)"; exit 1; fi

      # Gap 14 — Redis
      - name: Gap 14 — no infra.redis import in business
        run: |
          c=$(grep -rnE "from app\.infra\.redis import|from app\.infra import redis" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/ | wc -l) || true
          if [ "$c" != "0" ]; then echo "FAIL: $c hit(s)"; exit 1; fi

      - name: Gap 14 — no redis SETNX/eval/smembers/sadd in business
        run: |
          c=$(grep -rnE "redis\.set\(.*nx=True|redis\.eval\(|redis\.smembers\(|redis\.sadd\(" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/ | wc -l) || true
          if [ "$c" != "0" ]; then echo "FAIL: $c hit(s)"; exit 1; fi

      # Gap 16 — HTTP
      - name: Gap 16 — no httpx import in business
        run: |
          c=$(grep -rnE "import httpx|from httpx" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/ | wc -l) || true
          if [ "$c" != "0" ]; then echo "FAIL: $c hit(s)"; exit 1; fi

      - name: Gap 16 — no direct HTTPClient construction in business
        run: |
          c=$(grep -rnE "from app\.capabilities\.http import|from app\.capabilities import http" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/ | wc -l) || true
          if [ "$c" != "0" ]; then echo "FAIL: $c hit(s)"; exit 1; fi
```

- [ ] **Step 3: 删 baselines**

`.github/grep-baselines.json`：

```json
{
  "gap_19_create_task_business": 5
}
```

（原本是 `gap_13_get_session: 90, gap_14_redis_setnx_business: 5, gap_16_httpx_business: 3, gap_19_create_task_business: 5`，删除前三，留 19）

- [ ] **Step 4: 同步删除 baseline-job 里 gap 13/14/16 的检查（在同 workflow 里）**

```bash
grep -nE "gap_13|gap_14|gap_16" .github/workflows/grep-gate.yml
```

把 baseline 检查里 gap 13/14/16 那三段删掉。

- [ ] **Step 5: 本地跑 grep gate 模拟（用上面 step 9 的全套 grep）**

```bash
# 本地复刻一遍，全 0 才能 ship
bash -c '
echo "Gap 13 / 14 / 16 ALL grep gates:"
for cmd in \
  "grep -rnE \"from app\\.data\\.session import|from app\\.data import session\" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/" \
  "grep -rnE \"get_session\\(|async_session\\(|AsyncSessionLocal|current_session\\(\" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/" \
  "grep -rn \"AsyncSession\" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/" \
  "grep -rnE \"\\bsession\\.(execute|commit|add|merge|flush|rollback)\\(\" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/" \
  "grep -rn \"transactional_emit\" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/" \
  "grep -rnE \"from app\\.infra\\.redis import|from app\\.infra import redis\" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/" \
  "grep -rnE \"redis\\.set\\(.*nx=True|redis\\.eval\\(|redis\\.smembers\\(|redis\\.sadd\\(\" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/" \
  "grep -rnE \"import httpx|from httpx\" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/" \
  "grep -rnE \"from app\\.capabilities\\.http import|from app\\.capabilities import http\" apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/" \
; do
  echo "  ---"
  bash -c "$cmd | wc -l"
done
'
```

每条都应该 0。任何非 0 必须在合 PR 前消干净。

- [ ] **Step 6: commit**

```bash
git add .github/workflows/grep-gate.yml .github/grep-baselines.json
git commit -m "chore(ci): grep gate for Gap 13+14+16 closed

Move gap_13_get_session / gap_14_redis_setnx_business / gap_16_httpx_business
from baseline (no-new-occurrences) to closed-gap-zero (must == 0).
Add 7d-specific grep checks per spec §6.1 including current_session import,
direct session.*, app.runtime.outbox imports, and HTTPClient direct import."
```

---

## E2E Drill（dev 泳道）

完整 13 commit 推到 `feat/dataflow-phase7d-parse` 分支后部署 dev 泳道：

- [ ] **Drill 1: commit_abstract（多条原子写 + emit_tx）**

```bash
make deploy APP=agent-service LANE=dev GIT_REF=feat/dataflow-phase7d-parse
make deploy APP=vectorize-worker LANE=dev GIT_REF=feat/dataflow-phase7d-parse
/ops bind TYPE=bot KEY=dev LANE=dev
```

在飞书 dev bot 触发：让赤尾沉淀一条抽象记忆。验证：

```sql
SELECT id, persona_id FROM memory_abstract WHERE created_at > NOW() - INTERVAL '5 minutes';
SELECT id FROM memory_edge WHERE to_id IN (上面的 abstract id) AND from_type='fact';
SELECT data_type, state FROM runtime_outbox WHERE trace_id = '<本次 trace_id>';
```

三表都有，outbox 已 dispatched，vectorize-worker 收到 AbstractMemoryCommitted 后 vector 写入。

- [ ] **Drill 2: drift_check single_flight**

短时间内并发触发同 (chat, persona) drift（`/ops` 触发 admin debug 或 send 多条同 chat 消息让 debounce 生效），看 langfuse trace 看到一个跑、一个 raise SingleFlightConflict 后 DebounceReschedule。

- [ ] **Drill 3: banned_words 检查**

在飞书 dev bot 发命中 `banned_words` set 的违禁词（先用 `/ops-db @chiwei` 或 redis-cli 确认 set 内容），看 pre-safety 是否拦截 + langfuse trace 显示 `block_reason=banned_word`。

- [ ] **Drill 4: web_search**

让赤尾用 `web_search_tool`：「帮我搜一下 'phase7d dataflow refactor'」。看：
- tool 调用成功
- HTTPClient retry / lane / trace header 在 langfuse trace 中有 outbound 记录
- 无 httpx 异常

- [ ] **Drill 5: image_search**

「帮我搜一张 'akao' 的图片」，验证 ImageRegistry 能 register（用 `redis-cli HGETALL "image:reg:<message_id>"` 或 dashboard 查 redis）。

- [ ] **Drill 6: sandbox skill 调用**

让赤尾跑一个 skill（如 `use_skill simplify`），看：
- sandbox capability 调通 sandbox-worker
- LaneRouter base_url 解析正确（dev lane 路由到 dev sandbox-worker，否则 fallback prod）
- 命令只执行一次（manual 验证：在 sandbox-worker pod log 看请求计数 = 1）
- 失败时不 retry（构造 `command: exit 1` 或不存在的命令验证）

- [ ] **Drill 7: 节点内 `asyncio.gather` 并发查 DB**

safety pre-check 4 路并发是真实场景。在飞书 dev bot 发普通消息，langfuse trace 看 `_check_injection / _check_politics / _check_nsfw` 并发跑，无 `InvalidRequestError`。

- [ ] **Drill 8: nested tx 子事务**

跑 `tests/unit/test_runtime_db.py::test_nested_tx_savepoint` 在 dev 泳道连真 PG 跑：

```bash
kubectl exec -n prod deploy/agent-service-dev -- uv run pytest tests/unit/test_runtime_db.py::test_nested_tx_savepoint -v
```

> 不行，CLAUDE.md 禁止 kubectl exec 写操作。改用：本地连 dev PG 跑（用 `/ops-db @chiwei` 验证表存在）；或本地跑 unit test 用本地 PG。skip 这条 drill 也行，单测已覆盖。

- [ ] **Drill 9: tx 内 LLM 长调用 warning**

在 dev 跑一次 commit_abstract（内含 LLM 调用，但 LLM 在 tx 外，不会触发 warning）；如想验证 warning，构造 admin endpoint 故意 `async with tx(): await asyncio.sleep(6); await emit_tx(...)`，看 log 有 `tx() held for 6.xxs` warning。可选验证。

- [ ] **Drill 10: 清理 dev 泳道**

```bash
/ops unbind TYPE=bot KEY=dev
make undeploy APP=agent-service LANE=dev
make undeploy APP=vectorize-worker LANE=dev
```

---

## Ship 前 checklist

- [ ] 所有 13 commit 推到远端
- [ ] CI grep gate 全 green（如有 ghost FAILURE 可接受，按 phase 7c 经验）
- [ ] dev 泳道 8/10 drill 跑过（drill 8/9 可选）
- [ ] PR 描述列：13 commit 概览 + 闭合 Gap 13/14/16 + drill 验证摘要
- [ ] 用户明确说"合"再 `/ship`（CLAUDE.md `merge-and-ship.md` 铁律）

---

## Memory 更新（ship 后）

- [ ] 更新 `~/.claude/projects/-data00-home-yuanzhihong-chiwei-code-personal-chiwei-platform/memory/project_dataflow_phase7.md`
  - 7d 行：pending → shipped（PR 编号 + prod 版本号）
  - 删除 §「7d 范围」整段
- [ ] 如 7e 上手前要开新 spec，本 plan 可作模板
