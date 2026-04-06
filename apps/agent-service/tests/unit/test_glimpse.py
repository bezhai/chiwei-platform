import json
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta, timezone

import pytest

CST = timezone(timedelta(hours=8))


@pytest.mark.asyncio
async def test_glimpse_skips_quiet_hours():
    """安静时段不窥屏"""
    from app.services.glimpse import run_glimpse

    quiet_time = datetime(2026, 4, 6, 2, 0, tzinfo=CST)  # 02:00 CST
    with patch("app.services.glimpse._now_cst", return_value=quiet_time):
        result = await run_glimpse("akao-001")
        assert result == "skipped:quiet_hours"


@pytest.mark.asyncio
async def test_glimpse_skips_no_messages():
    """没有未读消息 → 跳过"""
    from app.services.glimpse import run_glimpse

    normal_time = datetime(2026, 4, 6, 14, 0, tzinfo=CST)
    with (
        patch("app.services.glimpse._now_cst", return_value=normal_time),
        patch("app.services.glimpse._pick_group", new_callable=AsyncMock, return_value="oc_test"),
        patch("app.services.glimpse.get_unseen_messages", new_callable=AsyncMock, return_value=[]),
    ):
        result = await run_glimpse("akao-001")
        assert result == "skipped:no_messages"


@pytest.mark.asyncio
async def test_glimpse_creates_fragment_when_interesting():
    """有趣的消息 → 创建 glimpse 碎片"""
    from app.services.glimpse import run_glimpse

    normal_time = datetime(2026, 4, 6, 14, 0, tzinfo=CST)
    mock_msg = MagicMock()
    mock_msg.user_id = "u1"
    mock_msg.create_time = int(normal_time.timestamp() * 1000)
    mock_msg.content = '{"v":2,"text":"好看的番","items":[]}'
    mock_msg.chat_type = "group"
    mock_msg.chat_id = "oc_test"
    mock_msg.role = "user"
    mock_msg.message_id = "m1"

    llm_response = json.dumps({
        "interesting": True,
        "observation": "群里在聊新番，挺有意思的",
        "want_to_speak": False,
    })

    with (
        patch("app.services.glimpse._now_cst", return_value=normal_time),
        patch("app.services.glimpse._pick_group", new_callable=AsyncMock, return_value="oc_test"),
        patch("app.services.glimpse.get_unseen_messages", new_callable=AsyncMock, return_value=[mock_msg]),
        patch("app.services.glimpse._format_messages", new_callable=AsyncMock, return_value="[14:00] someone: 好看的番"),
        patch("app.services.glimpse._call_glimpse_llm", new_callable=AsyncMock, return_value=llm_response),
        patch("app.services.glimpse.create_fragment", new_callable=AsyncMock) as mock_create,
        patch("app.services.glimpse._get_persona_info", new_callable=AsyncMock, return_value=("赤尾", "")),
        patch("app.services.glimpse._get_group_name", new_callable=AsyncMock, return_value="番剧群"),
    ):
        result = await run_glimpse("akao-001")
        assert result == "fragment_created"
        mock_create.assert_called_once()
        frag = mock_create.call_args[0][0]
        assert frag.grain == "glimpse"
        assert "在聊新番" in frag.content


@pytest.mark.asyncio
async def test_glimpse_triggers_proactive_when_want_to_speak():
    """想说话 → 触发主动搭话"""
    from app.services.glimpse import run_glimpse

    normal_time = datetime(2026, 4, 6, 14, 0, tzinfo=CST)
    mock_msg = MagicMock()
    mock_msg.user_id = "u1"
    mock_msg.create_time = int(normal_time.timestamp() * 1000)
    mock_msg.content = '{"v":2,"text":"话题","items":[]}'
    mock_msg.chat_type = "group"
    mock_msg.chat_id = "oc_test"
    mock_msg.role = "user"
    mock_msg.message_id = "m1"

    llm_response = json.dumps({
        "interesting": True,
        "observation": "群里在讨论我喜欢的东西",
        "want_to_speak": True,
        "stimulus": "好想聊聊这个话题",
        "target_message_id": "m1",
    })

    with (
        patch("app.services.glimpse._now_cst", return_value=normal_time),
        patch("app.services.glimpse._pick_group", new_callable=AsyncMock, return_value="oc_test"),
        patch("app.services.glimpse.get_unseen_messages", new_callable=AsyncMock, return_value=[mock_msg]),
        patch("app.services.glimpse._format_messages", new_callable=AsyncMock, return_value="[14:00] someone: 话题"),
        patch("app.services.glimpse._call_glimpse_llm", new_callable=AsyncMock, return_value=llm_response),
        patch("app.services.glimpse.create_fragment", new_callable=AsyncMock),
        patch("app.services.glimpse.submit_proactive_request", new_callable=AsyncMock) as mock_proactive,
        patch("app.services.glimpse._get_persona_info", new_callable=AsyncMock, return_value=("赤尾", "")),
        patch("app.services.glimpse._get_group_name", new_callable=AsyncMock, return_value="番剧群"),
    ):
        result = await run_glimpse("akao-001")
        assert result == "fragment_created+proactive"
        mock_proactive.assert_called_once()
