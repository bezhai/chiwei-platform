"""强提醒可以提前一轮，但不代她回复。

私聊来了、群里被点名 → 她被带到那一刻、看得到信封。**回不回是她的输出**，不进验收
条件；所以这里必须同时反证相反方向：存在被 @ 之后她没开口、而系统一切正常的轮次。

只加一个"跳过间隔"的开关是不够的，三个坑各有一条用例：

  1. 提前的那一轮会成为"最近一轮"，把常规节奏往后推 —— 常规的间隔判断只认常规轮次；
  2. 同一分钟会跟常规轮次撞 ``moment_id`` —— 提前轮次的身份是**把她叫来的那条消息**，
     不是钟点，天然撞不上；
  3. 提前轮次看过的，常规轮次不该再看一遍 —— 传到她这里的消息逐条记看过，跟着那一轮落地。

顺带还有一条不是坑但会烧钱的：**同一条消息只提前一次**。真人手机是新消息才震，
躺着的未读不会一直震；提前轮次的身份就是那条消息，所以"只震一次"是结构，不是冷却。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.data.persist import fetch_all, fetch_value, from_row
from app.data.tx import current_session, tx
from app.host import Signal
from app.living.moment import (
    DEFAULT_LIFE_MOMENT_MINUTES,
    LIFE_MOMENT_PROMPT_ID,
    LifeMoment,
    latest_moment,
)
from app.living.whereabouts import note_whereabouts
from app.plugins.living import nudge, regular
from tests.living.conftest import (
    Calls,
    clock_at,
    her_queue,
    her_round_runs,
    nudged_round,
    regular_round,
    settled,
)

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))

_AKAO_BOT_UID = uuid.uuid5(uuid.NAMESPACE_OID, "bot-akao-common-user")
_BEZHAI = uuid.uuid5(uuid.NAMESPACE_OID, "human-bezhai")
_SOMEONE = uuid.uuid5(uuid.NAMESPACE_OID, "human-someone")
_DM = uuid.uuid5(uuid.NAMESPACE_OID, "conv-dm-bezhai-akao")
_GROUP = uuid.uuid5(uuid.NAMESPACE_OID, "conv-group-lab")

def _at(hour: int, minute: int = 0, second: int = 0) -> dt.datetime:
    return dt.datetime(2026, 7, 25, hour, minute, second, tzinfo=_CST)


def _ms(moment: dt.datetime) -> int:
    return int(moment.timestamp() * 1000)


@pytest.fixture
async def nudge_db(living_db):
    from app.living.loose_ends.threads import LooseEnd
    from app.living.nudge import NudgeBegun
    from tests.runtime.conftest import migrate

    for cls in (LooseEnd, LifeMoment, NudgeBegun):
        await migrate(cls, living_db)
    async with tx():
        s = current_session()
        await s.execute(
            text(
                "INSERT INTO common_user (common_user_id, channel, display_name) "
                "VALUES (CAST(:b AS uuid), 'lark', '赤尾'),"
                "       (CAST(:h AS uuid), 'lark', 'bezhai'),"
                "       (CAST(:o AS uuid), 'lark', '路人')"
            ),
            {"b": str(_AKAO_BOT_UID), "h": str(_BEZHAI), "o": str(_SOMEONE)},
        )
        for conv, scope, title in ((_DM, "direct", "bezhai"), (_GROUP, "group", "群")):
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


async def _incoming(
    conv: uuid.UUID,
    *,
    body: str,
    at: dt.datetime,
    sender: uuid.UUID = _BEZHAI,
    sender_name: str = "bezhai",
    names_bot: uuid.UUID | None = None,
    mention_unrecorded: bool = False,
    message_id: uuid.UUID | None = None,
) -> str:
    """``names_bot`` 写进 ``mentioned_common_user_ids``，不是往 ``content`` 里塞
    一条 mention item —— 公共层的内容契约没有那种片段（见 test_phone 的同名夹具）。

    ``mention_unrecorded`` 写 NULL：没人算过这条消息（存量行、QQ 的行）。
    ``message_id`` 不给就随机取一个。
    """
    items = [{"kind": "text", "text": body}]
    mid = message_id or uuid.uuid4()
    async with tx():
        s = current_session()
        await s.execute(
            text(
                "INSERT INTO common_message "
                "(common_message_id, channel, common_conversation_id, common_user_id,"
                " sender_display_name, role, content, content_text, scope, bot_name,"
                " event_time, mentioned_common_user_ids) "
                "VALUES (CAST(:m AS uuid), 'lark', CAST(:c AS uuid), CAST(:u AS uuid),"
                " :sn, 'user', CAST(:body AS jsonb), :txt, :sc, 'chiwei', :et,"
                " CAST(:named AS text[])::uuid[])"
            ),
            {
                "m": str(mid),
                "c": str(conv),
                "u": str(sender),
                "sn": sender_name,
                "body": json.dumps(items, ensure_ascii=False),
                "txt": body,
                "sc": "direct" if conv == _DM else "group",
                "et": _ms(at),
                "named": None if mention_unrecorded else (
                    [str(names_bot)] if names_bot else []
                ),
            },
        )
    return str(mid)


@pytest.fixture
def stub_life(monkeypatch, models):
    """她这一轮的模型替身（:class:`tests.living.conftest.Calls`）：她调了哪些手、最后说了什么由
    用例写死。``prompts`` 收的是**这一轮新摆到她眼前那条**，也就是最后一条：喂给模型的列表是
    "连续上下文 + 这一轮的刺激"（:func:`app.living.moment.run_due_moment`），取第一条会取到几个
    moment 之前的快照。``meanwhile`` 是模型那一步跑着的时候发生的事（比如又到了一条消息），
    ``after`` 是她调完工具之后发生的事（比如这一轮在她已经做了些什么之后失败）。"""
    from app.living import moment as moment_mod
    from app.living.persona import chain as persona_mod

    async def fake_find_persona(persona_id: str):
        return SimpleNamespace(display_name="赤尾", persona_core="她泡抹茶店。")

    monkeypatch.setattr(persona_mod, "find_persona", fake_find_persona)

    async def fixed_minutes() -> int:
        return DEFAULT_LIFE_MOMENT_MINUTES

    monkeypatch.setattr(moment_mod, "life_moment_minutes", fixed_minutes)

    fake = Calls()
    models[LIFE_MOMENT_PROMPT_ID] = fake.client
    return fake


async def _all_moments(persona_id: str = "akao") -> list[LifeMoment]:
    rows = await fetch_all(
        "SELECT * FROM data_life_moment WHERE lane = :l "
        "AND persona_id = :p ORDER BY began_at ASC",
        l=LANE,
        p=persona_id,
    )
    return [from_row(LifeMoment, r) for r in rows]


# --------------------------------------------------------------------------
# 一 · 她获得一次决策机会 —— 开不开口不进验收
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_direct_message_brings_her_to_that_moment(
    nudge_db, stub_life, resident
):
    await regular_round(resident, "akao", _at(21, 30))
    await _incoming(_DM, body="在吗，抹茶店那事", at=_at(21, 31))

    moment = await nudged_round(resident, "akao", _at(21, 32))

    assert moment is not None and moment.nudged is True
    envelope_seen = stub_life.prompts[-1]
    assert "bezhai" in envelope_seen, (
        f"她被带到了这一刻却看不到信封。她看到的是：\n{envelope_seen}"
    )
    assert "抹茶店那事" not in envelope_seen, (
        "信封漏了正文 —— 那她根本不需要拿起手机"
    )


@pytest.mark.integration
async def test_she_can_be_named_and_still_say_nothing_and_everything_is_fine(
    nudge_db, stub_life, resident
):
    """**必须反证的方向**：被 @ 之后她没开口，而系统一切正常。

    "被叫到"和"开口"离得太近，一不小心 @ 就又变成了回复开关。这条用例存在的
    唯一目的，就是让"她没回"成为一个**正常轮次**，而不是一个失败。
    """
    await regular_round(resident, "akao", _at(21, 30))
    await _incoming(
        _GROUP, body=" 你说呢", at=_at(21, 31), sender=_SOMEONE,
        sender_name="路人", names_bot=_AKAO_BOT_UID,
    )
    stub_life.said = "继续"  # 她看了一眼信封，没说话

    moment = await nudged_round(resident, "akao", _at(21, 32))

    assert moment is not None, "她连被带到那一刻的机会都没有"
    assert moment.switched is False and moment.recorded == 0
    assert moment.said == "继续"
    # 这一轮照常留痕、游标照常推进 —— 没开口不是异常状态。
    assert (await latest_moment(lane=LANE, persona_id="akao")).moment_id == \
        moment.moment_id


@pytest.mark.integration
async def test_group_chatter_that_does_not_name_her_waits_for_the_next_regular_moment(
    nudge_db, stub_life, pinned, resident
):
    # 群固定加白，所以这个群**在**她视野里：这条用例要验的是"不点名不提前、但下一个
    # 常规轮次看得见"，不是白名单挡没挡住它（:mod:`app.living.whitelist`）。
    pinned(str(_GROUP))
    await regular_round(resident, "akao", _at(21, 30))
    await _incoming(
        _GROUP, body="今天好热", at=_at(21, 31), sender=_SOMEONE, sender_name="路人"
    )

    assert await nudged_round(resident, "akao", _at(21, 32)) is None

    later = await regular_round(resident, "akao", _at(21, 40))
    assert later is not None
    assert "路人" in stub_life.prompts[-1], (
        f"不点名的消息不提前，但下一个常规轮次一定看得到。她看到的是：\n"
        f"{stub_life.prompts[-1]}"
    )


# --------------------------------------------------------------------------
# 二 · 坑 1：提前的那一轮不能把常规节奏往后推
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_an_early_moment_does_not_delay_the_regular_rhythm(
    nudge_db, stub_life, resident
):
    await regular_round(resident, "akao", _at(21, 30))
    await _incoming(_DM, body="在吗", at=_at(21, 33))
    early = await nudged_round(resident, "akao", _at(21, 34))
    assert early is not None

    # 21:40 是原本就该来的那一轮。按"最近一轮"判间隔的话，21:34 到 21:40 只有
    # 六分钟，这一轮会被吞掉 —— 她的节奏就被每一条私聊往后拖。
    on_time = await regular_round(resident, "akao", _at(21, 40))

    assert on_time is not None, "提前的那一轮把常规节奏往后推了"
    assert on_time.nudged is False
    assert [m.began_at for m in await _all_moments()] == [
        _at(21, 30), _at(21, 34), _at(21, 40)
    ]


# --------------------------------------------------------------------------
# 三 · 坑 2：同一分钟不能跟常规轮次撞身份
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_an_early_moment_in_the_same_minute_is_still_its_own_moment(
    nudge_db, stub_life, resident
):
    regular = await regular_round(resident, "akao", _at(21, 30))
    await _incoming(_DM, body="在吗", at=_at(21, 30, 10))

    early = await nudged_round(resident, "akao", _at(21, 30, 20))

    assert early is not None, (
        "提前轮次跟常规轮次撞了 moment_id —— 自然键相同，这一轮被当成重放丢掉了"
    )
    assert early.moment_id != regular.moment_id
    assert len(await _all_moments()) == 2


# --------------------------------------------------------------------------
# 四 · 坑 3：提前轮次看过的，常规轮次不再看一遍
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_regular_moment_does_not_show_again_what_the_early_one_showed(
    nudge_db, stub_life, named, resident
):
    from tests.living.conftest import receive

    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m0", place="家/客厅",
        doing="翻胶片", noted_at=_at(21, 20),
    )
    await regular_round(resident, "akao", _at(21, 30))
    await receive(_to_her("当面对你说：「姐我出门了。」", at=_at(21, 32), sender="绫奈"))
    await _incoming(_DM, body="在吗", at=_at(21, 33))

    early = await nudged_round(resident, "akao", _at(21, 34))
    assert early is not None
    assert "姐我出门了。" in stub_life.prompts[-1], "提前轮次没看到刚传到她这里的话"

    await regular_round(resident, "akao", _at(21, 40))

    assert "姐我出门了。" not in stub_life.prompts[-1], "同一句话她看了两遍"


# --------------------------------------------------------------------------
# 五 · 同一条消息只提前一次（新消息才震）
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_same_message_only_pulls_her_early_once(
    nudge_db, stub_life, resident
):
    await regular_round(resident, "akao", _at(21, 30))
    await _incoming(_DM, body="在吗", at=_at(21, 31))

    first = await nudged_round(resident, "akao", _at(21, 32))
    second = await nudged_round(resident, "akao", _at(21, 33))
    third = await nudged_round(resident, "akao", _at(21, 34))

    assert first is not None
    assert (second, third) == (None, None), (
        "她没看手机所以那条一直未读 —— 如果按「还有没有未读」判，她每分钟被震一次，"
        "一天一千多次模型调用"
    )
    assert len(await _all_moments()) == 2


@pytest.mark.integration
async def test_a_newer_message_pulls_her_early_again(nudge_db, stub_life, resident):
    await regular_round(resident, "akao", _at(21, 30))
    await _incoming(_DM, body="在吗", at=_at(21, 31))
    assert await nudged_round(resident, "akao", _at(21, 32)) is not None

    await _incoming(_DM, body="睡了？", at=_at(21, 35))

    assert await nudged_round(resident, "akao", _at(21, 36)) is not None
    assert len(await _all_moments()) == 3


@pytest.mark.integration
async def test_nothing_new_means_no_early_moment_at_all(nudge_db, stub_life, resident):
    await regular_round(resident, "akao", _at(21, 30))

    assert await nudged_round(resident, "akao", _at(21, 31)) is None
    assert len(await _all_moments()) == 1


# --------------------------------------------------------------------------
# 六 · 游标跟着一轮落地；信封不漏掉在叫她的那条
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_moment_that_blew_up_did_not_read_her_phone(
    nudge_db, stub_life, resident
):
    """这一轮崩了 = 她没看过 = 下一轮原样再看到。

    工具返回不等于她看见了。游标跟 ``LifeMoment`` 在同一个事务里落库，这一轮没落地就
    一条都不算已读 —— 宁可重看，不可漏看。
    """
    from app.living.phone import read_through

    await _incoming(_DM, body="在吗", at=_at(21, 31))
    stub_life.calls = [("look_at_phone", {"channel_id": str(_DM)})]

    async def blow_up(*_a, **_kw):
        raise RuntimeError("收尾那一步崩了")

    import app.living.moment as moment_mod

    original = moment_mod.insert_in_commit_order
    moment_mod.insert_in_commit_order = blow_up
    try:
        with pytest.raises(RuntimeError):
            await regular_round(resident, "akao", _at(21, 32))
    finally:
        moment_mod.insert_in_commit_order = original

    assert await read_through(
        lane=LANE, persona_id="akao", channel_id=str(_DM)
    ) == (0, ""), "这一轮没跑完，游标却推过去了 —— 那条消息就此永久消失"
    assert await _all_moments() == []


@pytest.mark.integration
async def test_a_finished_moment_did_read_her_phone(nudge_db, stub_life, resident):
    """反面：这一轮跑完了，看过的就是看过了。"""
    from app.living.phone import read_through

    await _incoming(_DM, body="在吗", at=_at(21, 31))
    stub_life.calls = [("look_at_phone", {"channel_id": str(_DM)})]

    moment = await regular_round(resident, "akao", _at(21, 32))

    assert moment is not None
    assert (
        await read_through(lane=LANE, persona_id="akao", channel_id=str(_DM))
    )[0] == _ms(_at(21, 31))


@pytest.mark.integration
async def test_both_people_waiting_on_her_are_in_the_envelope(
    nudge_db, stub_life, resident
):
    """一个轮询间隔里来了两条私聊 —— 提前的那一轮里两条都得在信封上。

    每拍只取**最新**那条召唤把她带过来，是有意的（一轮把她带到就够了，带两次是
    重复烧钱）。但那条更早的绝不能因此消失：她被带到的那一刻，两个人在等她这件事
    必须都摆在眼前，谁值得先回是**她**判。
    """
    await _incoming(_DM, body="在吗", at=_at(21, 31))
    await _incoming(
        _GROUP, body=" 你说呢", at=_at(21, 32), sender=_SOMEONE,
        sender_name="路人", names_bot=_AKAO_BOT_UID,
    )

    moment = await nudged_round(resident, "akao", _at(21, 33))

    assert moment is not None
    seen = stub_life.prompts[-1]
    assert str(_DM) in seen and str(_GROUP) in seen, (
        f"只有最新那条会话进了信封，先来的那个人她根本不知道在等她。拿到：\n{seen}"
    )


# --------------------------------------------------------------------------
# 七 · 传到她这里的消息也提前叫醒她，每条只叫一次
#
# world 告诉她察觉到了什么、姐妹直接对她说的话，存进她的收件箱（:mod:`app.living.received`）。
# 有她没看过的就提前叫醒她，跟手机上有人叫她同一套：那一轮的身份就是那条消息，跑过就是
# 跑过了。同一条重投一遍（world 补发没发完的告知）不是新消息，不再叫醒她。
# --------------------------------------------------------------------------


@pytest.fixture
def named(monkeypatch):
    """收件存在 :data:`LANE` 上；``receive`` 按读好的三姐妹名字认收件人（``tests.living.conftest``）。"""
    monkeypatch.setenv("LANE", LANE)


def _to_her(
    body: str,
    *,
    at: dt.datetime,
    sender: str = "world",
    wakes_her: bool = True,
    message_id: str | None = None,
):
    from app.messaging.message import Kind, new_message

    return new_message(
        sender=sender,
        recipient="赤尾",
        body=body,
        kind=Kind.MESSAGE,
        time=at,
        wakes_recipient=wakes_her,
        message_id=message_id,
    )


@pytest.mark.integration
async def test_a_received_message_brings_her_to_that_moment_once(
    nudge_db, stub_life, named, resident
):
    from tests.living.conftest import receive

    await regular_round(resident, "akao", _at(21, 30))
    rain = _to_her("窗外下起了雨。", at=_at(21, 31))
    await receive(rain)

    first = await nudged_round(resident, "akao", _at(21, 32))
    second = await nudged_round(resident, "akao", _at(21, 33))

    assert first is not None and first.nudged is True
    assert first.moment_id == f"nudge:inbox:{rain.message_id}", (
        "那一轮的身份不是叫醒她的那条消息"
    )
    assert "窗外下起了雨。" in stub_life.prompts[-1]
    assert second is None
    assert len(await _all_moments()) == 2


@pytest.mark.integration
async def test_a_woken_round_that_fails_is_the_same_moment_when_it_runs_again(
    nudge_db, stub_life, named, resident
):
    """叫醒她的那一轮失败了：那条还没看过，下一拍再叫醒她，而且还是同一个 moment。

    身份由那条消息定、不由钟点定，所以重跑时她这一轮里所有派生 id 原样对上，失败之前
    已经做了的事重放一遍写不出第二行（见 :class:`app.living.moment.DueMoment`）。
    """
    from tests.living.conftest import receive

    await regular_round(resident, "akao", _at(21, 30))
    rain = _to_her("窗外下起了雨。", at=_at(21, 31))
    await receive(rain)

    async def blow_up():
        raise RuntimeError("这一轮的模型调用失败了")

    stub_life.meanwhile = blow_up
    with pytest.raises(RuntimeError):
        await nudged_round(resident, "akao", _at(21, 32))
    stub_life.meanwhile = None
    again = await nudged_round(resident, "akao", _at(21, 33))

    assert again is not None and again.moment_id == f"nudge:inbox:{rain.message_id}"
    assert "窗外下起了雨。" in stub_life.prompts[-1], "失败那一轮看到的，重跑时没再给她看"


@pytest.mark.integration
async def test_the_same_message_delivered_again_does_not_wake_her_again(
    nudge_db, stub_life, named, resident
):
    from tests.living.conftest import receive

    await regular_round(resident, "akao", _at(21, 30))
    rain = _to_her("窗外下起了雨。", at=_at(21, 31))
    await receive(rain)
    assert await nudged_round(resident, "akao", _at(21, 32)) is not None

    await receive(rain)  # world 带着原来的 id 重发

    assert await nudged_round(resident, "akao", _at(21, 33)) is None
    assert len(await _all_moments()) == 2


@pytest.mark.integration
async def test_a_message_that_arrived_during_her_round_wakes_her_after_it(
    nudge_db, stub_life, named, resident
):
    """一轮跑着的时候到的那条没摆进那一轮，它还没叫醒过她：下一拍叫醒她一次。"""
    from tests.living.conftest import receive

    await regular_round(resident, "akao", _at(21, 30))
    rain = _to_her("窗外下起了雨。", at=_at(21, 31))
    doorbell = _to_her("楼下有人按门铃。", at=_at(21, 32))
    await receive(rain)

    async def it_arrives():
        await receive(doorbell)

    stub_life.meanwhile = it_arrives
    woke_for_rain = await nudged_round(resident, "akao", _at(21, 32))
    stub_life.meanwhile = None
    woke_for_doorbell = await nudged_round(resident, "akao", _at(21, 33))

    assert woke_for_rain is not None and rain.message_id in woke_for_rain.moment_id
    assert woke_for_doorbell is not None
    assert doorbell.message_id in woke_for_doorbell.moment_id
    assert "楼下有人按门铃。" in stub_life.prompts[-1]
    assert await nudged_round(resident, "akao", _at(21, 34)) is None


@pytest.mark.integration
async def test_a_phone_call_she_ignored_does_not_keep_a_received_message_from_waking_her(
    nudge_db, stub_life, named, resident
):
    """手机上那条私聊已经叫醒过她、她没看手机所以一直未读；之后收到的消息照样叫醒她。"""
    from tests.living.conftest import receive

    await regular_round(resident, "akao", _at(21, 30))
    await _incoming(_DM, body="在吗", at=_at(21, 31))
    assert await nudged_round(resident, "akao", _at(21, 32)) is not None

    await receive(_to_her("千凪在厨房喊你吃饭。", at=_at(21, 33)))
    moment = await nudged_round(resident, "akao", _at(21, 34))

    assert moment is not None, "一条躺着的未读私聊挡住了收件箱里的新消息"
    assert "千凪在厨房喊你吃饭。" in stub_life.prompts[-1]


# --------------------------------------------------------------------------
# 发件方说了不叫醒她的：不提前叫醒，照样在她下一轮里、看过就算看过
#
# 这一条只管"找谁来叫醒她"，不管她一轮读到哪些：被别的叫醒的那一轮、常规的那一轮，摆给她的
# 还是全部没看过的，按发生的先后。
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_message_that_does_not_wake_her_waits_for_her_next_round_and_is_read_there(
    nudge_db, stub_life, named, resident
):
    from tests.living.conftest import receive

    await regular_round(resident, "akao", _at(21, 30))
    quiet = _to_her("窗外的雨小了一点。", at=_at(21, 31), wakes_her=False)
    await receive(quiet)

    assert await nudged_round(resident, "akao", _at(21, 32)) is None

    regular = await regular_round(resident, "akao", _at(21, 40))

    assert regular is not None and not regular.nudged
    assert "窗外的雨小了一点。" in stub_life.prompts[-1]
    assert await _unread_ids() == set()
    assert await nudged_round(resident, "akao", _at(21, 41)) is None
    assert len(await _all_moments()) == 2


@pytest.mark.integration
async def test_an_earlier_message_that_does_not_wake_her_does_not_hold_back_one_that_does(
    nudge_db, stub_life, named, resident
):
    """最早那条没看过的不叫醒她，后面那条叫醒她：叫醒她的是后面那条，那一轮里两条都有。"""
    from tests.living.conftest import receive

    await regular_round(resident, "akao", _at(21, 30))
    quiet = _to_her("窗外的雨小了一点。", at=_at(21, 31), wakes_her=False)
    doorbell = _to_her("楼下有人按门铃。", at=_at(21, 32))
    await receive(quiet)
    await receive(doorbell)

    woken = await nudged_round(resident, "akao", _at(21, 33))

    assert woken is not None
    assert woken.moment_id == f"nudge:inbox:{doorbell.message_id}"
    assert "窗外的雨小了一点。" in stub_life.prompts[-1], "不叫醒的那条没摆进被叫醒的那一轮"
    assert "楼下有人按门铃。" in stub_life.prompts[-1]
    assert await _unread_ids() == set()
    assert await nudged_round(resident, "akao", _at(21, 34)) is None


@pytest.mark.integration
async def test_more_quiet_messages_than_a_round_can_show_do_not_hold_back_one_that_wakes_her(
    nudge_db, stub_life, named, resident
):
    """不叫醒的攒得比一轮能摆的还多，后面一条要叫醒她的照样叫醒她，而且在那一轮里。剩下没摆进
    去的不叫醒的，也不会因为还没看过就接着叫醒她：它们等她下一轮。"""
    from app.living.received import RECEIVED_LIMIT
    from tests.living.conftest import receive

    await regular_round(resident, "akao", _at(21, 30))
    for i in range(RECEIVED_LIMIT + 1):
        await receive(_to_her(f"第 {i} 件小事。", at=_at(21) + dt.timedelta(seconds=i), wakes_her=False))
    doorbell = _to_her("楼下有人按门铃。", at=_at(21, 32))
    await receive(doorbell)

    woken = await nudged_round(resident, "akao", _at(21, 33))

    assert woken is not None
    assert woken.moment_id == f"nudge:inbox:{doorbell.message_id}"
    assert "楼下有人按门铃。" in stub_life.prompts[-1]
    assert doorbell.message_id not in await _unread_ids()
    assert len(await _unread_ids()) == 1, "前提没造出来：要有一条不叫醒的没摆进那一轮"
    assert await nudged_round(resident, "akao", _at(21, 34)) is None


@pytest.mark.integration
async def test_a_quiet_message_that_arrives_during_her_round_does_not_wake_her_after_it(
    nudge_db, stub_life, named, resident
):
    """她一轮跑着时到的不叫醒的那条：那一轮之后不叫醒她，下一个常规轮次里看到它。"""
    from tests.living.conftest import receive

    await regular_round(resident, "akao", _at(21, 30))
    rain = _to_her("窗外下起了雨。", at=_at(21, 31))
    quiet = _to_her("雨声小了一点。", at=_at(21, 32), wakes_her=False)
    await receive(rain)

    async def it_arrives():
        await receive(quiet)

    stub_life.meanwhile = it_arrives
    assert await nudged_round(resident, "akao", _at(21, 32))
    stub_life.meanwhile = None
    assert "雨声小了一点。" not in stub_life.prompts[-1], "前提没造出来：那一轮看到了它"

    assert await nudged_round(resident, "akao", _at(21, 33)) is None
    await regular_round(resident, "akao", _at(21, 40))
    assert "雨声小了一点。" in stub_life.prompts[-1]
    assert await _unread_ids() == set()


@pytest.mark.integration
async def test_a_message_stored_before_the_attribute_existed_wakes_her(
    nudge_db, stub_life, named, resident
):
    """收件表加这一列之前存下的行：迁移把列加上，这些行这一列为空，按叫醒她处理。

    走一遍真的迁移：先把表退回没有这一列的样子、按旧版本的写法存一行，再按线上启动时的做法
    读现有表结构、加列。
    """
    from app.data.migrator import plan_migration
    from app.data.schema import table_name
    from app.living.received import ReceivedMessage

    table = table_name(ReceivedMessage)
    async with tx():
        s = current_session()
        await s.execute(text(f"ALTER TABLE {table} DROP COLUMN wakes_recipient"))
        await s.execute(
            text(
                f"INSERT INTO {table} "
                "(lane, persona_id, message_id, sender, body, message_time, dedup_hash) "
                "VALUES (:l, 'akao', 'stored-before', 'world', '窗外下起了雨。', :t, 'h-1')"
            ),
            {"l": LANE, "t": _at(21, 31)},
        )
    columns = await fetch_all(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = :t",
        t=table,
    )
    plan = plan_migration(
        [ReceivedMessage], {table: {r["column_name"]: r["data_type"] for r in columns}}
    )
    async with tx():
        s = current_session()
        for stmt in plan.stmts:
            await s.execute(text(stmt.sql))
        stored = (
            await s.execute(text(f"SELECT wakes_recipient FROM {table}"))
        ).scalar_one()
    assert stored is None, "前提没造出来：旧行这一列要是空的"
    from app.living.received import unread_received

    (read_back,) = await unread_received(lane=LANE, persona_id="akao")
    assert read_back.wakes_recipient is True, "旧行读出来要当成叫醒，不是 None"

    woken = await nudged_round(resident, "akao", _at(21, 32))

    assert woken is not None and woken.moment_id == "nudge:inbox:stored-before"
    assert "窗外下起了雨。" in stub_life.prompts[-1]


# --------------------------------------------------------------------------
# 八 · 叫醒她的那条一定在那一轮里；没跑完的那一轮重跑时还是它自己
#
# 那一轮的身份就是叫醒她的那条消息，所以这两件事是同一个承诺的两半：
#
#   * 那一轮落地，叫醒她的那条就算看过了。它要是不在那一轮里，那一轮落地了它还没看过，
#     之后每一拍都拿它去叫醒她、每一拍都撞上"这一轮已经跑过"，收件箱的提前叫醒就卡在那儿，
#     直到一个常规轮次把它看掉；
#   * 那一轮在她已经做了些什么之后失败，重跑必须还是那个身份。她这一轮里做的事、发出去的
#     话，id 都由身份派生；换了身份重跑，同一个动作就落两遍、同一句话就发两遍。这是工程上
#     的幂等，不是替她决定什么：重跑时她照样看到这期间新到的一切，怎么做由她。
# --------------------------------------------------------------------------


async def _acts(persona_id: str = "akao") -> int:
    return await fetch_value(
        "SELECT count(*) FROM data_happening "
        "WHERE lane = :l AND actor = :p AND kind = 'act'",
        l=LANE,
        p=persona_id,
    )


async def _unread_ids(persona_id: str = "akao") -> set[str]:
    from app.living.received import unread_received

    return {
        m.message_id
        for m in await unread_received(lane=LANE, persona_id=persona_id)
    }


async def _fails() -> None:
    raise RuntimeError("这一轮在她做完那些事之后失败了")


@pytest.mark.integration
async def test_the_message_that_woke_her_is_in_that_round_even_if_earlier_ones_pile_up(
    nudge_db, stub_life, named, resident
):
    """她正在跑一轮，这条钟排在后面等；等的时候到了一批发生得更早的消息，多到一轮摆不下。
    醒来的那一轮是谁叫醒的，那条就得在那一轮里，落地后就算看过；剩下的接着一条一条叫醒她。"""
    from app.living.received import RECEIVED_LIMIT
    from tests.living.conftest import receive

    await regular_round(resident, "akao", _at(21, 30))
    doorbell = _to_her("楼下有人按门铃。", at=_at(21, 31))
    await receive(doorbell)
    bodies = {doorbell.message_id: doorbell.body}

    turns = her_queue("akao", clock=clock_at(_at(21, 32)))
    async with her_round_runs(turns):  # 她正在跑的那一轮
        waiting = asyncio.create_task(
            turns.take(nudge(resident), Signal(at=_at(21, 32)))
        )
        await settled()
        for i in range(RECEIVED_LIMIT):
            earlier = _to_her(f"第 {i} 件事。", at=_at(21, 0, i))
            await receive(earlier)
            bodies[earlier.message_id] = earlier.body
    woken = await waiting

    assert woken is not None
    woke_for = woken.moment_id.removeprefix("nudge:inbox:")
    assert bodies[woke_for] in stub_life.prompts[-1], (
        f"叫醒她的那条（{bodies[woke_for]}）不在那一轮里"
    )
    assert woke_for not in await _unread_ids(), "那一轮落地了，叫醒她的那条却还没看过"

    # 还有没看过的，下一拍就接着叫醒她，直到看完；不会卡在哪一条上。
    for minute in range(33, 40):
        if not await _unread_ids():
            break
        assert await nudged_round(resident, "akao", _at(21, minute)) is not None, (
            "收件箱的提前叫醒卡住了：还有没看过的，却叫不醒她"
        )
    assert await _unread_ids() == set()
    assert any("楼下有人按门铃。" in seen for seen in stub_life.prompts)
    assert await nudged_round(resident, "akao", _at(21, 40)) is None


@pytest.mark.integration
async def test_a_nudge_that_waited_behind_a_stuck_round_happens_when_it_gets_her(
    nudge_db, stub_life, named, post, resident
):
    """前一轮挂住了，21:32 那一拍的提前叫醒排在后面，21:47 才轮到。被叫醒的那一轮的『现在』是
    21:47：她做的事记在这一刻，发给 world 的汇总也带着这一刻，不是那一拍敲响的 21:32。"""
    from tests.living.conftest import receive

    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m0", place="家/客厅",
        doing="翻胶片", noted_at=_at(21, 20),
    )
    await regular_round(resident, "akao", _at(21, 30))
    await receive(_to_her("窗外下起了雨。", at=_at(21, 31)))
    stub_life.calls = [("act", {"what": "起身把窗关上了"})]

    clock = [_at(21, 32)]
    turns = her_queue("akao", clock=lambda: clock[0])
    async with her_round_runs(turns):  # 挂住的那一轮
        tick = asyncio.create_task(turns.take(nudge(resident), Signal(at=clock[0])))
        await settled()
        clock[0] = _at(21, 47)
    await tick

    (woken,) = [m for m in await _all_moments() if m.nudged]
    assert woken.began_at == _at(21, 47)
    # 恰好一行：一行都没有、或者落了两行，用例都要失败。
    (acted,) = await fetch_all(
        "SELECT occurred_at FROM data_happening "
        "WHERE lane = :l AND actor = 'akao' AND kind = 'act'",
        l=LANE,
    )
    acted_at = acted["occurred_at"]
    assert acted_at == _at(21, 47)
    assert [(s.recipient, s.time) for s in post.sent] == [("world", _at(21, 47))]


@pytest.mark.integration
async def test_two_ticks_queued_behind_her_round_wake_her_once(
    nudge_db, stub_life, named, resident
):
    """她正在跑一轮，这条钟的两拍叠在一起赶到（钟每一拍都不等上一拍跑完）。叫醒她的那条
    只叫醒她一次：先到的那一拍排着、轮到时跑那一轮，后到的那一拍什么都不做，也不出错。判"是什么
    叫醒了她"跟跑那一轮不在同一次轮到她的时候的话，两拍会各自判出同一条、各记一遍"开始了"。"""
    from tests.living.conftest import receive

    await regular_round(resident, "akao", _at(21, 30))
    doorbell = _to_her("楼下有人按门铃。", at=_at(21, 31))
    await receive(doorbell)

    turns = her_queue("akao", clock=clock_at(_at(21, 33)))
    async with her_round_runs(turns):  # 她正在跑的那一轮
        ticks = asyncio.gather(
            turns.take(nudge(resident), Signal(at=_at(21, 32))),
            turns.take(nudge(resident), Signal(at=_at(21, 33))),
            return_exceptions=True,
        )
        await asyncio.sleep(0.5)  # 两拍都到了她的队列门口
    outcomes = await ticks

    assert not [o for o in outcomes if isinstance(o, BaseException)], outcomes
    woke = [o for o in outcomes if o is not None]
    assert [o.moment_id for o in woke] == [f"nudge:inbox:{doorbell.message_id}"]
    assert len(await _all_moments()) == 2


