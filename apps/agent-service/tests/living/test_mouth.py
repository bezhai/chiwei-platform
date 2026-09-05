"""chat 是她的嘴 —— 只有出口，没有入口。

四条硬边界：

  * **出去的话是对话模型渲染的。** life 那个模型推理强但对话差；拿它写对外的话，
    出去的就是一段推理稿。所以嘴单独一套 prompt + ``main-chat-model``。
  * **出站契约照主动发那条走。** ``is_proactive=True``、``proactive:`` 前缀的本地
    派生 message_id、``root_id`` 留空 —— worker 靠这三样决定"别反查来源消息"。
  * **她说出去的话要落一条 Happening。** 不落的话，她下一缝不知道自己说过什么，
    于是对同一件事又说一遍（旧引擎实锤复现过，相隔三分钟前后矛盾）。
  * **渲染没出内容就不发。** 绝不回退发意图原文（那是 life 的内部措辞，不是人话），
    也绝不发空消息 —— 把"没发出去"喂回她自己处置。
"""
from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import text

from app.data import session as session_mod
from app.domain.chat_dataflow import PROACTIVE_MESSAGE_ID_PREFIX
from app.living.mouth import (
    _SEND_CHECK_TIMEOUT_S,
    LIVING_CHAT_VOICE_PROMPT_ID,
    send_message,
)
from app.living.records import MEDIUM_GROUP_CHAT, MEDIUM_PHONE
from app.living.snapshot import recent_own_happenings
from app.living.whereabouts import note_whereabouts

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))

_AKAO_BOT_UID = uuid.uuid5(uuid.NAMESPACE_OID, "bot-akao-common-user")
_BEZHAI = uuid.uuid5(uuid.NAMESPACE_OID, "human-bezhai")
_DM = uuid.uuid5(uuid.NAMESPACE_OID, "conv-dm-bezhai-akao")
_GROUP = uuid.uuid5(uuid.NAMESPACE_OID, "conv-group-lab")
_NOT_HERS = uuid.uuid5(uuid.NAMESPACE_OID, "conv-not-hers")

# ``in_a_moment`` 的默认缝。写出来是因为下面几条用例要在缝**外面**按 moment_id
# 把那条认领记录捞回来。
_MOMENT = "2026-07-25T21:30+08:00"


def _at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 7, 25, hour, minute, tzinfo=_CST)


async def _somebody_called_her(
    conversation: uuid.UUID, *, at: dt.datetime, names_her: bool = False
) -> None:
    """真人在这条会话里叫了她一声（私聊里任意一条就算，群里要点她的名）。

    判据跟 nudge 那条钟同一份，见 :mod:`app.living.whitelist`。
    """
    async with session_mod.get_session() as s:
        await s.execute(
            text(
                "INSERT INTO common_message (common_message_id, channel,"
                " common_conversation_id, common_user_id, sender_display_name,"
                " role, content, content_text, scope, event_time,"
                " mentioned_common_user_ids)"
                " VALUES (CAST(:m AS uuid), 'lark', CAST(:c AS uuid),"
                " CAST(:u AS uuid), 'bezhai', 'user', CAST(:body AS jsonb), :txt,"
                " :sc, :at, CAST(:named AS text[])::uuid[])"
            ),
            {
                "m": str(uuid.uuid4()),
                "c": str(conversation),
                "u": str(_BEZHAI),
                "body": '[{"kind": "text", "text": "在吗"}]',
                "txt": "在吗",
                "sc": "direct" if conversation == _DM else "group",
                "at": int(at.timestamp() * 1000),
                "named": [str(_AKAO_BOT_UID)] if names_her else [],
            },
        )


@pytest.fixture
async def mouth_db(living_db):
    """两个 bot 身份、一条私聊、一个群、一个不是她的群 —— 嘴要发的地址都在这儿。

    私聊和群里各有一条**有人在叫她**的消息：白名单收窄之后"bot 还在这个会话里"不再
    等于"她看得见它"（:mod:`app.living.whitelist`），一条动静都没有的会话她根本发不
    出去。这几条消息不改变下面任何一条用例要验的东西 —— 她没看过它们，所以既不进她
    开口前读的那段上下文，也不影响出站的任何一样。
    """
    async with session_mod.get_session() as s:
        await s.execute(
            text(
                "INSERT INTO common_user (common_user_id, channel, display_name) "
                "VALUES (CAST(:u AS uuid), 'lark', '赤尾'), "
                "       (CAST(:h AS uuid), 'lark', 'bezhai')"
            ),
            {"u": str(_AKAO_BOT_UID), "h": str(_BEZHAI)},
        )
        for conv, scope, title in (
            (_DM, "direct", "bezhai"),
            (_GROUP, "group", "宅居研究所"),
            (_NOT_HERS, "group", "别人的群"),
        ):
            await s.execute(
                text(
                    "INSERT INTO common_conversation "
                    "(common_conversation_id, channel, scope, display_name, is_active)"
                    " VALUES (CAST(:c AS uuid), 'lark', :s, :t, true)"
                ),
                {"c": str(conv), "s": scope, "t": title},
            )
        await s.execute(
            text(
                "INSERT INTO bot_config "
                "(bot_name, persona_id, common_user_id, is_active) "
                "VALUES ('chiwei', 'akao', CAST(:u AS uuid), true)"
            ),
            {"u": str(_AKAO_BOT_UID)},
        )
        for conv in (_DM, _GROUP):
            await s.execute(
                text(
                    "INSERT INTO common_bot_presence "
                    "(common_conversation_id, bot_name, is_active) "
                    "VALUES (CAST(:c AS uuid), 'chiwei', true)"
                ),
                {"c": str(conv)},
            )
    await _somebody_called_her(_DM, at=_at(21, 0))
    await _somebody_called_her(_GROUP, at=_at(21, 0), names_her=True)
    return living_db


@pytest.fixture
def spoken(monkeypatch):
    """接住嘴吐出去的 ``ChatResponseSegment``，不真的发 MQ。"""
    from app.living import mouth as mouth_mod

    out: list = []

    async def fake_emit(data):
        out.append(data)

    monkeypatch.setattr(mouth_mod, "emit", fake_emit)
    return out


@pytest.fixture
def guard(monkeypatch):
    """交出去之前那一关的替身。默认放行，而且是**判过之后**放行的。"""
    from app.capabilities.output_safety import OutputVerdict
    from app.living import mouth as mouth_mod

    class FakeGuard:
        def __init__(self) -> None:
            self.verdict = OutputVerdict(ok=True)
            self.judged: list[str] = []
            self.deadlines: list[float | None] = []

        async def __call__(self, text: str, *, timeout_s: float | None = None):
            self.judged.append(text)
            self.deadlines.append(timeout_s)
            return self.verdict

    fake = FakeGuard()
    monkeypatch.setattr(mouth_mod, "audit_output", fake)
    return fake


@pytest.fixture
def voice(monkeypatch, guard):
    """把渲染那一步换成替身：拿到什么、吐出什么，都由用例说了算。

    ``guard`` 挂在这儿而不是各条用例上：她开口就要过那一关，没有例外。哪条用例想
    换个判法，把 ``guard`` 也接进签名里改 ``verdict`` 就行。
    """
    from app.agent.neutral import Message, Role
    from app.living import mouth as mouth_mod

    class FakeVoice:
        def __init__(self) -> None:
            self.said = "你去过那家抹茶店吗？我最近老想去。"
            self.runs: list[tuple[list, dict]] = []

        async def run(self, messages, **kwargs):
            self.runs.append((messages, kwargs))
            return Message(role=Role.ASSISTANT, content=self.said)

    fake = FakeVoice()
    monkeypatch.setattr(mouth_mod, "build_voice_runner", lambda: fake)

    async def fake_find_persona(persona_id: str):
        from types import SimpleNamespace

        return SimpleNamespace(display_name="赤尾", persona_core="她拍胶片、泡抹茶店。")

    monkeypatch.setattr(mouth_mod, "find_persona", fake_find_persona)
    return fake


