"""Always-on injection: recall index hint — counts and recent abstract titles."""

from __future__ import annotations

import logging

from app.data.queries import count_abstracts_by_persona, get_recent_abstract_titles
from app.data.session import get_session

logger = logging.getLogger(__name__)

RECENT_N = 10
SNIPPET = 30


async def build_recall_index_section(*, persona_id: str) -> str:
    try:
        async with get_session() as s:
            total = await count_abstracts_by_persona(s, persona_id)
            recent = await get_recent_abstract_titles(s, persona_id=persona_id, limit=RECENT_N)
    except Exception as e:
        logger.warning("recall_index failed: %s", e)
        return ""

    if total == 0 and not recent:
        return ""

    lines = [f"你总共记得 {total} 条抽象认识。最近碰过的："]
    for r in recent:
        snippet = r.content[:SNIPPET].replace("\n", " ")
        lines.append(f"- [{r.subject}] {snippet}...")
    lines.append(
        "（如果眼前的事让你隐约想起别的，用 recall(queries=[\"...\"]) 查一查。"
        "批量传多条 query 可以并行搜。）"
    )
    return "\n".join(lines)