@pytest.mark.integration
async def test_ticks_while_her_round_is_stuck_do_not_pile_up_behind_it(
    nudge_db, stub_life, named, resident
):
    """她那一轮挂住了（最长占着 15 分钟），这条钟每分钟一拍。排在后面等的只有一拍：后面的拍
    看见已经有一拍在等，直接过去，不再排。等着的那一拍轮到时自己判是什么在叫她。"""
    from tests.living.conftest import receive

    await regular_round(resident, "akao", _at(21, 30))
    rain = _to_her("窗外下起了雨。", at=_at(21, 31))
    await receive(rain)

    turns = her_queue("akao", clock=clock_at(_at(21, 35)))
    async with her_round_runs(turns):  # 挂住的那一轮
        ticks = [
            asyncio.create_task(turns.take(nudge(resident), Signal(at=_at(21, m))))
            for m in (32, 33, 34)
        ]
        await settled()
        assert [t.done() for t in ticks] == [False, True, True], (
            "每一拍都排在了挂住的那一轮后面"
        )
        assert [t.result() for t in ticks[1:]] == [None, None]
    woken = await ticks[0]

    assert woken is not None and woken.moment_id == f"nudge:inbox:{rain.message_id}"


@pytest.mark.integration
async def test_a_message_that_arrives_while_her_round_holds_her_wakes_her_after_it(
    nudge_db, stub_life, named, resident
):
    """她那一轮跑着时到了一条，这一拍不排在那一轮后面；那一轮没看到它，那一轮之后的下一拍
    叫醒她。"""
    from tests.living.conftest import receive

    await regular_round(resident, "akao", _at(21, 30))
    rain = _to_her("窗外下起了雨。", at=_at(21, 31))
    doorbell = _to_her("楼下有人按门铃。", at=_at(21, 32))
    await receive(rain)

    clock = [_at(21, 32)]
    turns = her_queue("akao", clock=lambda: clock[0])
    during: list[asyncio.Task] = []

    async def it_arrives_and_the_clock_ticks():
        await receive(doorbell)
        tick = asyncio.create_task(turns.take(nudge(resident), Signal(at=_at(21, 33))))
        await asyncio.sleep(0.2)
        during.append(tick)

    stub_life.meanwhile = it_arrives_and_the_clock_ticks
    woke_for_rain = await turns.take(nudge(resident), Signal(at=_at(21, 32)))
    stub_life.meanwhile = None

    assert woke_for_rain is not None and rain.message_id in woke_for_rain.moment_id
    (tick,) = during
    assert tick.done() and tick.result() is None, "那一拍排在了她正在跑的那一轮后面"
    assert "楼下有人按门铃。" not in stub_life.prompts[-1], "前提没造出来：那一轮看到了它"

    clock[0] = _at(21, 34)
    woke_for_doorbell = await turns.take(nudge(resident), Signal(at=_at(21, 34)))
    assert woke_for_doorbell is not None
    assert doorbell.message_id in woke_for_doorbell.moment_id
    assert "楼下有人按门铃。" in stub_life.prompts[-1]


