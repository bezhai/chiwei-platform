# Memory v4 - Plan B: Tool 体系 + Recall Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 v4 的 5 个新 tool（`commit_abstract_memory` / `write_note` / `resolve_note` / `update_schedule` / `recall` 重写），让赤尾能在对话中主动沉淀抽象、写清单、调计划、按语义召回记忆。

**Architecture:** Langchain `@tool` 装饰器 + `AgentContext` 注入 persona_id。`commit_abstract_memory` 写 PG → 发 `memory_vectorize` 任务（异步补 embedding）。`recall` 走 Qdrant 语义检索 + PG graph 遍历。`update_schedule` 仅写 revision + 发 state_sync arq 任务（sync 由 Plan D 实现）。所有 tool 带 Langfuse trace、错误容忍。

**Tech Stack:** Python / langchain-core tool / SQLAlchemy / Qdrant / Ark embedding / Langfuse / arq / pytest

**前置:** Plan A（数据层）已完成

**Spec:** `docs/superpowers/specs/2026-04-16-memory-v4-design.md` §7.2、§7.3

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `app/agent/tools/commit_abstract.py` | Create | `commit_abstract_memory` tool |
| `app/agent/tools/notes.py` | Create | `write_note` / `resolve_note` tools |
| `app/agent/tools/update_schedule.py` | Create | `update_schedule` tool（写 revision + enqueue state_sync） |
| `app/agent/tools/recall.py` | Rewrite | Qdrant 语义检索 + graph 遍历；废弃 FTS |
| `app/agent/tools/__init__.py` | Modify | 注册新 tool 到 `BASE_TOOLS` / `ALL_TOOLS` |
| `app/memory/recall_engine.py` | Create | `run_recall(queries, ...)` 纯函数，tool 层调用它 |
| `app/memory/conflict.py` | Create | `detect_conflict(subject, content)` — commit_abstract_memory 的冲突检测 |
| `app/data/queries.py` | Modify | 新增 `list_edges_from` / `list_edges_to` / `get_abstracts_by_subject` |
| `app/infra/rabbitmq.py` | Modify | 新增 `STATE_SYNC` arq job name（调度由 Plan D 实现，这里只推入） |
| `tests/unit/agent/tools/test_commit_abstract.py` | Create | tool 单测（mock 冲突检测 + queries） |
| `tests/unit/agent/tools/test_notes.py` | Create | tool 单测 |
| `tests/unit/agent/tools/test_update_schedule.py` | Create | tool 单测 |
| `tests/unit/agent/tools/test_recall.py` | Rewrite | 新 recall 单测 |
| `tests/unit/memory/test_recall_engine.py` | Create | recall 纯函数单测 |
| `tests/unit/memory/test_conflict.py` | Create | 冲突检测单测 |

---

### Task 1: Recall Engine 纯函数

**Files:**
- Create: `app/memory/recall_engine.py`
- Create: `tests/unit/memory/test_recall_engine.py`

- [ ] **Step 1: 写测试**

```python
"""Test recall_engine run_recall() pure function."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory.recall_engine import RecallResult, run_recall


@pytest.mark.asyncio
async def test_run_recall_returns_abstracts_with_supporting_facts():
    # Mock embedding → mock qdrant search → mock PG lookup → mock edge traversal
    with patch("app.memory.recall_engine.embed_dense", new=AsyncMock(return_value=[0.1] * 1024)):
        with patch("app.memory.recall_engine.qdrant") as q:
            q.client.query_points = AsyncMock(
                return_value=MagicMock(points=[MagicMock(id="a_1", score=0.9, payload={"subject":"user:u1","clarity":"clear"})])
            )
            with patch("app.memory.recall_engine.get_abstract_by_id", new=AsyncMock(return_value=MagicMock(id="a_1", subject="user:u1", content="他是程序员", clarity="clear"))):
                with patch("app.memory.recall_engine.list_edges_to", new=AsyncMock(return_value=[MagicMock(from_id="f_1", from_type="fact", edge_type="supports")])):
                    with patch("app.memory.recall_engine.get_fragment_by_id", new=AsyncMock(return_value=MagicMock(id="f_1", content="他说他在写 Rust", clarity="clear"))):
                        with patch("app.memory.recall_engine.touch_abstract", new=AsyncMock()):
                            with patch("app.memory.recall_engine.touch_fragment", new=AsyncMock()):
                                result = await run_recall(
                                    persona_id="chiwei",
                                    queries=["浩南"],
                                    k_abs=5,
                                    k_facts_per_abs=3,
                                )
    assert isinstance(result, RecallResult)
    assert len(result.abstracts) == 1
    assert result.abstracts[0]["id"] == "a_1"
    assert len(result.abstracts[0]["supporting_facts"]) == 1
    assert result.abstracts[0]["supporting_facts"][0]["id"] == "f_1"


@pytest.mark.asyncio
async def test_run_recall_filters_forgotten():
    with patch("app.memory.recall_engine.embed_dense", new=AsyncMock(return_value=[0.1] * 1024)):
        with patch("app.memory.recall_engine.qdrant") as q:
            q.client.query_points = AsyncMock(return_value=MagicMock(points=[]))
            result = await run_recall(
                persona_id="chiwei", queries=["x"], k_abs=5, k_facts_per_abs=3,
            )
    assert result.abstracts == []


@pytest.mark.asyncio
async def test_run_recall_empty_query_list_returns_empty():
    result = await run_recall(persona_id="chiwei", queries=[], k_abs=5, k_facts_per_abs=3)
    assert result.abstracts == []
```

