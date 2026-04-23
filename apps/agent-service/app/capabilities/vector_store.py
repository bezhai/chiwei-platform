"""VectorStore — thin adapter over the shared ``qdrant`` hybrid APIs.

Fixes the collection name per instance and accepts ``HybridEmbedding`` directly
so nodes can pipe ``EmbedderClient.hybrid(...) -> VectorStore.upsert(...)``
without manually unpacking dense / sparse vectors.
"""

from __future__ import annotations

from typing import Any

from app.agent.embedding import HybridEmbedding
from app.infra.qdrant import qdrant


class VectorStore:
    """Adapter over ``app.infra.qdrant.qdrant`` — one collection per instance."""

    def __init__(self, collection: str) -> None:
        self._collection = collection

    async def upsert(
        self,
        point_id: str,
        embedding: HybridEmbedding,
        payload: dict[str, Any],
    ) -> bool:
        return await qdrant.upsert_hybrid_vectors(
            self._collection,
            point_id,
            embedding.dense,
            embedding.sparse.indices,
            embedding.sparse.values,
            payload,
        )

    async def upsert_dense(
        self,
        point_id: str,
        dense: list[float],
        payload: dict[str, Any],
    ) -> bool:
        """Upsert a single dense-only vector into this collection.

        Used by cluster-style collections (``messages_cluster``) that store
        one dense vector per point with no sparse component. ``upsert`` is
        the hybrid sibling for recall-style collections.
        """
        return await qdrant.upsert_vectors(
            self._collection,
            [dense],
            [point_id],
            [payload],
        )

    async def search(
        self,
        embedding: HybridEmbedding,
        *,
        limit: int = 10,
        query_filter: Any = None,
    ) -> list[dict[str, Any]]:
        return await qdrant.hybrid_search(
            self._collection,
            embedding.dense,
            embedding.sparse.indices,
            embedding.sparse.values,
            query_filter=query_filter,
            limit=limit,
        )
