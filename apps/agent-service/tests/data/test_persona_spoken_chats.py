"""睡前回顾的聊天证据查询 — 她在窗口内**发过言**的 chat 的窗口内消息.

参与边界合同（spec 决策 2b：common_message ≠ 她看见了）：**她在窗口内发过言的
chat 才算她的经历**——取这些 chat 在窗口内的消息（每 chat 条目上限、数量控制不
截断），被动在场没吭声的群不算（chat 是被动唤起模型，她没被唤起就没看见）。

「她发过言」的判定：assistant 消息经 ``response_id == common_agent_response.
session_id`` join 出 ``persona_id``——这是 common 口径下消息归属 persona 的
唯一来源（同 ``find_messages_with_user_chat_persona_*`` 的 join）。

返回带 user_id / username / chat_type（CommonMessageRecord 自带）+ 每条消息的
发言 persona（区分"她说的"和"别的 bot 说的"）+ chat 显示名（证据可读性）。

集成测试（真 Postgres）：正确性全在 join / 窗口 / 分组 / 上限的 SQL 语义。
"""

from __future__ import annotations

import uuid

import pytest

import app.data.session as session_mod
from app.data.models import (
    Base,
    CommonAgentResponse,
    CommonConversation,
    CommonMessage,
)
from app.data.queries.messages import find_persona_spoken_chats_in_window

_CHAT_A = uuid.uuid5(uuid.NAMESPACE_OID, "chat-a")
_CHAT_B = uuid.uuid5(uuid.NAMESPACE_OID, "chat-b")
_USER_1 = uuid.uuid5(uuid.NAMESPACE_OID, "user-1")
_USER_2 = uuid.uuid5(uuid.NAMESPACE_OID, "user-2")


@pytest.fixture
async def chat_db(test_db):
    """Build the common_* tables the query joins on."""
    tables = [
        CommonMessage.__table__,
        CommonAgentResponse.__table__,
        CommonConversation.__table__,
    ]
    async with test_db.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables)
        )
    yield test_db


async def _seed_conversation(chat_id, *, scope="group", name="测试群"):
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
    chat_id, *, event_time, text, user_id=_USER_1, username="贝壳",
    scope="group", message_type=None,
):
    async with session_mod.get_session() as s:
        s.add(
            CommonMessage(
                common_message_id=uuid.uuid4(),
                channel="lark",
                common_conversation_id=chat_id,
                common_user_id=user_id,
                sender_display_name=username,
                role="user",
                content=[{"kind": "text", "text": text}],
                content_text=text,
                scope=scope,
                message_type=message_type,
                event_time=event_time,
            )
        )


async def _seed_bot_message(
    chat_id, *, event_time, text, persona_id, scope="group"
):
    """assistant 消息 + 配套 agent_response（persona 归属经 response join 出来）。"""
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
async def test_spoken_chat_returns_window_messages_with_identity(chat_db):
    """她发过言的 chat：返回窗口内消息，带 user_id / username / chat_type / 发言 persona。"""
    await _seed_conversation(_CHAT_A, name="一家人")
    await _seed_user_message(_CHAT_A, event_time=1000, text="赤尾在吗")
    await _seed_bot_message(_CHAT_A, event_time=2000, text="在的在的", persona_id="akao")
    await _seed_user_message(_CHAT_A, event_time=3000, text="晚上吃什么", user_id=_USER_2, username="路人")

    got = await find_persona_spoken_chats_in_window(
        persona_id="akao", since_ms=0, until_ms=10_000, per_chat_limit=50
    )

    assert len(got) == 1
    chat_id, chat_name, entries = got[0]
    assert chat_id == str(_CHAT_A)
    assert chat_name == "一家人"
    texts = [r.content for r, _p in entries]
    assert len(entries) == 3
    # 按发生先后升序
    assert "赤尾在吗" in texts[0]
    assert "在的在的" in texts[1]
    assert "晚上吃什么" in texts[2]
    # 身份字段在
    first, first_persona = entries[0]
    assert first.user_id == str(_USER_1)
    assert first.username == "贝壳"
    assert first.chat_type == "group"
    assert first_persona is None, "用户消息没有 persona 归属"
    _bot, bot_persona = entries[1]
    assert bot_persona == "akao", "她说的那条要能认出是她"


@pytest.mark.integration
async def test_passive_presence_chat_excluded(chat_db):
    """被动在场没吭声的群不算她的经历（决策 2b 参与边界）。"""
    await _seed_conversation(_CHAT_A)
    await _seed_user_message(_CHAT_A, event_time=1000, text="群里热闹但她没说话")

    got = await find_persona_spoken_chats_in_window(
        persona_id="akao", since_ms=0, until_ms=10_000, per_chat_limit=50
    )

    assert got == []


