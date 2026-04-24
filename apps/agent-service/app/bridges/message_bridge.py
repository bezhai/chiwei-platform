"""Message Bridge: lift legacy ConversationMessage rows into new Message Data.

Exists during Phases 1-4. After Phase 5 the call sites are deleted along with
this file. Field mapping lives on ``Message.from_cm`` — this module only
wraps it with ``emit`` so callers don't need to know about the runtime edge.
"""
from __future__ import annotations

from app.data.models import ConversationMessage
from app.domain.message import Message
from app.runtime.emit import emit


async def emit_legacy_message(cm: ConversationMessage) -> None:
    await emit(Message.from_cm(cm))
