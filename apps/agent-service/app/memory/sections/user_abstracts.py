"""Always-on injection: abstracts about trigger_user (subject='user:<id>') and the relationship."""

from __future__ import annotations

import logging

from app.data.queries import get_abstracts_by_subjects
from app.data.session import get_session

logger = logging.getLogger(__name__)

MAX_PER_SUBJECT = 5


async def build_user_abstracts_section(
    *,
    persona_id: str,
    trigger_user_id: str | None,
    trigger_username: str | None,
) -> str:
    if not trigger_user_id or trigger_user_id == "__proactive__":
        return ""

    name_label = trigger_username or "该用户"
    subjects = [
        f"user:{trigger_user_id}",
        f"和 {trigger_user_id} 的关系",
    ]
    if trigger_username:
        subjects.extend([trigger_username, f"和 {trigger_username} 的关系"])

    try:
        async with get_session() as s:
            rows = await get_abstracts_by_subjects(
                s, persona_id=persona_id,
                subjects=subjects, limit_per_subject=MAX_PER_SUBJECT,
            )
    except Exception as e:
        logger.warning("user_abstracts failed: %s", e)
        return ""

    if not rows:
        return ""

    lines = [f"关于 {name_label}（以及你们的关系）："]
    for r in rows:
        lines.append(f"- {r.content}")
    return "\n".join(lines)
