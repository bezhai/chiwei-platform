"""按 chat_id 捞最近 N 条对话(proactive 渲染的历史上下文来源)。

proactive(赤尾主动给真人发消息)**没有源消息**,渲染历史不能走 quick_search
(它从 message_id 反查),只能靠 chat_id 取。``find_recent_chat_messages`` 就是
这一手:给一个 chat_id,捞这个会话最近 N 条消息(user + assistant 都要),按发生
先后升序,并为 assistant 行带出发言 persona(让 proactive context 能把赤尾自己
发过的认作她自己说的)。

锁死的 SQL 语义(真 Postgres 集成测试):
  1. user + assistant 都取(区别于 find_user_messages_after 只取 user)。
  2. assistant 行经 response_id → common_agent_response.session_id join 出 persona_id。
  3. 超 limit 只保最近 N 条、仍按先后升序(条目数量控制、不字符截断)。
  4. proactive_trigger 伪消息剔除(NULL-safe)。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

import app.data.session as session_mod
from app.data.models import (
    Base,
    CommonAgentResponse,
    CommonConversation,
    CommonMessage,
)
from app.data.queries.messages import find_recent_chat_messages

_CHAT = uuid.uuid5(uuid.NAMESPACE_OID, "recent-chat")
_USER = uuid.uuid5(uuid.NAMESPACE_OID, "recent-user")
# bot 在 common_user 里的身份。proactive 出站行真实落库时 common_user_id 是 bot 自己
# 的 common_user_id（channel-server storeLarkOutboundMessage 写 botCommonUserId），
# 不是 NULL —— 真实形态复现要带上它。
_BOT_USER = uuid.uuid5(uuid.NAMESPACE_OID, "recent-bot-user")

# bot_config 由 channel-server 管理、不在 agent-service 的 SQLAlchemy 模型里。
# proactive 出站落库的 assistant 行只带 bot_name（response_id=NULL），发言 persona
# 必须经 bot_config(bot_name → persona_id) 映射拿到，所以集成测试要手动建这张表 +
# 灌一行（镜像 channel-server 的 bot_config 列：bot_name / persona_id / is_active）。
_BOT_CONFIG_DDL = (
    "CREATE TABLE bot_config ("
    "  bot_name VARCHAR(50) PRIMARY KEY,"
    "  persona_id VARCHAR(50),"
    "  is_active BOOLEAN NOT NULL DEFAULT TRUE"
    ")"
)


@pytest.fixture
async def chat_db(test_db):
    tables = [
        CommonMessage.__table__,
        CommonAgentResponse.__table__,
        CommonConversation.__table__,
    ]
    async with test_db.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables)
        )
        await conn.execute(text(_BOT_CONFIG_DDL))
    yield test_db


async def _seed_bot_config(bot_name, persona_id, *, is_active=True):
    async with session_mod.get_session() as s:
        await s.execute(
            text(
                "INSERT INTO bot_config (bot_name, persona_id, is_active) "
                "VALUES (:bn, :pid, :active)"
            ),
            {"bn": bot_name, "pid": persona_id, "active": is_active},
        )


async def _seed_proactive_bot_message(
    chat_id, *, event_time, msg_text, bot_name, scope="direct", common_user_id=None
):
    """造一条**真实形态**的 proactive 出站 assistant 行：role=assistant、带 bot_name、
    **response_id=NULL**、**没有** CommonAgentResponse 行（worker 真实落库口径，见
    channel-server storeLarkOutboundMessage：proactive session_id=null → responseId 不挂）。

    ``common_user_id`` 默认 None；真实落库口径里它是 bot 自己的 common_user_id（非
    NULL），传进来即可复现完整形态。
    """
    async with session_mod.get_session() as s:
        s.add(
            CommonMessage(
                common_message_id=uuid.uuid4(),
                channel="lark",
                common_conversation_id=chat_id,
                common_user_id=common_user_id,
                sender_display_name=None,
                role="assistant",
                content=[{"kind": "text", "text": msg_text}],
                content_text=msg_text,
                scope=scope,
                message_type="post",
                bot_name=bot_name,
                response_id=None,
                event_time=event_time,
            )
        )


async def _seed_conversation(chat_id, *, scope="direct", name=None):
    async with session_mod.get_session() as s:
        s.add(
            CommonConversation(
                common_conversation_id=chat_id,
                channel="lark",
                scope=scope,
                display_name=name,
            )
        )


async def _seed_user_message(
    chat_id, *, event_time, text, message_type=None, scope="direct", bot_name=None
):
    async with session_mod.get_session() as s:
        s.add(
            CommonMessage(
                common_message_id=uuid.uuid4(),
                channel="lark",
                common_conversation_id=chat_id,
                common_user_id=_USER,
                sender_display_name="原智鸿",
                role="user",
                content=[{"kind": "text", "text": text}],
                content_text=text,
                scope=scope,
                message_type=message_type,
                bot_name=bot_name,
                event_time=event_time,
            )
        )


async def _seed_bot_message(chat_id, *, event_time, text, persona_id, scope="direct"):
    session_id = f"sess-{uuid.uuid4().hex}"
    async with session_mod.get_session() as s:
        s.add(
            CommonAgentResponse(
                response_id=uuid.uuid4(),
                session_id=session_id,
                trigger_common_message_id=uuid.uuid4(),
                common_conversation_id=chat_id,
                persona_id=persona_id,
            )
        )
        s.add(
            CommonMessage(
                common_message_id=uuid.uuid4(),
                channel="lark",
                common_conversation_id=chat_id,
                common_user_id=None,
                sender_display_name=None,
                role="assistant",
                content=[{"kind": "text", "text": text}],
                content_text=text,
                scope=scope,
                response_id=session_id,
                event_time=event_time,
            )
        )


@pytest.mark.integration
async def test_returns_both_roles_ascending_with_persona(chat_db):
    """user + assistant 都取，升序，assistant 行带出发言 persona。"""
    await _seed_conversation(_CHAT)
    await _seed_user_message(_CHAT, event_time=1000, text="在吗")
    await _seed_bot_message(_CHAT, event_time=2000, text="我刚在想你", persona_id="akao")
    await _seed_user_message(_CHAT, event_time=3000, text="哈哈")

    got = await find_recent_chat_messages(chat_id=str(_CHAT), limit=10)

    texts = [r.content for r, _p in got]
    assert len(got) == 3
    assert "在吗" in texts[0]
    assert "我刚在想你" in texts[1]
    assert "哈哈" in texts[2]
    # assistant 行带出 persona，user 行无 persona
    assert got[0][1] is None
    assert got[1][1] == "akao", "赤尾自己发的那条要能认出是她"
    assert got[2][1] is None


@pytest.mark.integration
async def test_limit_keeps_most_recent_ascending(chat_db):
    """超 limit 只保最近 N 条、仍升序(条目数量控制、不字符截断)。"""
    await _seed_conversation(_CHAT)
    for i in range(5):
        await _seed_user_message(_CHAT, event_time=1000 + i, text=f"第{i}条")

    got = await find_recent_chat_messages(chat_id=str(_CHAT), limit=3)

    texts = [r.content for r, _p in got]
    assert len(got) == 3, "超限只保最近 3 条"
    assert "第2" in texts[0] and "第3" in texts[1] and "第4" in texts[2], (
        f"保最近的、按先后升序，实际 {texts}"
    )


@pytest.mark.integration
async def test_proactive_trigger_pseudo_messages_excluded(chat_db):
    """proactive_trigger 伪消息不进历史(NULL-safe，正常消息 message_type 为 NULL)。"""
    await _seed_conversation(_CHAT)
    await _seed_user_message(_CHAT, event_time=1000, text="正常消息")
    await _seed_user_message(
        _CHAT, event_time=2000, text="伪消息", message_type="proactive_trigger"
    )

    got = await find_recent_chat_messages(chat_id=str(_CHAT), limit=10)

    texts = [r.content for r, _p in got]
    assert len(got) == 1
    assert "正常消息" in texts[0]
    assert not any("伪消息" in t for t in texts)


@pytest.mark.integration
async def test_other_chat_not_mixed_in(chat_db):
    """只取这个 chat_id 的消息，别的会话不混入。"""
    other = uuid.uuid5(uuid.NAMESPACE_OID, "other-chat")
    await _seed_conversation(_CHAT)
    await _seed_conversation(other)
    await _seed_user_message(_CHAT, event_time=1000, text="本会话")
    await _seed_user_message(other, event_time=2000, text="别的会话")

    got = await find_recent_chat_messages(chat_id=str(_CHAT), limit=10)

    texts = [r.content for r, _p in got]
    assert len(got) == 1
    assert "本会话" in texts[0]


@pytest.mark.integration
async def test_bad_chat_id_returns_empty(chat_db):
    """非 uuid 形 chat_id → 空(不炸)。"""
    got = await find_recent_chat_messages(chat_id="not-a-uuid", limit=10)
    assert got == []


@pytest.mark.integration
async def test_proactive_assistant_row_attributes_persona_via_bot_name(chat_db):
    """承重（codex 必改 1）：proactive 出站 assistant 行 **response_id=NULL**、没有
    CommonAgentResponse 行，发言 persona 必须经 bot_config(bot_name → persona_id) 映射
    拿到 —— 否则它被当成 persona=None、被 proactive context 误判为真人输入（串味）。

    旧实现只靠 response_id → common_agent_response.session_id join 取 persona，对
    proactive 行（response_id=NULL）必拿 None。这里按**真实落库形态**构造：assistant +
    bot_name=chiwei + response_id=NULL + bot_config(chiwei→akao)，验证它带出 persona=akao。
    """
    await _seed_conversation(_CHAT)
    await _seed_bot_config("chiwei", "akao")
    await _seed_user_message(_CHAT, event_time=1000, text="在吗")
    # 上一条 proactive：真实形态（无 agent_response 行、response_id=NULL、只有 bot_name）
    await _seed_proactive_bot_message(
        _CHAT, event_time=2000, msg_text="我刚在想你", bot_name="chiwei"
    )

    got = await find_recent_chat_messages(chat_id=str(_CHAT), limit=10)

    assert len(got) == 2
    assert got[0][1] is None, "真人那条无 persona"
    assert got[1][1] == "akao", (
        "proactive 出站行 response_id=NULL，必须经 bot_config(bot_name→persona) "
        f"认出是赤尾自己说的，实得 {got[1][1]!r}"
    )


@pytest.mark.integration
async def test_proactive_row_with_bot_common_user_id_attributes_persona(chat_db):
    """真实落库形态复现：proactive 出站行 **common_user_id = bot 自己的 id**（非 NULL，
    channel-server storeLarkOutboundMessage 写 botCommonUserId）、response_id=NULL、
    无 agent_response 行。仍必须经 bot_config(bot_name→persona) 认出是赤尾自己说的。

    这是与 ``test_proactive_assistant_row_attributes_persona_via_bot_name`` 唯一的差别
    （那条 common_user_id=None）—— 坐实「bot 身份 id 在场是否影响 persona 兜底」。
    """
    await _seed_conversation(_CHAT)
    await _seed_bot_config("chiwei", "akao")
    await _seed_user_message(_CHAT, event_time=1000, text="在吗")
    await _seed_proactive_bot_message(
        _CHAT,
        event_time=2000,
        msg_text="我刚在想你",
        bot_name="chiwei",
        common_user_id=_BOT_USER,
    )

    got = await find_recent_chat_messages(chat_id=str(_CHAT), limit=10)

    assert len(got) == 2
    assert got[0][1] is None, "真人那条无 persona"
    assert got[1][1] == "akao", (
        "proactive 出站行（带 bot common_user_id、response_id=NULL）必须经 "
        f"bot_config(bot_name→persona) 认出是赤尾自己说的，实得 {got[1][1]!r}"
    )


@pytest.mark.integration
async def test_user_row_with_bot_name_is_not_attributed_persona(chat_db):
    """承重红线（codex 必改 1）：真人 user 行也带 bot_name（channel-server
    storeLarkInboundMessage 给 user 行写 bot_name），它指向 active 的
    bot_config(bot_name→persona)。helper 必须只对 role='assistant' 行兜底——真人 user
    行的发言 persona 仍是 None，否则会被误判为某 persona 自己说的（串味）。
    """
    await _seed_conversation(_CHAT)
    await _seed_bot_config("chiwei", "akao")
    await _seed_user_message(_CHAT, event_time=1000, text="在吗", bot_name="chiwei")

    got = await find_recent_chat_messages(chat_id=str(_CHAT), limit=10)

    assert len(got) == 1
    assert got[0][1] is None, (
        "真人 user 行即便带 bot_name，也不能经 bot_config 兜底成 persona，"
        f"实得 {got[0][1]!r}"
    )


@pytest.mark.integration
async def test_response_id_persona_still_wins_over_bot_name(chat_db):
    """普通回复行（带 response_id + CommonAgentResponse）仍按 response_id 取 persona，
    bot_config 只是 response_id 取不到时的兜底（COALESCE 顺序：response_id 优先）。"""
    await _seed_conversation(_CHAT)
    # bot_config 把 chiwei 映射到 ayana；但这条普通回复的 agent_response 写的是 akao
    await _seed_bot_config("chiwei", "ayana")
    await _seed_bot_message(_CHAT, event_time=1000, text="正常回复", persona_id="akao")

    got = await find_recent_chat_messages(chat_id=str(_CHAT), limit=10)

    assert len(got) == 1
    assert got[0][1] == "akao", (
        "有 response_id 的回复行 persona 以 agent_response 为准，不被 bot_config 覆盖"
    )
