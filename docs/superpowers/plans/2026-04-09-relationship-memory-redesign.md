# 关系记忆重设计 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复关系记忆全面负面的问题，拆分 core_facts + impression、修复提取 prompt、提供批量回溯端点

**Architecture:** DB 加 version/core_facts/impression 三列，relationship_extract prompt 改为第一人称角色视角，afterthought 提取函数适配新字段，新增 admin rebuild 端点供本地脚本批量调用

**Tech Stack:** Python / FastAPI / SQLAlchemy / Langfuse / LangChain

---

### Task 1: ORM Model 加字段

**Files:**
- Modify: `apps/agent-service/app/orm/memory_models.py:114-131`

- [ ] **Step 1: 加三个字段到 RelationshipMemory**

```python
# apps/agent-service/app/orm/memory_models.py
# 在 RelationshipMemory 类中，memory_text 之后加：

class RelationshipMemory(Base):
    """关系记忆 — per-user 的自然语言关系描述，append-only"""

    __tablename__ = "relationship_memory"

    id: Mapped[int] = mapped_column(primary_key=True)
    persona_id: Mapped[str] = mapped_column(String(50), nullable=False)
    user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    user_name: Mapped[str] = mapped_column(String(100), nullable=False, server_default="")
    memory_text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    core_facts: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    impression: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("idx_rel_mem_persona_user_created", "persona_id", "user_id", created_at.desc()),
    )
```

注意：需要在文件顶部 import `Integer`（如果尚未导入）。

- [ ] **Step 2: 验证 import**

Run: `cd apps/agent-service && python -c "from app.orm.memory_models import RelationshipMemory; print('OK')"`
Expected: OK

- [ ] **Step 3: Commit**

```bash
git add apps/agent-service/app/orm/memory_models.py
git commit -m "feat(relationship-memory): add version/core_facts/impression to ORM model"
```

---

### Task 2: CRUD 函数适配新字段

**Files:**
- Modify: `apps/agent-service/app/orm/memory_crud.py:216-271`
- Modify: `apps/agent-service/tests/unit/test_memory_crud.py`

- [ ] **Step 1: 写 save_relationship_memory 的测试**

```python
# tests/unit/test_memory_crud.py — 追加到文件末尾

# ---------------------------------------------------------------------------
# save_relationship_memory (v2: core_facts + impression + version)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_relationship_memory_first_version():
    """首次写入，version 应为 1"""
    mock_session = _make_mock_session()
    # 查 max version 返回 None（无历史记录）
    mock_session.execute = AsyncMock(return_value=_make_mock_result(scalar_value=None))

    with patch("app.orm.memory_crud.AsyncSessionLocal", return_value=mock_session):
        from app.orm.memory_crud import save_relationship_memory
        await save_relationship_memory(
            persona_id="chiwei",
            user_id="user_001",
            user_name="crgg",
            core_facts="群昵称 crgg",
            impression="脑回路清奇",
            source="afterthought",
        )

    mock_session.add.assert_called_once()
    added_obj = mock_session.add.call_args[0][0]
    assert added_obj.version == 1
    assert added_obj.core_facts == "群昵称 crgg"
    assert added_obj.impression == "脑回路清奇"
    assert added_obj.memory_text == ""
    mock_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_save_relationship_memory_increments_version():
    """已有记录时，version 应在最大值基础上 +1"""
    mock_session = _make_mock_session()
    mock_session.execute = AsyncMock(return_value=_make_mock_result(scalar_value=3))

    with patch("app.orm.memory_crud.AsyncSessionLocal", return_value=mock_session):
        from app.orm.memory_crud import save_relationship_memory
        await save_relationship_memory(
            persona_id="chiwei",
            user_id="user_001",
            user_name="crgg",
            core_facts="群昵称 crgg",
            impression="更新的印象",
            source="afterthought",
        )

    added_obj = mock_session.add.call_args[0][0]
    assert added_obj.version == 4
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd apps/agent-service && python -m pytest tests/unit/test_memory_crud.py::test_save_relationship_memory_first_version tests/unit/test_memory_crud.py::test_save_relationship_memory_increments_version -v`
Expected: FAIL（签名不匹配）

