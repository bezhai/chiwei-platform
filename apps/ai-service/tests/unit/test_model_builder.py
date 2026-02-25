"""test_model_builder.py — ModelBuilder TTL 缓存测试

场景覆盖：
- 缓存命中（第二次调用不查 DB）
- 缓存过期（TTL 到期后重新查 DB）
- 缓存穿透保护（DB 返回 None 也缓存）
- 缓存清除（clear_model_info_cache）
- DB 异常不缓存（下次仍查 DB）
"""

import time
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.infra import model_builder as mb
from app.agents.infra.model_builder import ModelBuilder, clear_model_info_cache

pytestmark = pytest.mark.unit


@pytest.fixture()
def mock_db_query(model_info_factory):
    """Mock get_model_and_provider_info DB 查询"""
    mock = AsyncMock(return_value=model_info_factory())
    with patch("app.orm.crud.get_model_and_provider_info", mock):
        yield mock


class TestCacheHit:
    """缓存命中 — 第二次调用不查 DB"""

    async def test_second_call_uses_cache(self, mock_db_query):
        result1 = await ModelBuilder._get_model_and_provider_info("test-model")
        result2 = await ModelBuilder._get_model_and_provider_info("test-model")

        assert result1 == result2
        assert mock_db_query.call_count == 1  # 只查了一次 DB

    async def test_different_model_ids_are_cached_independently(
        self, model_info_factory
    ):
        factory_a = model_info_factory(model_id="model-a", model_name="gpt-4")
        factory_b = model_info_factory(model_id="model-b", model_name="gpt-3.5")

        mock = AsyncMock(side_effect=[factory_a, factory_b])
        with patch("app.orm.crud.get_model_and_provider_info", mock):
            r_a = await ModelBuilder._get_model_and_provider_info("model-a")
            r_b = await ModelBuilder._get_model_and_provider_info("model-b")
            r_a2 = await ModelBuilder._get_model_and_provider_info("model-a")
            r_b2 = await ModelBuilder._get_model_and_provider_info("model-b")

        assert r_a == r_a2
        assert r_b == r_b2
        assert mock.call_count == 2  # 每个 model_id 各查一次


class TestCacheExpiry:
    """缓存过期 — TTL 到期后重新查 DB"""

    async def test_expired_cache_triggers_db_query(self, mock_db_query):
        # 第一次调用，写入缓存
        await ModelBuilder._get_model_and_provider_info("test-model")
        assert mock_db_query.call_count == 1

        # 模拟时间流逝，让缓存过期
        with patch.object(mb, "_CACHE_TTL_SECONDS", 0):
            # 手动让缓存过期：把 expire_at 设到过去
            for key in mb._model_info_cache:
                value, _ = mb._model_info_cache[key]
                mb._model_info_cache[key] = (value, time.monotonic() - 1)

            await ModelBuilder._get_model_and_provider_info("test-model")

        assert mock_db_query.call_count == 2  # 过期后重新查了 DB


class TestCachePenetrationProtection:
    """缓存穿透保护 — DB 返回 None 也缓存"""

    async def test_none_result_is_cached(self):
        mock = AsyncMock(return_value=None)
        with patch("app.orm.crud.get_model_and_provider_info", mock):
            r1 = await ModelBuilder._get_model_and_provider_info("non-existent")
            r2 = await ModelBuilder._get_model_and_provider_info("non-existent")

        assert r1 is None
        assert r2 is None
        assert mock.call_count == 1  # None 被缓存，只查了一次


class TestCacheClear:
    """缓存清除"""

    async def test_clear_cache_forces_db_query(self, mock_db_query):
        await ModelBuilder._get_model_and_provider_info("test-model")
        assert mock_db_query.call_count == 1

        clear_model_info_cache()

        await ModelBuilder._get_model_and_provider_info("test-model")
        assert mock_db_query.call_count == 2  # 清缓存后重新查 DB

    def test_clear_cache_empties_dict(self, model_info_factory):
        mb._model_info_cache["foo"] = (model_info_factory(), time.monotonic() + 999)
        assert len(mb._model_info_cache) > 0

        clear_model_info_cache()
        assert len(mb._model_info_cache) == 0


class TestDBExceptionNotCached:
    """DB 异常不缓存 — 下次仍查 DB"""

    async def test_db_error_not_cached(self, model_info_factory):
        info = model_info_factory()
        mock = AsyncMock(side_effect=[RuntimeError("DB down"), info])

        with patch("app.orm.crud.get_model_and_provider_info", mock):
            # 第一次: DB 异常, 返回 None
            r1 = await ModelBuilder._get_model_and_provider_info("test-model")
            assert r1 is None

            # 第二次: DB 恢复, 应重新查询而非返回缓存的 None
            r2 = await ModelBuilder._get_model_and_provider_info("test-model")
            assert r2 == info

        assert mock.call_count == 2  # 异常不缓存，两次都查了 DB

    async def test_db_error_returns_none(self):
        mock = AsyncMock(side_effect=RuntimeError("connection refused"))
        with patch("app.orm.crud.get_model_and_provider_info", mock):
            result = await ModelBuilder._get_model_and_provider_info("model-x")

        assert result is None
