"""Memory v4 vectorization — embed and upsert fragments/abstracts to Qdrant.

Called by vectorize-worker when consuming memory_vectorize tasks.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agent.embedding import embed_dense
from app.data.queries import get_abstract_by_id, get_fragment_by_id
from app.data.session import get_session
from app.infra.qdrant import qdrant

logger = logging.getLogger(__name__)

COLLECTION_FRAGMENT = "memory_fragment"
COLLECTION_ABSTRACT = "memory_abstract"
EMBEDDING_MODEL_ID = "embedding-model"


async def vectorize_fragment(fragment_id: str) -> bool:
    """Embed a Fragment's content and upsert into the memory_fragment Qdrant collection."""
    async with get_session() as s:
        fragment = await get_fragment_by_id(s, fragment_id)
    if fragment is None:
        logger.warning("Fragment %s not found for vectorize", fragment_id)
        return False
    if not fragment.content.strip():
        logger.warning("Fragment %s has empty content", fragment_id)
        return False

    vector = await embed_dense(EMBEDDING_MODEL_ID, text=fragment.content)

    payload: dict[str, Any] = {
        "persona_id": fragment.persona_id,
        "source": fragment.source,
        "chat_id": fragment.chat_id,
        "clarity": fragment.clarity,
        "last_touched_at": fragment.last_touched_at.isoformat() if fragment.last_touched_at else None,
    }
    ok = await qdrant.upsert_vectors(
        collection=COLLECTION_FRAGMENT,
        vectors=[vector],
        ids=[fragment.id],
        payloads=[payload],
    )
    if not ok:
        raise RuntimeError(f"Qdrant upsert failed for fragment {fragment_id}")
    return True


async def vectorize_abstract(abstract_id: str) -> bool:
    """Embed an AbstractMemory's subject+content and upsert into memory_abstract."""
    async with get_session() as s:
        a = await get_abstract_by_id(s, abstract_id)
    if a is None:
        logger.warning("Abstract %s not found for vectorize", abstract_id)
        return False
    if not a.content.strip():
        logger.warning("Abstract %s has empty content", abstract_id)
        return False

    # Concatenate subject + content so subject terms contribute to embedding signal
    text = f"[{a.subject}] {a.content}"
    vector = await embed_dense(EMBEDDING_MODEL_ID, text=text)

    payload: dict[str, Any] = {
        "persona_id": a.persona_id,
        "subject": a.subject,
        "created_by": a.created_by,
        "clarity": a.clarity,
        "last_touched_at": a.last_touched_at.isoformat() if a.last_touched_at else None,
    }
    ok = await qdrant.upsert_vectors(
        collection=COLLECTION_ABSTRACT,
        vectors=[vector],
        ids=[a.id],
        payloads=[payload],
    )
    if not ok:
        raise RuntimeError(f"Qdrant upsert failed for abstract {abstract_id}")
    return True
