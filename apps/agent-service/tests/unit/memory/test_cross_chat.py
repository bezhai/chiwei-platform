"""Tests for cross-chat context module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.data.queries import find_bot_names_for_persona, find_cross_chat_messages


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


@pytest.mark.asyncio
async def test_find_cross_chat_messages_calls_db():
    """Should call execute with correct filters."""
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
    mock_session.execute.assert_called_once()