# --------------------------------------------------------------------------
# 一 · 用的是对话模型，不是 life 那个
# --------------------------------------------------------------------------


def test_the_mouth_speaks_with_the_chat_model_not_the_life_model():
    from app.living.mouth import _VOICE_CFG

    assert _VOICE_CFG.model_id == "main-chat-model", (
        "拿 life-model 写对外的话 —— 它推理强但对话差，出去的是一段推理稿"
    )
    assert _VOICE_CFG.prompt_id == LIVING_CHAT_VOICE_PROMPT_ID
    assert LIVING_CHAT_VOICE_PROMPT_ID.startswith("living_"), (
        "新引擎用新的 prompt id，不碰旧引擎那几个"
    )


def test_the_mouth_never_asks_her_who_the_message_is_replying_to():
    """第一版是单次渲染，不做对话窗口自主权 —— 签名里不该有"接着上一条"的位置。

    ``pictures`` 在这里：图是**结构化参数**，不是正文里的一句引用（见下面第十二节）。
    """
    props = set(send_message.definition.parameters["properties"])
    assert props == {"what", "channel_id", "pictures"}, props
    assert "reply" not in " ".join(props), props


# --------------------------------------------------------------------------
# 二 · 出站契约照主动发那条走
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_what_goes_out_is_a_proactive_segment_with_no_source_message(
    mouth_db, in_a_moment, spoken, voice
):
    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="翻胶片", noted_at=_at(21),
    )

    async with in_a_moment("akao"):
        said = await send_message.invoke(
            {"what": "问问他抹茶店去过没", "channel_id": str(_DM)}
        )

    assert not isinstance(said, dict), said
    (segment,) = spoken
    assert segment.is_proactive is True
    assert segment.message_id.startswith(PROACTIVE_MESSAGE_ID_PREFIX)
    assert segment.root_id is None, "主动发没有来源消息，不许伪造一条被回复的消息"
    assert segment.chat_id == str(_DM)
    assert segment.is_p2p is True
    assert segment.bot_name == "chiwei"
    assert segment.lane == LANE, "sink 不注入 header lane，必须显式带在 body 上"
    assert segment.persona_id == "akao"
    assert segment.is_last is True
    assert segment.content == voice.said, (
        "出去的必须是渲染后的人话，不是她那句内部意图"
    )


@pytest.mark.integration
async def test_a_group_message_goes_out_as_a_group_message(
    mouth_db, in_a_moment, spoken, voice
):
    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="翻胶片", noted_at=_at(21),
    )

    async with in_a_moment("akao"):
        await send_message.invoke({"what": "回一句", "channel_id": str(_GROUP)})

    (segment,) = spoken
    assert segment.is_p2p is False
    assert segment.chat_id == str(_GROUP)


@pytest.mark.integration
async def test_saying_the_same_thing_twice_only_goes_out_once(
    mouth_db, in_a_moment, spoken, voice
):
    """**去重必须挡在出站之前，不能指望下游。**

    实测过 chat-response-worker（chat-response-handler.ts:193-207 的自述 + :332 的
    无条件 ack）：出站**没有**发送级去重，同一个 ``message_id`` 投两次就是真人收到
    两条。所以派生一个稳定 id 只解决了"两条长得一样"，没解决"发了两次"——认领记录
    才是那道闸。工具重试、整轮 @retry 重放、这一缝重跑，全走同一个派生键。
    """
    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="翻胶片", noted_at=_at(21),
    )

    async with in_a_moment("akao"):
        await send_message.invoke({"what": "问问他", "channel_id": str(_DM)})
        await send_message.invoke({"what": "问问他", "channel_id": str(_DM)})

    assert len(spoken) == 1, (
        f"同一句话出站了 {len(spoken)} 次 —— 下游不去重，真人会收到两条"
    )


# --------------------------------------------------------------------------
# 三 · 她说出去的话也是一件发生过的事
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_what_she_sent_lands_as_a_happening_so_she_knows_she_said_it(
    mouth_db, in_a_moment, spoken, voice
):
    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="翻胶片", noted_at=_at(21),
    )

    async with in_a_moment("akao"):
        await send_message.invoke({"what": "问问他", "channel_id": str(_DM)})

    (h,) = await recent_own_happenings(lane=LANE, persona_id="akao")
    assert h.content == voice.said, "落的必须是真的发出去那句话"
    assert h.medium == MEDIUM_PHONE
    assert h.channel_id == str(_DM), "不带会话，下一缝她分不清这话是在哪儿说的"


@pytest.mark.integration
async def test_a_group_message_is_recorded_as_a_group_message(
    mouth_db, in_a_moment, spoken, voice
):
    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="翻胶片", noted_at=_at(21),
    )

    async with in_a_moment("akao"):
        await send_message.invoke({"what": "回一句", "channel_id": str(_GROUP)})

    (h,) = await recent_own_happenings(lane=LANE, persona_id="akao")
    assert h.medium == MEDIUM_GROUP_CHAT


@pytest.mark.integration
async def test_what_she_texts_is_not_overheard_by_the_sister_next_to_her(
    mouth_db, in_a_moment, spoken, voice
):
    """手机隔着设备：坐在她旁边的姐姐也看不见那些字。"""
    from app.living.happening import read_perceived_by

    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/客厅",
        doing="翻胶片", noted_at=_at(21),
    )
    await note_whereabouts(
        lane=LANE, persona_id="ayana", moment_id="m1", place="家/客厅",
        doing="看书", noted_at=_at(21),
    )

    async with in_a_moment("akao"):
        await send_message.invoke({"what": "问问他", "channel_id": str(_DM)})

    heard = await read_perceived_by(lane=LANE, persona_id="ayana")
    assert heard.items == []


# --------------------------------------------------------------------------
# 四 · 发不出去就说发不出去
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_an_empty_rendering_sends_nothing_instead_of_falling_back(
    mouth_db, in_a_moment, spoken, voice
):
    voice.said = "   "

    async with in_a_moment("akao"):
        outcome = await send_message.invoke(
            {"what": "问问他抹茶店去过没", "channel_id": str(_DM)}
        )

    assert spoken == [], "渲染没出内容却还是发了 —— 发出去的是空消息或者她的内部措辞"
    assert isinstance(outcome, dict), "该把「没发出去」喂回她自己处置"

    # 回喂的话只报事实。「失败了」由 @tool_error 的前缀说完，这里只补为什么；
    # 要不要重试、换不换说法、还是转头去干别的，是她的判断，工具不替她安排。
    # 对照同文件里 emit 断掉那条的写法：发生了什么 / 什么不知道 / 系统补不补发。
    assert "渲染没出内容" in outcome["message"], "得让她知道为什么没发出去"
    assert not any(s in outcome["message"] for s in ("再试", "换个说法")), (
        f"工具在指挥她下一步该干嘛：{outcome['message']!r}"
    )


@pytest.mark.integration
async def test_a_conversation_she_is_not_in_is_refused_loudly(
    mouth_db, in_a_moment, spoken, voice
):
    async with in_a_moment("akao"):
        outcome = await send_message.invoke(
            {"what": "喂", "channel_id": str(_NOT_HERS)}
        )

    assert spoken == []
    assert isinstance(outcome, dict)