- [ ] **Step 2: Run — expect ImportError**

```bash
cd apps/agent-service
uv run pytest tests/unit/memory/test_recall_engine.py -v
```

- [ ] **Step 3: 创建 `app/memory/recall_engine.py`**

```python
"""Memory v4 recall engine — Qdrant semantic + PG graph traversal.

Public API:
  - run_recall(persona_id, queries, k_abs, k_facts_per_abs) -> RecallResult
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from qdrant_client.http.models import FieldCondition, Filter, MatchValue

from app.agent.embedding import embed_dense
from app.data.queries import (
    get_abstract_by_id,
    get_fragment_by_id,
    list_edges_to,
    touch_abstract,
    touch_fragment,
)
from app.data.session import get_session
from app.infra.dynamic_config import get_dynamic_config
from app.infra.qdrant import qdrant
from app.memory.vectorize_memory import COLLECTION_ABSTRACT, COLLECTION_FRAGMENT

logger = logging.getLogger(__name__)


@dataclass
class RecallResult:
    abstracts: list[dict[str, Any]] = field(default_factory=list)
    facts: list[dict[str, Any]] = field(default_factory=list)


async def _embed_model_id() -> str:
    try:
        cfg = await get_dynamic_config()
        return cfg.get("memory.embedding.model_id") or "embedding-model"
    except Exception:
        return "embedding-model"


def _persona_filter(persona_id: str) -> Filter:
    return Filter(
        must=[FieldCondition(key="persona_id", match=MatchValue(value=persona_id))],
        must_not=[FieldCondition(key="clarity", match=MatchValue(value="forgotten"))],
    )


async def _search_abstracts(
    persona_id: str, query_vec: list[float], k: int
) -> list[str]:
    res = await qdrant.client.query_points(
        collection_name=COLLECTION_ABSTRACT,
        query=query_vec,
        query_filter=_persona_filter(persona_id),
        limit=k,
    )
    return [str(p.id) for p in res.points]


async def _search_fragments(
    persona_id: str, query_vec: list[float], k: int
) -> list[str]:
    res = await qdrant.client.query_points(
        collection_name=COLLECTION_FRAGMENT,
        query=query_vec,
        query_filter=_persona_filter(persona_id),
        limit=k,
    )
    return [str(p.id) for p in res.points]


async def run_recall(
    *,
    persona_id: str,
    queries: list[str],
    k_abs: int = 5,
    k_facts_per_abs: int = 3,
    also_search_facts: bool = False,
    fact_k_per_query: int = 5,
) -> RecallResult:
    """Run a recall query and return abstracts (with supporting facts) plus optional standalone facts.

    - For each query: embed → search abstracts → fetch abstract row + supporting facts via edges.
    - `also_search_facts=True` additionally does standalone fragment search (used when query is
      specifically about a specific fact, not an abstraction).
    """
    if not queries:
        return RecallResult()

    model_id = await _embed_model_id()
    result = RecallResult()
    seen_abstracts: set[str] = set()
    seen_facts: set[str] = set()

    for query in queries:
        if not query.strip():
            continue
        vec = await embed_dense(model_id, text=query)

        abs_ids = await _search_abstracts(persona_id, vec, k_abs)
        for aid in abs_ids:
            if aid in seen_abstracts:
                continue
            seen_abstracts.add(aid)
            async with get_session() as s:
                a = await get_abstract_by_id(s, aid)
                if a is None:
                    continue
                edges = await list_edges_to(
                    s, persona_id=persona_id, to_id=aid, edge_type="supports"
                )
            # fetch supporting facts Top-K
            supporting_facts: list[dict[str, Any]] = []
            for edge in edges[:k_facts_per_abs]:
                async with get_session() as s:
                    f = await get_fragment_by_id(s, edge.from_id)
                if f is None or f.clarity == "forgotten":
                    continue
                supporting_facts.append(
                    {
                        "id": f.id,
                        "content": f.content,
                        "clarity": f.clarity,
                    }
                )
                seen_facts.add(f.id)

            result.abstracts.append(
                {
                    "id": a.id,
                    "subject": a.subject,
                    "content": a.content,
                    "clarity": a.clarity,
                    "supporting_facts": supporting_facts,
                }
            )

        if also_search_facts:
            fact_ids = await _search_fragments(persona_id, vec, fact_k_per_query)
            for fid in fact_ids:
                if fid in seen_facts:
                    continue
                seen_facts.add(fid)
                async with get_session() as s:
                    f = await get_fragment_by_id(s, fid)
                if f is None or f.clarity == "forgotten":
                    continue
                result.facts.append(
                    {"id": f.id, "content": f.content, "clarity": f.clarity}
                )

    # touch all accessed nodes (strengthen memory)
    async with get_session() as s:
        for aid in seen_abstracts:
            await touch_abstract(s, aid)
        for fid in seen_facts:
            await touch_fragment(s, fid)

    return result
```

