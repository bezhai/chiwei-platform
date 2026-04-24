"""Proactive chat -- unseen message queries + synthetic message submission.

Called by Glimpse when Chiwei decides she wants to speak up in a group.
No independent scanning loop; Glimpse drives everything.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.future import select as sa_select

from app.data.models import ConversationMessage
from app.data.session import get_session
from app.infra.rabbitmq import CHAT_REQUEST, mq

logger = logging.getLogger(__name__)

PROACTIVE_USER_ID = "__proactive__"

_CST = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# Unseen message query
# ---------------------------------------------------------------------------


async def get_unseen_messages(
    chat_id: str, *, after: int = 0, limit: int = 30
) -> list[ConversationMessage]:
    """Fetch user messages newer than *after* (ms timestamp).

    Returns up to *limit* messages in chronological order.
    """
    async with get_session() as session:
        stmt = (
            sa_select(ConversationMessage)
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

    rows.reverse()  # restore chronological order
    return rows


# ---------------------------------------------------------------------------
# Proactive message submission
# ---------------------------------------------------------------------------


async def _resolve_target_message(
    target_message_id: str | None,
    chat_id: str,
) -> ConversationMessage | None:
    """Resolve a glimpse target to the real conversation message, if possible."""
    if not target_message_id:
        return None

    from app.data import queries as Q

    async with get_session() as s:
        if target_message_id.isdigit():
            message_id = await Q.resolve_message_id_by_row_id(s, target_message_id)
            if not message_id:
                return None
            msg = await Q.find_message_by_id(s, message_id)
        else:
            msg = await Q.find_message_by_id(s, target_message_id)

    if msg and msg.chat_id == chat_id:
        return msg
    if msg:
        logger.warning(
            "Proactive target ignored: target_chat=%s current_chat=%s message_id=%s",
            msg.chat_id,
            chat_id,
            msg.message_id,
        )
    return None


async def submit_proactive_chat(
    chat_id: str,
    persona_id: str,
    target_message_id: str | None,
    stimulus: str | None,
) -> str:
    """Create a synthetic trigger message and publish to chat_request queue.

    Returns the generated ``session_id``.
    """
    from app.data import queries as Q

    target_msg = await _resolve_target_message(target_message_id, chat_id)
    target_lark_id = target_msg.message_id if target_msg else None
    root_message_id = target_msg.root_message_id if target_msg else None

    async with get_session() as s:
        bot_name = await Q.resolve_bot_name_for_persona(s, persona_id, chat_id)

    session_id = str(uuid.uuid4())
    message_id = f"proactive_{int(time.time() * 1000)}"
    now_ms = int(time.time() * 1000)

    content = json.dumps(
        {
            "v": 2,
            "text": stimulus or "",
            "items": [{"type": "text", "value": stimulus or ""}],
        },
        ensure_ascii=False,
    )

    async with get_session() as session:
        msg = ConversationMessage(
            message_id=message_id,
            user_id=PROACTIVE_USER_ID,
            content=content,
            role="user",
            root_message_id=root_message_id or message_id,
            reply_message_id=target_lark_id,
            chat_id=chat_id,
            chat_type="group",
            create_time=now_ms,
            message_type="proactive_trigger",
            vector_status="skipped",
            bot_name=bot_name,
        )
        session.add(msg)
    # get_session() commits on block exit; emit AFTER commit so downstream
    # consumers querying pg will see the row.
    from app.bridges.message_bridge import emit_legacy_message  # local import to avoid boot cycles

    await emit_legacy_message(msg)

    # Publish to chat_request queue
    from app.infra.rabbitmq import current_lane

    lane = current_lane()
    await mq.publish(
        CHAT_REQUEST,
        {
            "session_id": session_id,
            "message_id": message_id,
            "chat_id": chat_id,
            "is_p2p": False,
            "root_id": target_lark_id or "",
            "user_id": PROACTIVE_USER_ID,
            "bot_name": bot_name,
            "is_proactive": True,
            "lane": lane,
            "enqueued_at": now_ms,
        },
    )

    logger.info(
        "Proactive request submitted: session_id=%s, target=%s",
        session_id,
        target_lark_id,
    )
    return session_id


# ---------------------------------------------------------------------------
# Proactive history query (used by Glimpse for rate-limiting context)
# ---------------------------------------------------------------------------


async def get_recent_proactive_records(chat_id: str, bot_name: str) -> list[dict]:
    """Query today's proactive trigger records for a specific persona in a chat.

    Returns list of ``{"time": "HH:MM", "summary": "..."}`` dicts.
    """
    from app.chat.content_parser import parse_content

    today_start = datetime.now(_CST).replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_ms = int(today_start.timestamp() * 1000)

    async with get_session() as session:
        stmt = (
            sa_select(ConversationMessage)
            .where(
                ConversationMessage.chat_id == chat_id,
                ConversationMessage.user_id == PROACTIVE_USER_ID,
                ConversationMessage.bot_name == bot_name,
                ConversationMessage.create_time >= today_start_ms,
            )
            .order_by(ConversationMessage.create_time.desc())
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()

    records = []
    for msg in rows:
        ts = datetime.fromtimestamp(msg.create_time / 1000, tz=_CST)
        records.append(
            {
                "time": ts.strftime("%H:%M"),
                "summary": parse_content(msg.content).render()[:80],
            }
        )
    return records
