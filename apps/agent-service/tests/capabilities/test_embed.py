from unittest.mock import AsyncMock, patch

import pytest

from app.agent.embedding import HybridEmbedding, SparseVector
from app.capabilities.embed import EmbedderClient


@pytest.mark.asyncio
async def test_dense_delegates():
    with patch(
        "app.capabilities.embed.embed_dense", new_callable=AsyncMock
    ) as m:
        m.return_value = [0.1, 0.2, 0.3]
        client = EmbedderClient(model_id="embedding-model")
        out = await client.dense(text="hello", images=["img-b64"], instructions="instr")

    assert out == [0.1, 0.2, 0.3]
    m.assert_awaited_once_with(
        "embedding-model",
        text="hello",
        image_base64_list=["img-b64"],
        instructions="instr",
    )


@pytest.mark.asyncio
async def test_hybrid_delegates():
    expected = HybridEmbedding(
        dense=[0.1, 0.2],
        sparse=SparseVector(indices=[1, 7], values=[0.9, 0.5]),
    )
    with patch(
        "app.capabilities.embed.embed_hybrid", new_callable=AsyncMock
    ) as m:
        m.return_value = expected
        client = EmbedderClient(model_id="embedding-model")
        out = await client.hybrid(text="hello")

    assert out is expected
    m.assert_awaited_once_with(
        "embedding-model",
        text="hello",
        image_base64_list=None,
        instructions="",
    )
