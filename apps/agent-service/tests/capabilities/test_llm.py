from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.capabilities.llm import LLMClient


@pytest.mark.asyncio
async def test_complete_delegates_to_langchain():
    with patch(
        "app.capabilities.llm.build_chat_model", new_callable=AsyncMock
    ) as m:
        fake = AsyncMock()
        fake.ainvoke = AsyncMock(return_value=SimpleNamespace(content="ok"))
        m.return_value = fake
        client = LLMClient(model_id="deepseek-chat")
        out = await client.complete("hi")
    assert out == "ok"
    fake.ainvoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_stream_yields_chunks():
    async def fake_stream(*args, **kwargs):
        for s in ["a", "b", "c"]:
            yield SimpleNamespace(content=s)

    with patch(
        "app.capabilities.llm.build_chat_model", new_callable=AsyncMock
    ) as m:
        m.return_value = SimpleNamespace(astream=fake_stream)
        client = LLMClient(model_id="x")
        out = [c async for c in client.stream("hi")]
    assert out == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_complete_passes_kwargs_through():
    with patch(
        "app.capabilities.llm.build_chat_model", new_callable=AsyncMock
    ) as m:
        fake = AsyncMock()
        fake.ainvoke = AsyncMock(return_value=SimpleNamespace(content="ok"))
        m.return_value = fake
        client = LLMClient(model_id="x")
        await client.complete("hi", temperature=0.7, max_tokens=100)
    fake.ainvoke.assert_awaited_once_with("hi", temperature=0.7, max_tokens=100)


@pytest.mark.asyncio
async def test_model_is_built_once_across_calls():
    with patch(
        "app.capabilities.llm.build_chat_model", new_callable=AsyncMock
    ) as m:
        fake = AsyncMock()
        fake.ainvoke = AsyncMock(return_value=SimpleNamespace(content="ok"))
        m.return_value = fake
        client = LLMClient(model_id="x")
        await client.complete("a")
        await client.complete("b")
    assert m.await_count == 1  # built_chat_model called once, model reused
