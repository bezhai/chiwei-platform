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
    get_fragments_by_ids,
    list_edges_to,
    touch_abstracts_bulk,
    touch_fragments_bulk,
)
from app.data.session import get_session
from app.infra.qdrant import qdrant
from app.memory.vectorize_memory import (
    COLLECTION_ABSTRACT,
    COLLECTION_FRAGMENT,
    EMBEDDING_MODEL_ID,
)

logger = logging.getLogger(__name__)


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


async def _vector_search(
    *, collection: str, persona_id: str, query_vec: list[float], k: int
) -> list[str]:
    res = await qdrant.client.query_points(
        collection_name=collection,
        query=query_vec,
        query_filter=_persona_filter(persona_id),
        limit=k,
        with_payload=["db_id"],
    )
    # Qdrant ids are UUID5-derived; the prefixed DB id lives in payload.db_id
    return [str(p.payload["db_id"]) for p in res.points if p.payload and p.payload.get("db_id")]


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

    for query in queries:
        query = query.strip()
        if not query:
            continue

        query_vec = await embed_dense(EMBEDDING_MODEL_ID, text=query)

        abstract_ids = await _vector_search(
            collection=COLLECTION_ABSTRACT,
            persona_id=persona_id,
            query_vec=query_vec,
            k=k_abs,
        )

        async with get_session() as s:
            for aid in abstract_ids:
                if aid in seen_abstract_ids:
                    continue

                abstract = await get_abstract_by_id(s, aid)
                if abstract is None or abstract.clarity == "forgotten":
                    continue

                seen_abstract_ids.add(aid)

                edges = await list_edges_to(
                    s,
                    persona_id=persona_id,
                    to_id=aid,
                    edge_type="supports",
                )
                fact_ids = [str(e.from_id) for e in edges[:k_facts_per_abs]]
                fragments = await get_fragments_by_ids(s, fact_ids)

                supporting_facts: list[dict[str, Any]] = []
                for fragment in fragments:
                    if fragment.clarity == "forgotten":
                        continue
                    supporting_facts.append(
                        {
                            "id": fragment.id,
                            "content": fragment.content,
                            "clarity": fragment.clarity,
                        }
                    )
                    seen_fragment_ids.add(str(fragment.id))

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
                fragment_ids = await _vector_search(
                    collection=COLLECTION_FRAGMENT,
                    persona_id=persona_id,
                    query_vec=query_vec,
                    k=fact_k_per_query,
                )
                new_ids = [
                    fid for fid in fragment_ids if fid not in seen_fragment_ids
                ]
                fragments = await get_fragments_by_ids(s, new_ids)
                for fragment in fragments:
                    if fragment.clarity == "forgotten":
                        continue
                    seen_fragment_ids.add(str(fragment.id))
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
            await touch_abstracts_bulk(s, list(seen_abstract_ids))
            await touch_fragments_bulk(s, list(seen_fragment_ids))

    return result
