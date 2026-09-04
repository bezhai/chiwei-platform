"""她把自己主动说出去的话撤回来。

五条硬边界，这个文件逐条钉：

  * **只能撤自己的。** 认领表上按 ``lane`` + ``persona_id`` 找，别人那条链、别的泳道
    那条链一律不在候选里。
  * **她指的是编号，不是内容。** 快照里她发出去的每条消息后面带着它的编号（``mouth:``
    之后那串 ``outbound_id``），撤回收的就是那串、按等值找。**印出去的和收进来的必须
    是同一个东西** —— 两边各拼一份就会漂移，而漂移的表现是她照抄了却撤不掉。
  * **先把撤回交出去，成功之后才写台账。** 顺序反了的话，台账写下了而撤回没发出去
    —— 她以为自己撤过了，那条消息还在群里。
  * **交出去之后不许再说"没撤成"。** 台账写不下只是这条记录不完整，撤回请求已经在路
    上了；报成失败她会重发一条撤回（无害）或者更糟：以为那句话还在。
  * **撤回是另一件发生的事。** 她说过就是说过 —— 不改那条 speech 记录，单独落一条。
"""
from __future__ import annotations

import datetime as dt
import re
import uuid

import pytest
from sqlalchemy import text

from app.data import session as session_mod
from app.living.happening import record_happening
from app.living.mouth import (
    STATE_CLAIMED,
    STATE_HANDED_OFF,
    SpokenOutbound,
    latest_outbound,
    send_message,
)
from app.living.records import KIND_ACT, KIND_SPEECH, MEDIUM_PHONE
from app.living.snapshot import read_snapshot, recent_own_happenings
from app.living.takeback import take_back_message
from app.living.whereabouts import note_whereabouts
from app.runtime.persist import insert_append, select_all_versions, select_latest

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))

_AKAO_BOT_UID = uuid.uuid5(uuid.NAMESPACE_OID, "bot-akao-common-user")
_BEZHAI = uuid.uuid5(uuid.NAMESPACE_OID, "human-bezhai")
_DM = uuid.uuid5(uuid.NAMESPACE_OID, "conv-dm-bezhai-akao")
_GROUP = uuid.uuid5(uuid.NAMESPACE_OID, "conv-group-lab")

# ``in_a_moment`` 的默认缝：下面几条用例要在缝**外面**按 outbound_id 把台账捞回来。
_MOMENT = "2026-07-25T21:30+08:00"

_SAID = "你去过那家抹茶店吗？我最近老想去。"


def _at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 7, 25, hour, minute, tzinfo=_CST)


@pytest.fixture
async def takeback_db(living_db):
    """她的 bot 身份、一条私聊、一个群 —— 撤回要认出渠道就得从这儿查。

    顺带建上 ``LooseEnd``：下面那条「快照印出来的编号能撤掉那一条」要真的读一次
    快照，而快照第二层读的就是它。
    """
    from app.living.loose_ends import LooseEnd
    from tests.runtime.conftest import migrate

    await migrate(LooseEnd, living_db)
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
def recalls(monkeypatch):
    """接住撤回请求，不真的投 MQ。"""
    from app.living import takeback as takeback_mod

    out: list = []

    async def fake_emit(data):
        out.append(data)

    monkeypatch.setattr(takeback_mod, "emit", fake_emit)
    return out


@pytest.fixture
def her_mouth(monkeypatch):
    """让 :func:`app.living.mouth.send_message` 真的跑完，但不碰模型、不投 MQ。

    只有这一条用例需要它：那条用例要的是**真的走一遍她开口那条路**，``happening_id``
    由嘴自己拼、快照自己解析、撤回自己收 —— 中间任何一处各写一份拼接规则，它就红。
    """
    from types import SimpleNamespace

    from app.agent.neutral import Message, Role
    from app.capabilities.output_safety import OutputVerdict
    from app.living import mouth as mouth_mod

    sent: list = []

    async def fake_emit(data):
        sent.append(data)

    class FakeVoice:
        async def run(self, messages, **kwargs):
            return Message(role=Role.ASSISTANT, content=_SAID)

    async def fake_audit(said: str, *, timeout_s: float | None = None):
        return OutputVerdict(ok=True)

    async def fake_find_persona(persona_id: str):
        return SimpleNamespace(display_name="赤尾", persona_core="")

    monkeypatch.setattr(mouth_mod, "emit", fake_emit)
    monkeypatch.setattr(mouth_mod, "build_voice_runner", lambda: FakeVoice())
    monkeypatch.setattr(mouth_mod, "audit_output", fake_audit)
    monkeypatch.setattr(mouth_mod, "find_persona", fake_find_persona)
    return sent


