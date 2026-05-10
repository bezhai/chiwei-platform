"""Always-on injection: active notes (windowed + capped + remainder hint).

The section pulls a limited set of "live-ish" notes via
``select_notes_for_context`` (3-day overdue / 7-day memo window, 15 rows max)
plus the total active count via ``list_active_notes`` so we can append a
truncation hint pointing the agent at ``list_note`` for the full picture.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.data.queries import list_active_notes, select_notes_for_context
from app.memory.notes_format import format_when_label

logger = logging.getLogger(__name__)


async def build_active_notes_section(*, persona_id: str) -> str:
    try:
        injected = await select_notes_for_context(persona_id=persona_id)
        all_active = await list_active_notes(persona_id=persona_id)
    except Exception as e:
        logger.warning("active_notes failed: %s", e)
        return ""

    if not all_active:
        return ""

    total = len(all_active)
    shown = len(injected)

    if shown == 0:
        return f"你的清单里还有 {total} 条没动的事（用 list_note 看）。"

    now = datetime.now(timezone.utc)
    lines = ["你的清单（最近活跃，全部用 list_note 查）："]
    for n in injected:
        label = format_when_label(when_at=n.when_at, created_at=n.created_at, now=now)
        lines.append(f"- {n.content} [{label}] (id: {n.id})")

    remainder = total - shown
    if remainder > 0:
        lines.append(
            f"（清单里还有 {remainder} 条更老的没列出来，用 list_note 看全部。）"
        )

    return "\n".join(lines)
