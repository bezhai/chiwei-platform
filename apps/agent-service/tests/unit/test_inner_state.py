import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_build_inner_state_with_schedule():
    """有日程时，内心状态包含日程内容"""
    mock_schedule = MagicMock(
        content="早上看了芙莉莲第8集，下午想出门走走",
        mood="开心",
        energy_level=4,
    )
    with patch(
        "app.services.inner_state.get_plan_for_period",
        new_callable=AsyncMock,
        return_value=mock_schedule,
    ):
        from app.services.inner_state import build_inner_state

        result = await build_inner_state()

    assert "芙莉莲" in result
    assert "开心" in result
    assert len(result) > 0


@pytest.mark.asyncio
async def test_build_inner_state_without_schedule():
    """无日程时，返回基于时间的基本状态"""
    with patch(
        "app.services.inner_state.get_plan_for_period",
        new_callable=AsyncMock,
        return_value=None,
    ):
        from app.services.inner_state import build_inner_state

        result = await build_inner_state()

    assert "没什么特别的安排" in result
    assert len(result) > 0


@pytest.mark.asyncio
async def test_build_inner_state_schedule_no_mood():
    """有日程但无心情字段"""
    mock_schedule = MagicMock(content="今天想在家躺着", mood=None, energy_level=None)
    with patch(
        "app.services.inner_state.get_plan_for_period",
        new_callable=AsyncMock,
        return_value=mock_schedule,
    ):
        from app.services.inner_state import build_inner_state

        result = await build_inner_state()

    assert "躺着" in result
    assert "心情" not in result  # mood is None, should not appear
