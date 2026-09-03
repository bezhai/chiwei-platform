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


@pytest.fixture
async def mouth_db(living_db):
    """两个 bot 身份、一条私聊、一个群、一个不是她的群 —— 嘴要发的地址都在这儿。"""
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
def voice(monkeypatch):
    """把渲染那一步换成替身：拿到什么、吐出什么，都由用例说了算。"""
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
    """第一版是单次渲染，不做对话窗口自主权 —— 签名里不该有"接着上一条"的位置。"""
    props = set(send_message.definition.parameters["properties"])
    assert props == {"what", "channel_id"}, props


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
