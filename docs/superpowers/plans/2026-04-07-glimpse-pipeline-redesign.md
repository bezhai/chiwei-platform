# Glimpse 管线重设计 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Glimpse 管线从 Life Engine tick 中解耦为独立 cron，引入 append-only 状态追踪实现增量去重和递进式观察，搭话改为 dry-run 只记录不发送。

**Architecture:** 新建 `glimpse_state` 表存储 per (persona, chat) 的观察状态。新建 `glimpse_worker.py` 作为独立 cron（每 5 分钟），自查 Life Engine 状态决定是否执行。重写 `glimpse.py` 核心流程为：读状态 → 拉增量消息 → LLM 观察（传入上次感想）→ 写碎片 + 状态。

**Tech Stack:** Python 3.12, SQLAlchemy (async), ARQ cron, FastAPI, Langfuse, pytest

**Spec:** `docs/superpowers/specs/2026-04-07-glimpse-pipeline-redesign.md`

---

### Task 1: GlimpseState ORM 模型 + CRUD

**Files:**
- Modify: `apps/agent-service/app/orm/memory_models.py:62` (在 MemoryEntity 前插入)
- Modify: `apps/agent-service/app/orm/memory_crud.py` (文件末尾追加)
- Test: `apps/agent-service/tests/unit/test_glimpse_state_crud.py` (新建)

- [ ] **Step 1: 写 GlimpseState CRUD 的测试**

```python
# tests/unit/test_glimpse_state_crud.py
"""glimpse_state CRUD 单元测试"""
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MODULE = "app.orm.memory_crud"


@pytest.mark.asyncio
async def test_get_latest_glimpse_state_returns_latest():
    """有记录时返回最新一条"""
    fake_state = MagicMock(
        persona_id="akao-001",
        chat_id="oc_test",
        last_seen_msg_time=1000,
        observation="上次的感想",
    )

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = fake_state
    mock_session = AsyncMock()
    mock_session.execute.return_value = mock_result

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch(f"{MODULE}.AsyncSessionLocal", return_value=mock_ctx):
        from app.orm.memory_crud import get_latest_glimpse_state

        result = await get_latest_glimpse_state("akao-001", "oc_test")

    assert result is not None
    assert result.last_seen_msg_time == 1000
    assert result.observation == "上次的感想"


@pytest.mark.asyncio
async def test_get_latest_glimpse_state_returns_none():
    """无记录时返回 None"""
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session = AsyncMock()
    mock_session.execute.return_value = mock_result

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch(f"{MODULE}.AsyncSessionLocal", return_value=mock_ctx):
        from app.orm.memory_crud import get_latest_glimpse_state

        result = await get_latest_glimpse_state("akao-001", "oc_test")

    assert result is None


@pytest.mark.asyncio
async def test_insert_glimpse_state():
    """插入新状态记录"""
    mock_session = AsyncMock()
    mock_session.commit = AsyncMock()

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch(f"{MODULE}.AsyncSessionLocal", return_value=mock_ctx):
        from app.orm.memory_crud import insert_glimpse_state

        await insert_glimpse_state(
            persona_id="akao-001",
            chat_id="oc_test",
            last_seen_msg_time=2000,
            observation="新感想",
        )

    mock_session.add.assert_called_once()
    added = mock_session.add.call_args[0][0]
    assert added.persona_id == "akao-001"
    assert added.chat_id == "oc_test"
    assert added.last_seen_msg_time == 2000
    assert added.observation == "新感想"
    mock_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_get_last_bot_reply_time_has_reply():
    """有 assistant 回复时返回最大 create_time"""
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = 5000
    mock_session = AsyncMock()
    mock_session.execute.return_value = mock_result

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch(f"{MODULE}.AsyncSessionLocal", return_value=mock_ctx):
        from app.orm.memory_crud import get_last_bot_reply_time

        result = await get_last_bot_reply_time("oc_test")

    assert result == 5000


@pytest.mark.asyncio
async def test_get_last_bot_reply_time_no_reply():
    """无 assistant 回复时返回 0"""
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session = AsyncMock()
    mock_session.execute.return_value = mock_result

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch(f"{MODULE}.AsyncSessionLocal", return_value=mock_ctx):
        from app.orm.memory_crud import get_last_bot_reply_time

        result = await get_last_bot_reply_time("oc_test")

    assert result == 0
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_glimpse_state_crud.py -v
```

预期：FAIL — `ImportError: cannot import name 'get_latest_glimpse_state'`

- [ ] **Step 3: 新增 GlimpseState 模型**

在 `apps/agent-service/app/orm/memory_models.py` 的 `MemoryEntity` 类前插入：

```python
class GlimpseState(Base):
    """Glimpse 观察状态 — append-only，每次观察 INSERT 一行"""

    __tablename__ = "glimpse_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    persona_id: Mapped[str] = mapped_column(String(50), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(100), nullable=False)
    last_seen_msg_time: Mapped[int] = mapped_column(BigInteger, nullable=False)
    observation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```

