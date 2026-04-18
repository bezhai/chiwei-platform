"""Memory v4 recall engine — pure function over Qdrant semantic search + graph traversal.

Given a persona_id and a list of queries, return recalled abstracts (each with
its supporting facts) plus optional standalone facts. Touches every recalled
node's ``last_touched_at`` so frequently-recalled memories stay clear.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from qdrant_client.http.models import FieldCondition, Filter, MatchValue

from app.agent.embedding import embed_dense
from app.data.queries import (
    get_abstract_by_id,
    get_fragment_by_id,
    list_edges_to,
    touch_abstract,
    touch_fragment,
)
from app.data.session import get_session
from app.infra.qdrant import qdrant
from app.memory.vectorize_memory import COLLECTION_ABSTRACT, COLLECTION_FRAGMENT

logger = logging.getLogger(__name__)

EMBEDDING_MODEL_ID = "embedding-model"


@dataclass
class RecallResult:
    """Result of a recall run.

    ``abstracts`` is the primary payload — each entry carries the abstract's
    ``id / subject / content / clarity`` plus a ``supporting_facts`` list of
    fragments linked via ``supports`` edges.

    ``facts`` is only populated when ``also_search_facts=True`` and contains
    fragments recalled directly (not already attached to a surfaced abstract).
    """

    abstracts: list[dict[str, Any]] = field(default_factory=list)
    facts: list[dict[str, Any]] = field(default_factory=list)


def _persona_filter(persona_id: str) -> Filter:
    return Filter(
        must=[
            FieldCondition(
                key="persona_id",
                match=MatchValue(value=persona_id),
            )
        ],
        must_not=[
            FieldCondition(
                key="clarity",
                match=MatchValue(value="forgotten"),
            )
        ],
    )


async def _search_abstracts(
    *, persona_id: str, query_vec: list[float], k: int
) -> list[str]:
    res = await qdrant.client.query_points(
        collection_name=COLLECTION_ABSTRACT,
        query=query_vec,
        query_filter=_persona_filter(persona_id),
        limit=k,
    )
    return [str(p.id) for p in res.points]


async def _search_fragments(
    *, persona_id: str, query_vec: list[float], k: int
) -> list[str]:
    res = await qdrant.client.query_points(
        collection_name=COLLECTION_FRAGMENT,
        query=query_vec,
        query_filter=_persona_filter(persona_id),
        limit=k,
    )
    return [str(p.id) for p in res.points]


async def run_recall(
    *,
    persona_id: str,
    queries: list[str],
    k_abs: int = 5,
    k_facts_per_abs: int = 3,
    also_search_facts: bool = False,
    fact_k_per_query: int = 5,
) -> RecallResult:
    """Recall abstracts (+ supporting facts) for a persona given query strings.

    For each non-blank query: embed → search ``memory_abstract``. For each
    abstract: fetch its ``supports`` edges and the linked fragments (up to
    ``k_facts_per_abs``). Forgotten nodes are skipped. Every surfaced abstract
    and fragment is touched so recall itself counts as usage.

    If ``also_search_facts`` is True, additionally search ``memory_fragment``
    directly per query for facts that aren't attached to any surfaced abstract.
    """
    result = RecallResult()
    if not queries:
        return result

    seen_abstract_ids: set[str] = set()
    seen_fragment_ids: set[str] = set()

    for raw_query in queries:
        query = raw_query.strip() if isinstance(raw_query, str) else ""
        if not query:
            continue

        query_vec = await embed_dense(EMBEDDING_MODEL_ID, text=query)

        abstract_ids = await _search_abstracts(
            persona_id=persona_id, query_vec=query_vec, k=k_abs
        )

        for aid in abstract_ids:
            if aid in seen_abstract_ids:
                continue

            async with get_session() as s:
                abstract = await get_abstract_by_id(s, aid)
            if abstract is None or getattr(abstract, "clarity", None) == "forgotten":
                continue

            async with get_session() as s:
                edges = await list_edges_to(
                    s,
                    persona_id=persona_id,
                    to_id=aid,
                    edge_type="supports",
                )

            supporting_facts: list[dict[str, Any]] = []
            for edge in edges[:k_facts_per_abs]:
                fid = str(edge.from_id)
                async with get_session() as s:
                    fragment = await get_fragment_by_id(s, fid)
                if fragment is None or getattr(fragment, "clarity", None) == "forgotten":
                    continue
                supporting_facts.append(
                    {
                        "id": fragment.id,
                        "content": fragment.content,
                        "clarity": fragment.clarity,
                    }
                )
                seen_fragment_ids.add(fid)

            seen_abstract_ids.add(aid)
            result.abstracts.append(
                {
                    "id": abstract.id,
                    "subject": abstract.subject,
                    "content": abstract.content,
                    "clarity": abstract.clarity,
                    "supporting_facts": supporting_facts,
                }
            )

        if also_search_facts:
            fragment_ids = await _search_fragments(
                persona_id=persona_id, query_vec=query_vec, k=fact_k_per_query
            )
            for fid in fragment_ids:
                if fid in seen_fragment_ids:
                    continue
                async with get_session() as s:
                    fragment = await get_fragment_by_id(s, fid)
                if fragment is None or getattr(fragment, "clarity", None) == "forgotten":
                    continue
                seen_fragment_ids.add(fid)
                result.facts.append(
                    {
                        "id": fragment.id,
                        "content": fragment.content,
                        "clarity": fragment.clarity,
                    }
                )

    # Touch every surfaced node so recall counts as usage.
    if seen_abstract_ids or seen_fragment_ids:
        async with get_session() as s:
            for aid in seen_abstract_ids:
                await touch_abstract(s, aid)
            for fid in seen_fragment_ids:
                await touch_fragment(s, fid)

    return result