async def _she_spoke(
    *,
    said: str = _SAID,
    at: dt.datetime | None = None,
    persona_id: str = "akao",
    lane: str = LANE,
    channel_id: uuid.UUID = _DM,
    state: str = STATE_HANDED_OFF,
) -> SpokenOutbound:
    """认领表上放一条她开过口的记录（v1）—— 就是她能撤的那种东西。"""
    when = at or _at(21, 0)
    row = SpokenOutbound(
        lane=lane,
        outbound_id=uuid.uuid5(
            uuid.NAMESPACE_OID, f"{lane}|{persona_id}|{said}|{when.isoformat()}"
        ).hex,
        ver=1,
        persona_id=persona_id,
        channel_id=str(channel_id),
        moment_id=when.isoformat(timespec="minutes"),
        said=said,
        state=state,
        claimed_at=when,
        settled_at=when if state == STATE_HANDED_OFF else None,
    )
    assert await insert_append(row, expected_current_ver=0) == 1
    return row


async def _latest(row: SpokenOutbound) -> SpokenOutbound:
    got = await select_latest(
        SpokenOutbound, {"lane": row.lane, "outbound_id": row.outbound_id}
    )
    assert isinstance(got, SpokenOutbound)
    return got


def _refused(outcome, *, saying: list[str]) -> None:
    """这次调用是**工具自己判下来的一句如实说明**，不是别处炸了被吞成的错。

    ``@tool_error`` 把任何异常都转成同一个形状的 dict，而 SQLAlchemy 的报错正文里
    带着整条 SQL 和绑定参数 —— 只断言"她给的那串出现在 message 里"的话，一次查询
    崩掉也能满足它（实测：把 ``persona_id`` 那条硬条件改成一个类型推不出来的表达式，
    用例照样绿）。所以这里连异常类型一起钉：工具自己判下来的一律是 ``ValueError``。
    """
    assert isinstance(outcome, dict), f"该如实说一句，而不是照常返回。拿到：{outcome!r}"
    assert outcome["detail"]["original_error_type"] == "ValueError", (
        f"这不是工具判下来的，是别处炸了被吞成一个 dict。拿到：{outcome!r}"
    )
    for line in saying:
        assert line in outcome["message"], (
            f"没说清楚「{line}」。拿到：{outcome['message']!r}"
        )


async def _somebody_said_something(*, at: dt.datetime) -> None:
    """真人在那条私聊里说了一句 —— 她开口那条路要求这条会话在她视野里。

    撤回不要它（那条路走的是未过滤的可达性），所以这个夹具只给真的要 ``send_message``
    的那条用例用，别塞进 ``takeback_db``：本文件其余用例正好跑在"这条会话不在名单里"
    的状态上，那是撤回不跟随白名单的实证。
    """
    async with session_mod.get_session() as s:
        await s.execute(
            text(
                "INSERT INTO common_message (common_message_id, channel,"
                " common_conversation_id, common_user_id, sender_display_name,"
                " role, content, content_text, scope, event_time)"
                " VALUES (CAST(:m AS uuid), 'lark', CAST(:c AS uuid),"
                " CAST(:u AS uuid), 'bezhai', 'user', CAST(:body AS jsonb), '在吗',"
                " 'direct', :at)"
            ),
            {
                "m": str(uuid.uuid4()),
                "c": str(_DM),
                "u": str(_BEZHAI),
                "body": '[{"kind": "text", "text": "\u5728\u5417"}]',
                "at": int(at.timestamp() * 1000),
            },
        )


