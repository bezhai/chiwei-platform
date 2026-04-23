from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from app.agent.core import AgentConfig
from app.capabilities.agent import AgentRunner


class _Extracted(BaseModel):
    answer: str


@pytest.mark.asyncio
async def test_run_delegates():
    cfg = AgentConfig(prompt_id="p", model_id="m", trace_name="t")
    with patch("app.capabilities.agent.Agent") as MAgent:
        fake_agent = MagicMock()
        fake_agent.run = AsyncMock(return_value="final-msg")
        MAgent.return_value = fake_agent

        runner = AgentRunner(cfg)
        out = await runner.run([{"role": "user", "content": "hi"}], prompt_vars={"x": 1})

    assert out == "final-msg"
    MAgent.assert_called_once_with(cfg, tools=None, model_kwargs=None)
    fake_agent.run.assert_awaited_once_with(
        [{"role": "user", "content": "hi"}], prompt_vars={"x": 1}
    )


@pytest.mark.asyncio
async def test_stream_yields_chunks():
    cfg = AgentConfig(prompt_id="p", model_id="m")

    async def fake_stream(*_args, **_kwargs):
        for chunk in ["a", "b", "c"]:
            yield chunk

    with patch("app.capabilities.agent.Agent") as MAgent:
        fake_agent = MagicMock()
        fake_agent.stream = fake_stream
        MAgent.return_value = fake_agent

        runner = AgentRunner(cfg)
        out = [c async for c in runner.stream([{"role": "user", "content": "hi"}])]

    assert out == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_extract_delegates():
    cfg = AgentConfig(prompt_id="p", model_id="m")
    expected = _Extracted(answer="ok")
    with patch("app.capabilities.agent.Agent") as MAgent:
        fake_agent = MagicMock()
        fake_agent.extract = AsyncMock(return_value=expected)
        MAgent.return_value = fake_agent

        runner = AgentRunner(cfg, tools=[object()], model_kwargs={"temperature": 0.5})
        out = await runner.extract(_Extracted, [{"role": "user", "content": "hi"}])

    assert out is expected
    # underlying Agent was constructed with the kwargs we passed
    MAgent.assert_called_once()
    _, call_kwargs = MAgent.call_args
    assert call_kwargs["model_kwargs"] == {"temperature": 0.5}
    assert len(call_kwargs["tools"]) == 1

    fake_agent.extract.assert_awaited_once_with(
        _Extracted, [{"role": "user", "content": "hi"}]
    )
