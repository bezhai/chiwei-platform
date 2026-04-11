"""群聊历史混合检索工具"""

import logging
from datetime import datetime

from langchain.tools import tool
from langgraph.runtime import get_runtime
from qdrant_client.http.models import FieldCondition, Filter, MatchValue
from sqlalchemy import select

from app.agents.clients import create_client
from app.agents.core.context import AgentContext
from app.agents.infra.embedding import InstructionBuilder, Modality
from app.agents.tools.decorators import tool_error_handler
from app.orm.base import AsyncSessionLocal
from app.orm.models import ConversationMessage, LarkUser
from app.services.qdrant import qdrant_service
from app.services.content_parser import parse_content

logger = logging.getLogger(__name__)

# 上下文时间窗口（毫秒）
CONTEXT_WINDOW_MS = 5 * 60 * 1000  # 5分钟
# 时间间隔分隔符阈值（毫秒）
TIME_GAP_THRESHOLD_MS = 10 * 60 * 1000  # 10分钟


def _format_timestamp(ts: int) -> str:
    """格式化时间戳"""
    return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")


def _truncate(text: str, max_len: int = 200) -> str:
    """截断并清理文本"""
    text = " ".join(text.split())  # 清理所有空白字符
    return f"{text[:max_len]}..." if len(text) > max_len else text


@tool
@tool_error_handler(error_message="搜索群聊历史失败")
async def search_group_history(
    query: str,
    limit: int = 10,
) -> str:
    """
    回想之前群里好像聊过的事

    只在你隐约记得群里讨论过某个话题、但细节模糊了的时候才用。
    注意：不要用来确认事实或引用别人的原话，你的记忆本来就是模糊的。
    大部分情况下你不需要翻历史——直接根据你的印象和日记回复就好。

    Args:
        query: 你隐约记得的内容（自然语言描述）
        limit: 返回的锚点消息数量（默认10条，每条会附带上下文）

    Returns:
        str: 搜索结果
    """
    context = get_runtime(AgentContext).context

    try:
        # 1. 生成查询的 Dense + Sparse 向量
        target_modality = InstructionBuilder.combine_corpus_modalities(
            Modality.TEXT, Modality.IMAGE, Modality.TEXT_AND_IMAGE
        )
        instructions = InstructionBuilder.for_query(
            target_modality=target_modality,
            instruction="为这个句子生成表示以用于检索相关消息",
        )

        async with await create_client("embedding-model") as client:
            hybrid_embedding = await client.embed_hybrid(
                text=query,
                instructions=instructions,
            )

        # 2. 构建 chat_id 过滤条件
        query_filter = Filter(
            must=[
                FieldCondition(
                    key="chat_id",
                    match=MatchValue(value=context.message.chat_id or ""),
                )
            ]
        )

        # 3. 执行混合搜索
        results = await qdrant_service.hybrid_search(
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

        # 4. 提取锚点消息 ID 和时间戳
        anchor_message_ids = []
        anchor_timestamps = []
        anchor_root_ids = set()

        for r in results:
            payload = r.get("payload", {})
            anchor_message_ids.append(payload.get("message_id"))
            anchor_timestamps.append(payload.get("timestamp", 0))
            if payload.get("root_message_id"):
                anchor_root_ids.add(payload.get("root_message_id"))

        # 5. 从 MySQL 查询上下文消息
        async with AsyncSessionLocal() as session:
            # 构建时间窗口条件
            time_conditions = []
            for ts in anchor_timestamps:
                if ts:
                    time_conditions.append(
                        ConversationMessage.create_time.between(
                            ts - CONTEXT_WINDOW_MS, ts + CONTEXT_WINDOW_MS
                        )
                    )

            # 查询：时间窗口内的消息 + 引用链消息
            from sqlalchemy import or_

            or_conditions = [
                *time_conditions,
                ConversationMessage.message_id.in_(anchor_message_ids),
            ]
            if anchor_root_ids:
                or_conditions.append(
                    ConversationMessage.root_message_id.in_(anchor_root_ids)
                )

            query_obj = (
                select(ConversationMessage, LarkUser)
                .join(LarkUser, ConversationMessage.user_id == LarkUser.union_id)
                .where(
                    ConversationMessage.chat_id == context.message.chat_id,
                    or_(*or_conditions),
                )
                .order_by(ConversationMessage.create_time.asc())
            )

            result = await session.execute(query_obj)
            rows = result.all()

        if not rows:
            return "未找到相关消息"

        # 6. 格式化输出（时间间隔超过10分钟插入分隔符）
        anchor_set = set(anchor_message_ids)
        lines = [f"找到 {len(anchor_set)} 条相关消息及其上下文：\n"]

        prev_ts = None
        for msg, user in rows:
            # 检查时间间隔
            if prev_ts and (msg.create_time - prev_ts) > TIME_GAP_THRESHOLD_MS:
                lines.append("\n--- 时间间隔 ---\n")

            time_str = _format_timestamp(msg.create_time)
            content = _truncate(parse_content(msg.content).render())

            # 标记锚点消息
            marker = "→ " if msg.message_id in anchor_set else "  "
            lines.append(f"{marker}[{time_str}] {user.name}: {content}")

            prev_ts = msg.create_time

        return "\n".join(lines)

    except Exception as e:
        logger.error(f"search_group_history error: {e}", exc_info=True)
        return f"搜索失败: {e}"
