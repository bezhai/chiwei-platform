"""quick_search 不得把历史 proactive_trigger 伪消息混进可见聊天上下文。

旧 proactive 外部判断器旁路（已删）曾以 ``message_type="proactive_trigger"``、
``common_user_id=None``、``role="user"`` 的形状写入伪触发消息。该旁路删除后不再
有新写入，但 prod 历史里仍可能残留这类行。``build_chat_context`` 此前靠 context
层 ``user_id == "__proactive__"`` 过滤排除它们——但现行 common 读模型给这类行的
``user_id`` 是 ``None``（不是 ``"__proactive__"``），那条过滤对真实数据从不命中。
真正可靠的排除点在 DB 查询层（与 ``find_user_messages_after`` /
``find_persona_spoken_chats_in_window`` 里既有的 ``proactive_trigger`` 防线一致）。

集成测试（真 Postgres）：正确性全在 SQL 过滤语义。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

import app.data.session as session_mod
from app.agent.neutral import Role
from app.chat._context_messages import build_p2p_messages
from app.chat.quick_search import quick_search
from app.data.models import (
    Base,
    CommonAgentResponse,
    CommonConversation,
    CommonMessage,
)

_CHAT = uuid.uuid5(uuid.NAMESPACE_OID, "qs-chat")
_USER = uuid.uuid5(uuid.NAMESPACE_OID, "qs-user")
_ROOT = uuid.uuid5(uuid.NAMESPACE_OID, "qs-root")

# bot_config 由 channel-server 管理、不在 agent-service 的 SQLAlchemy 模型里。quick_search
# 走的 by_root / in_chat 查询用相关标量子查询读它做 proactive persona 兜底，所以集成测试
# 要手动建这张表（生产里它一定存在；不建会让子查询炸 "relation bot_config does not exist"）。
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
        CommonConversation.__table__,
        CommonAgentResponse.__table__,
    ]
    async with test_db.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables)
        )
        await conn.execute(text(_BOT_CONFIG_DDL))
    yield test_db


async def _seed_conversation():
    async with session_mod.get_session() as s:
        s.add(
            CommonConversation(
                common_conversation_id=_CHAT,
                channel="lark",
                scope="group",
                display_name="测试群",
            )
        )


async def _seed_message(
    message_id, *, event_time, text, root_id, message_type=None,
    user_id=_USER, username="贝壳", bot_name=None, scope="group",
):
    async with session_mod.get_session() as s:
        s.add(
            CommonMessage(
                common_message_id=message_id,
                channel="lark",
                common_conversation_id=_CHAT,
                common_user_id=user_id,
                common_root_message_id=root_id,
                sender_display_name=username,
                role="user",
                content=[{"kind": "text", "text": text}],
                content_text=text,
                scope=scope,
                message_type=message_type,
                bot_name=bot_name,
                event_time=event_time,
            )
        )


async def _seed_bot_config(bot_name, persona_id, *, is_active=True):
    async with session_mod.get_session() as s:
        await s.execute(
            text(
                "INSERT INTO bot_config (bot_name, persona_id, is_active) "
                "VALUES (:bn, :pid, :active)"
            ),
            {"bn": bot_name, "pid": persona_id, "active": is_active},
        )


@pytest.mark.integration
async def test_quick_search_excludes_historical_proactive_trigger_in_root_chain(
    chat_db,
):
    """root 链里残留的 proactive_trigger 伪消息不进 quick_search 结果。"""
    await _seed_conversation()
    await _seed_message(
        _ROOT, event_time=1000, text="真实消息一", root_id=_ROOT
    )
    await _seed_message(
        uuid.uuid5(uuid.NAMESPACE_OID, "qs-pro"),
        event_time=2000,
        text="proactive 伪消息",
        root_id=_ROOT,
        message_type="proactive_trigger",
        user_id=None,
        username=None,
    )
    trigger = uuid.uuid5(uuid.NAMESPACE_OID, "qs-trigger")
    await _seed_message(
        trigger, event_time=3000, text="真实消息二", root_id=_ROOT
    )

    results = await quick_search(str(trigger), limit=15)
    texts = [r.content for r in results]
    assert not any("伪消息" in t for t in texts), (
        f"proactive_trigger 伪消息泄漏进可见上下文: {texts}"
    )
    assert any("真实消息一" in t for t in texts)
    assert any("真实消息二" in t for t in texts)


@pytest.mark.integration
async def test_quick_search_excludes_historical_proactive_trigger_in_chat_window(
    chat_db,
):
    """补历史那条路径（in_chat / additional_messages）也滤掉 proactive_trigger 伪消息。

    quick_search 两条路径：root 链（by_root）查满 limit 不够时，再走 in_chat 在同 chat
    时间窗内补历史消息。两条路径都加了 ``message_type != 'proactive_trigger'``（NULL-safe）
    过滤，但原测试只覆盖 root 链那条。这里专门把伪消息放在**另一个 root** 上、落在补历史
    的时间窗内，逼 quick_search 走 in_chat 路径，验证它在那条路径上也被滤掉（codex 建议 4）。

    构造：触发消息单独成一个 root（root 链只它一条 < limit → 触发补历史）；同 chat 里
    另一个 root 下有一条真实历史 + 一条 proactive_trigger 伪消息，都落在 30 分钟窗内。
    """
    await _seed_conversation()

    other_root = uuid.uuid5(uuid.NAMESPACE_OID, "qs-other-root")
    # 触发消息自成一个 root（root 链只有它一条，< limit → quick_search 去补历史）。
    trigger = uuid.uuid5(uuid.NAMESPACE_OID, "qs-window-trigger")
    await _seed_message(
        trigger, event_time=5000, text="触发消息", root_id=trigger
    )
    # 同 chat、另一个 root 下、窗口内的一条真实历史（应被 in_chat 补进来）。
    await _seed_message(
        uuid.uuid5(uuid.NAMESPACE_OID, "qs-window-real"),
        event_time=4000,
        text="窗口内真实历史",
        root_id=other_root,
    )
    # 同 chat、另一个 root 下、窗口内的 proactive_trigger 伪消息（必须被 in_chat 滤掉）。
    await _seed_message(
        uuid.uuid5(uuid.NAMESPACE_OID, "qs-window-pro"),
        event_time=4500,
        text="窗口内 proactive 伪消息",
        root_id=other_root,
        message_type="proactive_trigger",
        user_id=None,
        username=None,
    )

    results = await quick_search(str(trigger), limit=15)
    texts = [r.content for r in results]
    assert not any("伪消息" in t for t in texts), (
        f"in_chat 补历史路径泄漏了 proactive_trigger 伪消息: {texts}"
    )
    # 证明 in_chat 路径真被触发了（补历史那条真实消息进来了），否则上面的「无伪消息」
    # 可能是因为根本没走这条路径，断言就成了空头支票。
    assert any("窗口内真实历史" in t for t in texts), (
        f"in_chat 补历史路径没把窗口内真实历史补进来（路径没被触发）: {texts}"
    )


@pytest.mark.integration
async def test_human_chat_user_words_with_bot_name_stay_user_role_e2e(chat_db):
    """端到端（承重红线 codex 必改 1）：真人 user 行带 bot_name（channel-server 给
    inbound user 行写 bot_name=botName），bot_config(bot_name→persona) active。一路走
    quick_search → build_p2p_messages，真人的话**必须**仍是 ``Role.USER``——绝不串成
    赤尾自己说的（ASSISTANT）。

    人链路上有两道防线，本测试整条锁住：
      1. 查询层 helper 的 role 限定（codex 必改 1）：``_bot_config_persona`` 只对
         ``role='assistant'`` 行兜底，真人 user 行 join 出 persona=None；
      2. quick_search 第二道防线（:mod:`app.chat.quick_search` 第 109 行）：对 user 行
         强制 ``persona_id=None``。
    单测 helper 防线的「精确捕捉」在
    ``tests/data/test_proactive_persona_in_reply_chain.py`` /
    ``test_recent_chat_messages.py`` /
    ``test_persona_spoken_chats.py`` 的查询层用例里（去掉 role 限定就转红）；本测试是
    整条人链路的 smoke：真人话稳稳出成用户输入。
    """
    await _seed_conversation()
    await _seed_bot_config("chiwei", "akao")
    # 真人发起：root 行，带 bot_name=chiwei（channel-server inbound 落库口径）。
    await _seed_message(
        _ROOT, event_time=1000, text="赤尾在吗", root_id=_ROOT,
        bot_name="chiwei", scope="direct",
    )

    results = await quick_search(str(_ROOT), limit=15)
    by_text = {r.content: r for r in results}
    real = next(r for t, r in by_text.items() if "赤尾在吗" in t)
    assert real.persona_id is None, (
        f"真人 user 行不能带出 persona（两道防线之一漏了），实得 {real.persona_id!r}"
    )

    # 渲染层：build_p2p_messages 据 persona 判 is_self，真人话必须是 USER role。
    # Task 3 后 build_p2p_messages 是 async（按 common_user_id 查可信身份盖 rel）。
    msgs = await build_p2p_messages(
        results, image_key_to_url={}, image_key_to_filename={},
        current_persona_id="akao",
    )
    assert len(msgs) == 1
    assert msgs[0].role == Role.USER, (
        "真人带 bot_name 的话一路到渲染层仍必须是 Role.USER（不能串成她自己说的）"
    )
    assert "赤尾在吗" in msgs[0].text()