- [ ] **Step 4: 补 `list_edges_to` / `list_edges_from` / `get_abstracts_by_subject` 到 queries.py**

在 `app/data/queries.py` 追加：

```python
async def list_edges_to(
    session: AsyncSession,
    *,
    persona_id: str,
    to_id: str,
    edge_type: str | None = None,
) -> list[MemoryEdge]:
    stmt = (
        select(MemoryEdge)
        .where(MemoryEdge.persona_id == persona_id)
        .where(MemoryEdge.to_id == to_id)
    )
    if edge_type:
        stmt = stmt.where(MemoryEdge.edge_type == edge_type)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_edges_from(
    session: AsyncSession,
    *,
    persona_id: str,
    from_id: str,
    edge_type: str | None = None,
) -> list[MemoryEdge]:
    stmt = (
        select(MemoryEdge)
        .where(MemoryEdge.persona_id == persona_id)
        .where(MemoryEdge.from_id == from_id)
    )
    if edge_type:
        stmt = stmt.where(MemoryEdge.edge_type == edge_type)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_abstracts_by_subject(
    session: AsyncSession, *, persona_id: str, subject: str, limit: int = 20
) -> list[AbstractMemory]:
    result = await session.execute(
        select(AbstractMemory)
        .where(AbstractMemory.persona_id == persona_id)
        .where(AbstractMemory.subject == subject)
        .where(AbstractMemory.clarity != "forgotten")
        .order_by(AbstractMemory.last_touched_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())
```

- [ ] **Step 5: Run — expect PASS**

```bash
uv run pytest tests/unit/memory/test_recall_engine.py tests/unit/data/test_v4_queries.py -v
```

- [ ] **Step 6: Commit**

```bash
git add app/memory/recall_engine.py app/data/queries.py tests/unit/memory/test_recall_engine.py
git commit -m "feat(memory-v4): recall engine (qdrant semantic + graph traversal)"
```

---

### Task 2: 冲突检测模块

**Files:**
- Create: `app/memory/conflict.py`
- Create: `tests/unit/memory/test_conflict.py`

- [ ] **Step 1: 写测试**

```python
"""Test conflict detection for abstract commits."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory.conflict import detect_conflict


@pytest.mark.asyncio
async def test_detect_conflict_returns_hint_on_high_similarity():
    existing = MagicMock(id="a_old", content="他不爱吃甜食", clarity="clear")
    with patch("app.memory.conflict.get_abstracts_by_subject", new=AsyncMock(return_value=[existing])):
        with patch("app.memory.conflict.embed_dense", new=AsyncMock(side_effect=[[1.0] + [0.0]*1023, [0.99] + [0.01]*1023])):
            hint = await detect_conflict(
                persona_id="chiwei", subject="浩南",
                content="他今天主动买了奶茶",
                similarity_threshold=0.7,
            )
    assert hint is not None
    assert hint["conflicting_abstract_id"] == "a_old"


@pytest.mark.asyncio
async def test_detect_conflict_returns_none_when_low_similarity():
    existing = MagicMock(id="a_old", content="他是工程师", clarity="clear")
    with patch("app.memory.conflict.get_abstracts_by_subject", new=AsyncMock(return_value=[existing])):
        with patch("app.memory.conflict.embed_dense", new=AsyncMock(side_effect=[[1.0] + [0.0]*1023, [0.0]*1023 + [1.0]])):
            hint = await detect_conflict(
                persona_id="chiwei", subject="浩南",
                content="他喜欢跑步", similarity_threshold=0.7,
            )
    assert hint is None


@pytest.mark.asyncio
async def test_detect_conflict_empty_subject_returns_none():
    with patch("app.memory.conflict.get_abstracts_by_subject", new=AsyncMock(return_value=[])):
        hint = await detect_conflict(
            persona_id="chiwei", subject="new_subject",
            content="first fact", similarity_threshold=0.7,
        )
    assert hint is None
```

- [ ] **Step 2: 创建 `app/memory/conflict.py`**

