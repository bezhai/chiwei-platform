"""Message routing — decide which personas should reply to a message.

Channel-specific addressing is resolved before agent-service. For Lark,
channel-server maps mentioned bot app_ids to common persona_ids and sends
those ids in ``ChatTrigger.persona_ids``. agent-service never reads Lark
credentials or app ids.
"""

from __future__ import annotations

import logging

from app.data.queries import resolve_persona_id

logger = logging.getLogger(__name__)


class MessageRouter:
    """Decide which persona_ids should reply to a given message."""

    async def route(
        self,
        chat_id: str,
        persona_ids: list[str],
        bot_name: str,
        is_p2p: bool,
    ) -> list[str]:
        """Return persona_id list for responders.

        Args:
            chat_id: conversation ID
            persona_ids: channel-resolved persona IDs
            bot_name: the bot that grabbed the MQ lock
            is_p2p: whether this is a private chat

        Returns:
            persona_id list; empty means "don't reply"
        """
        if is_p2p:
            pid = await resolve_persona_id(bot_name)
            logger.info("P2P route: bot_name=%s -> persona_id=%s", bot_name, pid)
            return [pid]

        if persona_ids:
            deduped = list(dict.fromkeys(persona_ids))
            logger.info(
                "Group addressed route: persona_ids=%s",
                deduped,
            )
            return deduped

        # Group without channel-resolved persona target -> no reply.
        return []