@pytest.mark.integration
async def test_a_tick_that_waited_decides_on_what_is_true_when_it_gets_her(
    nudge_db, stub_life, named, resident
):
    """这一拍排在她正在跑的那一轮后面；那一轮把叫她的那条看掉了。轮到这一拍时已经没有什么在叫
    她，它不跑、也不记"开始了"。判在轮到她之前的话，它会拿排队之前判的那条再跑一轮。"""
    from app.data.schema import table_name
    from app.living.nudge import NudgeBegun
    from app.living.received import mark_read, unread_received
    from tests.living.conftest import receive

    await regular_round(resident, "akao", _at(21, 30))
    await receive(_to_her("窗外下起了雨。", at=_at(21, 31)))

    turns = her_queue("akao", clock=clock_at(_at(21, 32)))
    async with her_round_runs(turns):  # 她正在跑的那一轮
        tick = asyncio.create_task(turns.take(nudge(resident), Signal(at=_at(21, 32))))
        await settled()
        seen = await unread_received(lane=LANE, persona_id="akao")
        async with tx():  # 那一轮落地，把它记成看过
            await mark_read(seen, moment_id="2026-07-25T21:30+08:00")

    assert await tick is None
    begun = await fetch_value(f"SELECT count(*) FROM {table_name(NudgeBegun)}")
    assert begun == 0