```python
"""Conflict detection for commit_abstract_memory.

Compares new content against existing abstracts with same subject via embeddings.
Returns a hint (not a block) when similarity exceeds threshold.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agent.embedding import embed_dense
from app.data.queries import get_abstracts_by_subject
from app.data.session import get_session

logger = logging.getLogger(__name__)

DEFAULT_SIMILARITY_THRESHOLD = 0.85


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


async def detect_conflict(
    *,
    persona_id: str,
    subject: str,
    content: str,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> dict[str, Any] | None:
    """Return conflict hint dict if similar abstract exists, else None.

    Hint shape:
        {
            "conflicting_abstract_id": "a_xxx",
            "conflicting_content": "...",
            "similarity": 0.88,
        }
    """
    async with get_session() as s:
        existing = await get_abstracts_by_subject(
            s, persona_id=persona_id, subject=subject, limit=10
        )
    if not existing:
        return None

    try:
        new_vec = await embed_dense("embedding-model", text=content)
    except Exception as e:
        logger.warning("embed failed in conflict detect: %s", e)
        return None

    best: tuple[float, Any] = (0.0, None)
    for a in existing:
        try:
            old_vec = await embed_dense("embedding-model", text=a.content)
        except Exception:
            continue
        sim = _cosine(new_vec, old_vec)
        if sim > best[0]:
            best = (sim, a)

    score, a = best
    if a is None or score < similarity_threshold:
        return None
    return {
        "conflicting_abstract_id": a.id,
        "conflicting_content": a.content,
        "similarity": round(score, 3),
    }
```

**性能优化提示**：如果 cold path embedding 太慢，可以改成查 Qdrant 同 subject 已有 point 的 dense vector（但目前初期可以接受 online embedding cost，因为 subject 同名的抽象通常 <10 条）。

- [ ] **Step 3: Run — expect PASS**

```bash
uv run pytest tests/unit/memory/test_conflict.py -v
```

- [ ] **Step 4: Commit**

```bash
git add app/memory/conflict.py tests/unit/memory/test_conflict.py
git commit -m "feat(memory-v4): conflict detection for new abstract commits"
```

---

### Task 3: `commit_abstract_memory` tool

**Files:**
- Create: `app/agent/tools/commit_abstract.py`
- Create: `tests/unit/agent/tools/test_commit_abstract.py`

- [ ] **Step 1: 写测试**

```python
"""Test commit_abstract_memory tool."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.tools.commit_abstract import _commit_abstract_impl


@pytest.mark.asyncio
async def test_commit_writes_abstract_and_edges():
    with patch("app.agent.tools.commit_abstract.detect_conflict", new=AsyncMock(return_value=None)):
        with patch("app.agent.tools.commit_abstract.get_fragment_by_id", new=AsyncMock(return_value=MagicMock())):
            with patch("app.agent.tools.commit_abstract.insert_abstract_memory", new=AsyncMock()) as ins_a:
                with patch("app.agent.tools.commit_abstract.insert_memory_edge", new=AsyncMock()) as ins_e:
                    with patch("app.agent.tools.commit_abstract.enqueue_abstract_vectorize", new=AsyncMock()) as enq:
                        out = await _commit_abstract_impl(
                            persona_id="chiwei",
                            subject="浩南",
                            content="他最近压力大",
                            supported_by_fact_ids=["f_1", "f_2"],
                            reasoning=None,
                        )
    assert "id" in out
    assert out["conflict_hint"] is None
    ins_a.assert_awaited_once()
    assert ins_e.await_count == 2
    enq.assert_awaited_once()


@pytest.mark.asyncio
async def test_commit_returns_conflict_hint():
    hint = {"conflicting_abstract_id": "a_old", "similarity": 0.91, "conflicting_content": "他不爱甜"}
    with patch("app.agent.tools.commit_abstract.detect_conflict", new=AsyncMock(return_value=hint)):
        with patch("app.agent.tools.commit_abstract.insert_abstract_memory", new=AsyncMock()) as ins_a:
            with patch("app.agent.tools.commit_abstract.enqueue_abstract_vectorize", new=AsyncMock()):
                out = await _commit_abstract_impl(
                    persona_id="chiwei", subject="浩南",
                    content="他喝奶茶了", supported_by_fact_ids=None, reasoning=None,
                )
    # still committed (not blocked), but hint returned
    ins_a.assert_awaited_once()
    assert out["conflict_hint"] == hint


@pytest.mark.asyncio
async def test_commit_validates_fact_ids_exist():
    with patch("app.agent.tools.commit_abstract.detect_conflict", new=AsyncMock(return_value=None)):
        with patch("app.agent.tools.commit_abstract.get_fragment_by_id", new=AsyncMock(return_value=None)):
            with patch("app.agent.tools.commit_abstract.insert_abstract_memory", new=AsyncMock()) as ins_a:
                out = await _commit_abstract_impl(
                    persona_id="chiwei", subject="浩南",
                    content="x", supported_by_fact_ids=["f_missing"], reasoning=None,
                )
    assert "error" in out
    ins_a.assert_not_awaited()
```

- [ ] **Step 2: Run — expect ImportError**

- [ ] **Step 3: 创建 `app/agent/tools/commit_abstract.py`**

