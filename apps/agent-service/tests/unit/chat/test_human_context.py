"""真人聊天 context 构建独立一套 ``build_human_chat_context`` 的契约。

剥离后这一套**独占**所有源消息相关的活:用 message_id 反查历史(quick_search)、
收图、取触发信息、解析人设、拼 inner_context,产出一份 ChatTurnContext 交给
共享渲染层。它是真人聊天专属的 context 构建——proactive(task 2)不复用它,而是
单独做一套 life context 构建,同样产出 ChatTurnContext。

锁死:
  1. 返回的 ChatTurnContext 携带渲染层需要的全部:messages / image_registry /
     chat_id / persona bundle(persona_id/identity/appearance/persona) / inner_context。
  2. message_id 反查为空时返回 None(无 context,调用方据此走"未找到")。
  3. persona 解析 + inner_context 拼装在这一层完成(渲染层不再碰 persona / 生活状态)。
"""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.chat.quick_search import QuickSearchResult


def _v2_text(text: str) -> str:
    return json.dumps(
        {"v": 2, "text": text, "items": [{"type": "text", "value": text}]},
        ensure_ascii=False,
    )


def _msg(
    mid,
    *,
    text,
    user_id,
    username,
    minute,
    chat_type="p2p",
    role="user",
    persona_id=None,
    reply_message_id=None,
):
    return QuickSearchResult(
        message_id=mid,
        content=_v2_text(text),
        user_id=user_id,
        create_time=datetime(2026, 4, 21, 18, minute, 0),
        role=role,
        username=username,
        chat_type=chat_type,
        chat_name="测试群",
        reply_message_id=reply_message_id,
        chat_id="oc_test",
        persona_id=persona_id,
    )


class _FakePersona:
    display_name = "赤尾"
    persona_lite = "lite-body"
    appearance_detail = "looks"
    default_reply_style = "few-shot 口吻样例"
    error_messages = {"error": "ERR"}


@pytest.mark.asyncio
async def test_build_human_chat_context_packs_render_ready_context(monkeypatch):
    """真人 p2p:反查历史 -> 收图 -> 解析人设 -> 拼 inner_context,
    打包成 ChatTurnContext 给渲染层。"""
    from app.chat import context as ctx_mod

    history = [
        _msg("m1", text="在吗", user_id="u1", username="原智鸿", minute=0),
    ]

    async def fake_load_persona(pid):
        return _FakePersona()

    async def fake_inner(**kwargs):
        # inner_context 在这一层拼好,渲染层只消费
        return f"INNER[{kwargs['chat_id']}/{kwargs['persona_id']}]"

    with (
        patch("app.chat.context.quick_search", new=AsyncMock(return_value=history)),
        patch("app.chat.context.collect_images", new=AsyncMock(return_value=({}, {}))),
        patch("app.chat.context.build_p2p_messages",
              new=AsyncMock(return_value=["built-p2p"])),
        patch("app.chat.context.load_persona", new=fake_load_persona),
        patch("app.chat.context.build_inner_context", new=fake_inner),
    ):
        turn = await ctx_mod.build_human_chat_context("m1", persona_id="akao")

    assert turn is not None
    assert turn.messages == ["built-p2p"]
    assert turn.chat_id == "oc_test"
    assert turn.persona_id == "akao"
    assert turn.identity == "lite-body"
    assert turn.appearance == "looks"
    assert turn.inner_context == "INNER[oc_test/akao]"
    # per-persona 说话风格 default_reply_style 进 ChatTurnContext.reply_style
    assert turn.reply_style == "few-shot 口吻样例"
    # persona 对象随 context 走,渲染层据它出 persona error 文案
    assert turn.error_message("error") == "ERR"


@pytest.mark.asyncio
async def test_build_human_chat_context_forwards_bot_name_to_collect_images(monkeypatch):
    """bot_name 透传：build_human_chat_context 把接收消息的 bot 传给 collect_images，
    后者据此带 X-App-Name 下载入站图。修复前签名无 bot_name → 链路断、图片 422、
    赤尾否认用户发图（trace dbde982e146840cc00610c393fc5820e）。"""
    from app.chat import context as ctx_mod

    history = [_msg("m1", text="你看这个", user_id="u1", username="原智鸿", minute=0)]

    captured: dict[str, object] = {}

    async def fake_collect(results, chat_type, bot_name="", channel=None):
        captured["bot_name"] = bot_name
        return ({}, {})

    async def fake_load_persona(pid):
        return _FakePersona()

    async def fake_inner(**kwargs):
        return "INNER"

    with (
        patch("app.chat.context.quick_search", new=AsyncMock(return_value=history)),
        patch("app.chat.context.collect_images", new=fake_collect),
        patch("app.chat.context.build_p2p_messages",
              new=AsyncMock(return_value=["built"])),
        patch("app.chat.context.load_persona", new=fake_load_persona),
        patch("app.chat.context.build_inner_context", new=fake_inner),
    ):
        await ctx_mod.build_human_chat_context(
            "m1", persona_id="akao", bot_name="bot-x"
        )

    assert captured.get("bot_name") == "bot-x", (
        f"build_human_chat_context 必须把 bot_name 透传给 collect_images，"
        f"实得 {captured.get('bot_name')!r}"
    )


