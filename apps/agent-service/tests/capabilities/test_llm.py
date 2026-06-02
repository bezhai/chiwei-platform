"""LLMClient — neutral ModelClient adapter for dataflow nodes.

Post-cutover, ``LLMClient`` resolves a neutral ``ModelClient`` via
``build_model_client`` and speaks neutral ``Message`` / ``StreamChunk`` to it,
exposing plain ``str`` in / out. These tests assert that contract against a
fake ModelClient (no langchain).
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.agent.neutral import Message, Role, StreamChunk
from app.capabilities.llm import LLMClient


def _fake_model(*, complete_text="ok"):
    model = AsyncMock()
    model.complete = AsyncMock(
        return_value=Message(role=Role.ASSISTANT, content=complete_text)
    )
    return model


@pytest.mark.asyncio
async def test_complete_returns_message_text():
    model = _fake_model(complete_text="ok")
    with patch(
        "app.capabilities.llm.build_model_client",
        new_callable=AsyncMock,
        return_value=model,
    ):
        client = LLMClient(model_id="deepseek-chat")
        out = await client.complete("hi")
    assert out == "ok"
    model.complete.assert_awaited_once()
    # the prompt was wrapped as a neutral USER message
    sent_messages = model.complete.await_args.args[0]
    assert sent_messages[0].role == Role.USER
    assert sent_messages[0].text() == "hi"


@pytest.mark.asyncio
async def test_stream_yields_text_from_chunks():
    async def fake_stream(messages, **kwargs):
        for s in ["a", "b", "c"]:
            yield StreamChunk(text=s)

    model = AsyncMock()
    model.stream = fake_stream
    with patch(
        "app.capabilities.llm.build_model_client",
        new_callable=AsyncMock,
        return_value=model,
    ):
        client = LLMClient(model_id="x")
        out = [c async for c in client.stream("hi")]
    assert out == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_stream_skips_non_text_chunks():
    async def fake_stream(messages, **kwargs):
        yield StreamChunk(reasoning="thinking")
        yield StreamChunk(text="visible")
        yield StreamChunk(finish_reason="stop")

    model = AsyncMock()
    model.stream = fake_stream
    with patch(
        "app.capabilities.llm.build_model_client",
        new_callable=AsyncMock,
        return_value=model,
    ):
        client = LLMClient(model_id="x")
        out = [c async for c in client.stream("hi")]
    assert out == ["visible"]


@pytest.mark.asyncio
async def test_complete_passes_kwargs_through():
    model = _fake_model()
    with patch(
        "app.capabilities.llm.build_model_client",
        new_callable=AsyncMock,
        return_value=model,
    ):
        client = LLMClient(model_id="x")
        await client.complete("hi", temperature=0.7, max_tokens=100)
    kwargs = model.complete.await_args.kwargs
    assert kwargs["temperature"] == 0.7
    assert kwargs["max_tokens"] == 100


@pytest.mark.asyncio
async def test_model_is_built_once_across_calls():
    model = _fake_model()
    with patch(
        "app.capabilities.llm.build_model_client",
        new_callable=AsyncMock,
        return_value=model,
    ) as m:
        client = LLMClient(model_id="x")
        await client.complete("a")
        await client.complete("b")
    assert m.await_count == 1  # build_model_client called once, model reused
