"""Chat-side fire-and-forget background events.

Phase 6 v4 Gap 5 closure: replaces ad-hoc ``asyncio.create_task`` calls in
``app/chat/`` with proper Data classes that flow through the dataflow graph.
Each event Data declares an intent ("persist these tos files mappings");
the wire in ``app/wiring/chat.py`` routes it to a node that does the work,
and the durable channel gives us mq trace + DLQ instead of fire-and-forget
silence.
"""
from __future__ import annotations

from typing import Annotated

from app.runtime import Data, Key


class ConversationMessageContentSynced(Data):
    """Trigger background sync of message.content tos_file mappings.

    Carries the (message_id, content) tuples from quick_search plus the
    image_key -> tos_file mapping discovered during ``build_chat_context``.
    The ``persist_tos_files_node`` rewrites ``ConversationMessage.content``
    rows so subsequent reads find tos_file references inline.

    Persisted (NOT transient) — wire is ``.durable()`` so the DB write
    runs out of band of the chat stream; durable wires require a real pg
    table for ``insert_idempotent`` mq-redelivery dedup. The trigger
    chat ``message_id`` serves as the natural key.
    """

    message_id: Annotated[str, Key]
    messages_json: list[dict]
    image_key_to_file: dict[str, str]