- [ ] **Step 3: 改写 save_relationship_memory**

```python
# app/orm/memory_crud.py — 替换 save_relationship_memory 函数

async def save_relationship_memory(
    persona_id: str,
    user_id: str,
    user_name: str,
    core_facts: str,
    impression: str,
    source: str,
) -> None:
    """写入关系记忆（append-only，version 自增）"""
    from app.orm.memory_models import RelationshipMemory

    async with AsyncSessionLocal() as session:
        # 查当前最大 version
        result = await session.execute(
            select(func.max(RelationshipMemory.version))
            .where(RelationshipMemory.persona_id == persona_id)
            .where(RelationshipMemory.user_id == user_id)
        )
        max_version = result.scalar_one_or_none() or 0

        session.add(RelationshipMemory(
            persona_id=persona_id,
            user_id=user_id,
            user_name=user_name,
            memory_text="",
            version=max_version + 1,
            core_facts=core_facts,
            impression=impression,
            source=source,
        ))
        await session.commit()
```

注意：需要在文件顶部确认 `from sqlalchemy import func, select` 已导入。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd apps/agent-service && python -m pytest tests/unit/test_memory_crud.py::test_save_relationship_memory_first_version tests/unit/test_memory_crud.py::test_save_relationship_memory_increments_version -v`
Expected: PASS

- [ ] **Step 5: 写 get_latest_relationship_memory 的测试**

```python
# tests/unit/test_memory_crud.py — 追加

# ---------------------------------------------------------------------------
# get_latest_relationship_memory (v2: returns core_facts + impression tuple)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_latest_relationship_memory_new_fields():
    """有新字段时返回 (core_facts, impression) 元组"""
    mock_row = MagicMock()
    mock_row.core_facts = "群昵称 crgg"
    mock_row.impression = "脑回路清奇"
    mock_row.memory_text = ""

    mock_session = _make_mock_session()
    mock_result = MagicMock()
    mock_result.one_or_none.return_value = mock_row
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("app.orm.memory_crud.AsyncSessionLocal", return_value=mock_session):
        from app.orm.memory_crud import get_latest_relationship_memory
        result = await get_latest_relationship_memory("chiwei", "user_001")

    assert result == ("群昵称 crgg", "脑回路清奇")


@pytest.mark.asyncio
async def test_get_latest_relationship_memory_fallback_memory_text():
    """新字段为空时 fallback 到 memory_text"""
    mock_row = MagicMock()
    mock_row.core_facts = ""
    mock_row.impression = ""
    mock_row.memory_text = "旧的关系记忆文本"

    mock_session = _make_mock_session()
    mock_result = MagicMock()
    mock_result.one_or_none.return_value = mock_row
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("app.orm.memory_crud.AsyncSessionLocal", return_value=mock_session):
        from app.orm.memory_crud import get_latest_relationship_memory
        result = await get_latest_relationship_memory("chiwei", "user_001")

    assert result == ("旧的关系记忆文本", "")


@pytest.mark.asyncio
async def test_get_latest_relationship_memory_none():
    """无记录时返回 None"""
    mock_session = _make_mock_session()
    mock_result = MagicMock()
    mock_result.one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("app.orm.memory_crud.AsyncSessionLocal", return_value=mock_session):
        from app.orm.memory_crud import get_latest_relationship_memory
        result = await get_latest_relationship_memory("chiwei", "user_001")

    assert result is None
```

- [ ] **Step 6: 运行测试确认失败**

Run: `cd apps/agent-service && python -m pytest tests/unit/test_memory_crud.py::test_get_latest_relationship_memory_new_fields tests/unit/test_memory_crud.py::test_get_latest_relationship_memory_fallback_memory_text tests/unit/test_memory_crud.py::test_get_latest_relationship_memory_none -v`
Expected: FAIL

- [ ] **Step 7: 改写 get_latest_relationship_memory**

```python
# app/orm/memory_crud.py — 替换 get_latest_relationship_memory 函数