@pytest.mark.integration
async def test_the_two_clocks_still_queue_behind_each_other(
    nudge_db, stub_life, named, resident
):
    """她的常规节奏和提前叫醒在同一条队列上：各自只排一拍，两条钟之间照样排队、先来先跑、不互相
    顶掉。常规那一拍和提前叫醒那一拍都等到她。"""
    from tests.living.conftest import receive

    await regular_round(resident, "akao", _at(21, 30))
    rain = _to_her("窗外下起了雨。", at=_at(21, 41))
    await receive(rain)

    turns_at = iter([_at(21, 41), _at(21, 42)])
    turns = her_queue("akao", clock=lambda: next(turns_at))
    async with her_round_runs(turns):  # 挂住的那一轮
        nudged = asyncio.create_task(
            turns.take(nudge(resident), Signal(at=_at(21, 41)))
        )
        await settled()
        regular_tick = asyncio.create_task(
            turns.take(regular(resident), Signal(at=_at(21, 42)))
        )
        await settled()
        assert not nudged.done() and not regular_tick.done(), "一条钟把另一条钟顶掉了"

    woken = await nudged
    assert woken is not None and woken.moment_id == f"nudge:inbox:{rain.message_id}"
    assert woken.began_at == _at(21, 41), "后来的那一拍插到了先来的前面"
    on_time = await regular_tick
    assert on_time is not None, "21:40 那一格被提前叫醒那一拍顶掉了"
    assert on_time.began_at == _at(21, 42)


