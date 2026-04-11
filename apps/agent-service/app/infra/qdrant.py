"""Qdrant vector store — module-level ``qdrant`` instance.

Supports both pure-dense and hybrid (dense + sparse, RRF fusion) collections.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import numpy as np
from qdrant_client import QdrantClient
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
    """Thin wrapper around QdrantClient with domain-specific helpers."""

    def __init__(self) -> None:
        self.client = QdrantClient(
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
        try:
            self.client.create_collection(
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
        """Create a collection with dense + sparse vector support."""
        try:
            self.client.create_collection(
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

    async def delete_collection(self, collection_name: str) -> bool:
        try:
            self.client.delete_collection(collection_name=collection_name)
            return True
        except Exception as e:
            logger.error("Failed to delete collection: %s", e)
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
    ) -> bool:
        try:
            self.client.upsert(
                collection_name=collection,
                points=models.Batch(
                    ids=ids,
                    vectors=vectors,
                    payloads=payloads or [{}] * len(vectors),
                ),
            )
            return True
        except Exception as e:
            logger.error("Failed to upsert vectors: %s", e)
            return False

    async def upsert_hybrid_vectors(
        self,
        collection_name: str,
        point_id: str,
        dense_vector: list[float],
        sparse_indices: list[int],
        sparse_values: list[float],
        payload: dict[str, Any],
    ) -> bool:
        """Upsert a single point with both dense and sparse vectors."""
        try:
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
            self.client.upsert(collection_name=collection_name, points=[point])
            return True
        except Exception as e:
            logger.error("Failed to upsert hybrid vectors: %s", e)
            return False

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search_vectors(
        self,
        collection_name: str,
        query_vector: list[float],
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        try:
            results = self.client.search(
                collection_name=collection_name,
                query_vector=query_vector,
                limit=limit,
            )
            return [
                {"id": hit.id, "score": hit.score, "payload": hit.payload}
                for hit in results
            ]
        except Exception as e:
            logger.error("Vector search failed: %s", e)
            return []

    async def search_vectors_with_score_boost(
        self,
        collection_name: str,
        query_vector: list[float],
        query_filter: Filter | None = None,
        limit: int = 10,
        score_threshold: float = 0.8,
        time_boost_factor: float = 0.1,
    ) -> list[dict[str, Any]]:
        """Search with time-decay score boosting for recency weighting."""
        try:
            results = self.client.search(
                collection_name=collection_name,
                query_vector=query_vector,
                query_filter=query_filter,
                limit=limit * 2,
            )
            if not results:
                return []

            now_ts = datetime.now().timestamp()
            weighted: list[dict[str, Any]] = []

            for hit in results:
                if hit.score < score_threshold:
                    continue

                # Extract timestamp from payload
                payload = getattr(hit, "payload", None)
                if payload is not None and hasattr(payload, "get"):
                    msg_ts = payload.get("timestamp", now_ts)
                else:
                    msg_ts = now_ts
                if isinstance(msg_ts, str):
                    try:
                        msg_ts = float(msg_ts)
                    except (ValueError, TypeError):
                        msg_ts = now_ts

                hours_ago = (now_ts - msg_ts) / 3600
                time_weight = float(np.exp(-hours_ago * time_boost_factor))
                sort_score = hit.score + time_weight * time_boost_factor

                weighted.append(
                    {
                        "id": hit.id,
                        "score": hit.score,
                        "payload": hit.payload,
                        "sort_score": sort_score,
                        "time_weight": time_weight,
                    }
                )

            weighted.sort(key=lambda x: (x["score"], x["time_weight"]), reverse=True)
            return weighted[:limit]

        except Exception as e:
            logger.error("Score-boosted search failed: %s", e)
            return []

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
            results = self.client.query_points(
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
        ok = await qdrant.create_hybrid_collection(
            collection_name="messages_recall", dense_size=1024
        )
        if ok:
            logger.info("Qdrant recall hybrid collection created")
        else:
            logger.warning("Qdrant recall hybrid collection may already exist")

        ok = await qdrant.create_collection(
            collection_name="messages_cluster", vector_size=1024
        )
        if ok:
            logger.info("Qdrant cluster collection created")
        else:
            logger.warning("Qdrant cluster collection may already exist")
    except Exception as e:
        logger.error("Failed to init Qdrant collections: %s", e)