async def get_latest_relationship_memory(
    persona_id: str, user_id: str
) -> tuple[str, str] | None:
    """获取指定用户的最新关系记忆，返回 (core_facts, impression) 或 None

    若新字段为空，fallback 到 memory_text（作为 core_facts 返回）。
    """
    from app.orm.memory_models import RelationshipMemory

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(
                RelationshipMemory.core_facts,
                RelationshipMemory.impression,
                RelationshipMemory.memory_text,
            )
            .where(RelationshipMemory.persona_id == persona_id)
            .where(RelationshipMemory.user_id == user_id)
            .order_by(RelationshipMemory.created_at.desc())
            .limit(1)
        )
        row = result.one_or_none()
        if row is None:
            return None
        if row.core_facts or row.impression:
            return (row.core_facts, row.impression)
        # fallback: 旧记录只有 memory_text
        return (row.memory_text, "")
```

- [ ] **Step 8: 运行测试确认通过**

Run: `cd apps/agent-service && python -m pytest tests/unit/test_memory_crud.py::test_get_latest_relationship_memory_new_fields tests/unit/test_memory_crud.py::test_get_latest_relationship_memory_fallback_memory_text tests/unit/test_memory_crud.py::test_get_latest_relationship_memory_none -v`
Expected: PASS

- [ ] **Step 9: 改写 get_relationship_memories_for_users**

```python
# app/orm/memory_crud.py — 替换 get_relationship_memories_for_users 函数

async def get_relationship_memories_for_users(
    persona_id: str,
    user_ids: list[str],
) -> dict[str, tuple[str, str]]:
    """批量获取多个用户的最新关系记忆，返回 {user_id: (core_facts, impression)}"""
    from app.orm.memory_models import RelationshipMemory

    if not user_ids:
        return {}

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(
                RelationshipMemory.user_id,
                RelationshipMemory.core_facts,
                RelationshipMemory.impression,
                RelationshipMemory.memory_text,
            )
            .where(RelationshipMemory.persona_id == persona_id)
            .where(RelationshipMemory.user_id.in_(user_ids))
            .distinct(RelationshipMemory.user_id)
            .order_by(RelationshipMemory.user_id, RelationshipMemory.created_at.desc())
        )
        out: dict[str, tuple[str, str]] = {}
        for row in result.all():
            if row.core_facts or row.impression:
                out[row.user_id] = (row.core_facts, row.impression)
            else:
                out[row.user_id] = (row.memory_text, "")
        return out
```

- [ ] **Step 10: 运行全部 memory_crud 测试**

Run: `cd apps/agent-service && python -m pytest tests/unit/test_memory_crud.py -v`
Expected: ALL PASS

- [ ] **Step 11: Commit**

```bash
git add apps/agent-service/app/orm/memory_crud.py apps/agent-service/tests/unit/test_memory_crud.py
git commit -m "feat(relationship-memory): CRUD 适配 core_facts/impression/version"
```

---

### Task 3: memory_context 注入格式

**Files:**
- Modify: `apps/agent-service/app/services/memory_context.py:74-82`
- Modify: `apps/agent-service/tests/unit/test_memory_context.py`

- [ ] **Step 1: 写测试**

```python
# tests/unit/test_memory_context.py — 追加到文件末尾

@pytest.mark.asyncio
async def test_relationship_memory_injection_core_facts_and_impression():
    """关系记忆应以 [事实] + [印象] 格式注入"""
    with patch(
        "app.services.memory_context._build_life_state",
        new_callable=AsyncMock,
        return_value="",
    ), patch(
        "app.services.memory_context.get_latest_relationship_memory",
        new_callable=AsyncMock,
        return_value=("群昵称 crgg，经常被泼洗脚水", "脑回路清奇但偶尔挺好笑"),
    ):
        from app.services.memory_context import build_inner_context

        result = await build_inner_context(
            chat_id="chat_001",
            chat_type="group",
            user_ids=["u1"],
            trigger_user_id="u1",
            trigger_username="crgg",
            persona_id="chiwei",
            chat_name="KA群",
        )

    assert "关于 crgg" in result
    assert "[事实] 群昵称 crgg" in result
    assert "[印象] 脑回路清奇" in result