@pytest.mark.asyncio
async def test_build_human_chat_context_forwards_channel_to_collect_images(monkeypatch):
    """channel 透传：build_human_chat_context 把 channel 传给 collect_images，
    后者据此（结合 key 形态）区分 QQ 的公网 url 图与飞书的 file_key。"""
    from app.chat import context as ctx_mod

    history = [_msg("m1", text="你看这个", user_id="u1", username="原智鸿", minute=0)]

    captured: dict[str, object] = {}

    async def fake_collect(results, chat_type, bot_name="", channel=None):
        captured["channel"] = channel
        return ({}, {})

    async def fake_load_persona(pid):
        return _FakePersona()

    async def fake_inner(**kwargs):
        return "INNER"

    with (
        patch("app.chat.context.quick_search", new=AsyncMock(return_value=history)),
        patch("app.chat.context.collect_images", new=fake_collect),
        patch("app.chat.context.build_p2p_messages",
              new=AsyncMock(return_value=["built"])),
        patch("app.chat.context.load_persona", new=fake_load_persona),
        patch("app.chat.context.build_inner_context", new=fake_inner),
    ):
        await ctx_mod.build_human_chat_context(
            "m1", persona_id="akao", bot_name="bot-x", channel="qq"
        )

    assert captured.get("channel") == "qq", (
        f"build_human_chat_context 必须把 channel 透传给 collect_images，"
        f"实得 {captured.get('channel')!r}"
    )


@pytest.mark.asyncio
async def test_build_human_chat_context_returns_none_when_no_history(monkeypatch):
    """message_id 反查为空 -> 返回 None(调用方据此走"未找到")。"""
    from app.chat import context as ctx_mod

    with patch("app.chat.context.quick_search", new=AsyncMock(return_value=[])):
        turn = await ctx_mod.build_human_chat_context("missing", persona_id="akao")

    assert turn is None


@pytest.mark.asyncio
async def test_build_human_chat_context_inner_failure_does_not_crash(monkeypatch):
    """inner_context 拼装失败只 log,context 仍返回(inner_context 退回空串)。"""
    from app.chat import context as ctx_mod

    history = [_msg("m1", text="hi", user_id="u1", username="原智鸿", minute=0)]

    async def fake_load_persona(pid):
        return _FakePersona()

    async def boom(**kwargs):
        raise RuntimeError("inner down")

    with (
        patch("app.chat.context.quick_search", new=AsyncMock(return_value=history)),
        patch("app.chat.context.collect_images", new=AsyncMock(return_value=({}, {}))),
        patch("app.chat.context.build_p2p_messages",
              new=AsyncMock(return_value=["built"])),
        patch("app.chat.context.load_persona", new=fake_load_persona),
        patch("app.chat.context.build_inner_context", new=boom),
    ):
        turn = await ctx_mod.build_human_chat_context("m1", persona_id="akao")

    assert turn is not None
    assert turn.inner_context == ""
    assert turn.messages == ["built"]


# ---------------------------------------------------------------------------
# 真人 context 不打桩 build_p2p_messages / build_group_messages 的回归（建议③）。
# 上面那几个测试为隔离把消息构建 stub 掉了，漏掉了「真实消息构建」这条真路径——
# p2p 自己发言归属、群 reply chain、图片 registry 透传。这里让**真实**
# build_p2p_messages / build_group_messages 经 build_human_chat_context 跑，钉死这三件。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_human_p2p_attributes_self_messages_as_assistant(monkeypatch):
    """p2p 真实消息构建：当前 persona 发过的（assistant + persona_id 对得上）→ ASSISTANT，
    真人 → USER（不打桩 build_p2p_messages，走真实归属判定）。"""
    from app.agent.neutral import Role
    from app.chat import context as ctx_mod

    history = [
        _msg("m1", text="在吗", user_id="u1", username="原智鸿", minute=0),
        _msg(
            "m2", text="我刚在想你", user_id="", username=None, minute=1,
            role="assistant", persona_id="akao",
        ),
        _msg("m3", text="哈哈真的吗", user_id="u1", username="原智鸿", minute=2),
    ]

    async def fake_load_persona(pid):
        return _FakePersona()

    async def fake_inner(**kwargs):
        return "INNER"

    with (
        patch("app.chat.context.quick_search", new=AsyncMock(return_value=history)),
        patch("app.chat.context.collect_images", new=AsyncMock(return_value=({}, {}))),
        patch("app.chat.context.load_persona", new=fake_load_persona),
        patch("app.chat.context.build_inner_context", new=fake_inner),
    ):
        turn = await ctx_mod.build_human_chat_context("m3", persona_id="akao")

    roles = [m.role for m in turn.messages]
    assert roles == [Role.USER, Role.ASSISTANT, Role.USER], (
        f"真人 p2p：当前 persona 发言归 ASSISTANT、真人归 USER，实得 {roles}"
    )
    assert "我刚在想你" in turn.messages[1].text(), "她自己那条原话进 assistant 消息"


