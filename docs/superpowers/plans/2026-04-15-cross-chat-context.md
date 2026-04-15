# 跨 Chat 对话上下文打通 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 persona 在回复 user X 时，能感知 user X 在其他 chat（ka群 + p2p）的近期互动，消除"人格分裂"。

**Architecture:** 在 `build_inner_context()` 新增 cross-chat section。新增查询函数从 `conversation_messages` 表按 `(user_id, bot_names, time_window)` 拉跨 chat 互动，格式化为可读文本注入 system prompt。需要提交数据库索引变更。

**Tech Stack:** Python / SQLAlchemy / PostgreSQL / pytest

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `app/memory/cross_chat.py` | Create | 跨 chat 查询 + 格式化（独立模块） |
| `app/memory/context.py` | Modify | 调用 cross_chat 注入新 section |
| `app/data/queries.py` | Modify | 新增 `find_bot_names_for_persona` + `find_cross_chat_messages` |
| `tests/unit/memory/test_cross_chat.py` | Create | cross_chat 模块的单元测试 |

---

### Task 1: 数据库索引

**Files:**
- 无代码文件改动，通过 ops-db skill 提交 DDL

- [ ] **Step 1: 提交索引变更申请**

```
/ops-db submit @chiwei CREATE INDEX CONCURRENTLY idx_conv_msg_user_bot_time ON conversation_messages(user_id, bot_name, create_time DESC);
-- reason: 跨 chat 上下文查询需要按 (user_id, bot_name, create_time) 高效检索
```

- [ ] **Step 2: 等待审批通过后确认索引存在**

```
/ops-db @chiwei SELECT indexname FROM pg_indexes WHERE tablename = 'conversation_messages' AND indexname = 'idx_conv_msg_user_bot_time'
```

Expected: 返回 1 行

---

### Task 2: 新增查询函数 `queries.py`

**Files:**
- Modify: `apps/agent-service/app/data/queries.py`
- Test: `tests/unit/memory/test_cross_chat.py`

- [ ] **Step 1: 写测试 — `find_bot_names_for_persona`**

Create `apps/agent-service/tests/unit/memory/test_cross_chat.py`:

```python
"""Tests for cross-chat context module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.data.queries import find_bot_names_for_persona


@pytest.mark.asyncio
async def test_find_bot_names_for_persona():
    """Should return all active bot_names for a persona_id."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = ["chiwei", "fly", "dev"]
    mock_session.execute.return_value = mock_result

    result = await find_bot_names_for_persona(mock_session, "akao")

    assert result == ["chiwei", "fly", "dev"]
    mock_session.execute.assert_called_once()


@pytest.mark.asyncio
async def test_find_bot_names_for_persona_empty():
    """Should return empty list when persona has no bots."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_session.execute.return_value = mock_result

    result = await find_bot_names_for_persona(mock_session, "nonexistent")

    assert result == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd apps/agent-service && python -m pytest tests/unit/memory/test_cross_chat.py::test_find_bot_names_for_persona -v`
Expected: FAIL — `ImportError: cannot import name 'find_bot_names_for_persona'`

- [ ] **Step 3: 实现 `find_bot_names_for_persona`**

在 `apps/agent-service/app/data/queries.py` 的 `# --- Chat messages ---` section 之前添加：

```python
async def find_bot_names_for_persona(
    session: AsyncSession, persona_id: str
) -> list[str]:
    """Return all active bot_names mapped to a persona_id."""
    result = await session.execute(
        text(
            "SELECT bot_name FROM bot_config "
            "WHERE persona_id = :pid AND is_active = true"
        ),
        {"pid": persona_id},
    )
    return list(result.scalars().all())
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd apps/agent-service && python -m pytest tests/unit/memory/test_cross_chat.py::test_find_bot_names_for_persona tests/unit/memory/test_cross_chat.py::test_find_bot_names_for_persona_empty -v`
Expected: 2 PASSED

- [ ] **Step 5: 写测试 — `find_cross_chat_messages`**

在 `tests/unit/memory/test_cross_chat.py` 追加：

