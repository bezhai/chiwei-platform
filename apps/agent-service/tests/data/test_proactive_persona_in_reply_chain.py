"""proactive 出站行在「真人回复链」历史里也必须认出是赤尾自己说的。

承重场景：真人回复了赤尾**主动发**的那条消息后，agent-service 走 human-chat 路径
（``quick_search`` → ``find_messages_with_user_chat_persona_by_root`` /
``find_messages_with_user_chat_persona_in_chat``）把这条回复的历史拉出来组装上下文。
那段历史里**包含赤尾上一条 proactive 出站消息**。

proactive 出站行真实落库形态：``role=assistant``、带 ``bot_name``、**response_id=NULL**、
**没有** ``CommonAgentResponse`` 行（worker 口径：proactive session_id=null → responseId
不挂、不写 agent_response）。这两个回复链查询只经
``response_id → common_agent_response.session_id`` join 取 persona，对 proactive 行必拿
NULL —— 缺 ``find_recent_chat_messages`` 里那条 ``bot_config(bot_name→persona_id)`` 兜底。
persona=NULL 会让下游 ``build_p2p_messages`` 把这条判成 USER role（当成真人 bezhai 说的），
正是本 bug：**她自己说的话被认成用户输入**。

集成测试（真 Postgres）：正确性全在 SQL 的 persona 兜底语义。
"""

from __future__ import annotations

import json
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
from app.data.queries.messages import (
    find_messages_with_user_chat_persona_by_root,
    find_messages_with_user_chat_persona_in_chat,
)

_CHAT = uuid.uuid5(uuid.NAMESPACE_OID, "proactive-reply-chat")
_USER = uuid.uuid5(uuid.NAMESPACE_OID, "proactive-reply-user")
_ROOT = uuid.uuid5(uuid.NAMESPACE_OID, "proactive-reply-root")
# proactive 出站行真实落库时 common_user_id = bot 自己的 common_user_id（非 NULL）。
_BOT_USER = uuid.uuid5(uuid.NAMESPACE_OID, "proactive-reply-bot-user")

# bot_config 由 channel-server 管理、不在 agent-service 的 SQLAlchemy 模型里。proactive
# 出站行只带 bot_name（response_id=NULL），发言 persona 必须经 bot_config(bot_name →
# persona_id) 兜底拿到，所以集成测试要手动建这张表 + 灌一行。
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


async def _seed_conversation(*, scope="direct", name=None):
    async with session_mod.get_session() as s:
        s.add(
            CommonConversation(
                common_conversation_id=_CHAT,
                channel="lark",
                scope=scope,
                display_name=name,
            )
        )


async def _seed_user_message(
    message_id, *, event_time, text_, root_id, scope="direct", bot_name=None
):
    async with session_mod.get_session() as s:
        s.add(
            CommonMessage(
                common_message_id=message_id,
                channel="lark",
                common_conversation_id=_CHAT,
                common_user_id=_USER,
                common_root_message_id=root_id,
                sender_display_name="原智鸿",
                role="user",
                content=[{"kind": "text", "text": text_}],
                content_text=text_,
                scope=scope,
                bot_name=bot_name,
                event_time=event_time,
            )
        )


async def _seed_proactive_bot_message(
    message_id, *, event_time, text_, root_id, bot_name, scope="direct"
):
    """proactive 出站 assistant 行真实形态：bot_name + common_user_id=bot id +
    response_id=NULL + 无 CommonAgentResponse 行。"""
    async with session_mod.get_session() as s:
        s.add(
            CommonMessage(
                common_message_id=message_id,
                channel="lark",
                common_conversation_id=_CHAT,
                common_user_id=_BOT_USER,
                common_root_message_id=root_id,
                sender_display_name=None,
                role="assistant",
                content=[{"kind": "text", "text": text_}],
                content_text=text_,
                scope=scope,
                message_type="post",
                bot_name=bot_name,
                response_id=None,
                event_time=event_time,
            )
        )


@pytest.mark.integration
async def test_by_root_attributes_proactive_persona_via_bot_name(chat_db):
    """root 链历史里的 proactive 出站行经 bot_config(bot_name→persona) 认出是赤尾。

    构造一条回复链：真人发起（root）→ 赤尾 proactive 出站（同 root，response_id=NULL、
    无 agent_response）→ 真人回复。by_root 拉出整链，proactive 那条必须带出 persona=akao，
    否则下游会把她自己说的判成 USER role。
    """
    await _seed_conversation()
    await _seed_bot_config("chiwei", "akao")
    await _seed_user_message(_ROOT, event_time=1000, text_="在吗", root_id=_ROOT)
    proactive_id = uuid.uuid5(uuid.NAMESPACE_OID, "proactive-reply-out")
    await _seed_proactive_bot_message(
        proactive_id, event_time=2000, text_="我刚在想你", root_id=_ROOT,
        bot_name="chiwei",
    )
    trigger = uuid.uuid5(uuid.NAMESPACE_OID, "proactive-reply-trigger")
    await _seed_user_message(trigger, event_time=3000, text_="哈哈", root_id=_ROOT)

    rows = await find_messages_with_user_chat_persona_by_root(
        root_message_id=str(_ROOT), until_create_time=3000,
    )

    by_text = _persona_by_text(rows)
    assert by_text["在吗"] is None, "真人那条无 persona"
    assert by_text["我刚在想你"] == "akao", (
        "proactive 出站行（response_id=NULL、无 agent_response）必须经 "
        f"bot_config(bot_name→persona) 认出是赤尾自己说的，实得 {by_text['我刚在想你']!r}"
    )
    assert by_text["哈哈"] is None, "真人那条无 persona"