@pytest.mark.integration
async def test_the_message_that_woke_her_is_in_that_round_even_if_earlier_ones_arrive_as_it_starts(
    nudge_db, stub_life, named, monkeypatch, resident
):
    """那一轮已经定下是谁叫醒的、还没读收件箱的那一刻，到了一批发生得更早的消息，多到一轮
    摆不下：叫醒她的那条照样在那一轮里。"""
    from app.living import moment as moment_mod
    from app.living.received import RECEIVED_LIMIT
    from tests.living.conftest import receive

    await regular_round(resident, "akao", _at(21, 30))
    doorbell = _to_her("楼下有人按门铃。", at=_at(21, 31))
    await receive(doorbell)

    real = moment_mod.read_snapshot

    async def a_pile_arrives_first(**kwargs):
        for i in range(RECEIVED_LIMIT):
            await receive(_to_her(f"第 {i} 件事。", at=_at(21, 0, i)))
        return await real(**kwargs)

    monkeypatch.setattr(moment_mod, "read_snapshot", a_pile_arrives_first)
    woken = await nudged_round(resident, "akao", _at(21, 32))

    assert woken is not None
    assert woken.moment_id == f"nudge:inbox:{doorbell.message_id}"
    assert "楼下有人按门铃。" in stub_life.prompts[-1], "叫醒她的那条不在那一轮里"
    assert doorbell.message_id not in await _unread_ids()