@pytest.mark.integration
async def test_other_persona_speech_does_not_count_as_hers(chat_db):
    """同群里别的 persona 发过言 ≠ 她发过言（persona 归属按 response join 判）。"""
    await _seed_conversation(_CHAT_A)
    await _seed_user_message(_CHAT_A, event_time=1000, text="千凪在吗")
    await _seed_bot_message(_CHAT_A, event_time=2000, text="我在", persona_id="chinagi")

    got = await find_persona_spoken_chats_in_window(
        persona_id="akao", since_ms=0, until_ms=10_000, per_chat_limit=50
    )

    assert got == []


@pytest.mark.integration
async def test_speech_outside_window_does_not_qualify_chat(chat_db):
    """她只在窗口外发过言 → 这个 chat 不算这个生活日的经历。"""
    await _seed_conversation(_CHAT_A)
    await _seed_bot_message(_CHAT_A, event_time=100, text="昨天说的话", persona_id="akao")
    await _seed_user_message(_CHAT_A, event_time=5000, text="今天的新消息她没接")

    got = await find_persona_spoken_chats_in_window(
        persona_id="akao", since_ms=1000, until_ms=10_000, per_chat_limit=50
    )

    assert got == []


@pytest.mark.integration
async def test_window_filters_messages_within_qualified_chat(chat_db):
    """够格的 chat 里也只取窗口内消息（窗口外的不混进证据）。"""
    await _seed_conversation(_CHAT_A)
    await _seed_user_message(_CHAT_A, event_time=500, text="窗口前的旧消息")
    await _seed_bot_message(_CHAT_A, event_time=2000, text="她在窗口内说的", persona_id="akao")
    await _seed_user_message(_CHAT_A, event_time=20_000, text="窗口后的消息")

    got = await find_persona_spoken_chats_in_window(
        persona_id="akao", since_ms=1000, until_ms=10_000, per_chat_limit=50
    )

    assert len(got) == 1
    _cid, _name, entries = got[0]
    texts = [r.content for r, _p in entries]
    assert len(entries) == 1
    assert "她在窗口内说的" in texts[0]


@pytest.mark.integration
async def test_per_chat_limit_keeps_most_recent_ascending(chat_db):
    """每 chat 条目上限：超限只保最近 N 条（条目数量控制、不字符截断），仍升序。"""
    await _seed_conversation(_CHAT_A)
    await _seed_bot_message(_CHAT_A, event_time=1000, text="她说了话", persona_id="akao")
    for i in range(5):
        await _seed_user_message(
            _CHAT_A, event_time=2000 + i, text=f"第{i}条"
        )

    got = await find_persona_spoken_chats_in_window(
        persona_id="akao", since_ms=0, until_ms=10_000, per_chat_limit=3
    )

    _cid, _name, entries = got[0]
    texts = [r.content for r, _p in entries]
    assert len(entries) == 3, "超限只保最近 3 条"
    assert "第2" in texts[0] and "第3" in texts[1] and "第4" in texts[2], (
        f"保最近的、按先后升序，实际 {texts}"
    )


@pytest.mark.integration
async def test_multiple_chats_grouped_separately(chat_db):
    """多个发过言的 chat 各自一组（p2p 与群聊都算，chat_type 区分）。"""
    await _seed_conversation(_CHAT_A, scope="group", name="一家人")
    await _seed_conversation(_CHAT_B, scope="direct", name=None)
    await _seed_bot_message(_CHAT_A, event_time=1000, text="群里说的", persona_id="akao")
    await _seed_bot_message(
        _CHAT_B, event_time=2000, text="私聊说的", persona_id="akao", scope="direct"
    )
    await _seed_user_message(
        _CHAT_B, event_time=2500, text="私聊里对方的话", scope="direct"
    )

    got = await find_persona_spoken_chats_in_window(
        persona_id="akao", since_ms=0, until_ms=10_000, per_chat_limit=50
    )

    assert len(got) == 2
    by_id = {cid: (name, entries) for cid, name, entries in got}
    assert str(_CHAT_A) in by_id and str(_CHAT_B) in by_id
    _name_b, entries_b = by_id[str(_CHAT_B)]
    rec, _p = entries_b[-1]
    assert rec.chat_type == "p2p", "direct scope 归一成 p2p（同 _record 口径）"


@pytest.mark.integration
async def test_proactive_trigger_pseudo_messages_excluded(chat_db):
    """proactive_trigger 伪消息不进证据（它是触发器记录、不是真实对话）。"""
    await _seed_conversation(_CHAT_A)
    await _seed_bot_message(_CHAT_A, event_time=1000, text="她说了话", persona_id="akao")
    await _seed_user_message(
        _CHAT_A, event_time=2000, text="proactive 伪消息",
        message_type="proactive_trigger",
    )

    got = await find_persona_spoken_chats_in_window(
        persona_id="akao", since_ms=0, until_ms=10_000, per_chat_limit=50
    )

    _cid, _name, entries = got[0]
    texts = [r.content for r, _p in entries]
    assert not any("伪消息" in t for t in texts)
