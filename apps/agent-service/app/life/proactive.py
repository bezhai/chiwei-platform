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

from app.data.models import ConversationMessage
from app.runtime.db import emit_tx, tx

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
) -> ConversationMessage | None:
    """Resolve a glimpse target to the real conversation message, if possible."""
    if not target_message_id:
        return None

    from app.data import queries as Q

    async with tx():
        if target_message_id.isdigit():
            message_id = await Q.resolve_message_id_by_row_id(target_message_id)
            if not message_id:
                return None
            msg = await Q.find_message_by_id(message_id)
        else:
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


# Bug 2 fix: T5-5c 数据迁移完成前 conversation_messages 可能仍有飞书裸 message
# 字段（om_*）。proactive 拉出来的 target_msg.message_id / root_message_id
# 若仍是飞书裸 id，**不能**当全局 internal_message_id 用：放进 ChatTrigger.root_id
# 会让 chat-response-worker 出站 reverseResolveForLark 抛
# IdentityNotFoundError 丢消息（prod 已遇到）。落到 conversation_messages.
# root_message_id / reply_message_id 也会让按全局主键 walk 回复链的读取方
# (_context_messages.py / cross_chat.py) 失配。
# 防御：飞书裸 om_* 一律视为「没有合法全局 root」，置空走"无 reply 锚点直接
# 发新"的降级路径，而不是丢消息。T5-5c 全量迁移完成后这层防御仍保留 ——
# 任何新 channel 引入新 raw id prefix 都不会污染全局主键链路。
def _is_lark_raw_id(value: str | None) -> bool:
    """Return True iff value looks like a lark native message id (om_*).

    Global internal ids are Crockford-base32 ULIDs (uppercase, 26 chars) or
    synthetic ``proactive_<ts>``; neither starts with ``om_``.
    """
    return bool(value) and isinstance(value, str) and value.startswith("om_")


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
    raw_target_msg_id = target_msg.message_id if target_msg else None
    raw_root_message_id = target_msg.root_message_id if target_msg else None
    # 见上方 _is_lark_raw_id 注释：飞书裸 om_* 不准下游链路传播，强制置空。
    target_global_id = (
        None if _is_lark_raw_id(raw_target_msg_id) else raw_target_msg_id
    )
    root_message_id = (
        None if _is_lark_raw_id(raw_root_message_id) else raw_root_message_id
    )

    bot_name = await Q.resolve_bot_name_for_persona(persona_id, chat_id)

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

    from app.domain.chat_dataflow import ChatTrigger
    from app.domain.message import Message
    from app.infra.rabbitmq import current_lane

    msg = ConversationMessage(
        message_id=message_id,
        user_id=PROACTIVE_USER_ID,
        content=content,
        role="user",
        root_message_id=root_message_id or message_id,
        # 飞书裸 om_* 已被上方 _is_lark_raw_id 过滤为 None，避免污染
        # conversation_messages 的回复链 walk（按全局主键关联）
        reply_message_id=target_global_id,
        chat_id=chat_id,
        chat_type="group",
        create_time=now_ms,
        message_type="proactive_trigger",
        bot_name=bot_name,
    )

    async with tx():
        await Q.insert_proactive_message(msg)
        await emit_tx(Message.from_cm(msg))
        await emit_tx(
            ChatTrigger(
                message_id=message_id,
                session_id=session_id,
                chat_id=chat_id,
                is_p2p=False,
                # 飞书裸 om_* 已被 _is_lark_raw_id 过滤为 None。chat-response-worker
                # 出站 reverseResolveForLark(rootGlobalId) 要求全局 ULID，
                # 否则抛 IdentityNotFoundError 整段回复炸（prod 已遇到）。
                root_id=target_global_id,
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
        target_global_id,
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