@pytest.mark.asyncio
async def test_relationship_memory_injection_no_memory():
    """无关系记忆时不注入"""
    with patch(
        "app.services.memory_context._build_life_state",
        new_callable=AsyncMock,
        return_value="",
    ), patch(
        "app.services.memory_context.get_latest_relationship_memory",
        new_callable=AsyncMock,
        return_value=None,
    ):
        from app.services.memory_context import build_inner_context

        result = await build_inner_context(
            chat_id="chat_001",
            chat_type="group",
            user_ids=["u1"],
            trigger_user_id="u1",
            trigger_username="crgg",
            persona_id="chiwei",
            chat_name="KA群",
        )

    assert "[事实]" not in result
    assert "[印象]" not in result
```

- [ ] **Step 2: 修改注入代码**

```python
# app/services/memory_context.py — 替换 74-82 行的关系记忆注入段

    # === 关系记忆（对当前对话者的印象）===
    if trigger_user_id and trigger_user_id != "__proactive__":
        from app.orm.memory_crud import get_latest_relationship_memory
        from app.orm.crud import get_username

        rel_memory = await get_latest_relationship_memory(persona_id, trigger_user_id)
        if rel_memory:
            core_facts, impression = rel_memory
            name = trigger_username or await get_username(trigger_user_id) or trigger_user_id[:6]
            parts = [f"关于 {name}："]
            if core_facts:
                parts.append(f"[事实] {core_facts}")
            if impression:
                parts.append(f"[印象] {impression}")
            sections.append("\n".join(parts))
```

- [ ] **Step 3: 运行测试**

Run: `cd apps/agent-service && python -m pytest tests/unit/test_memory_context.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add apps/agent-service/app/services/memory_context.py apps/agent-service/tests/unit/test_memory_context.py
git commit -m "feat(relationship-memory): inner_context 注入 [事实]+[印象] 格式"
```

---

### Task 4: relationship_extract 提取函数适配

**Files:**
- Modify: `apps/agent-service/app/services/relationship_memory.py`

- [ ] **Step 1: 改写 extract_relationship_updates 函数**

```python
# app/services/relationship_memory.py — 完整替换文件内容

"""关系记忆提取 — afterthought 碎片生成后，判断是否需要更新 per-user 关系记忆"""

import json
import logging

from app.agents.infra.langfuse_client import get_prompt
from app.agents.infra.model_builder import ModelBuilder
from app.config.config import settings
from app.orm.crud import get_bot_persona, get_username
from app.orm.memory_crud import (
    get_relationship_memories_for_users,
    save_relationship_memory,
)

logger = logging.getLogger(__name__)


async def extract_relationship_updates(
    persona_id: str,
    chat_id: str,
    user_ids: list[str],
    messages_timeline: str,
) -> None:
    """从一段对话中提取关系记忆更新

    在 afterthought 生成 conversation 碎片后调用。
    让 LLM 以角色视角判断对话中涉及的人是否有关系变化，有则写入 relationship_memory。
    """
    if not user_ids:
        return

    # 获取 persona 信息（注入角色视角）
    persona = await get_bot_persona(persona_id)
    persona_name = persona.display_name if persona else persona_id
    persona_lite = persona.persona_lite if persona else ""

    # 获取当前关系记忆
    current_memories = await get_relationship_memories_for_users(persona_id, user_ids)

    # 构建当前记忆上下文（分 core_facts / impression）
    core_facts_lines = []
    impression_lines = []
    for uid in user_ids:
        name = await get_username(uid) or uid[:6]
        mem = current_memories.get(uid)
        if mem:
            core_facts, impression = mem
            core_facts_lines.append(f"- {name}({uid}): {core_facts or '（无）'}")
            impression_lines.append(f"- {name}({uid}): {impression or '（无）'}")
        else:
            core_facts_lines.append(f"- {name}({uid}): （第一次互动）")
            impression_lines.append(f"- {name}({uid}): （第一次互动）")

    prompt = get_prompt("relationship_extract")
    compiled = prompt.compile(
        persona_name=persona_name,
        persona_lite=persona_lite,
        messages=messages_timeline,
        current_core_facts="\n".join(core_facts_lines),
        current_impression="\n".join(impression_lines),
    )

    model = await ModelBuilder.build_chat_model(settings.diary_model)
    response = await model.ainvoke([{"role": "user", "content": compiled}])

    content = response.content
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        ).strip()

    if not content or content.strip() == "[]":
        logger.info(f"[{persona_id}] No relationship updates for chat {chat_id}")
        return

    # 解析 JSON 输出
    try:
        updates = json.loads(content)
    except json.JSONDecodeError:
        logger.warning(f"[{persona_id}] Failed to parse relationship extract: {content[:200]}")
        return

    for item in updates:
        if not isinstance(item, dict):
            continue
        uid = item.get("user_id", "")
        name = item.get("user_name", "") or await get_username(uid) or uid[:6]
        core_facts = item.get("core_facts", "")
        impression = item.get("impression", "")
        if uid and (core_facts or impression):
            await save_relationship_memory(
                persona_id=persona_id,
                user_id=uid,
                user_name=name,
                core_facts=core_facts,
                impression=impression,
                source="afterthought",
            )
            logger.info(
                f"[{persona_id}] Relationship updated for {name}: "
                f"facts={core_facts[:30]}... impression={impression[:30]}..."
            )