async def _she_is_home() -> None:
    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="翻胶片", noted_at=_at(21),
    )


# --------------------------------------------------------------------------
# 一 · 只能撤自己的
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_she_cannot_take_back_a_line_that_is_not_hers(
    takeback_db, in_a_moment, recalls
):
    """姐姐那条链的编号、别的泳道那条链的编号，都不是她能撤的东西。

    认领表是三个人共用的一张表。``lane`` + ``persona_id`` 是硬条件，不是过滤优化：
    少了它们，一个从别处拿到的编号就能撤掉姐姐那条链 —— 那是替另一个人做决定，而且
    她自己一个字都感知不到。
    """
    await _she_is_home()
    sister = await _she_spoke(persona_id="ayana")
    elsewhere = await _she_spoke(lane="coe-other")

    async with in_a_moment("akao", moment_id=_MOMENT):
        for line in (sister, elsewhere):
            outcome = await take_back_message.invoke(
                {"message_id": line.outbound_id}
            )
            _refused(outcome, saying=[line.outbound_id])

    assert recalls == [], (
        f"撤了一条不是她的话 —— 编号等值查找少了 lane / persona 那两条硬条件。"
        f"发出去 {len(recalls)} 条"
    )
    assert (await _latest(sister)).took_back_at is None
    assert (await _latest(elsewhere)).took_back_at is None


@pytest.mark.integration
async def test_a_handle_she_made_up_matches_nothing_and_it_says_so(
    takeback_db, in_a_moment, recalls
):
    """编号对不上时如实说没有这条 —— 不猜、不挑一条最近的顶上。

    对不上的原因有好几种（她编的、别人的、当面说的话根本没有编号），这一刻分不出是
    哪种，所以只说这一件确定的事：没有这条。
    """
    await _she_is_home()
    hers = await _she_spoke()
    made_up = uuid.uuid5(uuid.NAMESPACE_OID, "she-made-this-up").hex

    async with in_a_moment("akao", moment_id=_MOMENT):
        outcome = await take_back_message.invoke({"message_id": made_up})

    assert recalls == [], (
        f"编号对不上却撤了一条 —— 撤掉的是她没想撤的话。发出去 {len(recalls)} 条"
    )
    _refused(outcome, saying=[made_up])
    assert (await _latest(hers)).took_back_at is None


# --------------------------------------------------------------------------
# 二 · 印出去的那串就是她能撤的那串
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_handle_the_snapshot_showed_her_takes_back_that_message(
    takeback_db, in_a_moment, recalls, her_mouth
):
    """整条设计成立的那一条：她照抄快照印出来那串，撤掉的就是那条消息。

    这里走的是真的那条路 —— 嘴发一条（``happening_id`` 由 :mod:`app.living.mouth`
    自己拼）、快照把编号印出来（:mod:`app.living.snapshot` 自己解析）、撤回按等值
    收（:mod:`app.living.takeback` 自己查）。三处任何一处各写一份拼接规则，这条就红。
    """
    await _she_is_home()
    # 她要**发**这一条，所以这条私聊得在她视野里 —— 撤回不过白名单闸，开口过
    # （:mod:`app.living.whitelist`）。真人刚说过一句就够了。
    await _somebody_said_something(at=_at(21, 0))

    async with in_a_moment("akao", moment_id=_MOMENT):
        said = await send_message.invoke(
            {"what": "问问他抹茶店去过没", "channel_id": str(_DM)}
        )
        assert isinstance(said, str), f"她这条没发出去，后面无从谈起。拿到：{said!r}"

        shown = await read_snapshot(
            lane=LANE, persona_id="akao", after_seq=0, now=_at(21, 30)
        )
        rendered = shown.render()
        handles = re.findall(r"［([^］]+)］", rendered)
        assert len(handles) == 1, (
            f"快照没把她刚发出去那条的编号印出来（或者印了不止一个）—— "
            f"她手上没有指得动的东西。拿到：\n{rendered}"
        )

        outcome = await take_back_message.invoke({"message_id": handles[0]})

    assert isinstance(outcome, str), (
        f"照抄快照印出来那串却撤不掉 —— 印出去的和收进来的不是同一个东西。"
        f"拿到：{outcome!r}"
    )
    spoke = await latest_outbound(lane=LANE, moment_id=_MOMENT)
    assert spoke is not None
    assert handles[0] == spoke.outbound_id, (
        f"快照印出去的那串不是撤回要用的那个键。印的 {handles[0]!r}，"
        f"要的 {spoke.outbound_id!r}"
    )
    (recall,) = recalls
    assert recall.outbound_id == spoke.outbound_id
    assert spoke.took_back_at == _at(21, 30)