- [ ] **Step 4: 新增 CRUD 函数**

在 `apps/agent-service/app/orm/memory_crud.py` 文件末尾追加：

```python
from .memory_models import GlimpseState


async def get_latest_glimpse_state(
    persona_id: str, chat_id: str
) -> GlimpseState | None:
    """查最新一行 glimpse 状态，不存在返回 None"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(GlimpseState)
            .where(GlimpseState.persona_id == persona_id)
            .where(GlimpseState.chat_id == chat_id)
            .order_by(GlimpseState.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def insert_glimpse_state(
    persona_id: str,
    chat_id: str,
    last_seen_msg_time: int,
    observation: str,
) -> None:
    """INSERT 一行新 glimpse 状态"""
    async with AsyncSessionLocal() as session:
        session.add(
            GlimpseState(
                persona_id=persona_id,
                chat_id=chat_id,
                last_seen_msg_time=last_seen_msg_time,
                observation=observation,
            )
        )
        await session.commit()


async def get_last_bot_reply_time(chat_id: str) -> int:
    """查指定群最近一次 assistant 回复的 create_time（毫秒），无则返回 0"""
    from sqlalchemy import func as sa_func

    from app.orm.models import ConversationMessage

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(sa_func.max(ConversationMessage.create_time)).where(
                ConversationMessage.chat_id == chat_id,
                ConversationMessage.role == "assistant",
            )
        )
        return result.scalar_one_or_none() or 0
```

- [ ] **Step 5: 运行测试确认通过**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_glimpse_state_crud.py -v
```

预期：5 tests PASSED

- [ ] **Step 6: 提交**

```bash
git add apps/agent-service/app/orm/memory_models.py apps/agent-service/app/orm/memory_crud.py apps/agent-service/tests/unit/test_glimpse_state_crud.py
git commit -m "feat(glimpse): GlimpseState 模型 + CRUD（append-only 状态追踪）"
```

---

### Task 2: 改造 `get_unseen_messages` 签名

**Files:**
- Modify: `apps/agent-service/app/workers/proactive_scanner.py:51-84` (get_unseen_messages 函数)
- Modify: `apps/agent-service/app/workers/proactive_scanner.py:288` (run_proactive_scan 调用处)
- Test: `apps/agent-service/tests/unit/test_proactive_scanner.py:21-70` (适配新签名)

- [ ] **Step 1: 更新 get_unseen_messages 测试适配新签名**

`tests/unit/test_proactive_scanner.py` 中两个测试的调用从 `get_unseen_messages("test_chat", "akao")` 改为 `get_unseen_messages("test_chat", after=0)`：

在 `test_get_unseen_messages_has_messages` 中：
```python
        result = await get_unseen_messages("test_chat", after=0)
```

在 `test_get_unseen_messages_empty` 中：
```python
        result = await get_unseen_messages("test_chat", after=0)
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_proactive_scanner.py::test_get_unseen_messages_has_messages tests/unit/test_proactive_scanner.py::test_get_unseen_messages_empty -v
```

预期：FAIL — `TypeError: got an unexpected keyword argument 'after'`

- [ ] **Step 3: 重写 `get_unseen_messages`**

将 `apps/agent-service/app/workers/proactive_scanner.py` 中的 `get_unseen_messages` 函数替换为：

```python
async def get_unseen_messages(chat_id: str, after: int = 0, limit: int = 30) -> list[ConversationMessage]:
    """获取指定时间戳之后的用户消息

    Args:
        chat_id: 群 ID
        after: 只返回 create_time > after 的消息（毫秒时间戳），0 表示不限
        limit: 最多返回 N 条（取最新的）
    """
    async with AsyncSessionLocal() as session:
        stmt = (
            select(ConversationMessage)
            .where(
                ConversationMessage.chat_id == chat_id,
                ConversationMessage.role == "user",
                ConversationMessage.user_id != PROACTIVE_USER_ID,
                ConversationMessage.create_time > after,
            )
            .order_by(ConversationMessage.create_time.desc())
            .limit(limit)
        )

        result = await session.execute(stmt)
        rows = list(result.scalars().all())
        rows.reverse()  # 恢复时间正序
        return rows
```

- [ ] **Step 4: 适配 `run_proactive_scan` 调用处**

`apps/agent-service/app/workers/proactive_scanner.py` line 288，将：

```python
    messages = await get_unseen_messages(chat_id, persona_id)
```

改为：

```python
    messages = await get_unseen_messages(chat_id)
```

注：`run_proactive_scan` 目前已 disabled（cron 注释掉了），这里只做签名适配。不传 `after` 默认为 0，等价于原来的无限制行为。

- [ ] **Step 5: 运行全部 proactive_scanner 测试**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_proactive_scanner.py -v
```

预期：全部 PASSED

- [ ] **Step 6: 提交**

```bash
git add apps/agent-service/app/workers/proactive_scanner.py apps/agent-service/tests/unit/test_proactive_scanner.py
git commit -m "refactor(glimpse): get_unseen_messages 改为 after 时间戳过滤"
```

