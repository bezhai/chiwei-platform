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
    default_reply_style = "few-shot 口吻样例"
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
    # per-persona 说话风格 default_reply_style 进 ChatTurnContext.reply_style
    assert turn.reply_style == "few-shot 口吻样例"
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


# ---------------------------------------------------------------------------
# proactive 增量水位（``since``）：把「上次 life 处理水位」透传给
# find_recent_chat_messages，只取水位之后真人新发的（治她对着旧话反复主动开口）。
# since=None（默认）行为不变。水位之后无消息 → history 为空 → messages 只剩意图框架
# 消息（她纯凭意图 + life 状态主动发，不揪旧对话）。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_since_forwarded_to_find_recent(stub_proactive):
    """since 透传给 find_recent_chat_messages（增量水位下传）。"""
    from app.chat.proactive_context import build_proactive_chat_context

    stub_proactive["history"] = []

    await build_proactive_chat_context(
        intent="想接着上次的话说",
        persona_id="akao",
        chat_id="cc-direct-1",
        user_id="u1",
        since="2026-04-21T18:30:00+08:00",
    )

    assert stub_proactive["recent_calls"][0]["since"] == "2026-04-21T18:30:00+08:00", (
        "水位 since 要透传给 find_recent_chat_messages"
    )


@pytest.mark.asyncio
async def test_since_default_none_forwarded(stub_proactive):
    """不传 since（默认 None）→ 透传 None（退回全量、向后兼容）。"""
    from app.chat.proactive_context import build_proactive_chat_context

    stub_proactive["history"] = []

    await build_proactive_chat_context(
        intent="想聊聊",
        persona_id="akao",
        chat_id="cc-direct-1",
        user_id="u1",
    )

    assert stub_proactive["recent_calls"][0]["since"] is None, (
        "默认不带水位 → since=None（退回全量最近 limit）"
    )


@pytest.mark.asyncio
async def test_no_new_messages_after_watermark_only_intent_message(stub_proactive):
    """水位之后没有新消息（history 空）→ messages 只剩意图框架消息（不揪旧对话）。

    这是本修复的命门：增量水位下，水位后真人没新发的话 → history 为空 → 她这次主动
    发纯凭意图 + life 状态，绝不把早就说过的旧话拉进来反复对着它开口。
    """
    from app.chat.proactive_context import build_proactive_chat_context

    # find_recent_chat_messages 在 since 之后无消息时返回空 list。
    stub_proactive["history"] = []

    turn = await build_proactive_chat_context(
        intent="自己想主动开口说点什么",
        persona_id="akao",
        chat_id="cc-direct-1",
        user_id="u1",
        since="2026-04-21T18:30:00+08:00",
    )

    assert len(turn.messages) == 1, "水位后无新消息 → 只有意图框架消息"
    assert turn.messages[0].role == Role.USER, "唯一一条是意图框架消息（user 角色）"


# ---------------------------------------------------------------------------
# Task 2：proactive 上下文支持群场景。
#
# build_proactive_chat_context 解除 chat_type="p2p" 硬编：接 chat_scope（DB 原值
# direct/group）+ chat_name，内部映射成 build_inner_context 要的 chat_type
# （direct→p2p、group→group），群场景把群名传下去。
# ---------------------------------------------------------------------------


@pytest.fixture
def capture_inner(stub_proactive, monkeypatch):
    """把 build_inner_context 换成记录入参的 fake（断言传下去的 chat_type / chat_name）。"""
    from app.chat import proactive_context as pc

    calls: list[dict] = []

    async def fake_inner(**kwargs):
        calls.append(kwargs)
        return "INNER"

    monkeypatch.setattr(pc, "build_inner_context", fake_inner)
    return calls


@pytest.mark.asyncio
async def test_default_scope_maps_to_p2p(capture_inner):
    """不传 chat_scope（默认 direct）→ build_inner_context 收 chat_type='p2p'（p2p 行为不变）。"""
    from app.chat.proactive_context import build_proactive_chat_context

    await build_proactive_chat_context(
        intent="想聊聊",
        persona_id="akao",
        chat_id="cc-direct-1",
        user_id="u1",
    )

    assert len(capture_inner) == 1
    assert capture_inner[0]["chat_type"] == "p2p", "默认 direct → p2p（私聊行为不变）"


@pytest.mark.asyncio
async def test_direct_scope_maps_to_p2p(capture_inner):
    """显式 chat_scope='direct' → chat_type='p2p'。"""
    from app.chat.proactive_context import build_proactive_chat_context

    await build_proactive_chat_context(
        intent="想聊聊",
        persona_id="akao",
        chat_id="cc-direct-1",
        user_id="u1",
        chat_scope="direct",
    )

    assert capture_inner[0]["chat_type"] == "p2p"


@pytest.mark.asyncio
async def test_group_scope_maps_to_group_with_name(capture_inner):
    """chat_scope='group' + chat_name → build_inner_context 收 chat_type='group' + 群名。

    群主动发时她的渲染上下文是「在群聊『X』里说话」（_scene_section 群分支），不是 p2p。
    """
    from app.chat.proactive_context import build_proactive_chat_context

    await build_proactive_chat_context(
        intent="接着群里的话题说",
        persona_id="akao",
        chat_id="cc-group-1",
        user_id=None,
        chat_scope="group",
        chat_name="🐢🐢群（飞书版）",
    )

    assert len(capture_inner) == 1
    call = capture_inner[0]
    assert call["chat_type"] == "group", "group scope → chat_type=group（群场景，不是 p2p）"
    assert call["chat_name"] == "🐢🐢群（飞书版）", "群名传给 inner_context（_scene_section 群分支用）"
    assert call["chat_id"] == "cc-group-1"