@pytest.mark.asyncio
async def test_human_group_builds_reply_chain_with_trigger_marker(monkeypatch):
    """群聊真实消息构建：reply chain 串起来、触发那条带 reply_target 标记（不打桩 build_group_messages）。"""
    from app.chat import context as ctx_mod

    # m_trigger reply 到 m_root；m_other 是同群另一条不在链上的消息。
    history = [
        _msg(
            "m_root", text="今天去哪玩", user_id="u1", username="阿强", minute=0,
            chat_type="group",
        ),
        _msg(
            "m_other", text="路过看看", user_id="u2", username="小美", minute=1,
            chat_type="group",
        ),
        _msg(
            "m_trigger", text="去爬山吧", user_id="u1", username="阿强", minute=2,
            chat_type="group", reply_message_id="m_root",
        ),
    ]

    async def fake_load_persona(pid):
        return _FakePersona()

    async def fake_inner(**kwargs):
        return "INNER"

    # 群聊正文模板走 langfuse get_prompt（测试无 langfuse），stub 成回显 reply_chain /
    # other_messages，让断言能看到真实构建出的链与标记。
    fake_prompt = MagicMock()
    fake_prompt.compile.side_effect = (
        lambda *, reply_chain, other_messages: (
            f"REPLY_CHAIN:\n{reply_chain}\nOTHER:\n{other_messages}"
        )
    )

    with (
        patch("app.chat.context.quick_search", new=AsyncMock(return_value=history)),
        patch("app.chat.context.collect_images", new=AsyncMock(return_value=({}, {}))),
        patch("app.chat.context.load_persona", new=fake_load_persona),
        patch("app.chat.context.build_inner_context", new=fake_inner),
        patch("app.chat._context_messages.get_prompt", return_value=fake_prompt),
    ):
        turn = await ctx_mod.build_human_chat_context("m_trigger", persona_id="akao")

    # 群聊真实构建出单条 USER 消息，正文含 reply chain + 触发标记 + 其他消息。
    assert len(turn.messages) == 1
    body = turn.messages[0].text()
    assert "今天去哪玩" in body and "去爬山吧" in body, "reply chain 串起触发与被回复"
    assert 'reply_target="true"' in body, "触发那条带 reply_target 标记"
    assert body.count('reply_target="true"') == 1, "只触发那条带 reply_target，其他消息不带"
    assert "路过看看" in body, "同群其他消息也进上下文"


@pytest.mark.asyncio
async def test_human_p2p_passes_image_registry_through(monkeypatch):
    """图片 registry 透传：收到的图经 register_batch 注册后，registry 随 context 走，
    p2p 正文把图引成 @N.png（不打桩 build_p2p_messages / 不打桩 registry）。"""
    from app.chat import context as ctx_mod

    # 一条带图的真人消息：content 引用 image_key，collect_images 返回 key→url。
    img_key = "img_key_1"
    history = [
        QuickSearchResult(
            message_id="m1",
            content=json.dumps(
                {
                    "v": 2,
                    "text": "你看这个",
                    "items": [
                        {"type": "text", "value": "你看这个"},
                        {"type": "image", "value": img_key},
                    ],
                },
                ensure_ascii=False,
            ),
            user_id="u1",
            create_time=datetime(2026, 4, 21, 18, 0, 0),
            role="user",
            username="原智鸿",
            chat_type="p2p",
            chat_name="测试群",
            reply_message_id=None,
            chat_id="oc_test",
        ),
    ]

    async def fake_load_persona(pid):
        return _FakePersona()

    async def fake_inner(**kwargs):
        return "INNER"

    async def fake_register_batch(self, urls):
        # 注册返回稳定文件名，让正文能引成 @N.png（实例方法 → 带 self）。
        return [f"{i}.png" for i in range(len(urls))]

    with (
        patch("app.chat.context.quick_search", new=AsyncMock(return_value=history)),
        patch(
            "app.chat.context.collect_images",
            new=AsyncMock(return_value=({img_key: "https://tos/x.png"}, {})),
        ),
        patch("app.chat.context.load_persona", new=fake_load_persona),
        patch("app.chat.context.build_inner_context", new=fake_inner),
        patch(
            "app.infra.image.ImageRegistry.register_batch",
            new=fake_register_batch,
        ),
    ):
        turn = await ctx_mod.build_human_chat_context("m1", persona_id="akao")

    # registry 随 context 走（渲染层据它解图）。
    assert turn.image_registry is not None
    # p2p 正文把图引成 @0.png（真实 build_p2p_messages 用 register 返回的文件名）。
    body = turn.messages[0].text()
    assert "@0.png" in body, f"图片应引成 @0.png，实得正文 {body!r}"
