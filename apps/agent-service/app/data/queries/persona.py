"""Persona / bot config queries."""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.future import select

from app.data.models import BotPersona
from app.runtime.db import auto_tx, current_session

__all__ = [
    "find_persona",
    "list_all_persona_ids",
    "resolve_persona_id",
    "resolve_bot_name_for_persona",
    "find_bot_names_for_persona",
]


async def find_persona(persona_id: str) -> BotPersona | None:
    """Fetch a bot persona by primary key."""
    async with auto_tx():
        return await current_session().get(BotPersona, persona_id)


async def list_all_persona_ids() -> list[str]:
    """Return all persona_id values from bot_persona table."""
    async with auto_tx():
        result = await current_session().execute(select(BotPersona.persona_id))
        return [row[0] for row in result.all()]


async def resolve_persona_id(bot_name: str) -> str:
    """Map bot_name -> persona_id via bot_config table. Falls back to bot_name itself."""
    async with auto_tx():
        result = await current_session().execute(
            text("SELECT persona_id FROM bot_config WHERE bot_name = :bn"),
            {"bn": bot_name},
        )
        row = result.scalar_one_or_none()
        return row if row else bot_name


async def resolve_bot_name_for_persona(persona_id: str, chat_id: str) -> str | None:
    """Find the bot_name that should send messages for a persona in a specific chat."""
    try:
        common_conversation_id = str(UUID(chat_id))
    except ValueError:
        return None
    async with auto_tx():
        result = await current_session().execute(
            text(
                "SELECT bc.bot_name FROM bot_config bc "
                "JOIN common_bot_presence bp ON bc.bot_name = bp.bot_name "
                "WHERE bp.common_conversation_id = CAST(:cid AS uuid) "
                "AND bp.is_active = true "
                "AND bc.persona_id = :pid AND bc.is_active = true "
                "LIMIT 1"
            ),
            {"cid": common_conversation_id, "pid": persona_id},
        )
        return result.scalar_one_or_none()


async def find_bot_names_for_persona(persona_id: str) -> list[str]:
    """Return all active bot_names mapped to a persona_id."""
    async with auto_tx():
        result = await current_session().execute(
            text(
                "SELECT bot_name FROM bot_config "
                "WHERE persona_id = :pid AND is_active = true"
            ),
            {"pid": persona_id},
        )
        return list(result.scalars().all())
