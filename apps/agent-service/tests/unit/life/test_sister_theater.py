"""Tests for app.life.sister_theater — family event generation."""

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.life.sister_theater import run_sister_theater

MODULE = "app.life.sister_theater"


@pytest.mark.asyncio
@patch(f"{MODULE}.Agent")
async def test_generates_theater(MockAgent):
    mock_instance = AsyncMock()
    mock_instance.run.return_value = MagicMock(
        content="[上午] 绫奈书包拉链坏了\n[下午] 千凪带了公司的蛋糕回来"
    )
    MockAgent.return_value = mock_instance

    result = await run_sister_theater(date(2026, 4, 15))

    assert "绫奈" in result or "千凪" in result
    mock_instance.run.assert_called_once()


@pytest.mark.asyncio
@patch(f"{MODULE}.Agent")
async def test_passes_prev_summary(MockAgent):
    mock_instance = AsyncMock()
    mock_instance.run.return_value = MagicMock(content="theater output")
    MockAgent.return_value = mock_instance

    await run_sister_theater(date(2026, 4, 15), prev_theater_summary="昨天千凪加班")

    call_kwargs = mock_instance.run.call_args
    pvars = call_kwargs.kwargs.get("prompt_vars") or call_kwargs[1].get("prompt_vars")
    assert pvars["prev_theater_summary"] == "昨天千凪加班"


@pytest.mark.asyncio
@patch(f"{MODULE}.Agent")
async def test_default_prev_summary_when_empty(MockAgent):
    mock_instance = AsyncMock()
    mock_instance.run.return_value = MagicMock(content="theater output")
    MockAgent.return_value = mock_instance

    await run_sister_theater(date(2026, 4, 15))

    call_kwargs = mock_instance.run.call_args
    pvars = call_kwargs.kwargs.get("prompt_vars") or call_kwargs[1].get("prompt_vars")
    assert pvars["prev_theater_summary"] == "（昨天没有小剧场记录）"