```python
"""commit_abstract_memory tool — sink for in-conversation abstractions."""

from __future__ import annotations

import logging
import uuid

from langchain.tools import tool
from langgraph.runtime import get_runtime

from app.agent.context import AgentContext
from app.agent.tools._common import tool_error
from app.data.queries import (
    get_fragment_by_id,
    insert_abstract_memory,
    insert_memory_edge,
)
from app.data.session import get_session
from app.memory.conflict import detect_conflict
from app.memory.vectorize_memory import enqueue_abstract_vectorize

logger = logging.getLogger(__name__)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


async def _commit_abstract_impl(
    *,
    persona_id: str,
    subject: str,
    content: str,
    supported_by_fact_ids: list[str] | None,
    reasoning: str | None,
) -> dict:
    subject = (subject or "").strip()
    content = (content or "").strip()
    if not subject or not content:
        return {"error": "subject 和 content 不能为空"}

    # validate fact ids
    if supported_by_fact_ids:
        async with get_session() as s:
            for fid in supported_by_fact_ids:
                f = await get_fragment_by_id(s, fid)
                if f is None:
                    return {"error": f"fact id {fid} 不存在"}

    hint = await detect_conflict(
        persona_id=persona_id, subject=subject, content=content,
    )

    aid = _uid("a")
    async with get_session() as s:
        await insert_abstract_memory(
            s, id=aid, persona_id=persona_id,
            subject=subject, content=content,
            created_by="chiwei",
        )
        for fid in supported_by_fact_ids or []:
            await insert_memory_edge(
                s, id=_uid("e"), persona_id=persona_id,
                from_id=fid, from_type="fact",
                to_id=aid, to_type="abstract",
                edge_type="supports", created_by="chiwei",
                reason=reasoning,
            )

    await enqueue_abstract_vectorize(aid)

    return {"id": aid, "conflict_hint": hint}


@tool
@tool_error("抽象记忆保存失败")
async def commit_abstract_memory(
    subject: str,
    content: str,
    supported_by_fact_ids: list[str] | None = None,
    reasoning: str | None = None,
) -> dict:
    """沉淀一条抽象认识到长期记忆。

    当你在对话里对某个人/话题/自己有了一个新的认识（不是单一事实而是"认识"），
    把它用简洁的一段话写下来。subject 是这条认识是关于什么的（可以是人名、"self"、
    某个话题）。如果你有具体事实作为依据，传 supported_by_fact_ids。

    如果你写入的内容和已有抽象高度相似，返回里会有 conflict_hint，告诉你旧抽象是啥，
    你可以选择：忽略、稍后自己改写、或者再补充更精确的表达。

    Args:
        subject: 这条认识是关于什么的（自由字符串）
        content: 认识本身（简洁，一段话）
        supported_by_fact_ids: 可选，支撑这条认识的 fragment id 列表
        reasoning: 可选，你写下这条认识的原因（帮助未来 review）
    """
    context = get_runtime(AgentContext).context
    return await _commit_abstract_impl(
        persona_id=context.persona_id,
        subject=subject,
        content=content,
        supported_by_fact_ids=supported_by_fact_ids,
        reasoning=reasoning,
    )
```

- [ ] **Step 4: Run — expect PASS**

```bash
uv run pytest tests/unit/agent/tools/test_commit_abstract.py -v
```

- [ ] **Step 5: Commit**

```bash
git add app/agent/tools/commit_abstract.py tests/unit/agent/tools/test_commit_abstract.py
git commit -m "feat(memory-v4): commit_abstract_memory tool"
```

---

### Task 4: `write_note` / `resolve_note` tools

**Files:**
- Create: `app/agent/tools/notes.py`
- Create: `tests/unit/agent/tools/test_notes.py`

- [ ] **Step 1: 写测试**

```python
"""Test write_note / resolve_note tools."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.tools.notes import _resolve_note_impl, _write_note_impl


@pytest.mark.asyncio
async def test_write_note_creates_and_returns_id_with_active_list():
    active = [MagicMock(id="n_existing", content="已有笔记", when_at=None)]
    with patch("app.agent.tools.notes.insert_note", new=AsyncMock()) as ins:
        with patch("app.agent.tools.notes.get_active_notes", new=AsyncMock(return_value=active)):
            out = await _write_note_impl(
                persona_id="chiwei", content="周五看电影", when_at=None,
            )
    assert "id" in out
    assert out["id"].startswith("n_")
    assert len(out["active_notes"]) == 1
    ins.assert_awaited_once()


@pytest.mark.asyncio
async def test_write_note_rejects_empty_content():
    with patch("app.agent.tools.notes.insert_note", new=AsyncMock()) as ins:
        out = await _write_note_impl(persona_id="chiwei", content="  ", when_at=None)
    assert "error" in out
    ins.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_note_calls_query():
    with patch("app.agent.tools.notes.resolve_note_query", new=AsyncMock()) as rn:
        out = await _resolve_note_impl(
            persona_id="chiwei", note_id="n_1", resolution="看完了",
        )
    assert out.get("ok") is True
    rn.assert_awaited_once()
```

- [ ] **Step 2: Run — expect ImportError**

- [ ] **Step 3: 创建 `app/agent/tools/notes.py`**