---

### Task 3: 重写 `run_glimpse` 核心流程

**Files:**
- Rewrite: `apps/agent-service/app/services/glimpse.py`
- Test: `apps/agent-service/tests/unit/test_glimpse.py` (重写)

- [ ] **Step 1: 重写 glimpse 测试**

用新测试完整替换 `tests/unit/test_glimpse.py`：

```python
# tests/unit/test_glimpse.py
"""Glimpse 管线单元测试（重设计版）"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CST = timezone(timedelta(hours=8))
MODULE = "app.services.glimpse"


def _make_msg(user_id="u1", create_time=None, content="hello", msg_id="m1"):
    msg = MagicMock()
    msg.user_id = user_id
    msg.create_time = create_time or int(datetime(2026, 4, 7, 14, 0, tzinfo=CST).timestamp() * 1000)
    msg.content = f'{{"v":2,"text":"{content}","items":[]}}'
    msg.chat_id = "oc_test"
    msg.role = "user"
    msg.message_id = msg_id
    return msg


@pytest.mark.asyncio
async def test_glimpse_skips_quiet_hours():
    """安静时段不窥屏"""
    from app.services.glimpse import run_glimpse

    quiet_time = datetime(2026, 4, 7, 2, 0, tzinfo=CST)
    with patch(f"{MODULE}._now_cst", return_value=quiet_time):
        result = await run_glimpse("akao-001")
        assert result == "skipped:quiet_hours"


@pytest.mark.asyncio
async def test_glimpse_skips_no_new_messages():
    """没有增量消息 → 跳过，不写状态"""
    from app.services.glimpse import run_glimpse

    normal_time = datetime(2026, 4, 7, 14, 0, tzinfo=CST)
    with (
        patch(f"{MODULE}._now_cst", return_value=normal_time),
        patch(f"{MODULE}._pick_group", new_callable=AsyncMock, return_value="oc_test"),
        patch(f"{MODULE}.get_latest_glimpse_state", new_callable=AsyncMock, return_value=None),
        patch(f"{MODULE}.get_last_bot_reply_time", new_callable=AsyncMock, return_value=0),
        patch(f"{MODULE}.get_unseen_messages", new_callable=AsyncMock, return_value=[]),
        patch(f"{MODULE}.insert_glimpse_state", new_callable=AsyncMock) as mock_insert,
    ):
        result = await run_glimpse("akao-001")
        assert result == "skipped:no_messages"
        mock_insert.assert_not_called()


@pytest.mark.asyncio
async def test_glimpse_uses_effective_after():
    """effective_after = max(last_seen_msg_time, last_bot_reply_time)"""
    from app.services.glimpse import run_glimpse

    normal_time = datetime(2026, 4, 7, 14, 0, tzinfo=CST)

    # last_seen=1000, bot_reply=5000 → should pass after=5000
    mock_state = MagicMock(last_seen_msg_time=1000, observation="旧感想")

    with (
        patch(f"{MODULE}._now_cst", return_value=normal_time),
        patch(f"{MODULE}._pick_group", new_callable=AsyncMock, return_value="oc_test"),
        patch(f"{MODULE}.get_latest_glimpse_state", new_callable=AsyncMock, return_value=mock_state),
        patch(f"{MODULE}.get_last_bot_reply_time", new_callable=AsyncMock, return_value=5000),
        patch(f"{MODULE}.get_unseen_messages", new_callable=AsyncMock, return_value=[]) as mock_get,
    ):
        await run_glimpse("akao-001")
        mock_get.assert_called_once_with("oc_test", after=5000)


@pytest.mark.asyncio
async def test_glimpse_creates_fragment_and_state():
    """有趣消息 → 创建碎片 + 写 glimpse_state"""
    from app.services.glimpse import run_glimpse

    normal_time = datetime(2026, 4, 7, 14, 0, tzinfo=CST)
    mock_msg = _make_msg()

    llm_response = json.dumps({
        "interesting": True,
        "observation": "群里在聊新番",
        "want_to_speak": False,
    })

    with (
        patch(f"{MODULE}._now_cst", return_value=normal_time),
        patch(f"{MODULE}._pick_group", new_callable=AsyncMock, return_value="oc_test"),
        patch(f"{MODULE}.get_latest_glimpse_state", new_callable=AsyncMock, return_value=None),
        patch(f"{MODULE}.get_last_bot_reply_time", new_callable=AsyncMock, return_value=0),
        patch(f"{MODULE}.get_unseen_messages", new_callable=AsyncMock, return_value=[mock_msg]),
        patch(f"{MODULE}._format_messages", new_callable=AsyncMock, return_value="[14:00] someone: hello"),
        patch(f"{MODULE}._call_glimpse_llm", new_callable=AsyncMock, return_value=llm_response),
        patch(f"{MODULE}.create_fragment", new_callable=AsyncMock) as mock_frag,
        patch(f"{MODULE}.insert_glimpse_state", new_callable=AsyncMock) as mock_state,
        patch(f"{MODULE}._get_persona_info", new_callable=AsyncMock, return_value=("赤尾", "")),
        patch(f"{MODULE}._get_group_name", new_callable=AsyncMock, return_value="番剧群"),
    ):
        result = await run_glimpse("akao-001")

        assert result == "fragment_created"
        mock_frag.assert_called_once()
        assert mock_frag.call_args[0][0].grain == "glimpse"

        mock_state.assert_called_once()
        call_kwargs = mock_state.call_args[1]
        assert call_kwargs["persona_id"] == "akao-001"
        assert call_kwargs["chat_id"] == "oc_test"
        assert call_kwargs["last_seen_msg_time"] == mock_msg.create_time
        assert "群里在聊新番" in call_kwargs["observation"]


@pytest.mark.asyncio
async def test_glimpse_passes_last_observation_to_llm():
    """递进式观察：上次感想传入 LLM"""
    from app.services.glimpse import run_glimpse

    normal_time = datetime(2026, 4, 7, 14, 0, tzinfo=CST)
    mock_msg = _make_msg()
    mock_state = MagicMock(last_seen_msg_time=500, observation="上次看到他们在聊火锅")

    llm_response = json.dumps({
        "interesting": False,
    })

    with (
        patch(f"{MODULE}._now_cst", return_value=normal_time),
        patch(f"{MODULE}._pick_group", new_callable=AsyncMock, return_value="oc_test"),
        patch(f"{MODULE}.get_latest_glimpse_state", new_callable=AsyncMock, return_value=mock_state),
        patch(f"{MODULE}.get_last_bot_reply_time", new_callable=AsyncMock, return_value=0),
        patch(f"{MODULE}.get_unseen_messages", new_callable=AsyncMock, return_value=[mock_msg]),
        patch(f"{MODULE}._format_messages", new_callable=AsyncMock, return_value="[14:00] someone: hello"),
        patch(f"{MODULE}._call_glimpse_llm", new_callable=AsyncMock, return_value=llm_response) as mock_llm,
        patch(f"{MODULE}._get_persona_info", new_callable=AsyncMock, return_value=("赤尾", "")),
        patch(f"{MODULE}._get_group_name", new_callable=AsyncMock, return_value="番剧群"),
        patch(f"{MODULE}.insert_glimpse_state", new_callable=AsyncMock),
    ):
        await run_glimpse("akao-001")
        # last_observation 应该作为参数传给 _call_glimpse_llm
        call_kwargs = mock_llm.call_args[1]
        assert call_kwargs["last_observation"] == "上次看到他们在聊火锅"


@pytest.mark.asyncio
async def test_glimpse_want_to_speak_dry_run():
    """想搭话 → dry-run 只记录，不调 submit_proactive_request"""
    from app.services.glimpse import run_glimpse

    normal_time = datetime(2026, 4, 7, 14, 0, tzinfo=CST)
    mock_msg = _make_msg()

    llm_response = json.dumps({
        "interesting": True,
        "observation": "他们在讨论我喜欢的东西",
        "want_to_speak": True,
        "stimulus": "好想聊聊",
        "target_message_id": "m1",
    })

    with (
        patch(f"{MODULE}._now_cst", return_value=normal_time),
        patch(f"{MODULE}._pick_group", new_callable=AsyncMock, return_value="oc_test"),
        patch(f"{MODULE}.get_latest_glimpse_state", new_callable=AsyncMock, return_value=None),
        patch(f"{MODULE}.get_last_bot_reply_time", new_callable=AsyncMock, return_value=0),
        patch(f"{MODULE}.get_unseen_messages", new_callable=AsyncMock, return_value=[mock_msg]),
        patch(f"{MODULE}._format_messages", new_callable=AsyncMock, return_value="[14:00] someone: 话题"),
        patch(f"{MODULE}._call_glimpse_llm", new_callable=AsyncMock, return_value=llm_response),
        patch(f"{MODULE}.create_fragment", new_callable=AsyncMock),
        patch(f"{MODULE}.insert_glimpse_state", new_callable=AsyncMock) as mock_state,
        patch(f"{MODULE}._get_persona_info", new_callable=AsyncMock, return_value=("赤尾", "")),
        patch(f"{MODULE}._get_group_name", new_callable=AsyncMock, return_value="番剧群"),
    ):
        result = await run_glimpse("akao-001")

        assert result == "fragment_created"
        # glimpse_state.observation 应包含 want_to_speak 信息
        call_kwargs = mock_state.call_args[1]
        assert "[want_to_speak]" in call_kwargs["observation"]
        assert "好想聊聊" in call_kwargs["observation"]


@pytest.mark.asyncio
async def test_glimpse_not_interesting_still_writes_state():
    """不有趣 → 不创建碎片，但仍写 glimpse_state（记录看到了哪里）"""
    from app.services.glimpse import run_glimpse

    normal_time = datetime(2026, 4, 7, 14, 0, tzinfo=CST)
    mock_msg = _make_msg()

    llm_response = json.dumps({
        "interesting": False,
        "observation": "",
        "want_to_speak": False,
    })

    with (
        patch(f"{MODULE}._now_cst", return_value=normal_time),
        patch(f"{MODULE}._pick_group", new_callable=AsyncMock, return_value="oc_test"),
        patch(f"{MODULE}.get_latest_glimpse_state", new_callable=AsyncMock, return_value=None),
        patch(f"{MODULE}.get_last_bot_reply_time", new_callable=AsyncMock, return_value=0),
        patch(f"{MODULE}.get_unseen_messages", new_callable=AsyncMock, return_value=[mock_msg]),
        patch(f"{MODULE}._format_messages", new_callable=AsyncMock, return_value="[14:00] someone: hello"),
        patch(f"{MODULE}._call_glimpse_llm", new_callable=AsyncMock, return_value=llm_response),
        patch(f"{MODULE}.create_fragment", new_callable=AsyncMock) as mock_frag,
        patch(f"{MODULE}.insert_glimpse_state", new_callable=AsyncMock) as mock_state,
        patch(f"{MODULE}._get_persona_info", new_callable=AsyncMock, return_value=("赤尾", "")),
        patch(f"{MODULE}._get_group_name", new_callable=AsyncMock, return_value="番剧群"),
    ):
        result = await run_glimpse("akao-001")

        assert result == "skipped:not_interesting"
        mock_frag.assert_not_called()
        # 即使不有趣，也要记录看到了哪里，避免下次重复拉同一批
        mock_state.assert_called_once()
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_glimpse.py -v
```

