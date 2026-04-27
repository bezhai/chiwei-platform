"""Memory-vectorize MQ request envelopes.

These ``Data`` types only carry the DB id of a memory v4 row queued for
embedding. They never reach pg — the real Fragment / Abstract row is
already persisted by the publisher (``commit_abstract``, ``afterthought``,
``glimpse``); the request is just an MQ frame triggering the vectorize
worker to embed and upsert into qdrant.
"""
from __future__ import annotations

from typing import Annotated

from app.runtime import Data, Key


class MemoryFragmentRequest(Data):
    fragment_id: Annotated[str, Key]

    class Meta:
        transient = True


class MemoryAbstractRequest(Data):
    abstract_id: Annotated[str, Key]

    class Meta:
        transient = True
