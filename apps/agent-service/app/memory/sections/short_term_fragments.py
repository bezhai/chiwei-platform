"""§2.8 短期 fragment 注入：
- 当前 chat 最近 2-4h 最新 1 条
- 其他 chat 最近 1-2h（含 trigger_user 的）最多 2 条，每 chat 只取最新

作用：补 chat_history 30min/15 窗口以外的回忆 + 补 cross-chat 24h raw 的噪音。
"""

from __future__ import annotations

import logging
from datetime import timedelta, timezone

from app.data.queries import get_recent_fragments_for_injection
from app.data.session import get_session

logger = logging.getLogger(__name__)

MAX_TOTAL_CHARS = 1000
FRAGMENT_MAX = 350
_CST = timezone(timedelta(hours=8))


def _fmt_time(dt) -> str:
    return dt.astimezone(_CST).strftime("%H:%M")


async def build_short_term_fragments_section(
    *,
    persona_id: str,
    chat_id: str | None,
    trigger_user_id: str | None,
) -> str:
    try:
        async with get_session() as s:
            fragments = await get_recent_fragments_for_injection(
                s,
                persona_id=persona_id,
                chat_id=chat_id,
                _trigger_user_id=trigger_user_id,
            )
    except Exception as e:
        logger.warning("short_term_fragments failed: %s", e)
        return ""

    if not fragments:
        return ""

    lines = ["最近的新鲜经历："]
    total = 0
    for f in fragments:
        text = f.content.strip()
        if len(text) > FRAGMENT_MAX:
            text = text[:FRAGMENT_MAX] + "..."
        if total + len(text) > MAX_TOTAL_CHARS:
            break
        where = "这里" if f.chat_id == chat_id else f"别处({f.chat_id[:6] if f.chat_id else '?'})"
        lines.append(f"- [{where} {_fmt_time(f.created_at)}] {text}")
        total += len(text)
    if len(lines) == 1:
        return ""
    return "\n".join(lines)
