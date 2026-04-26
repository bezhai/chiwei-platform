"""save_fragment: persist a Fragment to the two qdrant collections.

Writes the hybrid (dense + sparse) vector to ``messages_recall`` and the
dense-only cluster vector to ``messages_cluster``. The two upserts run
concurrently via ``asyncio.gather`` — they target different collections
and have no ordering dependency, so sequential awaits would just double
the tail latency.

Failure semantics: if either upsert raises, the exception propagates.
The runtime's durable-edge layer nacks + retries the upstream message.
qdrant upsert is idempotent per point_id, so a retry simply re-upserts
the side that already succeeded — no half-populated fragments, no
cross-collection transaction needed (qdrant doesn't support them
anyway).
"""
from __future__ import annotations

import asyncio
import logging

from app.agent.embedding import HybridEmbedding, SparseVector
from app.capabilities.vector_store import VectorStore
from app.domain.fragment import Fragment
from app.runtime import node

logger = logging.getLogger(__name__)

recall_store = VectorStore("messages_recall")
cluster_store = VectorStore("messages_cluster")


@node
async def save_fragment(frag: Fragment) -> None:
    logger.info(
        "save_fragment: start message_id=%s fragment_id=%s",
        frag.message_id,
        frag.fragment_id,
    )
    hybrid = HybridEmbedding(
        dense=frag.dense,
        sparse=SparseVector(
            indices=frag.sparse["indices"],
            values=frag.sparse["values"],
        ),
    )
    await asyncio.gather(
        recall_store.upsert(frag.fragment_id, hybrid, frag.recall_payload),
        cluster_store.upsert_dense(
            frag.fragment_id, frag.dense_cluster, frag.cluster_payload
        ),
    )
    logger.info(
        "save_fragment: done message_id=%s fragment_id=%s",
        frag.message_id,
        frag.fragment_id,
    )
