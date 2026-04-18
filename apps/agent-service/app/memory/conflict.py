"""Conflict detection for new abstract-memory commits.

Before committing a new abstract about a subject, check whether any
existing abstract for the same subject is semantically close enough that
the caller should be warned (e.g. the new fact may contradict or
overlap with a prior belief).

Embedding failures are treated as soft errors: on failure we return
``None`` so commits proceed uninterrupted — conflict detection is a
hint, not a gate.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from app.agent.embedding import embed_dense
from app.data.queries import get_abstracts_by_subject
from app.data.session import get_session
from app.memory.vectorize_memory import EMBEDDING_MODEL_ID

logger = logging.getLogger(__name__)

DEFAULT_SIMILARITY_THRESHOLD = 0.85


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity; returns 0.0 if either vector has zero norm."""
    if len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=False):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


async def detect_conflict(
    *,
    persona_id: str,
    subject: str,
    content: str,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> dict[str, Any] | None:
    """Return a conflict hint if a same-subject abstract is semantically similar.

    The hint shape is::

        {
            "conflicting_abstract_id": str,
            "conflicting_content": str,
            "similarity": float,  # rounded to 3 decimals
        }

    Returns ``None`` when there are no existing abstracts for the subject,
    when the top match is below ``similarity_threshold``, or when embedding
    calls fail.
    """
    async with get_session() as s:
        existing = await get_abstracts_by_subject(
            s, persona_id=persona_id, subject=subject, limit=10
        )
    if not existing:
        return None

    try:
        new_vec = await embed_dense(EMBEDDING_MODEL_ID, text=content)
    except Exception as e:
        logger.warning(
            "detect_conflict: embed new content failed persona=%s subject=%s err=%s",
            persona_id,
            subject,
            e,
        )
        return None

    best_score = -1.0
    best_abstract = None
    for a in existing:
        try:
            old_vec = await embed_dense(EMBEDDING_MODEL_ID, text=a.content)
        except Exception as e:
            logger.warning(
                "detect_conflict: embed existing abstract failed id=%s err=%s",
                getattr(a, "id", "?"),
                e,
            )
            continue
        score = _cosine(new_vec, old_vec)
        if score > best_score:
            best_score = score
            best_abstract = a

    if best_abstract is None or best_score < similarity_threshold:
        return None

    return {
        "conflicting_abstract_id": best_abstract.id,
        "conflicting_content": best_abstract.content,
        "similarity": round(best_score, 3),
    }
