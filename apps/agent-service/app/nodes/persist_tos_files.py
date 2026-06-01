"""persist_tos_files_node — write tos_file mappings back into message.content.

Phase 6 v4 Gap 5: extracted from ``app/chat/context.py:_persist_tos_files``,
re-emitted as a durable wire so the DB write happens out-of-band of the
chat stream while keeping the same effective placement (agent-service
main process). Replaces the old ``asyncio.create_task`` fire-and-forget
that bypassed the graph.
"""
from __future__ import annotations

import logging

from app.chat.content_parser import parse_content
from app.data.queries import update_messages_tos_files
from app.domain.chat_events import CommonMessageContentSynced
from app.runtime import node

logger = logging.getLogger(__name__)


@node
async def persist_tos_files_node(e: CommonMessageContentSynced) -> None:
    """Persist tos_file mappings into common_message.content rows.

    Internal failures are logged and swallowed — this is fire-and-forget
    background sync, the data is still recoverable on the next chat turn
    because ``_collect_images`` will fall through to the full pipeline.
    """
    try:
        msg_updates: dict[str, dict[str, str]] = {}
        for msg in e.messages_json:
            content = msg.get("content")
            if content is None:
                continue
            parsed = parse_content(content)
            new_mappings: dict[str, str] = {}
            for key in parsed.image_keys:
                if key in e.image_key_to_file and key not in parsed.tos_files:
                    new_mappings[key] = e.image_key_to_file[key]
            if new_mappings:
                msg_updates[msg["message_id"]] = new_mappings

        if not msg_updates:
            return

        updated = await update_messages_tos_files(msg_updates)
        logger.info("tos_file persisted for %d messages", updated)
    except Exception:
        logger.warning("tos_file persistence failed", exc_info=True)
