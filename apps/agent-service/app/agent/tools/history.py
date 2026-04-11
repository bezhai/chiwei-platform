"""Chat history lookup, group history search, and group member listing.

Merges the old history/chat_history.py, history/search.py, and
history/members.py into a single module.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from langchain.tools import tool
from langgraph.runtime import get_runtime
from qdrant_client.http.models import FieldCondition, Filter, MatchValue
from sqlalchemy import or_, select

from app.agent.context import AgentContext
from app.agent.tools._common import tool_error
from app.data.models import ConversationMessage, LarkGroupMember, LarkUser
from app.data.session import get_session

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

# check_chat_history constants
MAX_MESSAGES = 50
LOOKBACK_HOURS = 24

# search_group_history constants
CONTEXT_WINDOW_MS = 5 * 60 * 1000  # 5 minutes
TIME_GAP_THRESHOLD_MS = 10 * 60 * 1000  # 10 minutes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_time_hint(hint: str) -> int:
    """Convert a Chinese time hint to lookback hours."""
    if not hint:
        return LOOKBACK_HOURS
    hint = hint.strip()
    if "昨天" in hint:
        return 48
    if "前天" in hint:
        return 72
    if any(k in hint for k in ("今天", "刚才", "上午", "下午")):
        return 12
    return LOOKBACK_HOURS


def _format_timestamp(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=CST).strftime("%Y-%m-%d %H:%M")


def _truncate(text: str, max_len: int = 200) -> str:
    text = " ".join(text.split())
    return f"{text[:max_len]}..." if len(text) > max_len else text


# =========================================================================
# Public tools
# =========================================================================


@tool
@tool_error("翻不到了...")
async def check_chat_history(what_to_look_for: str, time_hint: str = "") -> str:
    """翻翻聊天记录看看。
    你没印象但想确认一下的时候，可以翻翻聊天记录。

    Args:
        what_to_look_for: 你想找什么
        time_hint: 大概什么时候的（如"今天上午"、"昨天"），不确定可以不填
    """
    context = get_runtime(AgentContext).context
    chat_id = context.message.chat_id

    now = datetime.now(CST)
    hours = _parse_time_hint(time_hint)
    start_ts = int((now - timedelta(hours=hours)).timestamp() * 1000)
    end_ts = int(now.timestamp() * 1000)

    from app.chat.content_parser import parse_content
    from app.data.queries import find_messages_in_range, find_username

    async with get_session() as session:
        messages = await find_messages_in_range(session, chat_id, start_ts, end_ts)
        if not messages:
            return "这段时间好像没有聊天记录..."

        messages = messages[-MAX_MESSAGES:]
        lines: list[str] = []
        for msg in messages:
            msg_time = datetime.fromtimestamp(msg.create_time / 1000, tz=CST)
            time_str = msg_time.strftime("%m/%d %H:%M")
            if msg.role == "assistant":
                speaker = "我"
            else:
                name = await find_username(session, msg.user_id)
                speaker = name or "?"
            rendered = parse_content(msg.content).render()
            if rendered and rendered.strip():
                lines.append(f"[{time_str}] {speaker}: {rendered[:150]}")

    if not lines:
        return "翻了但没看到什么..."

    keywords = [w for w in what_to_look_for.split() if len(w) >= 2]
    if keywords:
        filtered = [line for line in lines if any(k in line for k in keywords)]
        if filtered:
            return "找到了一些相关的记录：\n" + "\n".join(filtered[-20:])

    return "最近的聊天记录：\n" + "\n".join(lines[-20:])


@tool
@tool_error("搜索群聊历史失败")
async def search_group_history(query: str, limit: int = 10) -> str:
    """回想之前群里好像聊过的事

    只在你隐约记得群里讨论过某个话题、但细节模糊了的时候才用。
    注意：不要用来确认事实或引用别人的原话，你的记忆本来就是模糊的。
    大部分情况下你不需要翻历史——直接根据你的印象和日记回复就好。

    Args:
        query: 你隐约记得的内容（自然语言描述）
        limit: 返回的锚点消息数量（默认10条，每条会附带上下文）
    """
    context = get_runtime(AgentContext).context

    # 1. Generate hybrid embedding for query
    from app.agent.embedding import InstructionBuilder, Modality

    target_modality = InstructionBuilder.combine_corpus_modalities(
        Modality.TEXT, Modality.IMAGE, Modality.TEXT_AND_IMAGE
    )
    instructions = InstructionBuilder.for_query(
        target_modality=target_modality,
        instruction="为这个句子生成表示以用于检索相关消息",
    )

    from app.agent.embedding import embed_hybrid

    hybrid_embedding = await embed_hybrid(
        "embedding-model", text=query, instructions=instructions
    )

    # 2. Qdrant hybrid search filtered by chat_id
    from app.infra.qdrant import qdrant

    query_filter = Filter(
        must=[
            FieldCondition(
                key="chat_id",
                match=MatchValue(value=context.message.chat_id or ""),
            )
        ]
    )
    results = await qdrant.hybrid_search(
        collection_name="messages_recall",
        dense_vector=hybrid_embedding.dense,
        sparse_indices=hybrid_embedding.sparse.indices,
        sparse_values=hybrid_embedding.sparse.values,
        query_filter=query_filter,
        limit=limit,
        prefetch_limit=limit * 5,
    )

    if not results:
        return "未找到相关消息"

    # 3. Extract anchor message ids and timestamps
    anchor_message_ids: list[str] = []
    anchor_timestamps: list[int] = []
    anchor_root_ids: set[str] = set()

    for r in results:
        payload = r.get("payload", {})
        anchor_message_ids.append(payload.get("message_id"))
        anchor_timestamps.append(payload.get("timestamp", 0))
        if payload.get("root_message_id"):
            anchor_root_ids.add(payload.get("root_message_id"))

    # 4. Query context messages from DB
    async with get_session() as session:
        time_conditions = [
            ConversationMessage.create_time.between(
                ts - CONTEXT_WINDOW_MS, ts + CONTEXT_WINDOW_MS
            )
            for ts in anchor_timestamps
            if ts
        ]
        or_conditions = [
            *time_conditions,
            ConversationMessage.message_id.in_(anchor_message_ids),
        ]
        if anchor_root_ids:
            or_conditions.append(
                ConversationMessage.root_message_id.in_(anchor_root_ids)
            )

        stmt = (
            select(ConversationMessage, LarkUser)
            .join(LarkUser, ConversationMessage.user_id == LarkUser.union_id)
            .where(
                ConversationMessage.chat_id == context.message.chat_id,
                or_(*or_conditions),
            )
            .order_by(ConversationMessage.create_time.asc())
        )
        result = await session.execute(stmt)
        rows = result.all()

    if not rows:
        return "未找到相关消息"

    # 5. Format with time-gap separators
    from app.chat.content_parser import parse_content

    anchor_set = set(anchor_message_ids)
    lines = [f"找到 {len(anchor_set)} 条相关消息及其上下文：\n"]
    prev_ts = None
    for msg, user in rows:
        if prev_ts and (msg.create_time - prev_ts) > TIME_GAP_THRESHOLD_MS:
            lines.append("\n--- 时间间隔 ---\n")
        time_str = _format_timestamp(msg.create_time)
        content = _truncate(parse_content(msg.content).render())
        marker = "→ " if msg.message_id in anchor_set else "  "
        lines.append(f"{marker}[{time_str}] {user.name}: {content}")
        prev_ts = msg.create_time

    return "\n".join(lines)


@tool
@tool_error("查询群成员失败")
async def list_group_members(role: str | None = None) -> str:
    """列出群成员列表

    Args:
        role: 筛选角色（可选）
            - "owner": 群主
            - "manager": 管理员
            - None: 所有成员
    """
    context = get_runtime(AgentContext).context

    async with get_session() as session:
        stmt = (
            select(LarkGroupMember, LarkUser)
            .join(LarkUser, LarkGroupMember.union_id == LarkUser.union_id)
            .where(
                LarkGroupMember.chat_id == context.message.chat_id,
                ~LarkGroupMember.is_leave,
            )
        )
        if role == "owner":
            stmt = stmt.where(LarkGroupMember.is_owner)
        elif role == "manager":
            stmt = stmt.where(LarkGroupMember.is_manager)

        result = await session.execute(stmt)
        rows = result.all()

    if not rows:
        return "群内无成员" if not role else f"未找到 {role} 角色的成员"

    lines = [f"群成员列表（共{len(rows)}人）：\n"]
    for member, user in rows:
        role_tag = (
            " [群主]" if member.is_owner else " [管理员]" if member.is_manager else ""
        )
        lines.append(f"• {user.name}{role_tag}")

    return "\n".join(lines)
