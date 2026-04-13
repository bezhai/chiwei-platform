"""Tests for app.life.schedule — Agent Team daily plan pipeline."""

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.life.schedule import (
    _fetch_search_anchors,
    _format_recent_schedules,
    _run_shared_pipeline,
    generate_daily_plan,
)

MODULE = "app.life.schedule"


# ---------------------------------------------------------------------------
# _format_recent_schedules
# ---------------------------------------------------------------------------


def test_format_recent_schedules_empty():
    assert _format_recent_schedules([]) == "（没有前几天的日程）"


def test_format_recent_schedules_formats_correctly():
    s1 = MagicMock(period_start="2026-04-14", content="昨天的日程")
    s2 = MagicMock(period_start="2026-04-13", content="前天的日程")
    result = _format_recent_schedules([s1, s2])
    assert "[2026-04-14]" in result
    assert "昨天的日程" in result
    assert "---" in result


# ---------------------------------------------------------------------------
# _fetch_search_anchors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch(f"{MODULE}.search_web")
async def test_search_anchors_returns_results(mock_search):
    mock_search.ainvoke = AsyncMock(return_value="[1] 杭州今天 22°C 多云")

    result = await _fetch_search_anchors(date(2026, 4, 15))

    assert "杭州" in result
    assert mock_search.ainvoke.call_count == 3  # 3 queries


@pytest.mark.asyncio
@patch(f"{MODULE}.search_web")
async def test_search_anchors_handles_failure(mock_search):
    mock_search.ainvoke = AsyncMock(side_effect=RuntimeError("timeout"))

    result = await _fetch_search_anchors(date(2026, 4, 15))

    assert result == "（搜索锚点获取失败）"


# ---------------------------------------------------------------------------
# _run_shared_pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch(f"{MODULE}.run_sister_theater", new_callable=AsyncMock)
@patch(f"{MODULE}._fetch_search_anchors", new_callable=AsyncMock)
@patch(f"{MODULE}.run_wild_agents", new_callable=AsyncMock)
async def test_shared_pipeline_runs_all_three(mock_wild, mock_search, mock_theater):
    mock_wild.return_value = "wild materials"
    mock_search.return_value = "search anchors"
    mock_theater.return_value = "theater events"

    wild, anchors, theater = await _run_shared_pipeline(date(2026, 4, 15))

    assert wild == "wild materials"
    assert anchors == "search anchors"
    assert theater == "theater events"


@pytest.mark.asyncio
@patch(f"{MODULE}.run_sister_theater", new_callable=AsyncMock)
@patch(f"{MODULE}._fetch_search_anchors", new_callable=AsyncMock)
@patch(f"{MODULE}.run_wild_agents", new_callable=AsyncMock)
async def test_shared_pipeline_handles_partial_failure(mock_wild, mock_search, mock_theater):
    mock_wild.side_effect = RuntimeError("wild failed")
    mock_search.return_value = "search anchors"
    mock_theater.return_value = "theater events"

    wild, anchors, theater = await _run_shared_pipeline(date(2026, 4, 15))

    assert wild == ""  # failed → empty string
    assert anchors == "search anchors"
    assert theater == "theater events"


# ---------------------------------------------------------------------------
# generate_daily_plan (integration-level mock test)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch(f"{MODULE}._run_persona_pipeline", new_callable=AsyncMock)
@patch(f"{MODULE}._run_shared_pipeline", new_callable=AsyncMock)
async def test_generate_daily_plan_wires_shared_to_persona(mock_shared, mock_persona):
    mock_shared.return_value = ("wild", "search", "theater")
    mock_persona.return_value = "schedule content"

    result = await generate_daily_plan("akao", date(2026, 4, 15))

    assert result == "schedule content"
    mock_persona.assert_called_once_with("akao", date(2026, 4, 15), "wild", "search", "theater")