@pytest.mark.integration
async def test_in_chat_attributes_proactive_persona_via_bot_name(chat_db):
    """补历史路径（in_chat）里的 proactive 出站行同样要带出 persona。

    in_chat 是 quick_search root 链不满 limit 时的补历史路径，也走相同的 persona join，
    必须有同样的 bot_config 兜底。
    """
    await _seed_conversation()
    await _seed_bot_config("chiwei", "akao")
    other_root = uuid.uuid5(uuid.NAMESPACE_OID, "proactive-reply-other-root")
    proactive_id = uuid.uuid5(uuid.NAMESPACE_OID, "proactive-reply-out2")
    await _seed_proactive_bot_message(
        proactive_id, event_time=2000, text_="我刚在想你", root_id=other_root,
        bot_name="chiwei",
    )

    rows = await find_messages_with_user_chat_persona_in_chat(
        chat_id=str(_CHAT),
        exclude_root_message_id=str(_ROOT),
        after_create_time=0,
        before_create_time=5000,
        exclude_user_id="",
        limit=15,
    )

    by_text = _persona_by_text(rows)
    assert by_text["我刚在想你"] == "akao", (
        "in_chat 补历史路径的 proactive 出站行也必须经 bot_config 认出 persona，"
        f"实得 {by_text['我刚在想你']!r}"
    )


@pytest.mark.integration
async def test_by_root_user_row_with_bot_name_is_not_attributed_persona(chat_db):
    """承重红线（codex 必改 1）：真人 user 行也带 bot_name（channel-server
    storeLarkInboundMessage 给 user 行写 bot_name=botName / claim 时再写），它指向一个
    active 的 bot_config(bot_name→persona)。helper 若不限 role='assistant'，会把这条
    真人话错归成某 persona —— 查询合同被串脏（睡前回顾会把真人话当成她自己说的）。

    构造：真人 root 行带 bot_name=chiwei、bot_config(chiwei→akao) active。该真人行的
    发言 persona 必须是 None（它不是 assistant 行），不能被兜底成 akao。
    """
    await _seed_conversation()
    await _seed_bot_config("chiwei", "akao")
    await _seed_user_message(
        _ROOT, event_time=1000, text_="在吗", root_id=_ROOT, bot_name="chiwei"
    )

    rows = await find_messages_with_user_chat_persona_by_root(
        root_message_id=str(_ROOT), until_create_time=3000,
    )

    by_text = _persona_by_text(rows)
    assert by_text["在吗"] is None, (
        "真人 user 行即便带 bot_name，也不能经 bot_config 兜底成 persona —— helper "
        f"必须只对 role='assistant' 行兜底，实得 {by_text['在吗']!r}"
    )


@pytest.mark.integration
async def test_in_chat_user_row_with_bot_name_is_not_attributed_persona(chat_db):
    """补历史路径（in_chat）同样：带 bot_name 的真人 user 行不被兜底成 persona。"""
    await _seed_conversation()
    await _seed_bot_config("chiwei", "akao")
    other_root = uuid.uuid5(uuid.NAMESPACE_OID, "proactive-reply-user-other-root")
    user_id = uuid.uuid5(uuid.NAMESPACE_OID, "proactive-reply-user-in-chat")
    await _seed_user_message(
        user_id, event_time=2000, text_="在吗", root_id=other_root, bot_name="chiwei"
    )

    rows = await find_messages_with_user_chat_persona_in_chat(
        chat_id=str(_CHAT),
        exclude_root_message_id=str(_ROOT),
        after_create_time=0,
        before_create_time=5000,
        exclude_user_id="",
        limit=15,
    )

    by_text = _persona_by_text(rows)
    assert by_text["在吗"] is None, (
        "in_chat 路径的真人 user 行带 bot_name 也不能被兜底成 persona，"
        f"实得 {by_text['在吗']!r}"
    )


def _persona_by_text(rows):
    """按消息纯文本索引发言 persona，方便逐条断言。"""
    out: dict[str, str | None] = {}
    for record, _username, _chat_name, persona in rows:
        out[json.loads(record.content)["text"]] = persona
    return out