@pytest.mark.integration
async def test_a_made_up_channel_id_is_refused_loudly(
    mouth_db, in_a_moment, spoken, voice
):
    async with in_a_moment("akao"):
        outcome = await send_message.invoke({"what": "喂", "channel_id": "随便编的"})

    assert spoken == []
    assert isinstance(outcome, dict)


# --------------------------------------------------------------------------
# 五 · 「她说过」和「真交出去了」的先后语义
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_handoff_that_blew_up_is_unknown_not_a_confirmed_failure(
    mouth_db, in_a_moment, spoken, voice, monkeypatch
):
    """``emit`` 抛错**不等于**没交出去 —— 那一格的事实是"结果未知"。

    publisher confirm 超时、连接断在确认之前，broker 都可能已经收件了。而下游
    chat-response-worker 出站失败照样 ack、MQ 重投也没有发送级去重 —— 所以"判成确定
    没发出去、允许自动重试"= 可能重复发给真人。

    重复打扰真人比少一条更糟，所以这一格跟"崩在 emit 和收口之间"完全同一档：行停在
    ``claimed``（可查、不重发）、不留记忆、如实告诉她不知道到没到。
    """
    from app.living import mouth as mouth_mod
    from app.living.mouth import STATE_CLAIMED, latest_outbound, unsettled_outbound

    async def boom(_data):
        raise RuntimeError("MQ 连不上")

    monkeypatch.setattr(mouth_mod, "emit", boom)
    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="翻胶片", noted_at=_at(21),
    )

    async with in_a_moment("akao") as ctx:
        await send_message.invoke({"what": "问问他", "channel_id": str(_DM)})

    assert await recent_own_happenings(lane=LANE, persona_id="akao") == [], (
        "结果不知道却记成说过了 —— 她从此不会再说这句话"
    )
    row = await latest_outbound(lane=LANE, moment_id=ctx.features["living_moment"])
    assert row is not None and row.state == STATE_CLAIMED, (
        "emit 抛错被判成「确定没发出去」了 —— 那就允许自动重试，而 broker 可能已经"
        f"收件，真人会收到两条。拿到：{row}"
    )
    assert [
        r.state for r in await unsettled_outbound(lane=LANE, persona_id="akao")
    ] == [STATE_CLAIMED], "结果未知的这条要能被捞出来给人看"


@pytest.mark.integration
async def test_she_is_told_the_outcome_is_unknown_not_told_to_try_again(
    mouth_db, in_a_moment, spoken, voice, monkeypatch
):
    """回给她的那句话：不许说"发失败了"，也不许让她以为已经说出去了。

    "再试一次"是在指挥她 —— 重不重说是她的决定，不是系统按的按钮。而"发出去了"是
    编的：这一格根本不知道。所以只剩一条路：**如实说不知道**。
    """
    from app.living import mouth as mouth_mod

    async def boom(_data):
        raise RuntimeError("MQ 连不上")

    monkeypatch.setattr(mouth_mod, "emit", boom)
    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="翻胶片", noted_at=_at(21),
    )

    async with in_a_moment("akao"):
        outcome = await send_message.invoke(
            {"what": "问问他", "channel_id": str(_DM)}
        )

    assert isinstance(outcome, str), f"结果未知不是工具坏了，别报成错。拿到：{outcome!r}"
    assert "不知道" in outcome, f"没告诉她这一格是不知道的。拿到：{outcome!r}"
    assert voice.said in outcome, "该让她看到自己那句话原文"
    for lie in ("没发出去", "失败", "发出去了："):
        assert lie not in outcome, f"「{lie}」是编的 —— 这一格不知道。拿到：{outcome!r}"
    for order in ("再试", "重试", "重发"):
        assert order not in outcome, f"「{order}」是在指挥她。拿到：{outcome!r}"


@pytest.mark.integration
async def test_a_blown_up_handoff_is_never_handed_off_again_in_the_same_seam(
    mouth_db, in_a_moment, spoken, voice, monkeypatch
):
    """同一缝里再说同一句 —— 不再交第二次。第一条可能已经躺在 broker 里了。"""
    from app.living import mouth as mouth_mod

    attempts: list = []

    async def boom(data):
        attempts.append(data)
        raise RuntimeError("MQ 连不上")

    monkeypatch.setattr(mouth_mod, "emit", boom)
    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="翻胶片", noted_at=_at(21),
    )

    async with in_a_moment("akao"):
        await send_message.invoke({"what": "问问他", "channel_id": str(_DM)})
        await send_message.invoke({"what": "问问他", "channel_id": str(_DM)})

    assert len(attempts) == 1, (
        f"同一缝里又交了一次 —— broker 可能已经收了第一条，真人收到两条。"
        f"交了 {len(attempts)} 次"
    )


@pytest.mark.integration
async def test_the_next_seam_is_a_new_seam_so_she_can_say_it_again(
    mouth_db, in_a_moment, spoken, voice, monkeypatch
):
    """这一缝不重发，不等于这句话被判死了。

    ``outbound_id`` 从 ``lane|persona|moment|会话|意图`` 派生 —— **下一缝是新的
    ``moment_id``，就是新的 ``outbound_id``**，认领表拦不住它。所以"要不要再说一次"
    这个决定回到了她手里，而不是系统替她按重试按钮。这条推理必须在代码里真的成立，
    所以这里把它跑一遍。
    """
    from app.living import mouth as mouth_mod

    attempts: list = []
    broker_down = {"yes": True}

    async def flaky(data):
        attempts.append(data)
        if broker_down["yes"]:
            raise RuntimeError("MQ 连不上")
        spoken.append(data)

    monkeypatch.setattr(mouth_mod, "emit", flaky)
    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="翻胶片", noted_at=_at(21),
    )

    async with in_a_moment("akao", moment_id="2026-07-25T21:30+08:00"):
        await send_message.invoke({"what": "问问他", "channel_id": str(_DM)})
    assert spoken == [] and len(attempts) == 1

    broker_down["yes"] = False
    async with in_a_moment("akao", moment_id="2026-07-25T21:40+08:00"):
        again = await send_message.invoke(
            {"what": "问问他", "channel_id": str(_DM)}
        )

    assert len(spoken) == 1, (
        "下一缝她再说一次却被认领表拦下了 —— 那这句话就被系统判死了，"
        f"而这个决定不该由系统做。交出去 {len(attempts)} 次，发出 {len(spoken)} 条"
    )
    assert isinstance(again, str) and voice.said in again
    (h,) = await recent_own_happenings(lane=LANE, persona_id="akao")
    assert h.content == voice.said, "这一次真发出去了，记忆就该落下"


@pytest.mark.integration
async def test_a_crash_after_handoff_leaves_a_row_that_says_so(
    mouth_db, in_a_moment, spoken, voice, monkeypatch
):
    """交出去了、但记忆那一步崩了 —— 留下一条**看得见**的未收口记录。

    这一段不是原子的、也做不到原子（一边是 broker，一边是库）。做得到的是：崩在
    中间时哪一边留下**可预期、可查**。所以有一条认领记录：``claimed`` 就是"交出去了
    但没确认完，可能已经送到、她可能不记得"，用 :func:`unsettled_outbound` 一句
    SQL 就能捞出来。
    """
    from app.living import mouth as mouth_mod
    from app.living.mouth import STATE_CLAIMED, unsettled_outbound

    async def boom(**_kw):
        raise RuntimeError("记这一步崩了")

    monkeypatch.setattr(mouth_mod, "record_happening", boom)
    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="翻胶片", noted_at=_at(21),
    )

    async with in_a_moment("akao"):
        await send_message.invoke({"what": "问问他", "channel_id": str(_DM)})

    assert len(spoken) == 1, "已经交给投递了"
    stuck = await unsettled_outbound(lane=LANE, persona_id="akao")
    assert [r.state for r in stuck] == [STATE_CLAIMED], (
        "交出去了但没记上，而且外面看不出来 —— 这正是旧引擎相隔三分钟自相矛盾那个 bug"
    )