```

- [ ] **Step 2: 验证 import 无误**

Run: `cd apps/agent-service && python -c "from app.services.relationship_memory import extract_relationship_updates; print('OK')"`
Expected: OK

- [ ] **Step 3: Commit**

```bash
git add apps/agent-service/app/services/relationship_memory.py
git commit -m "feat(relationship-memory): 提取函数注入 persona_lite + 适配新字段"
```

---

### Task 5: rebuild 端点

**Files:**
- Modify: `apps/agent-service/app/api/router.py`
- Modify: `apps/agent-service/app/services/relationship_memory.py`

- [ ] **Step 1: 在 relationship_memory.py 中添加 rebuild 核心函数**

```python
# app/services/relationship_memory.py — 追加到文件末尾

async def rebuild_relationship_memory_for_user(
    persona_id: str,
    user_id: str,
    messages: list,
    persona_name: str,
    persona_lite: str,
    batch_size: int = 50,
) -> dict:
    """为单个 (persona_id, user_id) 渐进式重建关系记忆

    Args:
        messages: 该用户参与的 ConversationMessage 列表（按时间正序）
        persona_name: 角色显示名
        persona_lite: 角色简介
        batch_size: 每批消息数量

    Returns:
        {"batches": int, "core_facts": str, "impression": str}
    """
    from app.orm.crud import get_username
    from datetime import datetime, timezone

    user_name = await get_username(user_id) or user_id[:6]
    current_core_facts = ""
    current_impression = ""
    batch_count = 0

    # 按 batch_size 分批
    for i in range(0, len(messages), batch_size):
        batch = messages[i : i + batch_size]
        batch_count += 1

        # 格式化时间线
        lines = []
        for msg in batch:
            msg_time = datetime.fromtimestamp(msg.create_time / 1000, tz=timezone.utc)
            time_str = msg_time.strftime("%H:%M")
            if msg.role == "assistant":
                speaker = persona_name
            else:
                name = await get_username(msg.user_id) or msg.user_id[:6]
                speaker = name
            content = msg.content or ""
            if content.strip():
                lines.append(f"[{time_str}] {speaker}: {content[:200]}")

        if not lines:
            continue

        timeline = "\n".join(lines)

        # 构建 prompt 上下文
        cf_line = f"- {user_name}({user_id}): {current_core_facts or '（第一次互动）'}"
        im_line = f"- {user_name}({user_id}): {current_impression or '（第一次互动）'}"

        prompt = get_prompt("relationship_extract")
        compiled = prompt.compile(
            persona_name=persona_name,
            persona_lite=persona_lite,
            messages=timeline,
            current_core_facts=cf_line,
            current_impression=im_line,
        )

        model = await ModelBuilder.build_chat_model(settings.diary_model)
        response = await model.ainvoke([{"role": "user", "content": compiled}])

        content_text = response.content
        if isinstance(content_text, list):
            content_text = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content_text
            ).strip()

        if not content_text or content_text.strip() == "[]":
            continue

        try:
            updates = json.loads(content_text)
        except json.JSONDecodeError:
            logger.warning(f"[rebuild] Failed to parse batch {batch_count}: {content_text[:100]}")
            continue

        # 取该 user 的更新
        for item in updates:
            if not isinstance(item, dict):
                continue
            if item.get("user_id") == user_id:
                current_core_facts = item.get("core_facts", current_core_facts)
                current_impression = item.get("impression", current_impression)
                break

        # 每批都存库（version 自增，保留审计链）
        if current_core_facts or current_impression:
            await save_relationship_memory(
                persona_id=persona_id,
                user_id=user_id,
                user_name=user_name,
                core_facts=current_core_facts,
                impression=current_impression,
                source="rebuild",
            )

    return {
        "batches": batch_count,
        "core_facts": current_core_facts,
        "impression": current_impression,
    }
