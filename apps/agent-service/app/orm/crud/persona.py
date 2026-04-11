"""Persona / bot_config CRUD operations"""

from sqlalchemy import text
from sqlalchemy.future import select

from app.orm.base import AsyncSessionLocal


async def get_bot_persona(persona_id: str) -> "BotPersona | None":
    """获取 bot 人设配置"""
    from app.orm.models import BotPersona

    async with AsyncSessionLocal() as session:
        return await session.get(BotPersona, persona_id)


async def get_all_persona_ids() -> list[str]:
    """获取所有 persona 的 persona_id 列表"""
    from app.orm.models import BotPersona

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(BotPersona.persona_id))
        return [row[0] for row in result.all()]


async def get_gray_config(message_id: str) -> dict | None:
    """根据 message_id 关联查询所属 chat 的灰度配置"""
    from app.orm.models import ConversationMessage, LarkBaseChatInfo

    async with AsyncSessionLocal() as session:
        stmt = (
            select(LarkBaseChatInfo.gray_config)
            .join(
                ConversationMessage,
                ConversationMessage.chat_id == LarkBaseChatInfo.chat_id,
            )
            .where(ConversationMessage.message_id == message_id)
        )
        return await session.scalar(stmt)


# ── Extracted from bot_context.py / message_router.py ──


async def resolve_persona_id(bot_name: str) -> str:
    """从 bot_config 表查 persona_id，找不到则用 bot_name 自身"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT persona_id FROM bot_config WHERE bot_name = :bn"),
            {"bn": bot_name},
        )
        row = result.scalar_one_or_none()
        return row if row else bot_name


async def resolve_bot_name_for_persona(persona_id: str, chat_id: str) -> str | None:
    """从 persona_id + chat_id 精确查找应该用哪个 bot 发消息

    查 bot_chat_presence JOIN bot_config，精确匹配群内的 bot。
    返回 bot_name 或 None（表示未命中）。
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT bc.bot_name FROM bot_config bc "
                "JOIN bot_chat_presence bp ON bc.bot_name = bp.bot_name "
                "WHERE bp.chat_id = :cid AND bp.is_active = true "
                "AND bc.persona_id = :pid AND bc.is_active = true "
                "LIMIT 1"
            ),
            {"cid": chat_id, "pid": persona_id},
        )
        return result.scalar_one_or_none()


async def resolve_mentioned_personas(mentions: list[str]) -> list[str]:
    """将 mention 的 app_id 列表映射到 persona_id 列表"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT DISTINCT persona_id FROM bot_config "
                "WHERE app_id = ANY(:mentions) "
                "AND is_active = true "
                "AND persona_id IS NOT NULL"
            ),
            {"mentions": mentions},
        )
        return [row[0] for row in result.fetchall()]