预期：多数 FAIL — 新 mock 目标（`get_latest_glimpse_state` 等）不存在于 glimpse 模块

- [ ] **Step 3: 重写 `glimpse.py`**

完整替换 `apps/agent-service/app/services/glimpse.py`：

```python
"""Glimpse 管线 — 赤尾"刷手机"时的窥屏观察（v2: 独立调度 + 增量去重 + 递进观察）

流程：读状态 → 拉增量消息 → LLM 观察（传入上次感想）→ 写碎片 + 状态
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from app.config.config import settings
from app.orm.memory_crud import (
    create_fragment,
    get_last_bot_reply_time,
    get_latest_glimpse_state,
    insert_glimpse_state,
)
from app.orm.memory_models import ExperienceFragment
from app.workers.proactive_scanner import get_unseen_messages

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

# 安静时段：23:00~09:00 CST 不窥屏
QUIET_HOURS = (23, 9)

# 初期白名单群（与 ProactiveManager 同群）
from app.workers.proactive_scanner import TARGET_CHAT_ID

_WHITELIST_GROUPS = [TARGET_CHAT_ID]


def _now_cst() -> datetime:
    return datetime.now(CST)


def _is_quiet(now: datetime) -> bool:
    h = now.hour
    start, end = QUIET_HOURS
    return h >= start or h < end


async def _pick_group(persona_id: str) -> str | None:
    """选一个群去翻。v1: 固定白名单。"""
    if _WHITELIST_GROUPS:
        return _WHITELIST_GROUPS[0]
    return None


async def _get_persona_info(persona_id: str) -> tuple[str, str]:
    from app.orm.crud import get_bot_persona

    persona = await get_bot_persona(persona_id)
    if persona:
        return persona.display_name, persona.persona_lite or ""
    return persona_id, ""


async def _get_group_name(chat_id: str) -> str:
    try:
        from app.orm.base import AsyncSessionLocal
        from app.orm.models import LarkGroupChatInfo
        from sqlalchemy import select

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(LarkGroupChatInfo.name).where(
                    LarkGroupChatInfo.chat_id == chat_id
                )
            )
            name = result.scalar_one_or_none()
            return name or chat_id[:10]
    except Exception:
        return chat_id[:10]


async def _format_messages(messages: list, persona_name: str = "") -> str:
    """格式化消息为时间线文本"""
    from app.orm.crud import get_username
    from app.utils.content_parser import parse_content

    lines = []
    for msg in messages[-30:]:
        ts = datetime.fromtimestamp(msg.create_time / 1000, tz=CST)
        time_str = ts.strftime("%H:%M")
        if msg.role == "assistant":
            speaker = persona_name or "bot"
        else:
            name = await get_username(msg.user_id)
            speaker = name or msg.user_id[:6]
        text = parse_content(msg.content).render()
        if text and text.strip():
            lines.append(f"[{time_str}] {speaker}: {text[:200]}")
    return "\n".join(lines)


async def _call_glimpse_llm(
    persona_name: str,
    persona_lite: str,
    group_name: str,
    messages_text: str,
    last_observation: str = "",
) -> str:
    """调用 LLM 进行窥屏观察"""
    from langfuse.langchain import CallbackHandler

    from app.agents.infra.langfuse_client import get_prompt
    from app.agents.infra.model_builder import ModelBuilder

    prompt = get_prompt("glimpse_observe")
    compile_args = {
        "persona_name": persona_name,
        "persona_lite": persona_lite,
        "group_name": group_name,
        "messages": messages_text,
    }
    # 递进观察：传入上次感想（prompt 模板需支持 last_observation 变量，不支持时忽略）
    if last_observation:
        compile_args["last_observation"] = last_observation
    try:
        compiled = prompt.compile(**compile_args)
    except KeyError:
        # prompt 模板尚未添加 last_observation 变量，先不传
        compiled = prompt.compile(
            persona_name=persona_name,
            persona_lite=persona_lite,
            group_name=group_name,
            messages=messages_text,
        )

    model = await ModelBuilder.build_chat_model(settings.life_engine_model)
    response = await model.ainvoke(
        [{"role": "user", "content": compiled}],
        config={"callbacks": [CallbackHandler()], "run_name": "glimpse-observe"},
    )

    if isinstance(response.content, list):
        return "".join(
            p.get("text", "") if isinstance(p, dict) else str(p)
            for p in response.content
        ).strip()
    return (response.content or "").strip()


def _parse_glimpse_response(raw: str) -> dict:
    """解析 glimpse LLM 响应"""
    try:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(raw[start:end])
            return {
                "interesting": bool(data.get("interesting", False)),
                "observation": data.get("observation", ""),
                "want_to_speak": bool(data.get("want_to_speak", False)),
                "stimulus": data.get("stimulus"),
                "target_message_id": data.get("target_message_id"),
            }
    except (json.JSONDecodeError, ValueError):
        pass
    return {"interesting": False}


async def run_glimpse(persona_id: str) -> str:
    """执行一次窥屏观察（v2: 增量 + 递进）

    Returns: 状态字符串（用于日志/测试/admin 接口）
    """
    now = _now_cst()

    # 1. 安静时段不窥屏
    if _is_quiet(now):
        logger.debug(f"[{persona_id}] Glimpse skipped: quiet hours")
        return "skipped:quiet_hours"

    # 2. 选群
    chat_id = await _pick_group(persona_id)
    if not chat_id:
        return "skipped:no_group"

    # 3. 读状态
    state = await get_latest_glimpse_state(persona_id, chat_id)
    last_seen = state.last_seen_msg_time if state else 0
    last_observation = state.observation if state else ""

    # 4. 跳过已参与的对话
    bot_reply_time = await get_last_bot_reply_time(chat_id)
    effective_after = max(last_seen, bot_reply_time)

    # 5. 拉增量消息
    messages = await get_unseen_messages(chat_id, after=effective_after)
    if not messages:
        logger.debug(f"[{persona_id}] Glimpse: no new messages in {chat_id}")
        return "skipped:no_messages"

    # 6. 准备上下文
    persona_name, persona_lite = await _get_persona_info(persona_id)
    group_name = await _get_group_name(chat_id)
    messages_text = await _format_messages(messages, persona_name)

    if not messages_text.strip():
        return "skipped:empty_timeline"

    # 7. LLM 观察（传入上次感想）
    raw = await _call_glimpse_llm(
        persona_name=persona_name,
        persona_lite=persona_lite,
        group_name=group_name,
        messages_text=messages_text,
        last_observation=last_observation,
    )
    decision = _parse_glimpse_response(raw)

    # 记录本次看到的最新消息时间戳
    new_last_seen = messages[-1].create_time

    if not decision.get("interesting"):
        logger.info(f"[{persona_id}] Glimpse: nothing interesting in {group_name}")
        # 不有趣也要记录看到了哪里，避免重复拉
        await insert_glimpse_state(
            persona_id=persona_id,
            chat_id=chat_id,
            last_seen_msg_time=new_last_seen,
            observation="",
        )
        return "skipped:not_interesting"

    # 8. 创建碎片
    observation = decision.get("observation", "")
    if observation:
        first_ts = messages[0].create_time
        last_ts = messages[-1].create_time
        fragment = ExperienceFragment(
            persona_id=persona_id,
            grain="glimpse",
            source_chat_id=chat_id,
            source_type="group",
            time_start=first_ts,
            time_end=last_ts,
            content=observation,
            mentioned_entity_ids=[],
            model=settings.life_engine_model,
        )
        await create_fragment(fragment)
        logger.info(f"[{persona_id}] Glimpse fragment: {observation[:60]}...")

    # 9. 搭话 dry-run
    state_observation = observation
    if decision.get("want_to_speak"):
        stimulus = decision.get("stimulus", "")
        target = decision.get("target_message_id", "")
        state_observation = f"{observation}\n[want_to_speak] stimulus={stimulus}, target={target}"
        logger.info(f"[{persona_id}] Glimpse want_to_speak (dry-run): {stimulus}")

    # 10. 写状态
    await insert_glimpse_state(
        persona_id=persona_id,
        chat_id=chat_id,
        last_seen_msg_time=new_last_seen,
        observation=state_observation,
    )

    return "fragment_created"
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_glimpse.py -v
```

