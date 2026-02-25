"""test_vectorize_permission.py — check_group_allows_download 权限检查测试"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.workers.vectorize_worker import (
    _PERMISSION_CACHE_TTL,
    _download_permission_cache,
    check_group_allows_download,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_cache():
    """每个测试前后清空缓存"""
    _download_permission_cache.clear()
    yield
    _download_permission_cache.clear()


def _mock_session(scalar_return):
    """构建 mock AsyncSessionLocal 上下文管理器"""
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = scalar_return

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    return mock_ctx


class TestCheckGroupAllowsDownload:
    """check_group_allows_download 权限检查逻辑"""

    async def test_p2p_always_allows(self):
        """P2P 聊天直接放行，不查 DB"""
        with patch(
            "app.workers.vectorize_worker.AsyncSessionLocal"
        ) as mock_session_local:
            result = await check_group_allows_download("chat_123", "p2p")
            assert result is True
            mock_session_local.assert_not_called()

    async def test_group_not_anyone_blocks(self):
        """download_has_permission_setting = 'not_anyone' → 禁止下载"""
        with patch(
            "app.workers.vectorize_worker.AsyncSessionLocal",
            return_value=_mock_session("not_anyone"),
        ):
            result = await check_group_allows_download("chat_group_1", "group")
            assert result is False

    async def test_group_all_members_allows(self):
        """download_has_permission_setting = 'all_members' → 允许下载"""
        with patch(
            "app.workers.vectorize_worker.AsyncSessionLocal",
            return_value=_mock_session("all_members"),
        ):
            result = await check_group_allows_download("chat_group_2", "group")
            assert result is True

    async def test_group_no_record_defaults_allow(self):
        """群聊无记录（scalar 返回 None）→ 默认允许"""
        with patch(
            "app.workers.vectorize_worker.AsyncSessionLocal",
            return_value=_mock_session(None),
        ):
            result = await check_group_allows_download("chat_unknown", "group")
            assert result is True

    async def test_db_error_fail_open(self):
        """DB 查询异常 → fail-open，返回 True"""
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(side_effect=Exception("DB connection error"))
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "app.workers.vectorize_worker.AsyncSessionLocal",
            return_value=mock_ctx,
        ):
            result = await check_group_allows_download("chat_broken", "group")
            assert result is True

    async def test_cache_hit(self):
        """缓存命中时不查 DB"""
        with patch(
            "app.workers.vectorize_worker.AsyncSessionLocal",
            return_value=_mock_session("not_anyone"),
        ) as mock_session_local:
            # 第一次调用 → 查 DB
            r1 = await check_group_allows_download("chat_cached", "group")
            assert r1 is False
            assert mock_session_local.call_count == 1

            # 第二次调用 → 命中缓存
            r2 = await check_group_allows_download("chat_cached", "group")
            assert r2 is False
            assert mock_session_local.call_count == 1  # 没有新的 DB 调用

    async def test_cache_expiry(self):
        """缓存过期后重新查 DB"""
        with patch(
            "app.workers.vectorize_worker.AsyncSessionLocal",
            return_value=_mock_session("not_anyone"),
        ):
            await check_group_allows_download("chat_expire", "group")

        # 手动设置缓存为已过期
        _download_permission_cache["chat_expire"] = (False, time.monotonic() - 1)

        # 下次调用应重新查 DB（这次返回 all_members）
        with patch(
            "app.workers.vectorize_worker.AsyncSessionLocal",
            return_value=_mock_session("all_members"),
        ):
            result = await check_group_allows_download("chat_expire", "group")
            assert result is True

    async def test_cache_ttl_value(self):
        """缓存 TTL 为 10 分钟"""
        assert _PERMISSION_CACHE_TTL == 600
