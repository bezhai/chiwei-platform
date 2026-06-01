"""Proactive chat -- unseen message queries + synthetic message submission.

Called by Glimpse when Chiwei decides she wants to speak up in a group.
No independent scanning loop; Glimpse drives everything.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID

from uuid6 import uuid7

from app.data.message_record import CommonMessageRecord
from app.data.models import CommonMessage
from app.runtime.db import emit_tx, tx

logger = logging.getLogger(__name__)

PROACTIVE_USER_ID = "__proactive__"

_CST = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# Unseen message query
# ---------------------------------------------------------------------------


async def get_unseen_messages(
    chat_id: str, *, after: int = 0, limit: int = 30
) -> list[CommonMessageRecord]:
    """Fetch user messages newer than *after* (ms timestamp).

    Returns up to *limit* messages in chronological order.
    """
    from app.data import queries as Q

    rows = await Q.find_user_messages_after(
        chat_id,
        after=after,
        limit=limit,
        exclude_user_id=PROACTIVE_USER_ID,
    )
    rows.reverse()  # restore chronological order
    return rows


# ---------------------------------------------------------------------------
# Proactive message submission
# ---------------------------------------------------------------------------


async def _resolve_target_message(
    target_message_id: str | None,
    chat_id: str,
) -> CommonMessageRecord | None:
    """Resolve a glimpse target to a common message in the same conversation."""
    if not target_message_id:
        return None

    from app.data import queries as Q

    async with tx():
        msg = await Q.find_message_by_id(target_message_id)

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


def _uuid(value: str) -> UUID:
    return UUID(value)


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
    target_common_id = target_msg.message_id if target_msg else None
    root_common_id = target_msg.root_message_id if target_msg else None

    bot_name = await Q.resolve_bot_name_for_persona(persona_id, chat_id)

    session_id = str(uuid7())
    message_uuid = uuid7()
    message_id = str(message_uuid)
    now_ms = int(time.time() * 1000)

    from app.domain.chat_dataflow import ChatTrigger
    from app.domain.message_request import MessageRequest
    from app.infra.rabbitmq import current_lane

    content_items = [{"type": "text", "value": stimulus or ""}]
    common_msg = CommonMessage(
        common_message_id=message_uuid,
        channel="system",
        common_conversation_id=_uuid(chat_id),
        common_user_id=None,
        sender_display_name="proactive",
        role="user",
        content=content_items,
        content_text=stimulus or "",
        common_root_message_id=_uuid(root_common_id) if root_common_id else message_uuid,
        common_reply_message_id=_uuid(target_common_id) if target_common_id else None,
        scope="group",
        message_type="proactive_trigger",
        bot_name=bot_name,
        response_id=session_id,
        event_time=now_ms,
    )
    async with tx():
        await Q.insert_proactive_message(common_msg)
        await emit_tx(MessageRequest(message_id=message_id))
        await emit_tx(
            ChatTrigger(
                message_id=message_id,
                session_id=session_id,
                chat_id=chat_id,
                is_p2p=False,
                root_id=target_common_id,
                user_id=PROACTIVE_USER_ID,
                bot_name=bot_name,
                is_proactive=True,
                lane=current_lane(),
                enqueued_at=now_ms,
            )
        )

    logger.info(
        "Proactive request submitted: session_id=%s, target=%s",
        session_id,
        target_common_id,
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
    from app.data import queries as Q

    today_start = datetime.now(_CST).replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_ms = int(today_start.timestamp() * 1000)

    rows = await Q.find_proactive_messages_in_chat(
        chat_id,
        bot_name=bot_name,
        proactive_user_id=PROACTIVE_USER_ID,
        since_ms=today_start_ms,
    )

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