@pytest.mark.integration
async def test_two_lines_word_for_word_the_same_are_told_apart_by_the_handle(
    takeback_db, in_a_moment, recalls
):
    """同一句话说过两遍，编号仍然指得出是哪一次 —— 这是换成编号的全部理由。

    按内容找的时候这一格是死路：两条逐字相同，只能连着时刻摊开让她再挑一次。
    """
    await _she_is_home()
    earlier = await _she_spoke(at=_at(20, 10))
    later = await _she_spoke(at=_at(21, 0))
    assert earlier.said == later.said and earlier.outbound_id != later.outbound_id

    async with in_a_moment("akao", moment_id=_MOMENT):
        outcome = await take_back_message.invoke({"message_id": earlier.outbound_id})

    assert isinstance(outcome, str), f"逐字相同的两条把它难住了。拿到：{outcome!r}"
    (recall,) = recalls
    assert recall.outbound_id == earlier.outbound_id, (
        f"撤的不是她指的那一次。拿到：{recall.outbound_id!r}"
    )
    assert (await _latest(earlier)).took_back_at == _at(21, 30)
    assert (await _latest(later)).took_back_at is None, (
        "把另一次也一起撤了 —— 那条她没想撤"
    )


# --------------------------------------------------------------------------
# 三 · 撤这一步的顺序和失败语义
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_recall_points_at_that_one_utterance_and_nothing_else(
    takeback_db, in_a_moment, recalls
):
    """撤回按 ``outbound_id`` 指她那次开口，**不带 session_id / 触发消息**。

    她主动开口既没有会话、也没有来源消息。硬填一个假值的后果是静默的：投递侧拿它
    去查台账、查不到、退避重投三次、进死信，一个渠道接口都不会调，那条消息安安静静
    留在群里。
    """
    await _she_is_home()
    spoke = await _she_spoke()

    async with in_a_moment("akao", moment_id=_MOMENT):
        outcome = await take_back_message.invoke({"message_id": spoke.outbound_id})

    assert isinstance(outcome, str), f"撤回不是工具坏了。拿到：{outcome!r}"
    (recall,) = recalls
    assert recall.outbound_id == spoke.outbound_id, (
        f"指的不是她那次开口。拿到：{recall.outbound_id!r}"
    )
    assert recall.session_id is None, "她主动开口没有会话，不许伪造一个"
    assert recall.trigger_message_id is None, "主动开口没有来源消息，不许伪造一条"
    assert recall.chat_id == str(_DM)
    assert recall.channel == "lark", (
        f"channel 决定撤回投哪条队列，猜错就投到别的渠道去了。拿到：{recall.channel!r}"
    )
    assert recall.lane == LANE, "sink 拿 lane 当 outbound_context 的 fallback，必须带"


@pytest.mark.integration
async def test_a_message_whose_delivery_was_never_confirmed_can_still_be_taken_back(
    takeback_db, in_a_moment, recalls
):
    """``claimed``（结果未知）那条也能撤 —— 未知意味着**可能已经在真人眼前**。

    按状态把它挡掉的话，恰恰是最该能撤的那一条撤不了。
    """
    await _she_is_home()
    spoke = await _she_spoke(state=STATE_CLAIMED)

    async with in_a_moment("akao", moment_id=_MOMENT):
        await take_back_message.invoke({"message_id": spoke.outbound_id})

    (recall,) = recalls
    assert recall.outbound_id == spoke.outbound_id, (
        "结果未知那条撤不掉 —— 而它恰恰可能已经送到真人手上了"
    )