@pytest.mark.integration
async def test_a_round_a_message_woke_that_failed_is_retried_as_itself_whatever_arrives(
    nudge_db, stub_life, named, resident
):
    """收件箱里那条叫醒她，她做了一个动作，这一轮失败了。之后到了一批发生得更早的消息、手机
    上也有人叫她：下一拍重跑的还是那一轮，同一个动作不落第二遍；叫她的那些排在它之后。"""
    from app.living.received import RECEIVED_LIMIT
    from tests.living.conftest import receive

    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m0", place="家/客厅",
        doing="翻胶片", noted_at=_at(21, 20),
    )
    await regular_round(resident, "akao", _at(21, 30))
    rain = _to_her("窗外下起了雨。", at=_at(21, 31))
    await receive(rain)

    stub_life.calls = [("act", {"what": "起身把窗关上了"})]
    stub_life.after = _fails
    with pytest.raises(RuntimeError, match="做完那些事之后失败"):
        await nudged_round(resident, "akao", _at(21, 32))
    stub_life.after = None
    assert await _acts() == 1, "前提没造出来：失败之前那个动作要已经落下"

    for i in range(RECEIVED_LIMIT):
        await receive(_to_her(f"第 {i} 件事。", at=_at(21, 0, i)))
    summons = await _incoming(_DM, body="在吗", at=_at(21, 33))

    again = await nudged_round(resident, "akao", _at(21, 34))

    assert again is not None
    assert again.moment_id == f"nudge:inbox:{rain.message_id}", (
        "没跑完的那一轮重跑时换了身份"
    )
    assert "窗外下起了雨。" in stub_life.prompts[-1], "重跑的那一轮里没有叫醒她的那条"
    assert await _acts() == 1, "失败之前做过的那个动作，重跑时又落了一遍"
    assert rain.message_id not in await _unread_ids()

    then = await nudged_round(resident, "akao", _at(21, 35))
    assert then is not None and then.moment_id == f"nudge:{summons}", (
        "手机上叫她的那条被重跑那一轮挡掉了，没有轮到它"
    )


