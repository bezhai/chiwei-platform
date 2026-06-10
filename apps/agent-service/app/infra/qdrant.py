"""Qdrant vector store — module-level ``qdrant`` instance.

Supports both pure-dense and hybrid (dense + sparse, RRF fusion) collections.
"""

from __future__ import annotations

import logging
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models
from qdrant_client.http.models import (
    Distance,
    ExtendedPointId,
    Filter,
    PointStruct,
    Prefetch,
    SparseIndexParams,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

from app.infra.config import settings

logger = logging.getLogger(__name__)


class _Qdrant:
    """Thin async wrapper around AsyncQdrantClient."""

    def __init__(self) -> None:
        self.client = AsyncQdrantClient(
            host=settings.qdrant_service_host,
            port=settings.qdrant_service_port,
            api_key=settings.qdrant_service_api_key,
            prefer_grpc=False,
            https=False,
        )

    # ------------------------------------------------------------------
    # Collection management
    # ------------------------------------------------------------------

    async def create_collection(self, collection_name: str, vector_size: int) -> bool:
        """Return True if newly created, False if create_collection rejected
        (typically "collection already exists"); init_collections relies on
        this False signal to log a benign warning.

        contract-allowed False (§4.8): idempotent create. Connection /
        auth failures still surface as warnings here; the caller
        (``init_collections``) wraps this in its own try/except and only
        runs at startup so transient infra failure becomes "collection
        not created" not "service down" — startup will re-attempt next
        boot. NOTE: this is a documented A3 exception. Tightening to
        catch only ``UnexpectedResponse`` (collection-exists) is tracked
        as backlog L1 once we have qdrant test coverage."""
        try:
            await self.client.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
            )
            return True
        except Exception as e:
            logger.warning("Failed to create collection: %s", e)
            return False

    async def create_hybrid_collection(
        self, collection_name: str, dense_size: int = 1024
    ) -> bool:
        """Create a collection with dense + sparse vector support.

        contract-allowed False (§4.8): same idempotent-create semantics as
        ``create_collection``. Tightening to ``UnexpectedResponse``-only
        is tracked as backlog L1."""
        try:
            await self.client.create_collection(
                collection_name=collection_name,
                vectors_config={
                    "dense": VectorParams(size=dense_size, distance=Distance.COSINE),
                },
                sparse_vectors_config={
                    "sparse": SparseVectorParams(
                        index=SparseIndexParams(on_disk=False),
                    ),
                },
            )
            return True
        except Exception as e:
            logger.warning("Failed to create hybrid collection: %s", e)
            return False

    # ------------------------------------------------------------------
    # Upsert
    # ------------------------------------------------------------------

    async def upsert_vectors(
        self,
        collection: str,
        vectors: list[list[float]],
        ids: list[ExtendedPointId],
        payloads: list[dict[str, Any]] | None = None,
    ) -> None:
        await self.client.upsert(
            collection_name=collection,
            points=models.Batch(
                ids=ids,
                vectors=vectors,
                payloads=payloads or [{}] * len(vectors),
            ),
        )

    async def upsert_hybrid_vectors(
        self,
        collection_name: str,
        point_id: str,
        dense_vector: list[float],
        sparse_indices: list[int],
        sparse_values: list[float],
        payload: dict[str, Any],
    ) -> None:
        """Upsert a single point with both dense and sparse vectors."""
        point = PointStruct(
            id=point_id,
            vector={
                "dense": dense_vector,
                "sparse": SparseVector(
                    indices=sparse_indices, values=sparse_values
                ),
            },
            payload=payload,
        )
        await self.client.upsert(collection_name=collection_name, points=[point])

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def hybrid_search(
        self,
        collection_name: str,
        dense_vector: list[float],
        sparse_indices: list[int],
        sparse_values: list[float],
        query_filter: Filter | None = None,
        limit: int = 10,
        prefetch_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Hybrid dense + sparse search with RRF fusion."""
        try:
            prefetch_count = prefetch_limit or limit * 5
            results = await self.client.query_points(
                collection_name=collection_name,
                prefetch=[
                    Prefetch(
                        query=dense_vector,
                        using="dense",
                        limit=prefetch_count,
                        filter=query_filter,
                    ),
                    Prefetch(
                        query=SparseVector(
                            indices=sparse_indices, values=sparse_values
                        ),
                        using="sparse",
                        limit=prefetch_count,
                        filter=query_filter,
                    ),
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                limit=limit,
            )
            return [
                {
                    "id": point.id,
                    "score": point.score,
                    "payload": point.payload,
                }
                for point in results.points
            ]
        except Exception as e:
            logger.error("Hybrid search failed: %s", e)
            return []


# Module-level instance
qdrant = _Qdrant()


async def init_collections() -> None:
    """Create standard collections if they don't already exist."""
    try:
        # v4 memory collections — dense only, 1024d COSINE
        for name in ("memory_fragment", "memory_abstract"):
            ok = await qdrant.create_collection(
                collection_name=name, vector_size=1024
            )
            if ok:
                logger.info("Qdrant v4 collection %s created", name)
            else:
                logger.warning("Qdrant v4 collection %s may already exist", name)
    except Exception as e:
        logger.error("Failed to init Qdrant collections: %s", e)
