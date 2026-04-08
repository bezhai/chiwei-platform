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
        patch(f"{MODULE}._get_recent_proactive_records", new_callable=AsyncMock, return_value=[]),
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
        patch(f"{MODULE}._get_recent_proactive_records", new_callable=AsyncMock, return_value=[]),
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
async def test_glimpse_want_to_speak_submits_proactive():
    """想搭话 → 记录状态 + 调 submit_proactive_request"""
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
        patch(f"{MODULE}._get_recent_proactive_records", new_callable=AsyncMock, return_value=[]),
        patch(f"{MODULE}._call_glimpse_llm", new_callable=AsyncMock, return_value=llm_response),
        patch(f"{MODULE}.create_fragment", new_callable=AsyncMock),
        patch(f"{MODULE}.insert_glimpse_state", new_callable=AsyncMock) as mock_state,
        patch(f"{MODULE}._get_persona_info", new_callable=AsyncMock, return_value=("赤尾", "")),
        patch(f"{MODULE}._get_group_name", new_callable=AsyncMock, return_value="番剧群"),
        patch("app.workers.proactive_scanner.submit_proactive_request", new_callable=AsyncMock) as mock_proactive,
    ):
        result = await run_glimpse("akao-001")

        assert result == "fragment_created"
        # glimpse_state.observation 应包含 want_to_speak 信息
        call_kwargs = mock_state.call_args[1]
        assert "[want_to_speak]" in call_kwargs["observation"]
        assert "好想聊聊" in call_kwargs["observation"]
        # 应调用 submit_proactive_request
        mock_proactive.assert_called_once_with(
            chat_id="oc_test",
            persona_id="akao-001",
            target_message_id="m1",
            stimulus="好想聊聊",
        )


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
        patch(f"{MODULE}._get_recent_proactive_records", new_callable=AsyncMock, return_value=[]),
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