预期：8 tests PASSED

- [ ] **Step 5: 提交**

```bash
git add apps/agent-service/app/services/glimpse.py apps/agent-service/tests/unit/test_glimpse.py
git commit -m "feat(glimpse): 重写 run_glimpse — 增量去重 + 递进观察 + 搭话 dry-run"
```

---

### Task 4: 独立 cron + 解耦 Life Engine

**Files:**
- Create: `apps/agent-service/app/workers/glimpse_worker.py`
- Modify: `apps/agent-service/app/workers/unified_worker.py:27,89` (import + cron 注册)
- Modify: `apps/agent-service/app/services/life_engine.py:154-161` (删除 glimpse 触发)
- Test: `apps/agent-service/tests/unit/test_glimpse_worker.py` (新建)

- [ ] **Step 1: 写 glimpse_worker 测试**

```python
# tests/unit/test_glimpse_worker.py
"""glimpse_worker cron 单元测试"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MODULE = "app.workers.glimpse_worker"


@pytest.mark.asyncio
async def test_cron_glimpse_skips_non_prod_lane():
    """非 prod 泳道跳过"""
    with patch(f"{MODULE}.settings") as mock_settings:
        mock_settings.lane = "dev"
        from app.workers.glimpse_worker import cron_glimpse

        await cron_glimpse(None)
        # 不应调用任何 persona 相关逻辑 — 函数直接 return


@pytest.mark.asyncio
async def test_cron_glimpse_skips_non_browsing():
    """activity_type != browsing 时跳过"""
    mock_state = MagicMock(activity_type="sleeping")

    with (
        patch(f"{MODULE}.settings") as mock_settings,
        patch(f"{MODULE}.get_all_persona_ids", new_callable=AsyncMock, return_value=["akao-001"]),
        patch(f"{MODULE}._load_life_engine_state", new_callable=AsyncMock, return_value=mock_state),
        patch(f"{MODULE}.run_glimpse", new_callable=AsyncMock) as mock_glimpse,
    ):
        mock_settings.lane = "prod"
        from app.workers.glimpse_worker import cron_glimpse

        await cron_glimpse(None)
        mock_glimpse.assert_not_called()


@pytest.mark.asyncio
async def test_cron_glimpse_runs_when_browsing():
    """activity_type == browsing 时执行 glimpse"""
    mock_state = MagicMock(activity_type="browsing")

    with (
        patch(f"{MODULE}.settings") as mock_settings,
        patch(f"{MODULE}.get_all_persona_ids", new_callable=AsyncMock, return_value=["akao-001"]),
        patch(f"{MODULE}._load_life_engine_state", new_callable=AsyncMock, return_value=mock_state),
        patch(f"{MODULE}.run_glimpse", new_callable=AsyncMock) as mock_glimpse,
    ):
        mock_settings.lane = "prod"
        from app.workers.glimpse_worker import cron_glimpse

        await cron_glimpse(None)
        mock_glimpse.assert_called_once_with("akao-001")


@pytest.mark.asyncio
async def test_cron_glimpse_no_state_skips():
    """没有 life engine 状态 → 跳过"""
    with (
        patch(f"{MODULE}.settings") as mock_settings,
        patch(f"{MODULE}.get_all_persona_ids", new_callable=AsyncMock, return_value=["akao-001"]),
        patch(f"{MODULE}._load_life_engine_state", new_callable=AsyncMock, return_value=None),
        patch(f"{MODULE}.run_glimpse", new_callable=AsyncMock) as mock_glimpse,
    ):
        mock_settings.lane = "prod"
        from app.workers.glimpse_worker import cron_glimpse

        await cron_glimpse(None)
        mock_glimpse.assert_not_called()
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_glimpse_worker.py -v
```

