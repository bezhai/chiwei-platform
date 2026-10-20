from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from app.agent.contracts import AgentSpec
from app.capabilities.agent import AgentRunner

SPEC = AgentSpec(segments=("p",), model="m", trace_name="t")


class _Extracted(BaseModel):
    answer: str


@pytest.mark.asyncio
async def test_run_delegates():
    with patch("app.capabilities.agent.Agent") as MAgent:
        fake_agent = MagicMock()
        fake_agent.run = AsyncMock(return_value="final-msg")
        MAgent.return_value = fake_agent

        runner = AgentRunner(SPEC)
        out = await runner.run([{"role": "user", "content": "hi"}], prompt_vars={"x": 1})

    assert out == "final-msg"
    MAgent.assert_called_once_with(SPEC, tools=())
    fake_agent.run.assert_awaited_once_with(
        [{"role": "user", "content": "hi"}], prompt_vars={"x": 1}
    )


@pytest.mark.asyncio
async def test_stream_yields_chunks():
    async def fake_stream(*_args, **_kwargs):
        for chunk in ["a", "b", "c"]:
            yield chunk

    with patch("app.capabilities.agent.Agent") as MAgent:
        fake_agent = MagicMock()
        fake_agent.stream = fake_stream
        MAgent.return_value = fake_agent

        runner = AgentRunner(SPEC)
        out = [c async for c in runner.stream([{"role": "user", "content": "hi"}])]

    assert out == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_extract_delegates():
    expected = _Extracted(answer="ok")
    with patch("app.capabilities.agent.Agent") as MAgent:
        fake_agent = MagicMock()
        fake_agent.extract = AsyncMock(return_value=expected)
        MAgent.return_value = fake_agent

        runner = AgentRunner(SPEC, tools=[object()])
        out = await runner.extract(_Extracted, [{"role": "user", "content": "hi"}])

    assert out is expected
    # underlying Agent was constructed with the spec and tools we passed
    MAgent.assert_called_once()
    args, call_kwargs = MAgent.call_args
    assert args == (SPEC,)
    assert len(call_kwargs["tools"]) == 1

    fake_agent.extract.assert_awaited_once_with(
        _Extracted, [{"role": "user", "content": "hi"}]
    )
