"""Tests for app.life.glimpse — browsing observation pipeline."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.life.glimpse import GlimpseResult, parse_glimpse_response

CST = timezone(timedelta(hours=8))

MODULE = "app.life.glimpse"


def _make_msg(user_id="u1", create_time=None, content="hello", msg_id="m1"):
    msg = MagicMock()
    msg.user_id = user_id
    msg.create_time = create_time or int(
        datetime(2026, 4, 7, 14, 0, tzinfo=CST).timestamp() * 1000
    )
    msg.content = f'{{"v":2,"text":"{content}","items":[]}}'
    msg.chat_id = "oc_test"
    msg.role = "user"
    msg.message_id = msg_id
    msg.id = 1
    return msg


# ---------------------------------------------------------------------------
# parse_glimpse_response
# ---------------------------------------------------------------------------


def test_parse_glimpse_response_valid():
    raw = json.dumps(
        {
            "interesting": True,
            "observation": "群里在聊新番",
            "want_to_speak": False,
        }
    )
    result = parse_glimpse_response(raw)
    assert result["interesting"] is True
    assert result["observation"] == "群里在聊新番"
    assert result["want_to_speak"] is False


def test_parse_glimpse_response_malformed():
    result = parse_glimpse_response("not json")
    assert result["interesting"] is False


def test_parse_glimpse_response_with_speak():
    raw = json.dumps(
        {
            "interesting": True,
            "observation": "聊得好热闹",
            "want_to_speak": True,
            "speak_reason": "想参与",
            "stimulus": "我也追了这部",
            "target_message_id": "m42",
        }
    )
    result = parse_glimpse_response(raw)
    assert result["want_to_speak"] is True
    assert result["stimulus"] == "我也追了这部"
    assert result["target_message_id"] == "m42"


# ---------------------------------------------------------------------------
# GlimpseResult enum
# ---------------------------------------------------------------------------


def test_glimpse_result_enum_values():
    assert GlimpseResult.FRAGMENT_CREATED == "fragment_created"
    assert isinstance(GlimpseResult.SKIPPED_NO_MESSAGES, str)


# ---------------------------------------------------------------------------
# run_glimpse — no new messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_glimpse_skips_no_new_messages():
    from app.life.glimpse import run_glimpse

    normal_time = datetime(2026, 4, 7, 14, 0, tzinfo=CST)

    with patch(f"{MODULE}.get_session") as mock_gs:
        mock_session = AsyncMock()
        mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(f"{MODULE}._now_cst", return_value=normal_time),
            patch(
                f"{MODULE}.Q.find_latest_glimpse_state",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                f"{MODULE}.Q.find_last_bot_reply_time",
                new_callable=AsyncMock,
                return_value=0,
            ),
            patch(
                f"{MODULE}.get_unseen_messages", new_callable=AsyncMock, return_value=[]
            ),
        ):
            result = await run_glimpse("akao-001", "oc_test")
            assert result == GlimpseResult.SKIPPED_NO_MESSAGES


# ---------------------------------------------------------------------------
# run_glimpse — creates fragment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_glimpse_creates_fragment_and_state():
    from app.life.glimpse import run_glimpse

    normal_time = datetime(2026, 4, 7, 14, 0, tzinfo=CST)
    mock_msg = _make_msg()

    llm_response = json.dumps(
        {
            "interesting": True,
            "observation": "群里在聊新番",
            "want_to_speak": False,
        }
    )

    with patch(f"{MODULE}.get_session") as mock_gs:
        mock_session = AsyncMock()
        mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(f"{MODULE}._now_cst", return_value=normal_time),
            patch(
                f"{MODULE}.Q.find_latest_glimpse_state",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                f"{MODULE}.Q.find_last_bot_reply_time",
                new_callable=AsyncMock,
                return_value=0,
            ),
            patch(
                f"{MODULE}.get_unseen_messages",
                new_callable=AsyncMock,
                return_value=[mock_msg],
            ),
            patch(
                f"{MODULE}.format_timeline",
                new_callable=AsyncMock,
                return_value="[14:00] someone: hello",
            ),
            patch(
                f"{MODULE}.get_recent_proactive_records",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                f"{MODULE}._call_glimpse_llm",
                new_callable=AsyncMock,
                return_value=llm_response,
            ),
            patch(f"{MODULE}.Q.insert_fragment", new_callable=AsyncMock) as mock_frag,
            patch(
                f"{MODULE}.Q.insert_glimpse_state", new_callable=AsyncMock
            ) as mock_state,
            patch(
                f"{MODULE}.load_persona",
                new_callable=AsyncMock,
                return_value=MagicMock(display_name="赤尾", persona_lite=""),
            ),
            patch(
                f"{MODULE}._get_group_name",
                new_callable=AsyncMock,
                return_value="番剧群",
            ),
            patch(
                f"{MODULE}.enqueue_fragment_vectorize", new_callable=AsyncMock
            ) as mock_enqueue,
        ):
            result = await run_glimpse("akao-001", "oc_test")

            assert result == GlimpseResult.FRAGMENT_CREATED
            mock_frag.assert_called_once()
            kwargs = mock_frag.call_args.kwargs
            assert kwargs["source"] == "glimpse"
            assert kwargs["persona_id"] == "akao-001"
            assert kwargs["chat_id"] == "oc_test"
            assert kwargs["content"] == "群里在聊新番"
            assert kwargs["id"].startswith("f_")
            mock_enqueue.assert_awaited_once()

            mock_state.assert_called_once()


# ---------------------------------------------------------------------------
# run_glimpse — not interesting still writes state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_glimpse_not_interesting_still_writes_state():
    from app.life.glimpse import run_glimpse

    normal_time = datetime(2026, 4, 7, 14, 0, tzinfo=CST)
    mock_msg = _make_msg()

    llm_response = json.dumps(
        {
            "interesting": False,
        }
    )

    with patch(f"{MODULE}.get_session") as mock_gs:
        mock_session = AsyncMock()
        mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(f"{MODULE}._now_cst", return_value=normal_time),
            patch(
                f"{MODULE}.Q.find_latest_glimpse_state",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                f"{MODULE}.Q.find_last_bot_reply_time",
                new_callable=AsyncMock,
                return_value=0,
            ),
            patch(
                f"{MODULE}.get_unseen_messages",
                new_callable=AsyncMock,
                return_value=[mock_msg],
            ),
            patch(
                f"{MODULE}.format_timeline",
                new_callable=AsyncMock,
                return_value="[14:00] someone: hello",
            ),
            patch(
                f"{MODULE}.get_recent_proactive_records",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                f"{MODULE}._call_glimpse_llm",
                new_callable=AsyncMock,
                return_value=llm_response,
            ),
            patch(f"{MODULE}.Q.insert_fragment", new_callable=AsyncMock) as mock_frag,
            patch(
                f"{MODULE}.Q.insert_glimpse_state", new_callable=AsyncMock
            ) as mock_state,
            patch(
                f"{MODULE}.load_persona",
                new_callable=AsyncMock,
                return_value=MagicMock(display_name="赤尾", persona_lite=""),
            ),
            patch(
                f"{MODULE}._get_group_name",
                new_callable=AsyncMock,
                return_value="番剧群",
            ),
            patch(f"{MODULE}.enqueue_fragment_vectorize", new_callable=AsyncMock),
        ):
            result = await run_glimpse("akao-001", "oc_test")

            assert result == GlimpseResult.SKIPPED_NOT_INTERESTING
            mock_frag.assert_not_called()
            mock_state.assert_called_once()


# ---------------------------------------------------------------------------
# run_glimpse — want_to_speak submits proactive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_glimpse_want_to_speak_submits_proactive():
    from app.life.glimpse import run_glimpse

    normal_time = datetime(2026, 4, 7, 14, 0, tzinfo=CST)
    mock_msg = _make_msg()

    llm_response = json.dumps(
        {
            "interesting": True,
            "observation": "他们在讨论我喜欢的东西",
            "want_to_speak": True,
            "stimulus": "好想聊聊",
            "target_message_id": "m1",
        }
    )

    with patch(f"{MODULE}.get_session") as mock_gs:
        mock_session = AsyncMock()
        mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(f"{MODULE}._now_cst", return_value=normal_time),
            patch(
                f"{MODULE}.Q.find_latest_glimpse_state",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                f"{MODULE}.Q.find_last_bot_reply_time",
                new_callable=AsyncMock,
                return_value=0,
            ),
            patch(
                f"{MODULE}.get_unseen_messages",
                new_callable=AsyncMock,
                return_value=[mock_msg],
            ),
            patch(
                f"{MODULE}.format_timeline",
                new_callable=AsyncMock,
                return_value="[14:00] someone: 话题",
            ),
            patch(
                f"{MODULE}.get_recent_proactive_records",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                f"{MODULE}._call_glimpse_llm",
                new_callable=AsyncMock,
                return_value=llm_response,
            ),
            patch(f"{MODULE}.Q.insert_fragment", new_callable=AsyncMock),
            patch(
                f"{MODULE}.Q.insert_glimpse_state", new_callable=AsyncMock
            ) as mock_state,
            patch(f"{MODULE}.enqueue_fragment_vectorize", new_callable=AsyncMock),
            patch(
                f"{MODULE}.load_persona",
                new_callable=AsyncMock,
                return_value=MagicMock(display_name="赤尾", persona_lite=""),
            ),
            patch(
                f"{MODULE}._get_group_name",
                new_callable=AsyncMock,
                return_value="番剧群",
            ),
            patch(
                f"{MODULE}.submit_proactive_chat", new_callable=AsyncMock
            ) as mock_proactive,
        ):
            result = await run_glimpse("akao-001", "oc_test")

            assert result == GlimpseResult.FRAGMENT_CREATED
            # State observation should contain want_to_speak info
            mock_state.assert_called_once()
            call_kwargs = mock_state.call_args[1]
            assert "[want_to_speak]" in call_kwargs["observation"]
            assert "好想聊聊" in call_kwargs["observation"]
            # Should call submit_proactive_chat
            mock_proactive.assert_called_once_with(
                chat_id="oc_test",
                persona_id="akao-001",
                target_message_id="m1",
                stimulus="好想聊聊",
            )
