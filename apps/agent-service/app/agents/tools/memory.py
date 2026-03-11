"""记忆检索工具 - 让赤尾能主动查日记和人物印象"""

import logging

from langchain.tools import tool
from langgraph.runtime import get_runtime

from app.agents.core.context import AgentContext
from app.agents.tools.decorators import tool_error_handler
from app.orm.crud import (
    get_diary_by_date,
    get_impressions_for_users,
    get_username,
    search_user_by_name,
)

logger = logging.getLogger(__name__)


@tool
@tool_error_handler(error_message="记忆检索失败")
async def load_memory(query_type: str, query: str) -> str:
    """从赤尾的记忆中检索信息。

    两种模式：
    - query_type="diary": 按日期查日记，query 为日期如 "2026-03-10"
    - query_type="impression": 按人名查印象，query 为用户名如 "陈儒"

    Args:
        query_type: 查询类型，"diary" 或 "impression"
        query: 查询内容（日期或人名）
    """
    context = get_runtime(AgentContext).context
    chat_id = context.message.chat_id

    if query_type == "diary":
        return await _load_diary(chat_id, query)
    elif query_type == "impression":
        return await _load_impression(chat_id, query)
    else:
        return f"不支持的查询类型: {query_type}，请使用 diary 或 impression"


async def _load_diary(chat_id: str, date_str: str) -> str:
    """按日期查日记"""
    diary = await get_diary_by_date(chat_id, date_str)
    if not diary:
        return "那天没有写日记哦"
    return f"--- {diary.diary_date} 的日记 ---\n{diary.content}"


async def _load_impression(chat_id: str, name: str) -> str:
    """按人名查印象"""
    users = await search_user_by_name(name)
    if not users:
        return "还没有对这个人形成印象呢"

    user_ids = [u.union_id for u in users]
    impressions = await get_impressions_for_users(chat_id, user_ids)
    if not impressions:
        return "还没有对这个人形成印象呢"

    lines = []
    for imp in impressions:
        username = await get_username(imp.user_id) or imp.user_id[:8]
        lines.append(f"【{username}】{imp.impression_text}")
    return "\n".join(lines)
