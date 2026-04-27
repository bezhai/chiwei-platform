"""Memory-vectorize @nodes — embed Fragment / Abstract rows into qdrant.

Thin adapters around the existing ``app.memory.vectorize_memory``
helpers; the heavy lifting (pg lookup, embedding, qdrant upsert) lives
there. These wrappers are what the dataflow runtime dispatches on
``MemoryFragmentRequest`` / ``MemoryAbstractRequest`` MQ frames.
"""
from __future__ import annotations

import logging

from app.domain.memory_request import MemoryAbstractRequest, MemoryFragmentRequest
from app.memory.vectorize_memory import vectorize_abstract, vectorize_fragment
from app.runtime import node

logger = logging.getLogger(__name__)


@node
async def vectorize_memory_fragment(req: MemoryFragmentRequest) -> None:
    logger.info("vectorize_memory_fragment: start id=%s", req.fragment_id)
    await vectorize_fragment(req.fragment_id)


@node
async def vectorize_memory_abstract(req: MemoryAbstractRequest) -> None:
    logger.info("vectorize_memory_abstract: start id=%s", req.abstract_id)
    await vectorize_abstract(req.abstract_id)
