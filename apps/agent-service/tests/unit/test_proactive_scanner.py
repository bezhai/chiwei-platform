# tests/unit/test_proactive_scanner.py
"""proactive_scanner 单元测试

覆盖: should_scan (3 cases), get_unseen_messages (2 cases), judge_response (2 cases)
"""
import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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


# ── judge_response ────────────────────────────────────────────────────────


async def test_judge_response_respond_true():
    """小模型判断应该回复"""
    judge_result = json.dumps({
        "respond": True,
        "target_message_id": "msg_42",
        "stimulus": "他们在讨论你喜欢的动漫",
    })

    mock_model = AsyncMock()
    mock_model.ainvoke.return_value = MagicMock(content=judge_result)

    with (
        patch(f"{MODULE}.get_prompt") as mock_prompt,
        patch(f"{MODULE}.ModelBuilder") as mock_mb,
    ):
        mock_prompt.return_value.compile.return_value = "compiled prompt"
        mock_mb.build_chat_model = AsyncMock(return_value=mock_model)

        from app.workers.proactive_scanner import judge_response

        result = await judge_response(
            messages_text="[10:00:00] user_abc: 你看过新番吗",
            reply_style="活泼可爱",
            group_culture="二次元讨论群",
            recent_proactive=[],
        )

    assert result["respond"] is True
    assert result["target_message_id"] == "msg_42"
    assert result["stimulus"] == "他们在讨论你喜欢的动漫"


async def test_judge_response_json_parse_failure():
    """小模型返回非 JSON → 默认 respond=False"""
    mock_model = AsyncMock()
    mock_model.ainvoke.return_value = MagicMock(content="I'm not sure what to say")

    with (
        patch(f"{MODULE}.get_prompt") as mock_prompt,
        patch(f"{MODULE}.ModelBuilder") as mock_mb,
    ):
        mock_prompt.return_value.compile.return_value = "compiled prompt"
        mock_mb.build_chat_model = AsyncMock(return_value=mock_model)

        from app.workers.proactive_scanner import judge_response

        result = await judge_response(
            messages_text="[10:00:00] user_abc: 今天天气不错",
            reply_style="淡定",
            group_culture="日常闲聊群",
            recent_proactive=[],
        )

    assert result["respond"] is False
