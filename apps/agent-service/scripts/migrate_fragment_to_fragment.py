"""Migration: experience_fragment (最近 7 天, grain='conversation') → v4 fragment.

Pure data copy — no LLM rewrite (old content is often too long; reviewer heavy
will consolidate on day 2).

Idempotent: re-runs delete rows with id starting with `f_mig_` before reprocessing.

Usage:
    python scripts/migrate_fragment_to_fragment.py [--dry-run] [--limit N] [--days N]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text

from app.data.models import ExperienceFragment
from app.data.queries import insert_fragment
from app.data.session import get_session
from app.memory.vectorize_memory import enqueue_fragment_vectorize

logger = logging.getLogger("migrate_fragment")

MIG_ID_PREFIX = "f_mig_"


async def copy_one_row(row: Any, *, dry_run: bool) -> bool:
    new_id = f"{MIG_ID_PREFIX}{row.id}"
    if dry_run:
        logger.info(
            "[DRY] %s → %s persona=%s chat=%s content_len=%d",
            row.id, new_id, row.persona_id, row.source_chat_id, len(row.content or ""),
        )
        return True
    try:
        async with get_session() as s:
            await insert_fragment(
                s, id=new_id, persona_id=row.persona_id,
                content=row.content, source="afterthought",
                chat_id=row.source_chat_id, clarity="clear",
                created_at=row.created_at,
            )
        await enqueue_fragment_vectorize(new_id)
        return True
    except Exception as e:
        logger.warning("Copy failed for %s: %s", row.id, e)
        return False


async def clear_prior_migration() -> None:
    async with get_session() as s:
        await s.execute(
            text("DELETE FROM fragment WHERE id LIKE :p"),
            {"p": f"{MIG_ID_PREFIX}%"},
        )


async def main(dry_run: bool, limit: int | None, days: int) -> None:
    logging.basicConfig(level=logging.INFO)
    since = datetime.now(UTC) - timedelta(days=days)
    logger.info(
        "Migrating experience_fragment (grain='conversation') since %s (dry_run=%s)",
        since, dry_run,
    )

    if not dry_run:
        await clear_prior_migration()
        logger.info("Cleared prior migrated fragments")

    async with get_session() as s:
        q = (
            select(ExperienceFragment)
            .where(ExperienceFragment.grain == "conversation")
            .where(ExperienceFragment.created_at >= since)
            .order_by(ExperienceFragment.created_at)
        )
        if limit:
            q = q.limit(limit)
        rows = list((await s.execute(q)).scalars().all())

    logger.info("Loaded %d rows", len(rows))
    success = failed = 0
    for i, row in enumerate(rows, 1):
        ok = await copy_one_row(row, dry_run=dry_run)
        success += int(ok)
        failed += int(not ok)
        if i % 50 == 0:
            logger.info(
                "Progress %d/%d success=%d failed=%d",
                i, len(rows), success, failed,
            )

    logger.info("Done. success=%d failed=%d", success, failed)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--days", type=int, default=7)
    args = p.parse_args()
    asyncio.run(main(args.dry_run, args.limit, args.days))
