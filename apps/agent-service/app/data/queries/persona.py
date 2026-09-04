"""Persona / bot config queries.

``bot_persona`` 有 SQLAlchemy 模型，走 ORM。``bot_config`` / ``common_bot_presence``
由 channel-server 管理、agent-service 侧没有模型（只读裸表），所以读它们的查询一律
走 ``text()``。
"""
from __future__ import annotations

from sqlalchemy import text

from app.data.models import BotPersona
from app.runtime.db import auto_tx, current_session

__all__ = [
    "find_persona",
    "find_bot_names_for_persona",
    "find_bot_user_ids_for_persona",
    "find_conversations_with_persona_bot",
]


async def find_persona(persona_id: str) -> BotPersona | None:
    """Fetch a bot persona by primary key."""
    async with auto_tx():
        return await current_session().get(BotPersona, persona_id)


async def find_bot_names_for_persona(persona_id: str) -> list[str]:
    """Return all active bot_names mapped to a persona_id.

    是列表不是单值：一个 persona 线上就挂着好几个 bot（正式那个和 dev 那个指向同一
    个人）。这份名单是"这句话是不是她说的"的全部依据（``common_message.bot_name``
    比对它，见 :mod:`app.data.queries.messages`）—— 查不到就是空列表，那意味着她根本
    没有 bot，:func:`find_conversations_with_persona_bot` 同样返回空，不会出现"把所有
    人的话都当成别人说的"这种半截状态。
    """
    async with auto_tx():
        result = await current_session().execute(
            text(
                "SELECT bot_name FROM bot_config "
                "WHERE persona_id = :pid AND is_active = true"
            ),
            {"pid": persona_id},
        )
        return [str(row) for row in result.scalars().all()]


async def find_bot_user_ids_for_persona(persona_id: str) -> list[str]:
    """这个 persona 那些 bot 在公共层的身份（``bot_config.common_user_id``）。

    拿它跟 ``common_message.mentioned_common_user_ids`` 比，就是"群里点的是不是她"
    的全部依据。那一列由投影层在落账时写下，装的是被 @ 的人在公共层的 id，所以
    "被点名"是**库里的客观事实**，不需要模型判断。

    ``common_user_id`` 还没回填的 bot 不进结果（``IS NOT NULL``）—— 一个 ``None``
    混进去就是拿它去跟 mention 数组比。全都没回填时返回空列表：点名判定恒为假，群
    里谁都叫不动她，但私聊不受影响。
    """
    async with auto_tx():
        result = await current_session().execute(
            text(
                "SELECT common_user_id FROM bot_config "
                "WHERE persona_id = :pid AND is_active = true "
                "AND common_user_id IS NOT NULL"
            ),
            {"pid": persona_id},
        )
        return [str(row) for row in result.scalars().all()]


# 这个 persona 的 bot 还在的那些会话。
#
# 用 ``common_bot_presence`` 而不是"聊过天就算"：bot 被移出群之后历史还在、但它既收
# 不到也发不出去。``cc.is_active`` 同理挡掉已归档的会话。
#
# ``MIN(bc.bot_name)`` 是**出站身份**：一个 persona 名下挂着好几个 bot 时同一条会话
# 会匹配出好几行，聚合成一行并定下用哪个身份说话。出站身份必须在这里就确定 —— 主动
# 发不写 ``common_agent_response``，投递方没有别处可推断用哪个 bot。
_CONVERSATIONS_WITH_BOT_SQL = """
SELECT cc.common_conversation_id AS channel_id,
       cc.scope                  AS scope,
       COALESCE(cc.display_name, '') AS title,
       cc.channel                AS channel,
       MIN(bc.bot_name)          AS bot_name
  FROM common_bot_presence bp
  JOIN bot_config bc
    ON bc.bot_name = bp.bot_name
   AND bc.persona_id = :persona_id
   AND bc.is_active = true
  JOIN common_conversation cc
    ON cc.common_conversation_id = bp.common_conversation_id
   AND cc.is_active = true
 WHERE bp.is_active = true
 GROUP BY cc.common_conversation_id, cc.scope, cc.display_name, cc.channel
"""


async def find_conversations_with_persona_bot(persona_id: str) -> list[dict]:
    """这个 persona 的 bot 还在的每一条会话：地址、私聊还是群、叫什么、走哪个渠道、
    用哪个 bot 身份说话。

    ``title`` 是 ``COALESCE(display_name, '')``（私聊多半没有标题，落空串不落
    ``None``）。查不到就是空列表，不猜、不兜底。
    """
    async with auto_tx():
        rows = (
            await current_session().execute(
                text(_CONVERSATIONS_WITH_BOT_SQL), {"persona_id": persona_id}
            )
        ).mappings().all()
    return [dict(r) for r in rows]
