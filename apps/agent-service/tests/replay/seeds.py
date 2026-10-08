"""What replayed rounds read that no round writes: the household, her phone, the people around.

Seeds are written before the steps that read them and are not part of any step's record. Ids
are fixed (``uuid5``) so they read the same in every baseline.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from sqlalchemy import text

from app.data.session import get_session

_NS = uuid.UUID("2f4c8e10-6a3b-4d59-9c7e-81b0a5d3e6f2")


def fixed_id(name: str) -> uuid.UUID:
    return uuid.uuid5(_NS, name)


# persona_id -> (name in the world, persona_core)
SISTERS = {
    "akao": ("赤尾", "她拍胶片、写角色分析、逛论坛、收周边、泡抹茶店。"),
    "ayana": ("绫奈", "她练钢琴、做手账，喜欢在阳光好的下午睡午觉。"),
    "chinagi": ("千凪", "她是大学生，研究古籍修复，说话慢条斯理。"),
}

AKAO_BOT = "chiwei"
AKAO_BOT_USER = fixed_id("user:akao-bot")
BEZHAI = fixed_id("user:bezhai")
DM_WITH_BEZHAI = fixed_id("conversation:dm-bezhai-akao")


async def seed_household() -> None:
    """The three sisters' persona rows: their names in the world and who they are."""
    async with get_session() as s:
        for persona_id, (name, core) in SISTERS.items():
            await s.execute(
                text(
                    "INSERT INTO bot_persona (persona_id, display_name, persona_core, "
                    "persona_lite, default_reply_style, error_messages) "
                    "VALUES (:p, :n, :c, :c, '', '{}')"
                ),
                {"p": persona_id, "n": name, "c": core},
            )


async def seed_akaos_phone() -> None:
    """Her bot, one private chat with bezhai, the bot present in it."""
    async with get_session() as s:
        await s.execute(
            text(
                "INSERT INTO common_user (common_user_id, channel, display_name) VALUES "
                "(CAST(:bot AS uuid), 'lark', '赤尾'), (CAST(:human AS uuid), 'lark', 'bezhai')"
            ),
            {"bot": str(AKAO_BOT_USER), "human": str(BEZHAI)},
        )
        await s.execute(
            text(
                "INSERT INTO common_conversation (common_conversation_id, channel, scope, "
                "display_name, is_active) VALUES (CAST(:c AS uuid), 'lark', 'direct', "
                "'bezhai', true)"
            ),
            {"c": str(DM_WITH_BEZHAI)},
        )
        await s.execute(
            text(
                "INSERT INTO bot_config (bot_name, persona_id, common_user_id, is_active) "
                "VALUES (:b, 'akao', CAST(:u AS uuid), true)"
            ),
            {"b": AKAO_BOT, "u": str(AKAO_BOT_USER)},
        )
        await s.execute(
            text(
                "INSERT INTO common_bot_presence (common_conversation_id, bot_name, is_active) "
                "VALUES (CAST(:c AS uuid), :b, true)"
            ),
            {"c": str(DM_WITH_BEZHAI), "b": AKAO_BOT},
        )


async def bezhai_says(words: str, *, at: datetime, name: str) -> None:
    """bezhai writes to her in the private chat (what lark-service would have stored)."""
    await bezhai_sends(
        [{"kind": "text", "text": words}], summary=words, at=at, name=name
    )


async def bezhai_sends(
    items: list[dict], *, summary: str, at: datetime, name: str
) -> None:
    """bezhai sends her a message made of ``items`` (common content items: ``text``, ``image``
    with its store ``object``, ``file`` with ``key`` and ``meta.file_name``) in the private
    chat. ``summary`` is the ``content_text`` the projection would have written (text
    items verbatim, every other item as ``[kind]``)."""
    async with get_session() as s:
        await s.execute(
            text(
                "INSERT INTO common_message (common_message_id, channel, "
                "common_conversation_id, common_user_id, sender_display_name, role, content, "
                "content_text, scope, event_time, mentioned_common_user_ids) VALUES "
                "(CAST(:m AS uuid), 'lark', CAST(:c AS uuid), CAST(:u AS uuid), 'bezhai', "
                "'user', CAST(:body AS jsonb), :words, 'direct', :at, "
                "CAST(:named AS text[])::uuid[])"
            ),
            {
                "m": str(fixed_id(f"message:{name}")),
                "c": str(DM_WITH_BEZHAI),
                "u": str(BEZHAI),
                "body": json.dumps(items, ensure_ascii=False),
                "words": summary,
                "at": int(at.timestamp() * 1000),
                "named": [],
            },
        )
