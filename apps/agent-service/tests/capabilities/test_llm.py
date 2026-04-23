from unittest.mock import AsyncMock, patch

import pytest

from app.capabilities.llm import LLMClient


@pytest.mark.asyncio
async def test_complete_delegates_to_langchain():
    with patch(
        "app.capabilities.llm.build_chat_model", new_callable=AsyncMock
    ) as m:
        fake = AsyncMock()
        fake.ainvoke = AsyncMock(return_value=type("R", (), {"content": "ok"})())
        m.return_value = fake
        client = LLMClient(model_id="deepseek-chat")
        out = await client.complete("hi")
    assert out == "ok"
    fake.ainvoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_stream_yields_chunks():
    async def fake_stream(*args, **kwargs):
        for s in ["a", "b", "c"]:
            yield type("C", (), {"content": s})()

    with patch(
        "app.capabilities.llm.build_chat_model", new_callable=AsyncMock
    ) as m:
        m.return_value = type("F", (), {"astream": fake_stream})()
        client = LLMClient(model_id="x")
        out = [c async for c in client.stream("hi")]
    assert out == ["a", "b", "c"]