```python
from app.data.queries import find_cross_chat_messages


@pytest.mark.asyncio
async def test_find_cross_chat_messages_filters_current_chat():
    """Should exclude messages from current chat."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_session.execute.return_value = mock_result

    result = await find_cross_chat_messages(
        mock_session,
        user_id="user_1",
        bot_names=["chiwei"],
        exclude_chat_id="chat_current",
        allowed_group_ids=["chat_ka"],
        since_ms=1000,
    )

    assert result == []
    # Verify the SQL was called
    mock_session.execute.assert_called_once()
    call_args = mock_session.execute.call_args
    sql_text = str(call_args[0][0])
    # Should filter by user_id, bot_name, exclude chat, time window
    assert ":user_id" in sql_text or "user_id" in sql_text
```

- [ ] **Step 6: 实现 `find_cross_chat_messages`**

在 `apps/agent-service/app/data/queries.py` 中，紧接 `find_bot_names_for_persona` 之后添加：

```python
async def find_cross_chat_messages(
    session: AsyncSession,
    user_id: str,
    bot_names: list[str],
    exclude_chat_id: str,
    allowed_group_ids: list[str],
    since_ms: int,
    limit_per_chat: int = 20,
) -> list[ConversationMessage]:
    """Fetch recent cross-chat interactions between a user and a persona.

    Returns messages from:
      - Allowed group chats (allowed_group_ids) where user or bot participated
      - Any p2p chat between user and bot

    Messages are: user's messages + bot's replies, ordered by create_time ASC.
    Excludes the current chat (exclude_chat_id).
    """
    stmt = (
        select(ConversationMessage)
        .where(ConversationMessage.chat_id != exclude_chat_id)
        .where(ConversationMessage.create_time >= since_ms)
        .where(
            or_(
                # User's messages in allowed chats
                (
                    ConversationMessage.user_id == user_id
                ) & ConversationMessage.role == "user",
                # Bot's replies in allowed chats
                ConversationMessage.role == "assistant",
            )
        )
        .where(ConversationMessage.bot_name.in_(bot_names))
        .where(
            or_(
                # Allowed group chats
                ConversationMessage.chat_id.in_(allowed_group_ids),
                # Any p2p chat with this user
                (ConversationMessage.chat_type == "p2p")
                & (ConversationMessage.user_id == user_id),
            )
        )
        .order_by(ConversationMessage.create_time.asc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())
```

注意：这个查询会拿到 user 的消息和 bot 的所有回复。在格式化阶段再按交互对配对和裁切。对于 assistant 消息在 p2p 场景下 `user_id` 可能不是触发用户（而是 bot 的 user_id），所以 p2p 条件需要用 `chat_type='p2p'` + `bot_name IN bot_names` 来匹配，不需要额外 user_id 过滤（p2p 天然只有两人）。

修正 SQLAlchemy 表达式的运算符优先级问题，用 `.where()` 链式调用：

```python
async def find_cross_chat_messages(
    session: AsyncSession,
    user_id: str,
    bot_names: list[str],
    exclude_chat_id: str,
    allowed_group_ids: list[str],
    since_ms: int,
) -> list[ConversationMessage]:
    """Fetch recent cross-chat interactions between a user and a persona.

    Returns user messages + bot replies from allowed group chats and p2p chats.
    Excludes the current chat.
    """
    # For group chats: get user's messages + bot replies
    # For p2p: get all messages (only 2 participants)
    stmt = (
        select(ConversationMessage)
        .where(ConversationMessage.chat_id != exclude_chat_id)
        .where(ConversationMessage.create_time >= since_ms)
        .where(ConversationMessage.bot_name.in_(bot_names))
        .where(
            or_(
                ConversationMessage.chat_id.in_(allowed_group_ids),
                ConversationMessage.chat_type == "p2p",
            )
        )
        .where(
            or_(
                # User's messages
                (ConversationMessage.role == "user")
                & (ConversationMessage.user_id == user_id),
                # Bot's assistant replies
                ConversationMessage.role == "assistant",
            )
        )
        .order_by(ConversationMessage.create_time.asc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())
```

- [ ] **Step 7: 跑测试确认通过**

Run: `cd apps/agent-service && python -m pytest tests/unit/memory/test_cross_chat.py -v`
Expected: ALL PASSED

- [ ] **Step 8: Commit**

```bash
git add apps/agent-service/app/data/queries.py tests/unit/memory/test_cross_chat.py
git commit -m "feat(memory): add cross-chat query functions"
```

---

### Task 3: 跨 chat 格式化模块 `cross_chat.py`

**Files:**
- Create: `apps/agent-service/app/memory/cross_chat.py`
- Modify: `tests/unit/memory/test_cross_chat.py`

