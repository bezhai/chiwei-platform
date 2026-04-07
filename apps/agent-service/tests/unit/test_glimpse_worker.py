"""glimpse_worker cron 单元测试"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MODULE = "app.workers.glimpse_worker"


@pytest.mark.asyncio
async def test_cron_glimpse_skips_non_prod_lane():
    """非 prod 泳道跳过"""
    with patch(f"{MODULE}.settings") as mock_settings:
        mock_settings.lane = "dev"
        from app.workers.glimpse_worker import cron_glimpse

        await cron_glimpse(None)
        # 不应调用任何 persona 相关逻辑 — 函数直接 return


@pytest.mark.asyncio
async def test_cron_glimpse_skips_non_browsing():
    """activity_type != browsing 时跳过"""
    mock_state = MagicMock(activity_type="sleeping")

    with (
        patch(f"{MODULE}.settings") as mock_settings,
        patch(f"{MODULE}.get_all_persona_ids", new_callable=AsyncMock, return_value=["akao-001"]),
        patch(f"{MODULE}._load_life_engine_state", new_callable=AsyncMock, return_value=mock_state),
        patch(f"{MODULE}.run_glimpse", new_callable=AsyncMock) as mock_glimpse,
    ):
        mock_settings.lane = "prod"
        from app.workers.glimpse_worker import cron_glimpse

        await cron_glimpse(None)
        mock_glimpse.assert_not_called()


@pytest.mark.asyncio
async def test_cron_glimpse_runs_when_browsing():
    """activity_type == browsing 时执行 glimpse"""
    mock_state = MagicMock(activity_type="browsing")

    with (
        patch(f"{MODULE}.settings") as mock_settings,
        patch(f"{MODULE}.get_all_persona_ids", new_callable=AsyncMock, return_value=["akao-001"]),
        patch(f"{MODULE}._load_life_engine_state", new_callable=AsyncMock, return_value=mock_state),
        patch(f"{MODULE}.run_glimpse", new_callable=AsyncMock) as mock_glimpse,
    ):
        mock_settings.lane = "prod"
        from app.workers.glimpse_worker import cron_glimpse

        await cron_glimpse(None)
        mock_glimpse.assert_called_once_with("akao-001")


@pytest.mark.asyncio
async def test_cron_glimpse_no_state_skips():
    """没有 life engine 状态 → 跳过"""
    with (
        patch(f"{MODULE}.settings") as mock_settings,
        patch(f"{MODULE}.get_all_persona_ids", new_callable=AsyncMock, return_value=["akao-001"]),
        patch(f"{MODULE}._load_life_engine_state", new_callable=AsyncMock, return_value=None),
        patch(f"{MODULE}.run_glimpse", new_callable=AsyncMock) as mock_glimpse,
    ):
        mock_settings.lane = "prod"
        from app.workers.glimpse_worker import cron_glimpse

        await cron_glimpse(None)
        mock_glimpse.assert_not_called()