@pytest.mark.integration
async def test_an_unsettled_send_is_never_handed_off_a_second_time(
    mouth_db, in_a_moment, spoken, voice, monkeypatch
):
    """未收口 = **不重发**。重复打扰真人比少一条更糟，而且那条记录会告诉人去看。

    **只还原 ``record_happening`` 这一个符号，不许 ``monkeypatch.undo()``。**
    ``test_db`` 那份 session 注入用的是同一个 function-scoped monkeypatch 实例
    （``spoken`` / ``voice`` 两个替身也是），``undo()`` 会把它们一起撤掉：第二次调
    用于是打向真 DSN、连不上，异常被 ``@tool_error`` 转成一个 dict，而"旧列表还是
    一条"照样成立 —— 这条用例要守的防重路径**一次都没被执行过**。所以下面断言的是
    第二次拿到「已经认领过、不再发」那句正常回话，不是一个被吞掉的异常。
    """
    from app.living import mouth as mouth_mod
    from app.living.mouth import STATE_CLAIMED, SpokenOutbound, latest_outbound
    from app.runtime.persist import select_all_versions

    async def boom(**_kw):
        raise RuntimeError("记这一步崩了")

    real_record_happening = mouth_mod.record_happening
    monkeypatch.setattr(mouth_mod, "record_happening", boom)
    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="翻胶片", noted_at=_at(21),
    )
    async with in_a_moment("akao", moment_id=_MOMENT):
        await send_message.invoke({"what": "问问他", "channel_id": str(_DM)})
    assert len(spoken) == 1

    monkeypatch.setattr(mouth_mod, "record_happening", real_record_happening)
    async with in_a_moment("akao", moment_id=_MOMENT):
        again = await send_message.invoke(
            {"what": "问问他", "channel_id": str(_DM)}
        )

    assert len(spoken) == 1, "同一件事重跑时又发了一次 —— 真人收到两条"
    # 下面这几条是"真的走到了防重路径"的凭据：拿到的是那句正常回话（不是异常被
    # 吞成的 dict），话里说了没再发一遍，而且这一次一个字都没往库里写。
    assert isinstance(again, str), (
        f"第二次调用炸在了防重之前 —— 这条路径根本没跑到。拿到：{again!r}"
    )
    assert "没有再发一遍" in again and voice.said in again, (
        f"回的不是「已经认领过、不再发」那句。拿到：{again!r}"
    )
    row = await latest_outbound(lane=LANE, moment_id=_MOMENT)
    assert row.state == STATE_CLAIMED, (
        f"第二次调用把这条记录往前推了 —— 它该在防重那一步就返回。拿到：{row}"
    )
    versions = await select_all_versions(
        SpokenOutbound, {"lane": LANE, "outbound_id": row.outbound_id}
    )
    assert [v.ver for v in versions] == [1], (
        f"第二次调用又认领了一版。拿到：{[v.ver for v in versions]}"
    )


# --------------------------------------------------------------------------
# 六 · 这条认领链上不止她一个写者
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_send_reconciled_before_it_settles_still_gets_settled(
    mouth_db, in_a_moment, spoken, voice, monkeypatch
):
    """对账钟抢在收口之前追了一版 —— 收口要合到那一版上，不是被它挤掉。

    :mod:`app.living.landing` 那条钟是这条认领链上的**第二个写者**。时序是可达的：

      1. ``send_message`` 写 v1 认领、把话交出去；
      2. 渠道那边落了账；
      3. 对账钟先跑，基于 v1 追了 v2（补上落地标识，``state`` 还是 ``claimed``）；
      4. 收口这才走到，而它手里那个版本号已经过期，CAS 写不进去。

    写不进去的后果是这条记录**永久停在「已落地、未收口」**：对账下一拍看它已经有
    落地标识、不再碰它，:func:`unsettled_outbound` 却一直把它当"她可能不记得自己
    说过"捞出来。全程零报错。

    所以这里把那一拍真的插进认领和收口之间 —— 造出真实的版本链，而不是让替身报一
    个假的 CAS 失败。
    """
    from app.living import mouth as mouth_mod
    from app.living.landing import reconcile_landings
    from app.living.mouth import (
        STATE_HANDED_OFF,
        SpokenOutbound,
        latest_outbound,
        unsettled_outbound,
    )
    from app.runtime.persist import select_all_versions

    # 「投递方在公共层落下的那一行」只在 test_landing 里定义一次，这边照用。
    from tests.living.test_landing import _the_channel_wrote

    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="翻胶片", noted_at=_at(21),
    )

    real_record_happening = mouth_mod.record_happening
    landed: list[uuid.UUID] = []

    async def the_channel_lands_and_the_clock_ticks(**kw):
        """记忆那一步之后、收口之前：渠道落账 + 对账真的跑一拍。"""
        await real_record_happening(**kw)
        claimed = await latest_outbound(lane=LANE, moment_id=_MOMENT)
        landed.append(
            await _the_channel_wrote(
                outbound_id=claimed.outbound_id, at=_at(21, 31)
            )
        )
        assert await reconcile_landings(lane=LANE) == 1, "对账那一拍没补上落地标识"

    monkeypatch.setattr(
        mouth_mod, "record_happening", the_channel_lands_and_the_clock_ticks
    )

    async with in_a_moment("akao", moment_id=_MOMENT):
        outcome = await send_message.invoke(
            {"what": "问问他", "channel_id": str(_DM)}
        )

    assert isinstance(outcome, str) and voice.said in outcome, outcome
    row = await latest_outbound(lane=LANE, moment_id=_MOMENT)
    assert row.state == STATE_HANDED_OFF, (
        "收口被对账那一版挤掉了 —— 这条永久停在「已落地、未收口」，对账下一拍也"
        f"不会再碰它，而且一句报错都没有。拿到：{row}"
    )
    assert row.settled_at is not None, "收口时刻没落下"
    assert row.landed_common_message_id == str(landed[0]), (
        "收口把对账补上的落地标识覆盖掉了 —— 那一版记的是已知事实，不能被抹回 NULL"
    )
    assert row.landed_at == _at(21, 31), "落地时刻被收口顺手改了"
    versions = await select_all_versions(
        SpokenOutbound, {"lane": LANE, "outbound_id": row.outbound_id}
    )
    assert [v.ver for v in versions] == [1, 2, 3], (
        f"版本链不是「认领 → 对账 → 收口」三版："
        f"{[(v.ver, v.state, v.landed_common_message_id) for v in versions]}"
    )
    assert await unsettled_outbound(lane=LANE, persona_id="akao") == [], (
        "收口了却还被当成「她可能不记得」捞出来"
    )


