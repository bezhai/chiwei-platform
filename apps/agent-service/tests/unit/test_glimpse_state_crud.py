"""glimpse_state CRUD 单元测试"""

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
