"""记忆检索工具 — 让赤尾能自然回忆过去的事

设计原则：自然联想，不是主动检索。
返回摘要和感受，不返回完整原文，防止 LLM 精确引用破坏拟人感。
"""

import logging
from datetime import date, timedelta

from langchain.tools import tool
from langgraph.runtime import get_runtime

from app.agents.core.context import AgentContext
from app.agents.tools.decorators import tool_error_handler
from app.orm.crud import (
    get_diary_by_date,
    get_impressions_for_users,
    get_recent_journals,
    get_username,
    search_diary_by_keyword,
    search_user_by_name,
)

logger = logging.getLogger(__name__)

DIARY_SUMMARY_LIMIT = 300


@tool
@tool_error_handler(error_message="想不起来了...")
async def load_memory(mode: str, hint: str) -> str:
    """想一想过去的事。

    当你隐约记得什么但细节模糊了，可以用这个翻翻记忆：
    - mode="recent": 回忆最近几天的事，hint 填天数如 "3"
    - mode="person": 回忆关于某个人的事，hint 填人名
    - mode="diary": 查看某天的日记，hint 填日期如 "2026-03-10"
    - mode="topic": 回忆某个话题相关的事，hint 填关键词

    不用精确知道日期，大概的也行。

    Args:
        mode: 回忆方式 "recent" | "person" | "diary" | "topic"
        hint: 线索（天数/人名/日期/关键词）
    """
    context = get_runtime(AgentContext).context
    chat_id = context.message.chat_id

    if mode == "recent":
        return await _recall_recent(hint)
    elif mode == "person":
        return await _recall_person(chat_id, hint)
    elif mode == "diary":
        return await _recall_diary(chat_id, hint)
    elif mode == "topic":
        return await _recall_topic(hint)
    else:
        return f"不支持的回忆方式: {mode}"


async def _recall_recent(hint: str) -> str:
    """回忆最近几天的事（从 Journal daily 取）"""
    try:
        days = int(hint)
    except ValueError:
        days = 3
    days = min(days, 7)

    today = date.today().isoformat()
    journals = await get_recent_journals("daily", today, limit=days)
    if not journals:
        return "最近几天好像没什么特别的事..."

    lines = []
    for j in reversed(journals):
        summary = j.content[:DIARY_SUMMARY_LIMIT]
        if len(j.content) > DIARY_SUMMARY_LIMIT:
            summary += "..."
        lines.append(f"--- {j.journal_date} ---\n{summary}")
    return "\n\n".join(lines)


async def _recall_person(chat_id: str, name: str) -> str:
    """回忆关于某个人的事"""
    users = await search_user_by_name(name)
    if not users:
        return "想不起来这个人..."

    user_ids = [u.union_id for u in users]
    impressions = await get_impressions_for_users(chat_id, user_ids)
    if not impressions:
        return "对这个人还没形成什么印象呢"

    lines = []
    for imp in impressions:
        username = await get_username(imp.user_id) or imp.user_id[:8]
        lines.append(f"【{username}】{imp.impression_text}")
    return "\n".join(lines)


async def _recall_diary(chat_id: str, date_str: str) -> str:
    """查看某天的日记（返回摘要）"""
    diary = await get_diary_by_date(chat_id, date_str)
    if not diary:
        return "那天好像没写日记..."

    summary = diary.content[:DIARY_SUMMARY_LIMIT]
    if len(diary.content) > DIARY_SUMMARY_LIMIT:
        summary += "..."
    return f"--- {diary.diary_date} 的日记 ---\n{summary}"


async def _recall_topic(hint: str) -> str:
    """回忆某个话题相关的事"""
    entries = await search_diary_by_keyword(hint, limit=3)
    if not entries:
        return f"想不起来关于「{hint}」的事了..."

    lines = []
    for e in entries:
        snippet = e.content[:200]
        if len(e.content) > 200:
            snippet += "..."
        lines.append(f"--- {e.diary_date} ---\n{snippet}")
    return "\n\n".join(lines)