@pytest.mark.integration
async def test_losing_the_claim_race_does_not_hand_the_same_words_off_twice(
    mouth_db, in_a_moment, spoken, voice, monkeypatch
):
    """认领没抢到 = **绝对不能 emit**。抢赢的那个可能已经交出去了。

    认领那一版的 CAS 是这张表存在的全部意义。下游没有发送级去重
    （chat-response-handler.ts:193-207 的自述 + :332 的无条件 ack），抢输了还照发
    就是真人收到两条 —— 而 CAS 的返回值不被看的话，这件事一句报错都不会有。

    这里让**另一次真的 ``send_message``** 插在预检查和认领之间（渲染那一步就是那
    条缝），版本链真的往前走了一版，不是让替身报一个假的失败。
    """
    from app.agent.neutral import Message, Role
    from app.living import mouth as mouth_mod
    from app.living.mouth import SpokenOutbound, latest_outbound
    from app.runtime.persist import select_all_versions

    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="翻胶片", noted_at=_at(21),
    )

    someone_else_went_first: list[bool] = []

    class RacingVoice:
        """渲染这一步里，另一个跑同一缝同一句话的执行先认领、先交出去了。"""

        async def run(self, messages, **kwargs):
            if not someone_else_went_first:
                someone_else_went_first.append(True)
                await send_message.invoke(
                    {"what": "问问他", "channel_id": str(_DM)}
                )
            return Message(role=Role.ASSISTANT, content=voice.said)

    monkeypatch.setattr(mouth_mod, "build_voice_runner", lambda: RacingVoice())

    async with in_a_moment("akao", moment_id=_MOMENT):
        outcome = await send_message.invoke(
            {"what": "问问他", "channel_id": str(_DM)}
        )

    assert someone_else_went_first, "并发那一次没跑起来，这条用例什么都没验"
    assert len(spoken) == 1, (
        f"认领抢输了还是交了一次 —— 下游不去重，真人收到两条。交了 {len(spoken)} 次"
    )
    assert isinstance(outcome, str), f"抢输不是工具坏了，别报成错。拿到：{outcome!r}"
    assert "没有再发一遍" in outcome, (
        f"没告诉她这一句已经有人交出去了。拿到：{outcome!r}"
    )
    assert len(await recent_own_happenings(lane=LANE, persona_id="akao")) == 1, (
        "同一句话落了两条记忆 —— 她下一缝会以为自己说了两遍"
    )
    row = await latest_outbound(lane=LANE, moment_id=_MOMENT)
    versions = await select_all_versions(
        SpokenOutbound, {"lane": LANE, "outbound_id": row.outbound_id}
    )
    assert [v.ver for v in versions] == [1, 2], (
        f"抢输那一次也往认领链上写了。拿到：{[v.ver for v in versions]}"
    )


# --------------------------------------------------------------------------
# 十 · 交出去之前先过这一关
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_words_that_do_not_pass_never_reach_anyone(
    mouth_db, in_a_moment, spoken, voice, guard
):
    """判不合格就是不发 —— 不是发了再撤。

    她主动开口没有实时性压力（不像真人在等一条流式回复），所以这一关放得进"交出去
    之前"。放在之后就只剩"先发后撤"，而那意味着真人已经看见了。
    """
    from app.capabilities.output_safety import OutputVerdict
    from app.living.mouth import latest_outbound

    guard.verdict = OutputVerdict(
        ok=False, reason="output_unsafe", detail="confidence=0.9"
    )
    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="翻胶片", noted_at=_at(21),
    )

    async with in_a_moment("akao", moment_id=_MOMENT):
        outcome = await send_message.invoke(
            {"what": "问问他", "channel_id": str(_DM)}
        )

    assert spoken == [], "判不合格还交出去了 —— 这一关等于没有"
    assert await recent_own_happenings(lane=LANE, persona_id="akao") == [], (
        "没说出去的话落成了记忆 —— 她下一缝会以为自己说过"
    )
    assert await latest_outbound(lane=LANE, moment_id=_MOMENT) is None, (
        "没发出去却占住了认领 —— 那个 id 就此作废，她这一缝再也说不成这件事"
    )
    assert isinstance(outcome, str), (
        f"拦下不是工具坏了。报成错她就会重试，而重试会换一套措辞再判一次 —— "
        f"那是一条绕过去的路。拿到：{outcome!r}"
    )
    assert "没发出去" in outcome, f"没告诉她这句没发出去。拿到：{outcome!r}"


@pytest.mark.integration
async def test_what_gets_judged_is_the_sentence_that_would_be_seen(
    mouth_db, in_a_moment, spoken, voice, guard
):
    """判的是渲染出来那句人话，不是她脑子里那个意思。

    真人看见的是前者。拿后者去判就是判了一个没人会读到的东西。
    """
    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="翻胶片", noted_at=_at(21),
    )

    async with in_a_moment("akao", moment_id=_MOMENT):
        await send_message.invoke(
            {"what": "问问他抹茶店去过没", "channel_id": str(_DM)}
        )

    assert guard.judged == [voice.said], (
        f"判的不是真要发出去那句。拿到：{guard.judged!r}"
    )
    assert guard.deadlines == [_SEND_CHECK_TIMEOUT_S], (
        "没给期限 —— 判词那一步挂住就是把她整缝卡在网络上"
    )


@pytest.mark.integration
async def test_being_stopped_does_not_burn_the_id_for_that_sentence(
    mouth_db, in_a_moment, spoken, voice, guard
):
    """被拦下的那次不占认领 —— 否则她这一缝里连改都改不成。

    认领是从 ``(这一缝, 她那句意思)`` 派生的：被拦时如果占住了，同一缝里再说同一件
    事就会撞上"你已经说过了"，而那是假话 —— 她一个字都没说出去。
    """
    from app.capabilities.output_safety import OutputVerdict

    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="翻胶片", noted_at=_at(21),
    )

    async with in_a_moment("akao", moment_id=_MOMENT):
        guard.verdict = OutputVerdict(ok=False, reason="output_unsafe")
        await send_message.invoke({"what": "问问他", "channel_id": str(_DM)})
        guard.verdict = OutputVerdict(ok=True)
        outcome = await send_message.invoke(
            {"what": "问问他", "channel_id": str(_DM)}
        )

    assert len(spoken) == 1, (
        f"第二次没真的发出去 —— 第一次被拦时占住了那个 id。交了 {len(spoken)} 次"
    )
    assert "发出去了" in outcome, f"拿到：{outcome!r}"


@pytest.mark.integration
async def test_a_replay_does_not_pay_for_the_check_twice(
    mouth_db, in_a_moment, spoken, voice, guard
):
    """这一缝重放时，那道去重的闸仍然在这一关**前面**。

    顺序反过来的话，工具重试和整轮重放都会各花一次模型调用去判一句根本不会再发的话。
    """
    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="翻胶片", noted_at=_at(21),
    )

    async with in_a_moment("akao", moment_id=_MOMENT):
        await send_message.invoke({"what": "问问他", "channel_id": str(_DM)})
        await send_message.invoke({"what": "问问他", "channel_id": str(_DM)})

    assert len(spoken) == 1
    assert len(guard.judged) == 1, (
        f"同一句话判了 {len(guard.judged)} 次 —— 去重那道闸跑到这一关后面去了"
    )


@pytest.mark.integration
async def test_a_guard_that_could_not_judge_does_not_make_her_go_quiet(
    mouth_db, in_a_moment, spoken, voice, guard, caplog
):
    """这一关自己坏了的时候照发 —— 但欠的这一笔要留得下来。

    坏掉时挡下来，挡的不是一条消息、是整条线：她和三个姐妹一起哑掉，挂多久哑多久。
    而这道检查的实测拦截率本来就很低。代价是它**静默**，所以必须数得出来。
    """
    import logging

    from app.capabilities.output_safety import OutputVerdict

    guard.verdict = OutputVerdict(ok=True, checked=False)
    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="翻胶片", noted_at=_at(21),
    )

    with caplog.at_level(logging.WARNING):
        async with in_a_moment("akao", moment_id=_MOMENT):
            outcome = await send_message.invoke(
                {"what": "问问他", "channel_id": str(_DM)}
            )

    assert len(spoken) == 1, "这一关坏掉就把她的嘴堵上了"
    assert "发出去了" in outcome, f"拿到：{outcome!r}"
    assert "living_mouth_unchecked" in caplog.text, (
        "没检查就发出去了，却没留下任何痕迹 —— 那段时间漏了多少条永远查不出来"
    )


# --------------------------------------------------------------------------
# 十一 · 撤回是第三根轴
# --------------------------------------------------------------------------