@pytest.mark.integration
async def test_the_ledger_is_written_only_after_the_recall_is_handed_off(
    takeback_db, in_a_moment, monkeypatch
):
    """顺序钉死：先把撤回交出去，成功之后才写台账。

    反过来的话，台账写下了而撤回没发出去 —— 她下一缝看台账以为自己撤过了，那句话
    还好端端留在会话里。
    """
    from app.living import takeback as takeback_mod

    await _she_is_home()
    spoke = await _she_spoke()
    seen_at_handoff: list = []

    async def emit_and_look(data):
        seen_at_handoff.append((await _latest(spoke)).took_back_at)

    monkeypatch.setattr(takeback_mod, "emit", emit_and_look)

    async with in_a_moment("akao", moment_id=_MOMENT):
        await take_back_message.invoke({"message_id": spoke.outbound_id})

    assert seen_at_handoff == [None], (
        f"交出去之前台账就写下了 —— 撤回要是没发出去，她会以为自己撤过了。"
        f"拿到：{seen_at_handoff!r}"
    )
    assert (await _latest(spoke)).took_back_at == _at(21, 30), (
        "撤回交出去了，台账却没记下她按下撤回那一刻"
    )


@pytest.mark.integration
async def test_the_ledger_records_when_she_pressed_it_without_touching_the_other_axes(
    takeback_db, in_a_moment, recalls
):
    """``took_back_at`` 是独立一根轴：认领状态和落地那两列一个字都不动。"""
    await _she_is_home()
    spoke = await _she_spoke()

    async with in_a_moment("akao", moment_id=_MOMENT):
        await take_back_message.invoke({"message_id": spoke.outbound_id})

    row = await _latest(spoke)
    assert row.took_back_at == _at(21, 30), (
        f"记的不是她按下撤回那一刻（这一缝的时间锚）。拿到：{row.took_back_at!r}"
    )
    assert row.recalled_at is None, (
        "渠道那边还没撤呢 —— 这一列是对账钟补的，工具不许替它填"
    )
    assert row.state == STATE_HANDED_OFF, (
        f"撤回把认领状态改了 —— 那是另一根轴。拿到：{row.state!r}"
    )
    versions = await select_all_versions(
        SpokenOutbound, {"lane": LANE, "outbound_id": spoke.outbound_id}
    )
    assert [v.ver for v in versions] == [1, 2], (
        f"撤回没有 append 一版（或者写了两版）。拿到：{[v.ver for v in versions]}"
    )


@pytest.mark.integration
async def test_a_recall_that_never_went_out_leaves_the_ledger_alone(
    takeback_db, in_a_moment, monkeypatch
):
    """撤回没交出去就不写台账 —— 她可以再撤一次。

    **跟她开口那侧的判法相反，这是刻意的。** 那边 ``emit`` 抛错不重发，因为重复发一
    条消息会打扰真人；撤回重复投是幂等的（删一条已经删掉的消息，真人一个字都看不到），
    所以这边把"没撤成"如实说出来、台账留空，她想再撤就再撤。
    """
    from app.living import takeback as takeback_mod

    async def boom(_data):
        raise RuntimeError("MQ 连不上")

    monkeypatch.setattr(takeback_mod, "emit", boom)
    await _she_is_home()
    spoke = await _she_spoke()

    async with in_a_moment("akao", moment_id=_MOMENT):
        outcome = await take_back_message.invoke({"message_id": spoke.outbound_id})

    assert isinstance(outcome, dict), (
        f"撤回没送出去却报成正常返回 —— 她以为撤掉了。拿到：{outcome!r}"
    )
    row = await _latest(spoke)
    assert row.took_back_at is None, (
        "撤回没交出去却记了一笔"
        "「她撤过了」—— 台账从此说着一件没发生的事，而那条消息还在群里"
    )
    assert (
        len(
            await select_all_versions(
                SpokenOutbound, {"lane": LANE, "outbound_id": spoke.outbound_id}
            )
        )
        == 1
    )
    assert await recent_own_happenings(lane=LANE, persona_id="akao") == [], (
        "撤回没发出去却落成了记忆 —— 她下一缝会以为自己撤过了"
    )


