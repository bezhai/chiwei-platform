"""recall 工具 — 自然语言记忆搜索

单参数。内部走 PostgreSQL 全文搜索 experience_fragment。
返回碎片摘要，不是精确原文。
"""

import logging

from langchain.tools import tool
from langgraph.runtime import get_runtime

from app.agents.core.context import AgentContext
from app.agents.tools.decorators import tool_error_handler
from app.orm.memory_crud import search_fragments_fts

logger = logging.getLogger(__name__)
SUMMARY_LIMIT = 300


async def _get_persona_id() -> str:
    from app.services.bot_context import _resolve_persona_id
    from app.utils.middlewares.trace import header_vars

    bot_name = header_vars["app_name"].get() or "chiwei"
    return await _resolve_persona_id(bot_name)


async def _recall_impl(what: str) -> str:
    """recall 核心实现（方便测试）"""
    persona_id = await _get_persona_id()
    fragments = await search_fragments_fts(persona_id, what, limit=5)
    if not fragments:
        return f"想不起来关于「{what}」的事了..."

    lines = []
    for f in fragments:
        date_str = f.created_at.strftime("%m月%d日") if f.created_at else "某天"
        summary = f.content[:SUMMARY_LIMIT]
        if len(f.content) > SUMMARY_LIMIT:
            summary += "..."
        grain_label = {"daily": "日记", "weekly": "回顾", "conversation": "", "glimpse": ""}.get(
            f.grain, ""
        )
        prefix = f"({grain_label}) " if grain_label else ""
        lines.append(f"--- {date_str} ---\n{prefix}{summary}")
    return "\n\n".join(lines)


@tool
@tool_error_handler(error_message="想不起来了...")
async def recall(what: str) -> str:
    """想一想过去的事。
    当你隐约记得什么但细节模糊了，把你模糊记得的写下来，想一想。
    比如："上次聊新番是什么时候"、"A哥最近怎么了"、"那个好吃的店叫什么"
    Args:
        what: 你想回忆的事（自然语言）
    """
    return await _recall_impl(what)
