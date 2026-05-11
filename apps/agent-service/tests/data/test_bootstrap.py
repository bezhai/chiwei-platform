import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.data.bootstrap import ensure_business_schema


def _make_engine_mock(mock_conn: AsyncMock) -> MagicMock:
    """Build a mock engine whose .begin() works as an async context manager."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    mock_engine = MagicMock()
    mock_engine.begin.return_value = cm
    return mock_engine


@pytest.mark.asyncio
async def test_ensure_business_schema_triggers_for_coe_lane():
    mock_conn = AsyncMock()
    mock_engine = _make_engine_mock(mock_conn)

    with patch("app.data.bootstrap.engine", mock_engine), \
         patch("app.data.bootstrap.settings") as mock_settings:
        mock_settings.lane = "coe-foo"
        await ensure_business_schema()

    mock_conn.run_sync.assert_called_once()


@pytest.mark.asyncio
async def test_ensure_business_schema_skips_for_prod():
    mock_engine = MagicMock()
    with patch("app.data.bootstrap.engine", mock_engine), \
         patch("app.data.bootstrap.settings") as mock_settings:
        mock_settings.lane = "prod"
        await ensure_business_schema()
    mock_engine.begin.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_business_schema_skips_for_blue():
    mock_engine = MagicMock()
    with patch("app.data.bootstrap.engine", mock_engine), \
         patch("app.data.bootstrap.settings") as mock_settings:
        mock_settings.lane = "blue"
        await ensure_business_schema()
    mock_engine.begin.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_business_schema_skips_for_ppe():
    """ppe-* 连 prod 基建，绝不能跑 create_all（会在 prod DB 上跑）"""
    mock_engine = MagicMock()
    with patch("app.data.bootstrap.engine", mock_engine), \
         patch("app.data.bootstrap.settings") as mock_settings:
        mock_settings.lane = "ppe-canary"
        await ensure_business_schema()
    mock_engine.begin.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_business_schema_skips_for_none_lane():
    """LANE env 没注入（None）也不跑 create_all"""
    mock_engine = MagicMock()
    with patch("app.data.bootstrap.engine", mock_engine), \
         patch("app.data.bootstrap.settings") as mock_settings:
        mock_settings.lane = None
        await ensure_business_schema()
    mock_engine.begin.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_business_schema_failure_raises():
    """create_all 失败必须 raise 让 pod CrashLoopBackoff，绝不 swallow"""
    mock_conn = AsyncMock()
    mock_conn.run_sync.side_effect = Exception("PG connection refused")
    mock_engine = _make_engine_mock(mock_conn)

    with patch("app.data.bootstrap.engine", mock_engine), \
         patch("app.data.bootstrap.settings") as mock_settings:
        mock_settings.lane = "coe-foo"
        with pytest.raises(Exception, match="PG connection refused"):
            await ensure_business_schema()