def test_taking_it_back_is_its_own_axis_not_a_third_state():
    """本地认领、渠道落地、撤回 —— 三件事都真、也都可能单独发生。

    压进 ``state`` 就会用一个"撤回了"盖掉 ``claimed``，:func:`unsettled_outbound`
    从此捞不出它，"她可能不记得自己说过"这件事再没人看得见。
    """
    from app.living.mouth import SpokenOutbound

    fields = SpokenOutbound.model_fields
    assert "took_back_at" in fields, "她按下撤回那一刻"
    assert "recalled_at" in fields, "渠道那边真撤掉那一刻"
    for name in ("took_back_at", "recalled_at"):
        assert fields[name].default is None, (
            f"{name} 必须可空、且不能有默认值：migrator 加列生成的是可空无默认的 "
            f"ADD COLUMN，声明成别的样子的话这张表上每一条旧记录一读就炸"
        )


def test_the_take_back_axis_refuses_a_clockless_instant():
    """跟另外三个时刻同一个待遇：naive datetime 溜进 TIMESTAMPTZ 列就是错时区。"""
    import datetime as _dt

    import pydantic

    from app.living.mouth import STATE_HANDED_OFF, SpokenOutbound

    base = {
        "lane": LANE, "outbound_id": "x" * 32, "ver": 1, "persona_id": "akao",
        "channel_id": str(_DM), "moment_id": _MOMENT, "said": "话",
        "state": STATE_HANDED_OFF, "claimed_at": _at(21),
    }
    for name in ("took_back_at", "recalled_at"):
        with pytest.raises(pydantic.ValidationError):
            SpokenOutbound(**base, **{name: _dt.datetime(2026, 7, 25, 21, 0)})


@pytest.mark.integration
async def test_a_line_that_lost_the_claim_race_is_not_counted_as_unchecked(
    mouth_db, in_a_moment, spoken, voice, guard, monkeypatch, caplog
):
    """认领抢输的那次一个字都没发出去，不该记进"漏检了多少"那本账。

    ``living_mouth_unchecked`` 是数"那段时间有多少话没检查就到了真人手上"的唯一
    锚。把它记在认领**之前**，抢输的那次也会被算进去 —— 那条根本没出站，账就虚了。
    """
    import logging

    from app.agent.neutral import Message, Role
    from app.capabilities.output_safety import OutputVerdict
    from app.living import mouth as mouth_mod

    guard.verdict = OutputVerdict(ok=True, checked=False)
    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="翻胶片", noted_at=_at(21),
    )

    someone_else_went_first: list[bool] = []

    class RacingVoice:
        async def run(self, messages, **kwargs):
            if not someone_else_went_first:
                someone_else_went_first.append(True)
                await send_message.invoke(
                    {"what": "问问他", "channel_id": str(_DM)}
                )
            return Message(role=Role.ASSISTANT, content=voice.said)

    monkeypatch.setattr(mouth_mod, "build_voice_runner", lambda: RacingVoice())

    with caplog.at_level(logging.WARNING):
        async with in_a_moment("akao", moment_id=_MOMENT):
            await send_message.invoke(
                {"what": "问问他", "channel_id": str(_DM)}
            )

    assert someone_else_went_first, "并发那一次没跑起来，这条用例什么都没验"
    assert len(spoken) == 1
    assert caplog.text.count("living_mouth_unchecked") == 1, (
        f"真交出去的只有一条，这本账却记了 "
        f"{caplog.text.count('living_mouth_unchecked')} 笔"
    )


@pytest.mark.integration
async def test_being_stopped_over_and_over_never_leaks_a_line(
    mouth_db, in_a_moment, spoken, voice, guard
):
    """一直判不合格就是一条都不出去 —— 换多少种说法都一样。

    拦下时返回的是一句正常的话而不是错误，所以她可以在同一缝里换个说法再来。这条
    守的是那条路的另一端：只要每次都判不合格，就一次都不会漏出去。**这里没有重试
    预算**，因为"还要不要再说一次"是她的判断；能保证的是每个候选都被判过。
    """
    from app.capabilities.output_safety import OutputVerdict

    guard.verdict = OutputVerdict(ok=False, reason="output_unsafe")
    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="翻胶片", noted_at=_at(21),
    )

    async with in_a_moment("akao", moment_id=_MOMENT):
        for what in ("这么说", "换个说法", "再换一个"):
            outcome = await send_message.invoke(
                {"what": what, "channel_id": str(_DM)}
            )
            assert "没发出去" in outcome, f"拿到：{outcome!r}"

    assert spoken == [], f"判了三次不合格，还是漏出去 {len(spoken)} 条"
    assert len(guard.judged) == 3, (
        f"每个候选都该被判一次，实际判了 {len(guard.judged)} 次"
    )
    assert await recent_own_happenings(lane=LANE, persona_id="akao") == []


# --------------------------------------------------------------------------
# 十 · 会话白名单：她能发的严格等于她看得见的
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_conversation_out_of_sight_is_refused_even_though_her_bot_is_in_it(
    mouth_db, in_a_moment, spoken, voice
):
    """bot 还在那个群里、但没人叫她 —— 她发不出去。

    这是白名单主闸在嘴这一侧的样子：她能发的严格等于她能看见的那些会话
    （:func:`app.living.phone.reachable_conversations`），而不是"bot 还在的那些"。
    """
    quiet = uuid.uuid5(uuid.NAMESPACE_OID, "conv-group-nobody-calls-her")
    async with session_mod.get_session() as s:
        await s.execute(
            text(
                "INSERT INTO common_conversation"
                " (common_conversation_id, channel, scope, display_name, is_active)"
                " VALUES (CAST(:c AS uuid), 'lark', 'group', '没人叫她的群', true)"
            ),
            {"c": str(quiet)},
        )
        await s.execute(
            text(
                "INSERT INTO common_bot_presence"
                " (common_conversation_id, bot_name, is_active)"
                " VALUES (CAST(:c AS uuid), 'chiwei', true)"
            ),
            {"c": str(quiet)},
        )

    async with in_a_moment("akao"):
        outcome = await send_message.invoke({"what": "喂", "channel_id": str(quiet)})

    assert spoken == [], "名单外的会话她照样发出去了"
    assert isinstance(outcome, dict)


@pytest.mark.integration
async def test_sending_checks_where_her_bot_is_right_now_not_the_settled_list(
    mouth_db, in_a_moment, spoken, voice
):
    """发消息那一刻查的 presence 是**实时**的，不是这一缝开头那份快照。

    这一道由两半组成，时效性刻意不同：名单读这一缝的锚（下一条用例验那半），
    presence 每次重新查。"bot 还在不在那个会话里"是这句话能不能真的送到的物理前
    提，读一份缝开头的快照等于这道保护不存在 —— bot 半路被移出会话，她还会照着过
    期的判断把话交出去。
    """
    from app.living.phone import reachable_conversations

    async with in_a_moment("akao"):
        settled = {
            c.channel_id
            for c in await reachable_conversations(persona_id="akao", now=_at(21, 30))
        }
        assert str(_DM) in settled, "用例前提没成立：这条私聊本该在名单里"

        async with session_mod.get_session() as s:
            await s.execute(
                text(
                    "DELETE FROM common_bot_presence"
                    " WHERE common_conversation_id = CAST(:c AS uuid)"
                ),
                {"c": str(_DM)},
            )

        outcome = await send_message.invoke({"what": "喂", "channel_id": str(_DM)})

    assert spoken == [], "bot 已经被移出去了，她还是把话交了出去"
    assert isinstance(outcome, dict)