@pytest.mark.integration
async def test_a_round_a_summons_woke_that_failed_is_retried_as_itself_whatever_arrives(
    nudge_db, stub_life, named, resident
):
    """手机上那条叫醒她，她做了一个动作，这一轮失败了。之后手机上来了更新的一条叫她、收件箱
    里也到了一条：下一拍重跑的还是那一轮，同一个动作不落第二遍。"""
    from tests.living.conftest import receive

    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m0", place="家/客厅",
        doing="翻胶片", noted_at=_at(21, 20),
    )
    await regular_round(resident, "akao", _at(21, 30))
    first = await _incoming(_DM, body="在吗", at=_at(21, 31))

    stub_life.calls = [("act", {"what": "放下胶片去拿手机"})]
    stub_life.after = _fails
    with pytest.raises(RuntimeError, match="做完那些事之后失败"):
        await nudged_round(resident, "akao", _at(21, 32))
    stub_life.after = None
    assert await _acts() == 1, "前提没造出来：失败之前那个动作要已经落下"

    newer = await _incoming(
        _GROUP, body=" 你说呢", at=_at(21, 33), sender=_SOMEONE,
        sender_name="路人", names_bot=_AKAO_BOT_UID,
    )
    await receive(_to_her("千凪在厨房喊你吃饭。", at=_at(21, 33)))

    again = await nudged_round(resident, "akao", _at(21, 34))

    assert again is not None
    assert again.moment_id == f"nudge:{first}", "没跑完的那一轮重跑时换了身份"
    assert await _acts() == 1, "失败之前做过的那个动作，重跑时又落了一遍"

    then = await nudged_round(resident, "akao", _at(21, 35))
    assert then is not None and then.moment_id == f"nudge:{newer}"


