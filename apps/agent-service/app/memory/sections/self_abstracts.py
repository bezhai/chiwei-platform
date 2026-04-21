"""Always-on injection: abstracts with subject='self' (or '我自己')."""

from __future__ import annotations

import logging

from app.data.queries import get_abstracts_by_subjects
from app.data.session import get_session

logger = logging.getLogger(__name__)

SELF_SUBJECTS = ["self", "我自己"]
MAX_PER_SUBJECT = 5


async def build_self_abstracts_section(*, persona_id: str) -> str:
    try:
        async with get_session() as s:
            rows = await get_abstracts_by_subjects(
                s, persona_id=persona_id,
                subjects=SELF_SUBJECTS,
                limit_per_subject=MAX_PER_SUBJECT,
            )
    except Exception as e:
        logger.warning("self_abstracts failed: %s", e)
        return ""

    if not rows:
        return ""

    lines = ["关于你自己："]
    for r in rows:
        lines.append(f"- {r.content}")
    return "\n".join(lines)