预期：FAIL — `ModuleNotFoundError: No module named 'app.workers.glimpse_worker'`

- [ ] **Step 3: 创建 `glimpse_worker.py`**

```python
# apps/agent-service/app/workers/glimpse_worker.py
"""Glimpse 独立 cron — 每 5 分钟，仅 browsing 状态下执行"""

import logging

from app.config.config import settings
from app.services.glimpse import run_glimpse

logger = logging.getLogger(__name__)


async def _load_life_engine_state(persona_id: str):
    """查 Life Engine 最新状态"""
    from app.orm.base import AsyncSessionLocal
    from app.orm.memory_models import LifeEngineState
    from sqlalchemy.future import select

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(LifeEngineState)
            .where(LifeEngineState.persona_id == persona_id)
            .order_by(LifeEngineState.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def cron_glimpse(ctx) -> None:
    """arq cron: 每 5 分钟为 browsing 状态的 persona 执行 glimpse

    非 prod 泳道跳过，避免与 prod 写同表冲突。
    泳道测试请用 POST /admin/trigger-glimpse。
    """
    if settings.lane and settings.lane != "prod":
        return

    from app.orm.crud import get_all_persona_ids

    for persona_id in await get_all_persona_ids():
        try:
            state = await _load_life_engine_state(persona_id)
            if not state or state.activity_type != "browsing":
                continue
            await run_glimpse(persona_id)
        except Exception as e:
            logger.error(f"[{persona_id}] Glimpse cron failed: {e}")
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_glimpse_worker.py -v
```