```python
"""write_note / resolve_note — 赤尾的主动清单 tool。"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from langchain.tools import tool
from langgraph.runtime import get_runtime

from app.agent.context import AgentContext
from app.agent.tools._common import tool_error
from app.data.queries import get_active_notes, insert_note
from app.data.queries import resolve_note as resolve_note_query
from app.data.session import get_session

logger = logging.getLogger(__name__)


def _uid() -> str:
    return f"n_{uuid.uuid4().hex[:12]}"


async def _write_note_impl(
    *, persona_id: str, content: str, when_at: datetime | None,
) -> dict:
    content = (content or "").strip()
    if not content:
        return {"error": "content 不能为空"}

    nid = _uid()
    async with get_session() as s:
        await insert_note(
            s, id=nid, persona_id=persona_id, content=content, when_at=when_at,
        )

    async with get_session() as s:
        active = await get_active_notes(s, persona_id=persona_id)

    return {
        "id": nid,
        "active_notes": [
            {
                "id": n.id,
                "content": n.content,
                "when_at": n.when_at.isoformat() if n.when_at else None,
            }
            for n in active
        ],
    }


async def _resolve_note_impl(
    *, persona_id: str, note_id: str, resolution: str,
) -> dict:
    resolution = (resolution or "").strip()
    if not note_id or not resolution:
        return {"error": "note_id 和 resolution 都不能为空"}
    async with get_session() as s:
        await resolve_note_query(s, note_id=note_id, resolution=resolution)
    return {"ok": True}


@tool
@tool_error("笔记保存失败")
async def write_note(content: str, when_at: str | None = None) -> dict:
    """把一件你觉得必须记住的事写进清单。

    这是你自己的清单，不是系统强加的承诺列表。只有你觉得"不能忘"、"需要专门记住"的
    事才写。当时间相关的（比如"周五和浩南看电影"），传 when_at（ISO 8601 格式）。
    普通的备忘、情绪留痕也行。

    Args:
        content: 笔记内容
        when_at: 可选，ISO 8601 时间戳（"2026-04-18T19:00:00+08:00"）
    """
    context = get_runtime(AgentContext).context
    parsed_when: datetime | None = None
    if when_at:
        try:
            parsed_when = datetime.fromisoformat(when_at)
        except ValueError:
            return {"error": f"when_at 格式无效: {when_at}"}
    return await _write_note_impl(
        persona_id=context.persona_id, content=content, when_at=parsed_when,
    )


@tool
@tool_error("清单更新失败")
async def resolve_note(note_id: str, resolution: str) -> dict:
    """把一条已经完结的笔记划掉。

    比如电影看了、想法落实了、或者你改主意不做了。resolution 写一句话说明结果
    （"看完了"/"改了主意"/"忘了"）。

    Args:
        note_id: 笔记 id（形如 "n_xxxxxx"）
        resolution: 结果描述
    """
    context = get_runtime(AgentContext).context
    return await _resolve_note_impl(
        persona_id=context.persona_id, note_id=note_id, resolution=resolution,
    )
```

- [ ] **Step 4: Run — expect PASS**

```bash
uv run pytest tests/unit/agent/tools/test_notes.py -v
```

- [ ] **Step 5: Commit**

```bash
git add app/agent/tools/notes.py tests/unit/agent/tools/test_notes.py
git commit -m "feat(memory-v4): write_note and resolve_note tools"
```

---

### Task 5: `update_schedule` tool + state_sync enqueue

**Files:**
- Create: `app/agent/tools/update_schedule.py`
- Modify: `app/infra/rabbitmq.py`（可选，如果 state_sync 走 MQ 而非 arq）
- Create: `tests/unit/agent/tools/test_update_schedule.py`

**Note:** state_sync 的消费由 Plan D 实现；这里只 enqueue。

- [ ] **Step 1: 写测试**

```python
"""Test update_schedule tool."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.agent.tools.update_schedule import _update_schedule_impl


@pytest.mark.asyncio
async def test_update_schedule_writes_revision_and_enqueues_sync():
    with patch("app.agent.tools.update_schedule.insert_schedule_revision", new=AsyncMock()) as ins:
        with patch("app.agent.tools.update_schedule.enqueue_state_sync", new=AsyncMock()) as enq:
            out = await _update_schedule_impl(
                persona_id="chiwei", content="今天...", reason="first draft",
                created_by="chiwei",
            )
    assert "revision_id" in out
    ins.assert_awaited_once()
    enq.assert_awaited_once_with(revision_id=out["revision_id"])


@pytest.mark.asyncio
async def test_update_schedule_rejects_empty():
    with patch("app.agent.tools.update_schedule.insert_schedule_revision", new=AsyncMock()) as ins:
        out = await _update_schedule_impl(
            persona_id="chiwei", content=" ", reason="", created_by="chiwei",
        )
    assert "error" in out
    ins.assert_not_awaited()
```

- [ ] **Step 2: 创建 `app/agent/tools/update_schedule.py`**