```

- [ ] **Step 2: 在 router.py 中添加 rebuild 端点**

```python
# app/api/router.py — 在 admin 端点区域追加

from pydantic import BaseModel


class RebuildRelationshipMemoryRequest(BaseModel):
    persona_ids: list[str]
    chat_ids: list[str]
    start_time: str  # ISO 8601 格式
    end_time: str  # ISO 8601 格式
    batch_size: int = 50


@api_router.post("/admin/rebuild-relationship-memory", tags=["Admin"])
async def rebuild_relationship_memory(req: RebuildRelationshipMemoryRequest):
    """批量回溯重建关系记忆

    从 conversation_messages 按 user_id 分组，渐进式提取 core_facts + impression。
    耗时较长，建议单次限制 persona/chat 范围。
    """
    from datetime import datetime, timezone
    from app.orm.crud import get_bot_persona, get_chat_messages_in_range
    from app.services.relationship_memory import rebuild_relationship_memory_for_user

    start_dt = datetime.fromisoformat(req.start_time)
    end_dt = datetime.fromisoformat(req.end_time)
    start_ts = int(start_dt.timestamp() * 1000)
    end_ts = int(end_dt.timestamp() * 1000)

    results = []

    for chat_id in req.chat_ids:
        # 查消息
        messages = await get_chat_messages_in_range(chat_id, start_ts, end_ts, limit=10000)
        if not messages:
            continue

        # 按 user_id 分组（只取 role=user 的消息对应的 user）
        user_ids = list({m.user_id for m in messages if m.role == "user" and m.user_id and m.user_id != "__proactive__"})

        for persona_id in req.persona_ids:
            persona = await get_bot_persona(persona_id)
            if not persona:
                continue
            persona_name = persona.display_name
            persona_lite = persona.persona_lite or ""

            for user_id in user_ids:
                # 筛选该 user 参与的消息（包含该 user 发的和 bot 回的）
                user_messages = [m for m in messages if m.user_id == user_id or m.role == "assistant"]
                user_messages.sort(key=lambda m: m.create_time)

                result = await rebuild_relationship_memory_for_user(
                    persona_id=persona_id,
                    user_id=user_id,
                    messages=user_messages,
                    persona_name=persona_name,
                    persona_lite=persona_lite,
                    batch_size=req.batch_size,
                )
                from app.orm.crud import get_username
                user_name = await get_username(user_id) or user_id[:6]

                results.append({
                    "persona_id": persona_id,
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "user_name": user_name,
                    **result,
                })

    return {"results": results}
```

- [ ] **Step 3: 验证 import 无误**

Run: `cd apps/agent-service && python -c "from app.api.router import api_router; print('OK')"`
Expected: OK

- [ ] **Step 4: Commit**

```bash
git add apps/agent-service/app/services/relationship_memory.py apps/agent-service/app/api/router.py
git commit -m "feat(relationship-memory): 添加 rebuild 端点 + 渐进式重建函数"
```

---

### Task 6: relationship_extract prompt v2（Langfuse）

**Files:**
- Langfuse prompt: `relationship_extract`

这一步需要手动在 Langfuse 中创建/更新 prompt。

- [ ] **Step 1: 编写 prompt v2 内容**

Langfuse prompt name: `relationship_extract`
Label: `production`（或先用泳道 label 测试）

变量：`{{persona_name}}`, `{{persona_lite}}`, `{{messages}}`, `{{current_core_facts}}`, `{{current_impression}}`

Prompt 内容（需根据实际测试效果微调）：

```
你是{{persona_name}}。以下是你的性格：
{{persona_lite}}

