"""主动发言基础设施 — 未读消息查询 + 搭话记录查询 + 合成消息投递

由 Glimpse 管线调用，不再有独立的扫描编排。
"""

import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.clients.rabbitmq import CHAT_REQUEST, RabbitMQClient
from app.orm.base import AsyncSessionLocal
from app.orm.models import ConversationMessage
from app.services.content_parser import parse_content

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────────────────
TARGET_CHAT_ID = "oc_a44255e98af05f1359aeb29eeb503536"
PROACTIVE_USER_ID = "__proactive__"

CST = timezone(timedelta(hours=8))


# ── 未读消息获取 ──────────────────────────────────────────────────────────


async def get_unseen_messages(chat_id: str, after: int = 0, limit: int = 30) -> list[ConversationMessage]:
    """获取指定时间戳之后的用户消息

    Args:
        chat_id: 群 ID
        after: 只返回 create_time > after 的消息（毫秒时间戳），0 表示不限
        limit: 最多返回 N 条（取最新的）
    """
    async with AsyncSessionLocal() as session:
        stmt = (
            select(ConversationMessage)
            .where(
                ConversationMessage.chat_id == chat_id,
                ConversationMessage.role == "user",
                ConversationMessage.user_id != PROACTIVE_USER_ID,
                ConversationMessage.create_time > after,
            )
            .order_by(ConversationMessage.create_time.desc())
            .limit(limit)
        )

        result = await session.execute(stmt)
        rows = list(result.scalars().all())
        rows.reverse()  # 恢复时间正序
        return rows


# ── 搭话记录查询 ─────────────────────────────────────────────────────────


async def _get_recent_proactive_records(chat_id: str) -> list[dict]:
    """查询今日的主动触发记录（user_id=PROACTIVE_USER_ID）"""
    today_start = datetime.now(CST).replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_ms = int(today_start.timestamp() * 1000)

    async with AsyncSessionLocal() as session:
        stmt = (
            select(ConversationMessage)
            .where(
                ConversationMessage.chat_id == chat_id,
                ConversationMessage.user_id == PROACTIVE_USER_ID,
                ConversationMessage.create_time >= today_start_ms,
            )
            .order_by(ConversationMessage.create_time.desc())
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()

    records = []
    for msg in rows:
        ts = datetime.fromtimestamp(msg.create_time / 1000, tz=CST)
        records.append({
            "time": ts.strftime("%H:%M"),
            "summary": parse_content(msg.content).render()[:80],
        })
    return records


# ── 合成消息与投递 ─────────────────────────────────────────────────────────


async def submit_proactive_request(
    chat_id: str,
    persona_id: str,
    target_message_id: str | None,
    stimulus: str | None,
) -> str:
    """创建合成触发消息并发布 chat_request

    Returns:
        生成的 session_id
    """
    from app.services.bot_context import _resolve_bot_name_for_persona
    bot_name = await _resolve_bot_name_for_persona(persona_id, chat_id)

    session_id = str(uuid.uuid4())
    message_id = f"proactive_{int(time.time() * 1000)}"
    now_ms = int(time.time() * 1000)

    # 构建合成消息内容
    content = json.dumps(
        {"v": 2, "text": stimulus or "", "items": [{"type": "text", "value": stimulus or ""}]},
        ensure_ascii=False,
    )

    # 写入数据库
    async with AsyncSessionLocal() as session:
        msg = ConversationMessage(
            message_id=message_id,
            user_id=PROACTIVE_USER_ID,
            content=content,
            role="user",
            root_message_id=message_id,
            reply_message_id=target_message_id,
            chat_id=chat_id,
            chat_type="group",
            create_time=now_ms,
            message_type="proactive_trigger",
            vector_status="skipped",
            bot_name=bot_name,
        )
        session.add(msg)
        await session.commit()

    # 发布到 chat_request 队列
    from app.clients.rabbitmq import _current_lane
    current_lane = _current_lane()
    client = RabbitMQClient.get_instance()
    await client.publish(
        CHAT_REQUEST,
        {
            "session_id": session_id,
            "message_id": message_id,
            "chat_id": chat_id,
            "is_p2p": False,
            "root_id": target_message_id or "",
            "user_id": PROACTIVE_USER_ID,
            "bot_name": bot_name,
            "is_proactive": True,
            "lane": current_lane,
            "enqueued_at": now_ms,
        },
    )

    logger.info(
        "Proactive request submitted: session_id=%s, target=%s",
        session_id,
        target_message_id,
    )
    return session_id