```python
"""update_schedule tool — append schedule_revision + enqueue state_sync."""

from __future__ import annotations

import logging
import uuid

from langchain.tools import tool
from langgraph.runtime import get_runtime

from app.agent.context import AgentContext
from app.agent.tools._common import tool_error
from app.data.queries import insert_schedule_revision
from app.data.session import get_session

logger = logging.getLogger(__name__)


async def enqueue_state_sync(*, revision_id: str) -> None:
    """Enqueue arq job `sync_life_state_after_schedule` (implemented in Plan D)."""
    from app.workers.arq_settings import arq_pool

    pool = await arq_pool()
    await pool.enqueue_job(
        "sync_life_state_after_schedule", revision_id=revision_id
    )


async def _update_schedule_impl(
    *, persona_id: str, content: str, reason: str, created_by: str,
) -> dict:
    content = (content or "").strip()
    reason = (reason or "").strip()
    if not content or not reason:
        return {"error": "content 和 reason 都不能为空"}

    rid = f"sr_{uuid.uuid4().hex[:12]}"
    async with get_session() as s:
        await insert_schedule_revision(
            s, id=rid, persona_id=persona_id,
            content=content, reason=reason, created_by=created_by,
        )

    try:
        await enqueue_state_sync(revision_id=rid)
    except Exception as e:
        logger.warning("enqueue_state_sync failed: %s (schedule still saved)", e)

    return {"revision_id": rid, "schedule": content}


@tool
@tool_error("日程更新失败")
async def update_schedule(content: str, reason: str) -> dict:
    """更新你今天剩下的日程（覆盖式，你决定保留什么舍弃什么）。

    content 是一段自然语言，描述你当下状态 + 接下来要干嘛。稳定骨架和近期改动都
    写在一起。粒度自由，你觉得合适就行。reason 简短说一下为什么要改。

    调用后，state 会在后台重新评估（可能会立刻切状态或段内刷新）。

    Args:
        content: 新的日程段落
        reason: 本次更新的原因
    """
    context = get_runtime(AgentContext).context
    return await _update_schedule_impl(
        persona_id=context.persona_id,
        content=content, reason=reason,
        created_by="chiwei",  # chat-agent 触发
    )
```

**Note:** 如果 `app/workers/arq_settings.py` 里没有 `arq_pool()` helper，需要同步查一下现有代码怎么 enqueue arq job 并调整调用方式。

- [ ] **Step 3: Run — expect PASS**

```bash
uv run pytest tests/unit/agent/tools/test_update_schedule.py -v
```

- [ ] **Step 4: Commit**

```bash
git add app/agent/tools/update_schedule.py tests/unit/agent/tools/test_update_schedule.py
git commit -m "feat(memory-v4): update_schedule tool (revision + state_sync enqueue)"
```

---

### Task 6: Recall tool 重写

**Files:**
- Rewrite: `app/agent/tools/recall.py`
- Rewrite: `tests/unit/agent/tools/test_recall.py`

- [ ] **Step 1: 写新测试**（覆盖掉现有）

```python
"""Test recall tool — v4 Qdrant + graph semantic recall."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.agent.tools.recall import _recall_impl
from app.memory.recall_engine import RecallResult


@pytest.mark.asyncio
async def test_recall_returns_structured_json():
    rr = RecallResult(
        abstracts=[
            {
                "id": "a_1",
                "subject": "浩南",
                "content": "他最近压力大",
                "clarity": "clear",
                "supporting_facts": [
                    {"id": "f_1", "content": "他加班到凌晨", "clarity": "clear"}
                ],
            }
        ],
        facts=[],
    )
    with patch("app.agent.tools.recall.run_recall", new=AsyncMock(return_value=rr)):
        out = await _recall_impl(
            persona_id="chiwei",
            queries=["浩南最近怎么了"],
            k_abs=5,
            k_facts_per_abs=3,
        )
    assert out["abstracts"][0]["id"] == "a_1"
    assert out["abstracts"][0]["supporting_facts"][0]["id"] == "f_1"


@pytest.mark.asyncio
async def test_recall_accepts_batch_queries():
    with patch("app.agent.tools.recall.run_recall", new=AsyncMock(return_value=RecallResult())) as rr:
        await _recall_impl(
            persona_id="chiwei",
            queries=["浩南", "学习"],
            k_abs=5,
            k_facts_per_abs=3,
        )
    call = rr.await_args
    assert call.kwargs["queries"] == ["浩南", "学习"]


@pytest.mark.asyncio
async def test_recall_empty_returns_structured_empty():
    with patch("app.agent.tools.recall.run_recall", new=AsyncMock(return_value=RecallResult())):
        out = await _recall_impl(
            persona_id="chiwei", queries=["x"],
            k_abs=5, k_facts_per_abs=3,
        )
    assert out == {"abstracts": [], "facts": []}
```

- [ ] **Step 2: 重写 `app/agent/tools/recall.py`**（完整替换现有内容）