# --------------------------------------------------------------------------
# 提前一轮的身份和"已开始"那一行，写死字面值
#
# 被叫醒的那一轮里她做的事、发的话都按这一轮的身份派生 id；"已开始"那一行记下的是这一轮被
# 什么叫醒，重跑时按它还原同一个身份。两者的写法改一个字节，重跑的那一轮就对不上第一遍写下的
# 行。所以这里写的是字面值，不从实现里算。
# --------------------------------------------------------------------------


async def _begun() -> list[str]:
    from app.data.schema import table_name
    from app.living.nudge import NudgeBegun

    rows = await fetch_all(
        f"SELECT nudged_by FROM {table_name(NudgeBegun)} "
        f"WHERE lane = :l AND persona_id = 'akao' ORDER BY created_at",
        l=LANE,
    )
    return [r["nudged_by"] for r in rows]


@pytest.mark.integration
async def test_a_round_a_summons_woke_is_named_after_that_chat_message(
    nudge_db, stub_life, resident
):
    await regular_round(resident, "akao", _at(21, 30))
    await _incoming(
        _DM,
        body="在吗",
        at=_at(21, 31),
        message_id=uuid.UUID("0b6f3c1e-7d24-4a59-8e13-5c2f9a71d406"),
    )

    moment = await nudged_round(resident, "akao", _at(21, 32))

    assert moment.moment_id == "nudge:0b6f3c1e-7d24-4a59-8e13-5c2f9a71d406"
    assert await _begun() == ["0b6f3c1e-7d24-4a59-8e13-5c2f9a71d406"]


@pytest.mark.integration
async def test_a_round_a_received_message_woke_is_named_after_that_message(
    nudge_db, stub_life, named, resident
):
    from app.messaging.message import Kind, new_message
    from tests.living.conftest import receive

    await regular_round(resident, "akao", _at(21, 30))
    await receive(
        new_message(
            sender="绫奈",
            recipient="赤尾",
            body="姐，下来吃饭。",
            kind=Kind.MESSAGE,
            time=_at(21, 31),
            message_id="ayana-dinner-1",
        )
    )

    moment = await nudged_round(resident, "akao", _at(21, 32))

    assert moment.moment_id == "nudge:inbox:ayana-dinner-1"
    assert await _begun() == ["inbox:ayana-dinner-1"]


# --------------------------------------------------------------------------
# 提前叫醒判不判、判出哪一轮（不调模型）
#
# :func:`nudged_moment_due` 读钟和库，判出来要跑的那一轮先记下"开始了"再交回。它在内存里什么
# 都不留，所以一个刚起来的进程从库里推出的判断跟一直跑着的进程一样。
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_nudge_due_reads_the_clock_before_it_reads_anything(nudge_db, monkeypatch):
    from app.living import nudge as nudge_mod
    from app.living.nudge import nudged_moment_due

    read: list[str] = []

    def clock() -> dt.datetime:
        read.append("clock")
        return _at(21, 32)

    real = nudge_mod._begun_not_landed

    async def begun(**kwargs):
        read.append("begun")
        return await real(**kwargs)

    monkeypatch.setattr(nudge_mod, "_begun_not_landed", begun)

    assert await nudged_moment_due(lane=LANE, persona_id="akao", clock=clock) is None
    assert read[:2] == ["clock", "begun"]


@pytest.mark.integration
async def test_nothing_calling_her_is_no_round_and_nothing_written(nudge_db, stub_life):
    from app.living.nudge import nudged_moment_due

    assert await nudged_moment_due(lane=LANE, persona_id="akao", clock=clock_at(_at(21, 32))) is None
    assert await _begun() == []


@pytest.mark.integration
async def test_the_nudge_due_writes_that_the_round_began_before_it_hands_it_back(
    nudge_db, stub_life, named
):
    """判出来就记"开始了"，在模型调用之前：之后这一轮失败了，重跑的还是它。"""
    from app.living.nudge import nudged_moment_due
    from tests.living.conftest import receive

    await receive(_to_her("窗外下起了雨。", at=_at(21, 31), message_id="rain"))

    due = await nudged_moment_due(lane=LANE, persona_id="akao", clock=clock_at(_at(21, 32)))

    assert (due.moment_id, due.now, due.nudged, due.must_show) == (
        "nudge:inbox:rain",
        _at(21, 32),
        True,
        "rain",
    )
    assert await _begun() == ["inbox:rain"]
    assert stub_life.prompts == [], "判的时候调到了模型"


@pytest.mark.integration
async def test_a_summons_on_her_phone_comes_before_her_inbox(nudge_db, stub_life, named):
    from app.living.nudge import nudged_moment_due
    from tests.living.conftest import receive

    await receive(_to_her("窗外下起了雨。", at=_at(21, 30), message_id="rain"))
    summons = await _incoming(_DM, body="在吗", at=_at(21, 31))

    due = await nudged_moment_due(lane=LANE, persona_id="akao", clock=clock_at(_at(21, 32)))

    assert (due.moment_id, due.must_show) == (f"nudge:{summons}", None)
    assert await _begun() == [summons]


@pytest.mark.integration
async def test_her_inbox_wakes_her_for_the_earliest_message_she_has_not_seen(
    nudge_db, stub_life, named
):
    from app.living.nudge import nudged_moment_due
    from tests.living.conftest import receive

    await receive(_to_her("楼下有人按门铃。", at=_at(21, 31), message_id="doorbell"))
    await receive(_to_her("窗外下起了雨。", at=_at(21, 20), message_id="rain"))

    due = await nudged_moment_due(lane=LANE, persona_id="akao", clock=clock_at(_at(21, 32)))

    assert due.moment_id == "nudge:inbox:rain"


@pytest.mark.integration
async def test_a_round_that_began_and_never_landed_is_due_again_as_itself_before_anything_new(
    nudge_db, stub_life, named
):
    """开始了没落地的那一轮，新进程判出的还是它，不再记一遍"开始了"，也不让手机上更新的那条、
    收件箱里新到的那条插到它前面。"""
    from app.living.nudge import nudged_moment_due
    from tests.living.conftest import receive

    await receive(_to_her("窗外下起了雨。", at=_at(21, 31), message_id="rain"))
    first = await nudged_moment_due(lane=LANE, persona_id="akao", clock=clock_at(_at(21, 32)))
    # 那一轮没落地，进程没了。之后手机上来了叫她的一条，收件箱里也到了一条更早的。
    await _incoming(_DM, body="在吗", at=_at(21, 33))
    await receive(_to_her("千凪在厨房喊你。", at=_at(21, 0), message_id="dinner"))

    again = await nudged_moment_due(lane=LANE, persona_id="akao", clock=clock_at(_at(21, 34)))

    assert first.moment_id == again.moment_id == "nudge:inbox:rain"
    assert (again.now, again.must_show) == (_at(21, 34), "rain")
    assert await _begun() == ["inbox:rain"]
