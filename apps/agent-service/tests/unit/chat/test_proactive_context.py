"""proactive(赤尾主动给真人发消息) 的 life context 构建 ``build_proactive_chat_context``。

这是和真人聊天那套(``build_human_chat_context``)**并列的、单独一套** context 构建。
输入是 life 的意图/要点 + 按 chat_id 捞的历史对话(不碰源消息),产出同一个
``ChatTurnContext`` 形,交给共享渲染层。

锁死:
  1. 返回的 ChatTurnContext 携带渲染层需要的全部:messages / chat_id / persona
     bundle(persona_id/identity/appearance/persona) / inner_context;意图作为最后
     一条 user 框架消息进 messages(驱动渲染产出她的出站话)。
  2. **history 把赤尾自己发过的(含上一条 proactive)认作她自己说的(ASSISTANT 角色)、
     不当成真人输入(USER 角色)**——按 persona_id 区分。
  3. 不反查源消息:历史只靠 chat_id 取(``find_recent_chat_messages``)。
  4. inner_context 拼装失败只 log、context 仍返回(inner_context 退回空串)。
  5. 没有历史(第一次主动发)也能产出 context(只有意图框架消息),不返回 None。
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from app.agent.neutral import Role
from app.data.message_record import CommonMessageRecord


def _v2_text(text: str) -> str:
    return json.dumps(
        {"v": 2, "text": text, "items": [{"type": "text", "value": text}]},
        ensure_ascii=False,
    )


def _rec(mid, *, text, role, minute, user_id=None, username=None, persona_id=None):
    """造一条 (record, 发言 persona) —— find_recent_chat_messages 的返回元素形态。"""
    record = CommonMessageRecord(
        message_id=mid,
        user_id=user_id,
        username=username,
        content=_v2_text(text),
        role=role,
        root_message_id=mid,
        reply_message_id=None,
        chat_id="cc-direct-1",
        chat_type="p2p",
        create_time=int(datetime(2026, 4, 21, 18, minute, 0).timestamp() * 1000),
        message_type=None,
        bot_name="chiwei" if role == "assistant" else None,
        response_id="sess-x" if role == "assistant" else None,
    )
    return (record, persona_id)


class _FakePersona:
    persona_id = "akao"
    display_name = "赤尾"
    persona_lite = "lite-body"
    appearance_detail = "looks"
    error_messages = {"error": "ERR"}


@pytest.fixture
def stub_proactive(monkeypatch):
    """把 context 构建下游的取历史 / 取人设 / 取用户名 / 拼 inner_context 换成 fake。"""
    from app.chat import proactive_context as pc

    state: dict = {
        "history": [],
        "recent_calls": [],
        "username": "原智鸿",
    }

    async def fake_recent(**kwargs):
        state["recent_calls"].append(kwargs)
        return state["history"]

    async def fake_load_persona(pid):
        return _FakePersona()

    async def fake_username(uid):
        return state["username"]

    async def fake_inner(**kwargs):
        return f"INNER[{kwargs['chat_id']}/{kwargs['persona_id']}]"

    monkeypatch.setattr(pc, "find_recent_chat_messages", fake_recent)
    monkeypatch.setattr(pc, "load_persona", fake_load_persona)
    monkeypatch.setattr(pc, "find_username", fake_username)
    monkeypatch.setattr(pc, "build_inner_context", fake_inner)
    return state


@pytest.mark.asyncio
async def test_packs_render_ready_context_with_intent(stub_proactive):
    """意图 + chat_id 历史 → ChatTurnContext(persona bundle + inner_context + messages)。"""
    from app.chat.proactive_context import build_proactive_chat_context

    stub_proactive["history"] = [
        _rec("m1", text="在吗", role="user", minute=0, user_id="u1", username="原智鸿"),
    ]

    turn = await build_proactive_chat_context(
        intent="想问问他周末有没有空一起吃饭",
        persona_id="akao",
        chat_id="cc-direct-1",
        user_id="u1",
    )

    assert turn is not None
    assert turn.chat_id == "cc-direct-1"
    assert turn.persona_id == "akao"
    assert turn.identity == "lite-body"
    assert turn.appearance == "looks"
    assert turn.inner_context == "INNER[cc-direct-1/akao]"
    assert turn.error_message("error") == "ERR"
    # 历史只靠 chat_id 取，不反查源消息
    assert stub_proactive["recent_calls"][0]["chat_id"] == "cc-direct-1"


@pytest.mark.asyncio
async def test_intent_is_last_message_driving_render(stub_proactive):
    """意图作为最后一条 user 框架消息进 messages(驱动渲染产出她的出站话)。"""
    from app.chat.proactive_context import build_proactive_chat_context

    stub_proactive["history"] = []
    intent = "想跟他说我刚看完那部电影超好看"

    turn = await build_proactive_chat_context(
        intent=intent,
        persona_id="akao",
        chat_id="cc-direct-1",
        user_id="u1",
    )

    assert turn.messages, "至少要有意图框架消息驱动渲染"
    last = turn.messages[-1]
    assert last.role == Role.USER, "意图框架消息是 user 角色(让模型据它产出她的出站话)"
    assert intent in last.text(), "意图原文要进框架消息"


@pytest.mark.asyncio
async def test_history_attributes_her_own_messages_as_assistant(stub_proactive):
    """承重:历史里赤尾自己发过的(含上一条 proactive)是 ASSISTANT、真人是 USER。"""
    from app.chat.proactive_context import build_proactive_chat_context

    stub_proactive["history"] = [
        _rec("m1", text="在吗", role="user", minute=0, user_id="u1", username="原智鸿"),
        # 上一条 proactive:赤尾自己发的(persona_id=akao),必须认作她自己说的
        _rec("m2", text="我刚在想你", role="assistant", minute=1, persona_id="akao"),
        _rec("m3", text="哈哈真的吗", role="user", minute=2, user_id="u1", username="原智鸿"),
    ]

    turn = await build_proactive_chat_context(
        intent="接着上次的话题聊",
        persona_id="akao",
        chat_id="cc-direct-1",
        user_id="u1",
    )

    # 历史部分(意图框架消息之前)的角色映射
    history_msgs = turn.messages[:-1]
    roles = [m.role for m in history_msgs]
    assert roles == [Role.USER, Role.ASSISTANT, Role.USER], (
        f"赤尾自己的消息要认作 ASSISTANT、真人认作 USER，实得 {roles}"
    )
    # 她自己那条原话要在 assistant 消息里(不能被当成真人输入)
    assert "我刚在想你" in history_msgs[1].text()


@pytest.mark.asyncio
async def test_no_history_still_builds_context(stub_proactive):
    """第一次主动发(无历史)也产出 context(只有意图框架消息)、不返回 None。"""
    from app.chat.proactive_context import build_proactive_chat_context

    stub_proactive["history"] = []

    turn = await build_proactive_chat_context(
        intent="第一次主动找他说话",
        persona_id="akao",
        chat_id="cc-direct-1",
        user_id="u1",
    )

    assert turn is not None
    assert len(turn.messages) == 1, "无历史时只有意图框架消息"
    assert turn.messages[0].role == Role.USER


@pytest.mark.asyncio
async def test_inner_context_failure_does_not_crash(stub_proactive, monkeypatch):
    """inner_context 拼装失败只 log，context 仍返回(inner_context 退回空串)。"""
    from app.chat import proactive_context as pc
    from app.chat.proactive_context import build_proactive_chat_context

    async def boom(**kwargs):
        raise RuntimeError("inner down")

    monkeypatch.setattr(pc, "build_inner_context", boom)
    stub_proactive["history"] = []

    turn = await build_proactive_chat_context(
        intent="想聊聊",
        persona_id="akao",
        chat_id="cc-direct-1",
        user_id="u1",
    )

    assert turn is not None
    assert turn.inner_context == ""
