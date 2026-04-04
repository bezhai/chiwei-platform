# tests/unit/test_journal_crud.py
"""测试 Journal CRUD 函数的参数传递和 SQL 构建"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_upsert_journal_insert():
    """首次写入 journal"""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("app.orm.crud.AsyncSessionLocal", return_value=mock_session):
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        from app.orm.crud import upsert_journal
        await upsert_journal("daily", "2026-03-26", "今天过得不错", "test-model")

    mock_session.add.assert_called_once()
    mock_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_journal_returns_none_when_missing():
    """查不到日志时返回 None"""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("app.orm.crud.AsyncSessionLocal", return_value=mock_session):
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        from app.orm.crud import get_journal
        result = await get_journal("daily", "2026-03-26", persona_id="akao")

    assert result is None