预期：4 tests PASSED

- [ ] **Step 5: 注册 cron 到 unified_worker.py**

在 `apps/agent-service/app/workers/unified_worker.py` 的 import 区（line 27 附近）添加：

```python
from app.workers.glimpse_worker import cron_glimpse
```

在 `cron_jobs` 列表中（line 93 `cron_life_engine_tick` 之后）添加：

```python
        # 1c. Glimpse 窥屏：每 5 分钟（仅 browsing 状态）
        cron(cron_glimpse, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}, timeout=120),
```

- [ ] **Step 6: 从 life_engine.py 删除 glimpse 触发**

在 `apps/agent-service/app/services/life_engine.py` 中，删除以下代码块（lines 154-161）：

```python
        # browsing → 触发 glimpse
        if new["activity_type"] == "browsing":
            from app.services.glimpse import run_glimpse

            try:
                await run_glimpse(persona_id)
            except Exception as e:
                logger.error(f"[{persona_id}] Glimpse failed: {e}")
```

- [ ] **Step 7: 运行所有相关测试**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_glimpse_worker.py tests/unit/test_glimpse.py tests/unit/test_life_engine.py -v
```

预期：全部 PASSED

- [ ] **Step 8: 提交**

```bash
git add apps/agent-service/app/workers/glimpse_worker.py apps/agent-service/app/workers/unified_worker.py apps/agent-service/app/services/life_engine.py apps/agent-service/tests/unit/test_glimpse_worker.py
git commit -m "feat(glimpse): 独立 cron 调度 + 解耦 Life Engine tick"
```

---

### Task 5: Admin 手动触发接口

**Files:**
- Modify: `apps/agent-service/app/api/router.py:60` (新增端点)

- [ ] **Step 1: 在 router.py 添加 trigger-glimpse 端点**

在 `apps/agent-service/app/api/router.py` 的 `trigger-life-engine-tick` 端点之后（line 60 后）添加：

```python
@api_router.post("/admin/trigger-glimpse", tags=["Admin"])
async def trigger_glimpse(persona_id: str):
    """手动触发一次 Glimpse 窥屏观察

    不检查 browsing 状态和泳道限制，强制执行。
    """
    from app.services.glimpse import run_glimpse

    result = await run_glimpse(persona_id)
    return {"ok": True, "persona_id": persona_id, "result": result}