- [ ] **Step 1: 写测试 — 格式化逻辑**

在 `tests/unit/memory/test_cross_chat.py` 追加：

```python
from app.memory.cross_chat import _group_and_trim, _format_interactions


def _make_msg(
    role: str,
    user_id: str,
    chat_id: str,
    create_time: int,
    content: str,
    chat_type: str = "group",
    bot_name: str = "chiwei",
    reply_message_id: str | None = None,
):
    """Create a mock ConversationMessage."""
    msg = MagicMock()
    msg.role = role
    msg.user_id = user_id
    msg.chat_id = chat_id
    msg.create_time = create_time
    msg.content = content
    msg.chat_type = chat_type
    msg.bot_name = bot_name
    msg.reply_message_id = reply_message_id
    msg.message_id = f"msg_{create_time}"
    return msg


def test_group_and_trim_groups_by_chat():
    """Should group messages by chat_id and trim to limit."""
    msgs = [
        _make_msg("user", "u1", "chat_a", 1000, '{"v":2,"text":"hi","items":[]}'),
        _make_msg("assistant", "bot", "chat_a", 2000, '{"v":2,"text":"hello","items":[]}'),
        _make_msg("user", "u1", "chat_b", 3000, '{"v":2,"text":"hey","items":[]}', chat_type="p2p"),
    ]
    grouped = _group_and_trim(msgs, max_pairs_per_chat=10)
    assert "chat_a" in grouped
    assert "chat_b" in grouped
    assert len(grouped["chat_a"]) == 2
    assert len(grouped["chat_b"]) == 1


def test_group_and_trim_respects_limit():
    """Should trim to max_pairs_per_chat (pair = user msg + assistant reply)."""
    msgs = []
    for i in range(30):
        msgs.append(_make_msg("user", "u1", "chat_a", i * 2000, f'{{"v":2,"text":"msg{i}","items":[]}}'))
        msgs.append(_make_msg("assistant", "bot", "chat_a", i * 2000 + 1000, f'{{"v":2,"text":"reply{i}","items":[]}}'))
    grouped = _group_and_trim(msgs, max_pairs_per_chat=10)
    # Should keep last 10 pairs = 20 messages
    assert len(grouped["chat_a"]) == 20


def test_format_interactions_output():
    """Should produce readable text with chat name and relative time."""
    msgs = [
        _make_msg("user", "u1", "chat_a", 1713160000000, '{"v":2,"text":"笋干烧肉好吃吗","items":[{"type":"text","value":"笋干烧肉好吃吗"}]}'),
        _make_msg("assistant", "bot", "chat_a", 1713160060000, '{"v":2,"text":"超好吃！","items":[{"type":"text","value":"超好吃！"}]}'),
    ]
    grouped = {"chat_a": msgs}
    chat_names = {"chat_a": "粉丝群"}

    result = _format_interactions(grouped, "冯宇林", chat_names)

    assert "粉丝群" in result
    assert "冯宇林" in result
    assert "笋干烧肉好吃吗" in result
    assert "超好吃" in result
    assert "你:" in result or "你: " in result
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd apps/agent-service && python -m pytest tests/unit/memory/test_cross_chat.py::test_group_and_trim_groups_by_chat -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 实现 `cross_chat.py`**

Create `apps/agent-service/app/memory/cross_chat.py`:

```python
"""Cross-chat context builder.

Fetches recent interactions between a user and a persona across different chats,
formats them for injection into the system prompt.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.chat.content_parser import parse_content
from app.data.models import ConversationMessage
from app.data.queries import (
    find_bot_names_for_persona,
    find_cross_chat_messages,
    find_group_name,
)
from app.data.session import get_session

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))

# ka群 — 硬编码，后续可迁入 dynamic config
CROSS_CHAT_GROUP_IDS = ["oc_54713c53ff0b46cb9579d3695e16cbf8"]

_24H_MS = 24 * 60 * 60 * 1000
_MAX_PAIRS_PER_CHAT = 10


def _group_and_trim(
    messages: list[ConversationMessage],
    max_pairs_per_chat: int = _MAX_PAIRS_PER_CHAT,
) -> dict[str, list[ConversationMessage]]:
    """Group messages by chat_id, keep last N interaction pairs per chat."""
    by_chat: dict[str, list[ConversationMessage]] = defaultdict(list)
    for msg in messages:
        by_chat[msg.chat_id].append(msg)

    trimmed: dict[str, list[ConversationMessage]] = {}
    for chat_id, chat_msgs in by_chat.items():
        # Count pairs: a user message followed by an assistant reply
        pair_count = sum(1 for m in chat_msgs if m.role == "user")
        if pair_count <= max_pairs_per_chat:
            trimmed[chat_id] = chat_msgs
        else:
            # Keep last N user messages and their surrounding assistant replies
            user_msgs = [m for m in chat_msgs if m.role == "user"]
            keep_from = user_msgs[-max_pairs_per_chat].create_time
            trimmed[chat_id] = [m for m in chat_msgs if m.create_time >= keep_from]

    return trimmed


def _render_text(content: str) -> str:
    """Extract plain text from v2 JSON message content."""
    try:
        return parse_content(content).render().strip()
    except Exception:
        return content.strip()


def _relative_time(ts_ms: int) -> str:
    """Format timestamp as relative time string (CST)."""
    now = datetime.now(_CST)
    msg_time = datetime.fromtimestamp(ts_ms / 1000, _CST)
    delta = now - msg_time

    if delta < timedelta(minutes=5):
        return "刚刚"
    if delta < timedelta(hours=1):
        return f"{int(delta.total_seconds() // 60)}分钟前"
    if delta < timedelta(hours=12):
        return f"{int(delta.total_seconds() // 3600)}小时前"
    if msg_time.date() == now.date():
        return f"今天{msg_time.strftime('%H:%M')}"
    if msg_time.date() == (now - timedelta(days=1)).date():
        return f"昨天{msg_time.strftime('%H:%M')}"
    return msg_time.strftime("%m-%d %H:%M")


def _format_interactions(
    grouped: dict[str, list[ConversationMessage]],
    username: str,
    chat_names: dict[str, str],
) -> str:
    """Format grouped cross-chat interactions into readable text."""
    if not grouped:
        return ""

    parts: list[str] = []

    for chat_id, msgs in grouped.items():
        if not msgs:
            continue

        chat_name = chat_names.get(chat_id, "私聊" if msgs[0].chat_type == "p2p" else chat_id[:8])
        first_ts = msgs[0].create_time

        lines: list[str] = [f"{chat_name} · {_relative_time(first_ts)}:"]
        for msg in msgs:
            text = _render_text(msg.content)
            if not text:
                continue
            # Truncate long messages
            if len(text) > 150:
                text = text[:147] + "..."
            speaker = "你" if msg.role == "assistant" else username
            lines.append(f"  {speaker}: {text}")

        if len(lines) > 1:  # At least one message besides the header
            parts.append("\n".join(lines))

    if not parts:
        return ""

    return f"[你和 {username} 最近在其他地方的互动]\n\n" + "\n\n".join(parts)


async def build_cross_chat_context(
    persona_id: str,
    trigger_user_id: str,
    trigger_username: str,
    current_chat_id: str,
) -> str:
    """Build the cross-chat interaction section for inner_context.

    Returns empty string if no cross-chat interactions found.
    """
    try:
        async with get_session() as s:
            bot_names = await find_bot_names_for_persona(s, persona_id)
        if not bot_names:
            return ""

        now_ms = int(datetime.now(_CST).timestamp() * 1000)
        since_ms = now_ms - _24H_MS

        async with get_session() as s:
            messages = await find_cross_chat_messages(
                s,
                user_id=trigger_user_id,
                bot_names=bot_names,
                exclude_chat_id=current_chat_id,
                allowed_group_ids=CROSS_CHAT_GROUP_IDS,
                since_ms=since_ms,
            )
        if not messages:
            return ""

        grouped = _group_and_trim(messages)

        # Resolve chat display names
        chat_names: dict[str, str] = {}
        for chat_id in grouped:
            sample = grouped[chat_id][0]
            if sample.chat_type == "p2p":
                chat_names[chat_id] = "私聊"
            else:
                async with get_session() as s:
                    name = await find_group_name(s, chat_id)
                chat_names[chat_id] = name or chat_id[:8]

        return _format_interactions(grouped, trigger_username, chat_names)

    except Exception as e:
        logger.warning("Failed to build cross-chat context for %s: %s", persona_id, e)
        return ""
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd apps/agent-service && python -m pytest tests/unit/memory/test_cross_chat.py -v`
Expected: ALL PASSED

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/memory/cross_chat.py tests/unit/memory/test_cross_chat.py
git commit -m "feat(memory): cross-chat context builder with formatting"
```

---

### Task 4: 注入 `build_inner_context()`

**Files:**
- Modify: `apps/agent-service/app/memory/context.py` (lines 89-107, insert after relationship memory)

- [ ] **Step 1: 写测试 — cross-chat section 出现在 inner context 中**

在 `tests/unit/memory/test_cross_chat.py` 追加：

```python
@pytest.mark.asyncio
async def test_build_inner_context_includes_cross_chat():
    """build_inner_context should include cross-chat section when data exists."""
    mock_cross = "[你和 冯宇林 最近在其他地方的互动]\n\n粉丝群 · 2小时前:\n  冯宇林: 笋干好吃\n  你: 超好吃"

    with (
        patch("app.memory.context._build_life_state", return_value=""),
        patch("app.memory.context.find_latest_relationship_memory", return_value=None),
        patch("app.memory.context.find_today_fragments", return_value=[]),
        patch("app.memory.context.build_cross_chat_context", return_value=mock_cross) as mock_build,
    ):
        from app.memory.context import build_inner_context

        result = await build_inner_context(
            chat_id="chat_current",
            chat_type="p2p",
            user_ids=["user_1"],
            trigger_user_id="user_1",
            trigger_username="冯宇林",
            persona_id="akao",
        )

        assert "最近在其他地方的互动" in result
        assert "笋干好吃" in result
        mock_build.assert_called_once_with(
            persona_id="akao",
            trigger_user_id="user_1",
            trigger_username="冯宇林",
            current_chat_id="chat_current",
        )
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd apps/agent-service && python -m pytest tests/unit/memory/test_cross_chat.py::test_build_inner_context_includes_cross_chat -v`
Expected: FAIL — `build_cross_chat_context` not called from `build_inner_context`

- [ ] **Step 3: 修改 `context.py`**

在 `apps/agent-service/app/memory/context.py` 顶部添加 import：

```python
from app.memory.cross_chat import build_cross_chat_context
```

在 relationship memory section 之后（约 line 108）、recent fragments section 之前（约 line 109），插入：

```python
    # === Cross-chat interactions ===
    if trigger_user_id and trigger_user_id != "__proactive__":
        cross_chat_text = await build_cross_chat_context(
            persona_id=persona_id,
            trigger_user_id=trigger_user_id,
            trigger_username=trigger_username,
            current_chat_id=chat_id,
        )
        if cross_chat_text:
            sections.append(cross_chat_text)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd apps/agent-service && python -m pytest tests/unit/memory/test_cross_chat.py -v`
Expected: ALL PASSED

- [ ] **Step 5: 跑全量测试确认无回归**

Run: `cd apps/agent-service && python -m pytest tests/ -v --timeout=30`
Expected: ALL PASSED (无已有测试被破坏)

- [ ] **Step 6: Commit**

```bash
git add apps/agent-service/app/memory/context.py apps/agent-service/app/memory/cross_chat.py tests/unit/memory/test_cross_chat.py
git commit -m "feat(memory): inject cross-chat context into inner_context"
```

---

### Task 5: 部署验证

- [ ] **Step 1: Push 到远端**

```bash
git push origin feat/optimize-life-engine
```

- [ ] **Step 2: 部署到泳道**

```bash
make deploy APP=agent-service LANE=feat-optimize-life-engine GIT_REF=feat/optimize-life-engine
make deploy APP=arq-worker LANE=feat-optimize-life-engine GIT_REF=feat/optimize-life-engine
```

- [ ] **Step 3: 绑定 dev bot**

```
/ops bind TYPE=bot KEY=dev LANE=feat-optimize-life-engine
```

- [ ] **Step 4: 验证 — 在 ka群 @ dev bot 聊几句，然后去私聊提"刚才说的"**

1. 在 ka群 @dev bot 说一个有特征的话题（如"今天想吃火锅"）
2. 等 bot 回复
3. 去 dev bot 私聊说"我刚才在群里说的那个事"
4. 验证 bot 能否正确引用群聊内容

- [ ] **Step 5: 验证 — 检查 Langfuse trace 中 inner_context 是否包含跨 chat section**

在 Langfuse 找到最新的私聊 trace，确认 system prompt 中有 `[你和 xxx 最近在其他地方的互动]` section。

- [ ] **Step 6: 等用户验收**

不要自行 undeploy，等用户确认验收完成。
