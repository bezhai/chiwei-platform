"""Tests for app.life.wild_agents — parallel wild agent execution."""

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.life.wild_agents import run_wild_agents

MODULE = "app.life.wild_agents"


def _mock_agent_factory(*, fail_prompt_id: str | None = None):
    """Return a side_effect for Agent() that builds per-config mocks."""

    def _make(cfg, **kwargs):
        instance = AsyncMock()
        if fail_prompt_id and cfg.prompt_id == fail_prompt_id:
            instance.run.side_effect = RuntimeError("agent failed")
        else:
            instance.run.return_value = MagicMock(
                content=f"output from {cfg.prompt_id}"
            )
        return instance

    return _make


@pytest.mark.asyncio
@patch(f"{MODULE}.Agent")
async def test_all_agents_succeed(MockAgent):
    MockAgent.side_effect = _mock_agent_factory()

    result = await run_wild_agents(date(2026, 4, 15))

    assert "互联网漫游" in result
    assert "城市观察" in result
    assert "兔子洞" in result
    assert "情绪天气" in result
    assert MockAgent.call_count == 4


@pytest.mark.asyncio
@patch(f"{MODULE}.Agent")
async def test_one_agent_fails_others_continue(MockAgent):
    MockAgent.side_effect = _mock_agent_factory(fail_prompt_id="wild_agent_city")

    result = await run_wild_agents(date(2026, 4, 15))

    assert "互联网漫游" in result
    assert "城市观察" not in result
    assert "兔子洞" in result
    assert "情绪天气" in result


@pytest.mark.asyncio
@patch(f"{MODULE}.Agent")
async def test_passes_correct_date_vars(MockAgent):
    instances = {}

    def _make(cfg, **kwargs):
        inst = AsyncMock()
        inst.run.return_value = MagicMock(content="ok")
        instances[cfg.prompt_id] = inst
        return inst

    MockAgent.side_effect = _make

    await run_wild_agents(date(2026, 4, 15), weather="多云 22°C")

    # Internet agent gets date/weekday/season
    call_kwargs = instances["wild_agent_internet"].run.call_args
    pvars = call_kwargs.kwargs.get("prompt_vars") or call_kwargs[1].get("prompt_vars")
    assert pvars["date"] == "2026-04-15"
    assert pvars["weekday"] == "周三"
    assert pvars["season"] == "春天"

    # City agent gets weather
    call_kwargs = instances["wild_agent_city"].run.call_args
    pvars = call_kwargs.kwargs.get("prompt_vars") or call_kwargs[1].get("prompt_vars")
    assert pvars["weather"] == "多云 22°C"