```

- [ ] **Step 2: 提交**

```bash
git add apps/agent-service/app/api/router.py
git commit -m "feat(glimpse): admin trigger-glimpse 手动触发端点"
```

---

### Task 6: 建表 + 全量测试

**Files:**
- 无新文件（通过 ops-db skill 建表）

- [ ] **Step 1: 运行全量单元测试**

```bash
cd apps/agent-service && uv run pytest tests/unit/ -v
```

预期：全部 PASSED。如有失败，修复后重跑。

- [ ] **Step 2: 通过 ops-db 建表**

```
/ops-db @chiwei CREATE TABLE glimpse_state (
    id          SERIAL PRIMARY KEY,
    persona_id  VARCHAR(50) NOT NULL,
    chat_id     VARCHAR(100) NOT NULL,
    last_seen_msg_time BIGINT NOT NULL,
    observation TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

- [ ] **Step 3: 提交所有改动（如有修复）**

```bash
git add -A && git commit -m "fix(glimpse): 测试修复"
```

---

### Task 7: 部署泳道 + 手动验证

**Files:**
- 无代码改动

- [ ] **Step 1: push 到远端**

```bash
git push origin feat/glimpse-pipeline-redesign
```

- [ ] **Step 2: 部署 agent-service 到泳道**

```bash
make deploy APP=agent-service LANE=feat-glimpse-pipeline-redesign GIT_REF=feat/glimpse-pipeline-redesign
```

- [ ] **Step 3: 手动触发 glimpse**

```
POST /admin/trigger-glimpse?persona_id=akao-001
```

通过 `/api-test` skill 调用。

- [ ] **Step 4: 验证结果**

1. 查 `glimpse_state` 表：`/ops-db @chiwei SELECT * FROM glimpse_state ORDER BY id DESC LIMIT 5;`
2. 查 `experience_fragment` 表：`/ops-db @chiwei SELECT id, persona_id, grain, content, created_at FROM experience_fragment WHERE grain='glimpse' ORDER BY id DESC LIMIT 5;`
3. 查 Langfuse trace：确认 `glimpse-observe` trace 存在，输入含 `last_observation`

- [ ] **Step 5: 多次触发验证递进**

连续触发 2-3 次，验证：
- `glimpse_state` 中 `last_seen_msg_time` 递增
- 第二次调用时 `last_observation` 非空（Langfuse trace 可见）
- 没有新消息时返回 `skipped:no_messages`

- [ ] **Step 6: 验证 want_to_speak dry-run**

检查 `glimpse_state` 表中是否有 `[want_to_speak]` 记录。如没有自然触发，需等实际群聊活跃时重试。
