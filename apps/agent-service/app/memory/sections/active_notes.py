"""Always-on injection: 未 resolve 的 notes。"""

from __future__ import annotations

import logging
from datetime import timedelta, timezone

from app.data.queries import get_active_notes
from app.data.session import get_session

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))


def _fmt_when(dt) -> str:
    if dt is None:
        return ""
    local = dt.astimezone(_CST)
    return local.strftime("%m-%d %H:%M")


async def build_active_notes_section(*, persona_id: str) -> str:
    try:
        async with get_session() as s:
            notes = await get_active_notes(s, persona_id=persona_id)
    except Exception as e:
        logger.warning("active_notes failed: %s", e)
        return ""

    if not notes:
        return ""

    lines = ["你的清单（没处理的事）："]
    for n in notes:
        when = _fmt_when(n.when_at)
        suffix = f" [{when}]" if when else ""
        lines.append(f"- {n.content}{suffix} (id: {n.id})")
    return "\n".join(lines)
