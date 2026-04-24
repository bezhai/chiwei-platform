"""hydrate_message @node: MessageRequest -> Message | None.

MQ-entry adapter: consumes ``MessageRequest`` frames that the engine
decoded from ``Source.mq("vectorize")`` bodies, looks up the real
``ConversationMessage`` row by id, and lifts it into a ``Message`` Data
via ``Message.from_cm`` (same mapping as ``emit_legacy_message``).

Returns ``None`` when the row is missing — a publisher racing a pg
deletion should ack-and-skip, never poison the queue. The runtime's
``@node -> Data | None`` contract drops ``None`` before the next edge.
"""
from __future__ import annotations

import logging

from app.data.queries import find_message_by_id
from app.data.session import get_session
from app.domain.message import Message
from app.domain.message_request import MessageRequest
from app.runtime.node import node

logger = logging.getLogger(__name__)


@node
async def hydrate_message(req: MessageRequest) -> Message | None:
    async with get_session() as s:
        cm = await find_message_by_id(s, req.message_id)
    if cm is None:
        logger.warning(
            "hydrate_message: message_id=%s not found, drop", req.message_id
        )
        return None
    return Message.from_cm(cm)
