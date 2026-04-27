from unittest.mock import AsyncMock, patch

import pytest

from app.agent.embedding import HybridEmbedding, SparseVector
from app.capabilities.vector_store import VectorStore


def _sample_embedding() -> HybridEmbedding:
    return HybridEmbedding(
        dense=[0.1, 0.2, 0.3],
        sparse=SparseVector(indices=[1, 7], values=[0.9, 0.5]),
    )


@pytest.mark.asyncio
async def test_upsert_unpacks_embedding_into_qdrant_call():
    emb = _sample_embedding()
    payload = {"chat_id": "c1", "content": "hello"}
    with patch("app.capabilities.vector_store.qdrant") as mq:
        mq.upsert_hybrid_vectors = AsyncMock(return_value=None)
        store = VectorStore(collection="messages_recall")
        await store.upsert("point-42", emb, payload)

    mq.upsert_hybrid_vectors.assert_awaited_once_with(
        "messages_recall",
        "point-42",
        [0.1, 0.2, 0.3],
        [1, 7],
        [0.9, 0.5],
        payload,
    )


@pytest.mark.asyncio
async def test_upsert_propagates_qdrant_failure():
    """Qdrant exceptions must surface — no swallow / bool-False return."""
    emb = _sample_embedding()
    with patch("app.capabilities.vector_store.qdrant") as mq:
        mq.upsert_hybrid_vectors = AsyncMock(
            side_effect=RuntimeError("qdrant offline")
        )
        store = VectorStore(collection="messages_recall")
        with pytest.raises(RuntimeError, match="qdrant offline"):
            await store.upsert("point-42", emb, {})


@pytest.mark.asyncio
async def test_upsert_dense_delegates_to_qdrant_upsert_vectors():
    """Cluster-style collections store a single dense vector per point."""
    payload = {"message_id": "m1", "chat_id": "c1"}
    dense = [0.1] * 1024
    with patch("app.capabilities.vector_store.qdrant") as mq:
        mq.upsert_vectors = AsyncMock(return_value=None)
        store = VectorStore(collection="messages_cluster")
        await store.upsert_dense("point-1", dense, payload)

    mq.upsert_vectors.assert_awaited_once_with(
        "messages_cluster",
        [dense],
        ["point-1"],
        [payload],
    )


@pytest.mark.asyncio
async def test_search_unpacks_embedding_and_forwards_filter():
    emb = _sample_embedding()
    hits = [{"id": "p1", "score": 0.9, "payload": {}}]
    sentinel_filter = object()
    with patch("app.capabilities.vector_store.qdrant") as mq:
        mq.hybrid_search = AsyncMock(return_value=hits)
        store = VectorStore(collection="messages_recall")
        out = await store.search(emb, limit=5, query_filter=sentinel_filter)

    assert out == hits
    mq.hybrid_search.assert_awaited_once_with(
        "messages_recall",
        [0.1, 0.2, 0.3],
        [1, 7],
        [0.9, 0.5],
        query_filter=sentinel_filter,
        limit=5,
    )
