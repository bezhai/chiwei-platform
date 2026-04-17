"""Migration: relationship_memory_v2 → v4 fragment + abstract + supports edges.

For each relationship_memory_v2 row:
  - Split core_facts into individual fragments (one per non-empty line)
  - LLM-rewrite impression into a clean abstract content
  - Create abstract_memory with subject=f"user:{user_id}"
  - Connect each fragment to the abstract via supports edges

Idempotent: re-runs delete prior migration=source rows before reprocessing.

Usage:
    python scripts/migrate_relationship_to_abstract.py [--dry-run] [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import uuid
from typing import Any

from langchain_core.messages import HumanMessage
from sqlalchemy import select, text

from app.agent.core import Agent, AgentConfig, extract_text
from app.data.models import RelationshipMemoryV2
from app.data.queries import (
    insert_abstract_memory,
    insert_fragment,
    insert_memory_edge,
)
from app.data.session import get_session
from app.memory.vectorize_memory import (
    enqueue_abstract_vectorize,
    enqueue_fragment_vectorize,
)

logger = logging.getLogger("migrate_relationship")

MIGRATION_SOURCE = "migration"

_REWRITE_CFG = AgentConfig(
    prompt_id="memory_migrate_relationship",
    model_id="offline-model",
    trace_name="memory-migrate-relationship",
)


async def llm_rewrite_impression(core_facts: str, impression: str) -> str:
    """Rewrite facts + impression into one clean abstract content via Langfuse prompt."""
    result = await Agent(_REWRITE_CFG).run(
        prompt_vars={"facts": core_facts, "impression": impression},
        messages=[HumanMessage(content="请合成")],
    )
    return extract_text(result.content)


def _uid(prefix: str) -> str:
    return f"{prefix}_mig_{uuid.uuid4().hex[:12]}"


async def process_one_row(row: Any, *, dry_run: bool) -> bool:
    """Process a single relationship_memory_v2 row. Return True on success."""
    try:
        abstract_content = await llm_rewrite_impression(row.core_facts, row.impression)
    except Exception as e:
        logger.warning(
            "LLM rewrite failed for persona=%s user=%s: %s",
            row.persona_id, row.user_id, e,
        )
        return False

    fact_lines = [ln.strip() for ln in row.core_facts.splitlines() if ln.strip()]
    aid = _uid("a")
    fact_ids = [_uid("f") for _ in fact_lines]

    if dry_run:
        logger.info(
            "[DRY] persona=%s user=%s facts=%d abstract=%s",
            row.persona_id, row.user_id, len(fact_lines), abstract_content[:60],
        )
        return True

    async with get_session() as s:
        for fid, content in zip(fact_ids, fact_lines, strict=False):
            await insert_fragment(
                s, id=fid, persona_id=row.persona_id,
                content=content, source=MIGRATION_SOURCE,
            )
        await insert_abstract_memory(
            s, id=aid, persona_id=row.persona_id,
            subject=f"user:{row.user_id}", content=abstract_content,
            created_by=MIGRATION_SOURCE,
        )
        for fid in fact_ids:
            await insert_memory_edge(
                s, id=_uid("e"), persona_id=row.persona_id,
                from_id=fid, from_type="fact",
                to_id=aid, to_type="abstract",
                edge_type="supports", created_by=MIGRATION_SOURCE,
                reason="migrated from relationship_memory_v2",
            )

    for fid in fact_ids:
        await enqueue_fragment_vectorize(fid)
    await enqueue_abstract_vectorize(aid)
    return True


async def clear_prior_migration() -> None:
    """Idempotency: delete rows created by migration before re-running."""
    async with get_session() as s:
        await s.execute(
            text("DELETE FROM memory_edge WHERE created_by = :src"),
            {"src": MIGRATION_SOURCE},
        )
        await s.execute(
            text("DELETE FROM abstract_memory WHERE created_by = :src"),
            {"src": MIGRATION_SOURCE},
        )
        await s.execute(
            text("DELETE FROM fragment WHERE source = :src"),
            {"src": MIGRATION_SOURCE},
        )


async def main(dry_run: bool, limit: int | None) -> None:
    logging.basicConfig(level=logging.INFO)
    logger.info("Migration starting (dry_run=%s limit=%s)", dry_run, limit)

    if not dry_run:
        await clear_prior_migration()
        logger.info("Cleared prior migration rows")

    async with get_session() as s:
        q = select(RelationshipMemoryV2).order_by(RelationshipMemoryV2.id)
        if limit:
            q = q.limit(limit)
        result = await s.execute(q)
        rows = list(result.scalars().all())

    logger.info("Loaded %d relationship_memory_v2 rows", len(rows))

    success = 0
    failed = 0
    for i, row in enumerate(rows, 1):
        ok = await process_one_row(row, dry_run=dry_run)
        if ok:
            success += 1
        else:
            failed += 1
        if i % 50 == 0:
            logger.info(
                "Progress %d/%d (success=%d failed=%d)",
                i, len(rows), success, failed,
            )

    logger.info("Done. success=%d failed=%d", success, failed)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()
    asyncio.run(main(args.dry_run, args.limit))