@pytest.mark.integration
async def test_a_ledger_write_that_fails_after_the_handoff_is_not_reported_as_a_failure(
    takeback_db, in_a_moment, recalls, monkeypatch
):
    """撤回已经在路上了，这时候告诉她"没撤成"是假话。

    台账写不下的后果是这条记录不完整（可查、可补）；报成失败的后果是她相信那句话还
    留在会话里 —— 后者严重得多。
    """
    from app.living import takeback as takeback_mod

    async def boom(*_a, **_kw):
        raise RuntimeError("库连不上")

    monkeypatch.setattr(takeback_mod, "insert_append", boom)
    await _she_is_home()
    spoke = await _she_spoke()

    async with in_a_moment("akao", moment_id=_MOMENT):
        outcome = await take_back_message.invoke({"message_id": spoke.outbound_id})

    assert len(recalls) == 1, "撤回本身该照常交出去"
    assert isinstance(outcome, str), (
        f"台账写不下被报成了撤回失败 —— 而撤回请求已经发出去了。拿到：{outcome!r}"
    )


@pytest.mark.integration
async def test_losing_the_version_race_merges_onto_the_latest_version(
    takeback_db, in_a_moment, recalls, monkeypatch
):
    """对账钟抢在写台账之前追了一版 —— 撤回那根轴要合到那一版上，不是把它抹回 NULL。

    这条链上有三个写者（她开口那条路径、对账钟、撤回），撞版本是常态。输了就重读
    最新一版、把自己那根轴合上去 —— 拿手里那份过期的重写，会把对账已经补上的落地
    标识抹掉，而那是已知事实。
    """
    from app.living import takeback as takeback_mod

    await _she_is_home()
    spoke = await _she_spoke()
    landed_id = str(uuid.uuid5(uuid.NAMESPACE_OID, "the-channel-wrote-this-row"))
    real_record_happening = takeback_mod.record_happening

    async def the_clock_ticks_first(**kw):
        """记忆那一步之后、写台账之前：对账钟追了一版上去。"""
        await real_record_happening(**kw)
        current = await _latest(spoke)
        assert (
            await insert_append(
                SpokenOutbound(
                    **{
                        **current.model_dump(),
                        "landed_common_message_id": landed_id,
                        "landed_at": _at(21, 1),
                    }
                ),
                expected_current_ver=current.ver,
            )
            == 1
        )

    monkeypatch.setattr(takeback_mod, "record_happening", the_clock_ticks_first)

    async with in_a_moment("akao", moment_id=_MOMENT):
        outcome = await take_back_message.invoke({"message_id": spoke.outbound_id})

    assert isinstance(outcome, str), f"撞版本不是工具坏了。拿到：{outcome!r}"
    row = await _latest(spoke)
    assert row.took_back_at == _at(21, 30), (
        f"CAS 输了就放掉了 —— 台账永远不知道她撤过。拿到：{row}"
    )
    assert row.landed_common_message_id == landed_id, (
        "把对账补上的落地标识抹回 NULL 了 —— 那一版记的是已知事实"
    )
    assert row.landed_at == _at(21, 1), "落地时刻被撤回顺手改了"
    versions = await select_all_versions(
        SpokenOutbound, {"lane": LANE, "outbound_id": spoke.outbound_id}
    )
    assert [v.ver for v in versions] == [1, 2, 3], (
        f"版本链不是「认领 → 对账 → 撤回」三版："
        f"{[(v.ver, v.took_back_at, v.landed_common_message_id) for v in versions]}"
    )