@pytest.mark.integration
async def test_she_can_still_answer_a_line_that_slid_out_of_the_window_mid_moment(
    mouth_db, in_a_moment, spoken, voice
):
    """名单那一半读的是**这一缝的锚**：缝开头看得见，缝中途滑出时间窗照样答得上。

    上一条用例验的是 presence 那一半实时重查。这一条钉的是另一半 —— 两半必须都被钉
    住，否则"改成整条主闸都实时重算"和"改成整条主闸都读快照"这两个方向的回归各有一
    条用例挡不住。

    她这一缝要回的就是缝开头摆在她眼前的那些会话。时间窗是按 ``now`` 滑的，而模型想
    一想、渲染一次话要花掉真实时间；名单在她张嘴那一刻重算的话，她会在"刚看完那条私
    聊、正要回"的中途发现那条会话没了 —— 而消息还挂在真人眼前。
    """
    from app.data.queries.messages import count_summons_since
    from app.living.phone import reachable_conversations

    async with in_a_moment("akao"):
        settled = {
            c.channel_id
            for c in await reachable_conversations(persona_id="akao", now=_at(21, 30))
        }
        assert str(_DM) in settled, "用例前提没成立：这条私聊本该在名单里"

        # 把让它进名单的那条消息删掉 —— 现在重算的话它一档都够不着。
        async with session_mod.get_session() as s:
            await s.execute(
                text(
                    "DELETE FROM common_message"
                    " WHERE common_conversation_id = CAST(:c AS uuid)"
                ),
                {"c": str(_DM)},
            )
        counted = await count_summons_since(
            conversations=[{"channel_id": str(_DM), "scope": "direct", "title": ""}],
            bot_user_ids=[str(_AKAO_BOT_UID)],
            own_bots=["chiwei"],
            since_ms=[0],
        )
        assert [int(row["n"]) for row in counted] == [0], (
            f"用例前提没成立：这条私聊现在重算该是一条都数不出来，拿到 {counted}"
        )

        outcome = await send_message.invoke({"what": "在", "channel_id": str(_DM)})

    assert len(spoken) == 1, f"缝开头看得见的会话，她答不上了：{spoken}"
    assert spoken[0].chat_id == str(_DM)
    assert isinstance(outcome, str) and "发出去了" in outcome, outcome


# --------------------------------------------------------------------------
# 十二 · 她带出去的图
# --------------------------------------------------------------------------

# 不带图那条路派生出来的 outbound_id，写死。
#
# 它 = uuid5(_ID_NS, "coe-living\x1fakao\x1f2026-07-25T21:30+08:00\x1f{_DM}\x1f问问他")，
# 也就是**加图这件事之前**那个 seed 一字不差算出来的东西。写死而不是"跟重新算一遍
# 相等"：后者会跟着实现一起变，等于什么都没钉住。seed 里只要多出一个分隔符，历史上
# 派生过的每一个 outbound_id 都会换一批 —— 认领表从此拦不住任何一次重放，同一句话
# 会被再发一遍，而且一句报错都没有。
_NO_PICTURE_OUTBOUND_ID = "1f5ab29326e0548b83eebb0d8f54fa24"


@pytest.fixture
def a_picture(mouth_db):
    """造一张"她做过的图"。默认是这条泳道上、她自己名下的那种。"""
    from app.living.pictures import remember_a_picture

    async def make(
        *,
        file_name: str,
        persona_id: str = "akao",
        lane: str = LANE,
        what: str = "抹茶店门口那只猫",
    ):
        return await remember_a_picture(
            lane=lane,
            persona_id=persona_id,
            file_name=file_name,
            what=what,
            made_at=_at(20),
        )

    return make


@pytest.mark.integration
async def test_a_picture_goes_out_on_its_own_field_and_never_reaches_the_voice_step(
    mouth_db, in_a_moment, spoken, voice, a_picture
):
    """图走结构化字段，一个字符都不经过渲染那一步。

    渲染是**自由生成**：prompt 明写"把它说成你会说的那句话"，没有任何原样保留的
    通道。图片引用只要进了那一步的输入，它要么被改写、要么被丢掉 —— 两种下场都不
    报错，她以为图发出去了，而真人看到的是一条没有图的消息。

    所以这里两头都验：渲染那一步**看不到**句柄和永久句柄，而出站消息上**带着**
    永久句柄。
    """
    pic = await a_picture(file_name="temp/tos_matcha_cat.jpg")
    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="翻胶片", noted_at=_at(21),
    )

    async with in_a_moment("akao"):
        outcome = await send_message.invoke(
            {
                "what": "给他看我画的那只猫",
                "channel_id": str(_DM),
                "pictures": [pic.picture_id],
            }
        )

    assert isinstance(outcome, str), outcome
    (segment,) = spoken
    assert segment.picture_file_names == ["temp/tos_matcha_cat.jpg"], (
        "出站消息上没有图 —— 那条链从这里就断了"
    )
    assert segment.content == voice.said, "正文仍然只是渲染出来那句人话"

    seen = repr(voice.runs)
    assert pic.picture_id not in seen, (
        f"句柄进了渲染那一步的输入 —— 它会被改写或丢掉。看到：{seen!r}"
    )
    assert "temp/tos_matcha_cat.jpg" not in seen, (
        f"永久句柄进了渲染那一步的输入。看到：{seen!r}"
    )


@pytest.mark.integration
async def test_several_pictures_ride_out_in_the_order_she_gave_them(
    mouth_db, in_a_moment, spoken, voice, a_picture
):
    one = await a_picture(file_name="temp/tos_one.jpg")
    two = await a_picture(file_name="temp/tos_two.jpg")
    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="翻胶片", noted_at=_at(21),
    )

    async with in_a_moment("akao"):
        await send_message.invoke(
            {
                "what": "两张一起给他",
                "channel_id": str(_DM),
                "pictures": [two.picture_id, one.picture_id],
            }
        )

    (segment,) = spoken
    assert segment.picture_file_names == ["temp/tos_two.jpg", "temp/tos_one.jpg"]


@pytest.mark.integration
async def test_the_same_words_with_a_different_picture_is_a_different_send(
    mouth_db, in_a_moment, spoken, voice, a_picture
):
    """**图算进发送身份。**

    ``outbound_id`` 从 ``(lane, persona, 这一缝, 会话, 她那句意图)`` 派生。不把图
    算进去的话，同一缝、同一条会话、同样的话配另一张图会撞上同一个 id，被认领表判
    成重发直接挡掉 —— 她换了张图重发，真人什么都收不到，而她以为发了。

    选把图算进 seed，**不是**绕开去重：那道闸漏一次就是真人收到两条。
    """
    cat = await a_picture(file_name="temp/tos_cat.jpg")
    dog = await a_picture(file_name="temp/tos_dog.jpg")
    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="翻胶片", noted_at=_at(21),
    )

    async with in_a_moment("akao", moment_id=_MOMENT):
        await send_message.invoke(
            {"what": "看这个", "channel_id": str(_DM), "pictures": [cat.picture_id]}
        )
        await send_message.invoke(
            {"what": "看这个", "channel_id": str(_DM), "pictures": [dog.picture_id]}
        )

    assert len(spoken) == 2, (
        f"同一句话换了张图，第二次被判成重发挡掉了 —— 真人什么都收不到，"
        f"而她以为发了。出站 {len(spoken)} 条"
    )
    assert [s.picture_file_names for s in spoken] == [
        ["temp/tos_cat.jpg"],
        ["temp/tos_dog.jpg"],
    ]
    assert spoken[0].message_id != spoken[1].message_id, (
        "两条出站消息撞了同一个 message_id"
    )


