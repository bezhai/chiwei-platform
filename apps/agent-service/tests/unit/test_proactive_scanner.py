# tests/unit/test_proactive_scanner.py
"""proactive_scanner 单元测试

覆盖: get_unseen_messages (2 cases)
"""
import time
from datetime import timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

CST = timezone(timedelta(hours=8))

MODULE = "app.workers.proactive_scanner"


# ── get_unseen_messages ───────────────────────────────────────────────────


async def test_get_unseen_messages_has_messages():
    """有未读消息时返回 ConversationMessage 列表"""
    fake_msg = MagicMock(
        message_id="msg_1",
        user_id="user_abc",
        content='{"v": 2, "text": "hello", "items": []}',
        role="user",
        create_time=int(time.time() * 1000),
    )

    mock_scalars = MagicMock()
    mock_scalars.all.return_value = [fake_msg]
    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars

    mock_session = AsyncMock()
    mock_session.execute.return_value = mock_result

    mock_session_ctx = AsyncMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch(f"{MODULE}.AsyncSessionLocal", return_value=mock_session_ctx):
        from app.workers.proactive_scanner import get_unseen_messages

        result = await get_unseen_messages("test_chat", after=0)

    assert len(result) == 1
    assert result[0].message_id == "msg_1"


async def test_get_unseen_messages_empty():
    """无未读消息时返回空列表"""
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = []
    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars

    mock_session = AsyncMock()
    mock_session.execute.return_value = mock_result

    mock_session_ctx = AsyncMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch(f"{MODULE}.AsyncSessionLocal", return_value=mock_session_ctx):
        from app.workers.proactive_scanner import get_unseen_messages

        result = await get_unseen_messages("test_chat", after=0)

    assert result == []