你刚和大家聊了一会儿天。回忆一下刚才的对话，更新你对每个人的认识。

## 你现在记得的事实
{{current_core_facts}}

## 你现在对他们的印象
{{current_impression}}

## 刚才的对话
{{messages}}

---

请根据上面的对话，更新你对每个人的记忆。

### 输出规则
- core_facts：关于这个人的客观事实（昵称、城市、工作、习惯、经历等）。只记你确定的事，不要猜。
- impression：你对这个人的主观感觉和印象。用你自己的语气写，像在心里想的那样。

### 记忆原则
- 互怼、吐槽是亲近的表现。经常来找你聊天的人，你心里知道是在闹着玩。
- 如果旧印象和实际互动对不上，按你真实的感觉重写，不要在旧印象上加码。
- 如果最近没什么互动，印象可以变模糊、淡化。
- 保持简洁，每个人的 core_facts 和 impression 各控制在 2-4 句话。

### 输出格式
输出 JSON 数组，只包含这次对话中你对其有新认识或印象变化的人。如果没有变化输出空数组 []。

```json
[
  {
    "user_id": "用户ID",
    "user_name": "用户名",
    "core_facts": "事实性知识...",
    "impression": "你的主观印象..."
  }
]
```
```

- [ ] **Step 2: 在 Langfuse 中创建 prompt**

通过 Langfuse 技能或 UI 创建 `relationship_extract` v2 prompt，设置 label 为泳道名（用于测试）或 `production`。

- [ ] **Step 3: Commit 空步骤（prompt 在 Langfuse 中管理）**

无代码变更，跳过 commit。

---

### Task 7: DDL 提交 + 清空历史

- [ ] **Step 1: 提交 DDL 加列**

通过 `/ops-db` 提交：

```sql
ALTER TABLE relationship_memory
  ALTER COLUMN memory_text SET DEFAULT '',
  ADD COLUMN version INT NOT NULL DEFAULT 1,
  ADD COLUMN core_facts TEXT NOT NULL DEFAULT '',
  ADD COLUMN impression TEXT NOT NULL DEFAULT '';
```

等待审批执行。

- [ ] **Step 2: 清空历史负面记忆**

DDL 加列执行完成后，通过 `/ops-db` 提交：

```sql
DELETE FROM relationship_memory;
```

等待审批执行。

---

### Task 8: 部署 + 批量回溯

- [ ] **Step 1: 推送代码**

```bash
git push origin feat/relationship-memory-redesign
```

- [ ] **Step 2: 部署 agent-service 到测试泳道**

```bash
make deploy APP=agent-service LANE=rel-mem GIT_REF=feat/relationship-memory-redesign
```

- [ ] **Step 3: 本地脚本调 rebuild 端点**

编写临时脚本，调用 `POST /admin/rebuild-relationship-memory`：
- 赤尾 KA 群：`persona_ids=["chiwei"]`, `chat_ids=["<赤尾KA群chat_id>"]`, 时间范围近几个月
- 绫奈/千千 KA 群：`persona_ids=["ayane"]` / `persona_ids=["chichi"]`, 各自的 chat_id

- [ ] **Step 4: 验证结果**

通过 `/ops-db` 查询新记忆：

```sql
SELECT persona_id, user_id, user_name, version, core_facts, impression, source, created_at
FROM relationship_memory
ORDER BY persona_id, user_id, version DESC
LIMIT 50;
```

确认 core_facts 和 impression 内容合理、不再全面负面。

- [ ] **Step 5: 绑定 dev bot 端到端测试**

```
/ops bind TYPE=bot KEY=dev LANE=rel-mem
```

在飞书 dev bot 发消息，验证 inner_context 中关系记忆格式正确（`[事实]` + `[印象]`）。

- [ ] **Step 6: 清理测试环境**

```
/ops unbind TYPE=bot KEY=dev
make undeploy APP=agent-service LANE=rel-mem
```