@pytest.mark.integration
async def test_replaying_the_very_same_request_with_pictures_still_goes_out_once(
    mouth_db, in_a_moment, spoken, voice, a_picture
):
    """带图不放松去重：同一句话配同一张图，重放照样只出去一次。"""
    cat = await a_picture(file_name="temp/tos_cat.jpg")
    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="翻胶片", noted_at=_at(21),
    )

    async with in_a_moment("akao", moment_id=_MOMENT):
        await send_message.invoke(
            {"what": "看这个", "channel_id": str(_DM), "pictures": [cat.picture_id]}
        )
        again = await send_message.invoke(
            {"what": "看这个", "channel_id": str(_DM), "pictures": [cat.picture_id]}
        )

    assert len(spoken) == 1, (
        f"同一句话配同一张图出站了 {len(spoken)} 次 —— 下游不去重，真人收到两条"
    )
    assert isinstance(again, str) and "没有再发一遍" in again, again


@pytest.mark.integration
async def test_a_send_without_pictures_derives_exactly_the_id_it_always_did(
    mouth_db, in_a_moment, spoken, voice
):
    """不带图那条路的 seed 一个字节都没变。

    图那一段是**追加**上去的，空的时候拼出来的仍是从前那个字符串。这里钉的是一个
    写死的快照（见 :data:`_NO_PICTURE_OUTBOUND_ID`）：改坏了的症状是认领表在历史
    记录上全部失效，而不是任何一条报错。
    """
    from app.living.mouth import latest_outbound

    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="翻胶片", noted_at=_at(21),
    )

    async with in_a_moment("akao", moment_id=_MOMENT):
        await send_message.invoke({"what": "问问他", "channel_id": str(_DM)})

    row = await latest_outbound(lane=LANE, moment_id=_MOMENT)
    assert row.outbound_id == _NO_PICTURE_OUTBOUND_ID, (
        f"不带图那条路的派生 seed 变了 —— 历史上所有 outbound_id 从此对不上，"
        f"认领表拦不住任何一次重放。拿到：{row.outbound_id}"
    )
    (segment,) = spoken
    assert segment.message_id == (
        f"{PROACTIVE_MESSAGE_ID_PREFIX}{uuid.UUID(_NO_PICTURE_OUTBOUND_ID)}"
    )
    assert segment.picture_file_names == [], "不带图就是不带图"


@pytest.mark.integration
async def test_a_handle_that_is_not_hers_never_gets_sent(
    mouth_db, in_a_moment, spoken, voice, a_picture
):
    """她引用一个不属于她的句柄 —— 拒，而且一个字都不发出去。

    句柄是从 ``file_name`` 派生的，所以别的泳道上那张同名的图算出来的是**同一串**：
    只按句柄取的话，一个从别处拿到的串就能把姐姐画的、或者 prod 上那张取出来当成
    自己的发出去。``lane`` + ``persona_id`` 是硬条件，两半都得对上。

    取不到就整条不发，**绝不跳过那一张把剩下的发出去** —— 她说的是"把这几张给他
    看"，少一张的那条消息不是她要发的那条。
    """
    from app.living.mouth import latest_outbound

    mine = await a_picture(file_name="temp/tos_mine.jpg")
    sisters = await a_picture(file_name="temp/tos_ayana.jpg", persona_id="ayana")
    elsewhere = await a_picture(file_name="temp/tos_prod.jpg", lane="prod")
    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="翻胶片", noted_at=_at(21),
    )

    async with in_a_moment("akao", moment_id=_MOMENT):
        for handles in (
            [sisters.picture_id],
            [elsewhere.picture_id],
            ["0" * 32],
            [mine.picture_id, sisters.picture_id],
        ):
            outcome = await send_message.invoke(
                {"what": "看这个", "channel_id": str(_DM), "pictures": handles}
            )
            assert isinstance(outcome, dict), (
                f"{handles} 没被挡下来。拿到：{outcome!r}"
            )

    assert spoken == [], f"引用了不属于她的图，还是发出去了 {len(spoken)} 条"
    assert await latest_outbound(lane=LANE, moment_id=_MOMENT) is None, (
        "被挡下的那次占住了认领 —— 她这一缝里换成自己那张图都发不成了"
    )
    assert voice.runs == [], "被挡下的那次还白花了一次渲染"


@pytest.mark.integration
async def test_a_refused_picture_does_not_stop_her_from_sending_the_right_one(
    mouth_db, in_a_moment, spoken, voice, a_picture
):
    """挡下来是拒这一次，不是把这一缝判死。"""
    mine = await a_picture(file_name="temp/tos_mine.jpg")
    sisters = await a_picture(file_name="temp/tos_ayana.jpg", persona_id="ayana")
    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="翻胶片", noted_at=_at(21),
    )

    async with in_a_moment("akao", moment_id=_MOMENT):
        await send_message.invoke(
            {"what": "看这个", "channel_id": str(_DM), "pictures": [sisters.picture_id]}
        )
        outcome = await send_message.invoke(
            {"what": "看这个", "channel_id": str(_DM), "pictures": [mine.picture_id]}
        )

    assert len(spoken) == 1, f"换成自己那张也发不出去了：{spoken}"
    assert spoken[0].picture_file_names == ["temp/tos_mine.jpg"]
    assert isinstance(outcome, str) and "发出去了" in outcome, outcome


@pytest.mark.integration
async def test_the_handle_she_copies_off_her_own_list_is_the_one_this_accepts(
    mouth_db, in_a_moment, spoken, voice, a_picture
):
    """印出去的那串和认回来的那串必须是同一串。

    看图那边印给她的是 ``pic=<id>``（:func:`app.living.pictures._handle_of`），它的
    docstring 反复对她说"原样抄回来"。两边各写一遍解析的话，印的是 ``pic=<id>``、认
    的是裸 id，她照做就撞死路 —— ``read_a_bit`` 在 coe-living 上实测踩过这个坑，而这
    次她收到的还不是"没找到"，是"这不是你做过的图"，等于系统告诉她自己的记录是假的。
    """
    from app.living.pictures import _handle_of

    pic = await a_picture(file_name="temp/tos_matcha_cat.jpg")
    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="翻胶片", noted_at=_at(21),
    )

    copied = _handle_of(pic)
    assert copied.startswith("pic="), f"这条用例钉的形状变了：{copied!r}"

    async with in_a_moment("akao"):
        outcome = await send_message.invoke(
            {"what": "给他看这只猫", "channel_id": str(_DM), "pictures": [copied]}
        )

    assert isinstance(outcome, str) and "发出去了" in outcome, (
        f"她照抄清单上那串，发不出去：{outcome!r}"
    )
    (segment,) = spoken
    assert segment.picture_file_names == ["temp/tos_matcha_cat.jpg"]


@pytest.mark.integration
async def test_the_words_she_says_can_never_impersonate_a_picture(
    mouth_db, in_a_moment, spoken, voice, a_picture
):
    """正文和图在发送身份里必须分得开。

    seed 是把几段拼起来再取 uuid5 的。图那几段直接追加在正文后面的话，"说这句话配
    这张图"和"说的话本身正好长得像那句话加那张图"算出同一串 —— 后发那条被当成重放
    挡掉，真人什么都收不到，而她以为发了。
    """
    pic = await a_picture(file_name="temp/tos_cat.jpg")
    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="翻胶片", noted_at=_at(21),
    )

    async with in_a_moment("akao", moment_id=_MOMENT):
        await send_message.invoke(
            {"what": "看这个", "channel_id": str(_DM), "pictures": [pic.picture_id]}
        )
        await send_message.invoke(
            {
                "what": f"看这个\x1f{pic.file_name}",
                "channel_id": str(_DM),
            }
        )

    assert len(spoken) == 2, (
        f"两条不同的消息被算成了同一条：{[s.content for s in spoken]}"
    )