# --------------------------------------------------------------------------
# 四 · 撤回是另一件发生的事
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_taking_it_back_lands_as_its_own_happening_and_leaves_the_old_one_alone(
    takeback_db, in_a_moment, recalls
):
    """她说过就是说过。撤回是另一件发生的事，不是把那条记忆改掉。

    改历史还会破坏 ``seq`` 的连续前缀：读侧的游标推到"本次读到的最大 seq"，被改写的
    那一行不会因此再被读一遍。
    """
    from app.living.records import OUTBOUND_HAPPENING_PREFIX

    await _she_is_home()
    spoke = await _she_spoke()
    await record_happening(
        lane=LANE,
        happening_id=f"{OUTBOUND_HAPPENING_PREFIX}{spoke.outbound_id}",
        actor="akao",
        place="家/我房间",
        kind=KIND_SPEECH,
        content=_SAID,
        occurred_at=_at(21, 0),
        audience=(),
        medium=MEDIUM_PHONE,
        channel_id=str(_DM),
    )

    async with in_a_moment("akao", moment_id=_MOMENT):
        await take_back_message.invoke({"message_id": spoke.outbound_id})

    said, took_back = await recent_own_happenings(lane=LANE, persona_id="akao")
    assert said.kind == KIND_SPEECH and said.content == _SAID, (
        f"她说那句话的那条记忆被改写了 —— 说过就是说过。拿到：{said}"
    )
    assert took_back.kind == KIND_ACT, (
        f"撤回是她做的一个动作，不是又说了一句话。拿到：{took_back.kind!r}"
    )
    assert _SAID in took_back.content, (
        f"没说清楚她撤的是哪一句 —— 下一缝她自己看不出来。拿到：{took_back.content!r}"
    )
    assert took_back.occurred_at == _at(21, 30), (
        f"落的不是她请求撤回那一刻。拿到：{took_back.occurred_at!r}"
    )
    assert took_back.channel_id == str(_DM), (
        "不带会话，下一缝她分不清撤的是哪条会话上的话"
    )
    assert took_back.medium == MEDIUM_PHONE, (
        "撤回是在手机上按的 —— 落成当面做的动作，坐她旁边的姐姐就看见了"
    )


@pytest.mark.integration
async def test_the_happening_does_not_claim_the_message_is_already_gone(
    takeback_db, in_a_moment, recalls
):
    """落记忆的这一刻还不知道撤没撤掉 —— 措辞不能替渠道宣布结果。"""
    await _she_is_home()
    spoke = await _she_spoke()

    async with in_a_moment("akao", moment_id=_MOMENT):
        await take_back_message.invoke({"message_id": spoke.outbound_id})

    (took_back,) = await recent_own_happenings(lane=LANE, persona_id="akao")
    for lie in ("撤掉了", "已撤回", "撤回成功"):
        assert lie not in took_back.content, (
            f"「{lie}」是编的 —— 这一刻渠道那边还没动。拿到：{took_back.content!r}"
        )


# --------------------------------------------------------------------------
# 五 · 结果得等下一次
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_she_is_told_the_outcome_comes_later_not_that_it_is_already_gone(
    takeback_db, in_a_moment, recalls
):
    """撤回是异步的：工具返回时结果还没回来。

    为了给她一个当场的答案去同步等，就是把一缝卡在网络上。所以如实说"去撤了"，撤没
    撤掉她下次拿起手机自己看得见 —— 撤掉了的那条就不在会话历史里了。
    """
    await _she_is_home()
    spoke = await _she_spoke()

    async with in_a_moment("akao", moment_id=_MOMENT):
        outcome = await take_back_message.invoke({"message_id": spoke.outbound_id})

    assert isinstance(outcome, str)
    assert _SAID in outcome, "该让她看到自己撤的是哪句话"
    for lie in ("撤掉了", "已撤回", "撤回成功"):
        assert lie not in outcome, (
            f"「{lie}」是编的 —— 这一缝还不知道结果。拿到：{outcome!r}"
        )
    assert "手机" in outcome, (
        f"没告诉她结果去哪儿看 —— 她下次拿起手机才知道撤没撤掉。拿到：{outcome!r}"
    )


