"""Memory recall tool — natural language search over experience fragments.

Runs PG full-text search on ``experience_fragment``.
"""

from __future__ import annotations

import logging

from langchain.tools import tool

from app.agent.tools._common import tool_error

logger = logging.getLogger(__name__)

SUMMARY_LIMIT = 300


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _get_persona_id() -> str:
    """Resolve the current persona_id from request context."""
    from app.api.middleware import header_vars
    from app.data.queries import resolve_persona_id
    from app.data.session import get_session

    bot_name = header_vars["app_name"].get() or "chiwei"
    async with get_session() as s:
        return await resolve_persona_id(s, bot_name)


async def _recall_impl(what: str) -> str:
    """Core recall logic (separated for testability)."""
    from app.data.queries import search_fragments_fts
    from app.data.session import get_session

    persona_id = await _get_persona_id()

    async with get_session() as session:
        fragments = await search_fragments_fts(session, persona_id, what, limit=5)

    if not fragments:
        return f"想不起来关于「{what}」的事了..."

    lines: list[str] = []
    for f in fragments:
        date_str = f.created_at.strftime("%m月%d日") if f.created_at else "某天"
        summary = f.content[:SUMMARY_LIMIT]
        if len(f.content) > SUMMARY_LIMIT:
            summary += "..."
        grain_label = {"daily": "日记", "weekly": "回顾"}.get(f.grain, "")
        prefix = f"({grain_label}) " if grain_label else ""
        lines.append(f"--- {date_str} ---\n{prefix}{summary}")

    return "\n\n".join(lines)


# =========================================================================
# Public tool
# =========================================================================


@tool
@tool_error("想不起来了...")
async def recall(what: str) -> str:
    """想一想过去的事。
    当你隐约记得什么但细节模糊了，把你模糊记得的写下来，想一想。
    比如："上次聊新番是什么时候"、"A哥最近怎么了"、"那个好吃的店叫什么"

    Args:
        what: 你想回忆的事（自然语言）
    """
    return await _recall_impl(what)