```python
"""Memory recall tool — v4 Qdrant semantic + graph traversal.

FTS path is deprecated and removed. See app/memory/recall_engine.py for the
pure function; this file only wires it into the agent tool system.
"""

from __future__ import annotations

import logging

from langchain.tools import tool
from langgraph.runtime import get_runtime

from app.agent.context import AgentContext
from app.agent.tools._common import tool_error
from app.memory.recall_engine import run_recall

logger = logging.getLogger(__name__)

DEFAULT_K_ABS = 5
DEFAULT_K_FACTS_PER_ABS = 3


async def _recall_impl(
    *,
    persona_id: str,
    queries: list[str],
    k_abs: int,
    k_facts_per_abs: int,
) -> dict:
    result = await run_recall(
        persona_id=persona_id,
        queries=queries,
        k_abs=k_abs,
        k_facts_per_abs=k_facts_per_abs,
    )
    return {"abstracts": result.abstracts, "facts": result.facts}


@tool
@tool_error("想不起来了...")
async def recall(queries: list[str]) -> dict:
    """回忆过去。传一个或多个关键词/描述，按语义在记忆里搜。

    每个 query 都是一次独立搜索；批量传可以一次查多条线索。
    返回你记得的抽象认识 + 每条认识下具体的事实支撑。

    例子：
      recall(queries=["浩南最近怎么了"])
      recall(queries=["学习 Rust", "他答应过我什么"])

    Args:
        queries: 自然语言查询列表（批量）
    """
    context = get_runtime(AgentContext).context
    return await _recall_impl(
        persona_id=context.persona_id,
        queries=queries,
        k_abs=DEFAULT_K_ABS,
        k_facts_per_abs=DEFAULT_K_FACTS_PER_ABS,
    )
```

- [ ] **Step 3: 删除 FTS 函数 `search_fragments_fts`**

确认无其他调用方（grep `search_fragments_fts`）。如仍有调用方，由 Plan C 处理；这里先保留函数但标记 deprecated 注释。

```bash
rg -n "search_fragments_fts" --type py
```

如果只 recall.py 用过，直接删除 queries.py 里的定义；如果还有别的用户，在函数定义上加注释 `# DEPRECATED: removed with v4, last users in plan C`，不在本 plan 删。

- [ ] **Step 4: Run — expect PASS**

```bash
uv run pytest tests/unit/agent/tools/test_recall.py -v
```

- [ ] **Step 5: Commit**

```bash
git add app/agent/tools/recall.py tests/unit/agent/tools/test_recall.py app/data/queries.py
git commit -m "feat(memory-v4): rewrite recall tool to use qdrant + graph"
```

---

### Task 7: 注册所有新 tool 到 `BASE_TOOLS` / `ALL_TOOLS`

**Files:**
- Modify: `app/agent/tools/__init__.py`

- [ ] **Step 1: 修改 `__init__.py`**

```python
from app.agent.tools.commit_abstract import commit_abstract_memory
from app.agent.tools.notes import resolve_note, write_note
from app.agent.tools.update_schedule import update_schedule
```

然后在 `BASE_TOOLS` 列表里追加：

```python
BASE_TOOLS = [
    search_web,
    search_images,
    generate_image,
    read_images,
    recall,
    commit_abstract_memory,
    write_note,
    resolve_note,
    update_schedule,
]
```

同步更新 `__all__` export 列表。

- [ ] **Step 2: 验证导入正常**

```bash
uv run python -c "from app.agent.tools import ALL_TOOLS, BASE_TOOLS; print(len(BASE_TOOLS), len(ALL_TOOLS))"
```

- [ ] **Step 3: 跑全量测试确保没有导入冲突**

```bash
uv run pytest tests/unit/agent/ -v
```

- [ ] **Step 4: Commit**

```bash
git add app/agent/tools/__init__.py
git commit -m "feat(memory-v4): register v4 tools in BASE_TOOLS"
```

---

### Task 8: 合并自检

- [ ] **Step 1: 全量测试**

```bash
cd apps/agent-service
uv run pytest tests/unit/memory/ tests/unit/agent/tools/ -v
```

- [ ] **Step 2: lint + 类型**

```bash
uv run ruff check app tests
uv run basedpyright app tests
```

- [ ] **Step 3: Final commit**

```bash
git commit --allow-empty -m "chore(memory-v4): Plan B tools and recall ready"
```

---

## Self-Review

- ✅ Recall engine 纯函数（Task 1）+ conflict 模块（Task 2）
- ✅ 4 个 tool 各一个 TDD 循环（Task 3-6）
- ✅ Tool 注册（Task 7）
- ⚠️ `enqueue_state_sync` 依赖 arq pool helper，Plan D 里会补消费者；本 plan 只发任务
- ⚠️ `arq_pool()` 如果项目没这个 helper，Task 5 执行时要同步补（看现有 arq job 怎么 enqueue 的）
- ⚠️ `search_fragments_fts` 的删除可能被 Plan C 里的 afterthought / build_inner_context 引用——Plan C 会一并清理

## Execution Handoff

Plan B 完成标志：所有 8 个 task 绿灯 + tool 能被 langgraph runtime 装载（python import 成功）。