# --------------------------------------------------------------------------
# 六 · 这只手真的在她手上
# --------------------------------------------------------------------------


def test_the_take_back_hand_is_in_her_hands():
    from app.living.moment import MOMENT_TOOLS

    assert take_back_message in MOMENT_TOOLS, "她手里没有撤回这只手"


def test_taking_something_back_takes_the_message_id_she_was_shown():
    """签名里只有那条消息的编号：没有原话、没有 channel_id、没有时刻。

    收内容就要在逻辑层猜她指的是哪一句；编号是快照直接印在她眼前的，照抄就行。
    """
    props = set(take_back_message.definition.parameters["properties"])
    assert props == {"message_id"}, props


# --------------------------------------------------------------------------
# 六 · 撤回**刻意不过**会话白名单那道闸
# --------------------------------------------------------------------------
#
# 她撤的是自己已经发出去的话，用的是自己那张认领表上的编号（模型编不出别人的）——
# 跟"她现在还能不能看见那条会话"是两件事。一条发出去时在名单里、之后掉出名单的消息
# 撤不回来，是纯粹的倒退：那句话还挂在真人眼前，而她眼睁睁没办法。
#
# 所以撤回走的是**未过滤**的可达性（bot 还在不在那个会话里），它也是全项目唯一一条
# 走那份的路（门禁在 ``test_phone.py`` 那条源码检查上）。
#
# 上面那几节的用例其实全都跑在"这条会话不在名单里"的状态上（``takeback_db`` 一条
# 消息都没种），这里再把三种情况明写出来，免得哪天有人把撤回接回主闸还全绿。


@pytest.mark.integration
async def test_she_can_take_back_a_line_in_a_conversation_out_of_sight(
    takeback_db, in_a_moment, recalls
):
    """掉出白名单、但 bot 还在那个会话里 —— 撤得了。"""
    from app.living.phone import reachable_conversations

    line = await _she_spoke()
    await _she_is_home()

    assert await reachable_conversations(persona_id="akao", now=_at(21, 30)) == [], (
        "用例前提没成立：这条会话本来就该在名单外"
    )

    async with in_a_moment("akao"):
        outcome = await take_back_message.invoke({"message_id": line.outbound_id})

    assert not isinstance(outcome, dict), f"名单外就撤不了了。拿到：{outcome!r}"
    assert len(recalls) == 1
    assert recalls[0].chat_id == str(_DM)


@pytest.mark.integration
async def test_she_cannot_take_back_a_line_from_a_conversation_her_bot_left(
    takeback_db, in_a_moment, recalls
):
    """bot 已经被移出那个会话 —— 撤不了，如实说。

    这一条跟上面那条的区别就是撤回该认的那条判据：``channel`` 从哪儿来、这条撤回还
    做不做得成，靠的是 bot 在不在，不是这条会话在不在她视野里。
    """
    line = await _she_spoke()
    await _she_is_home()
    async with session_mod.get_session() as s:
        await s.execute(
            text(
                "DELETE FROM common_bot_presence"
                " WHERE common_conversation_id = CAST(:c AS uuid)"
            ),
            {"c": str(_DM)},
        )

    async with in_a_moment("akao"):
        outcome = await take_back_message.invoke({"message_id": line.outbound_id})

    _refused(outcome, saying=["够不着"])
    assert recalls == []


@pytest.mark.integration
async def test_a_made_up_handle_takes_nothing_back(takeback_db, in_a_moment, recalls):
    """编出来的编号撤不掉任何东西 —— 认领表是那件事的唯一依据。

    撤回不过白名单闸，所以"她指的是自己说过的哪一句"这件事全靠那张表按等值找。这条
    在闸拆掉之后仍然是硬边界：编号对不上就是没有这条，绝不排个序取最近的顶上。
    """
    await _she_spoke()
    await _she_is_home()

    async with in_a_moment("akao"):
        outcome = await take_back_message.invoke({"message_id": uuid.uuid4().hex})

    _refused(outcome, saying=["你没有编号"])
    assert recalls == []
