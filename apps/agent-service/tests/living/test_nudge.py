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

from app.agent.neutral import Message, Role
from app.agent.runtime_context import agent_context
from app.data import session as session_mod
from app.living.moment import (
    DEFAULT_LIFE_MOMENT_MINUTES,
    LifeMoment,
    latest_moment,
    run_moment,
)
from app.living.nudge import nudge_once
from app.living.whereabouts import note_whereabouts

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
    from app.living.loose_ends import LooseEnd
    from app.living.nudge import NudgeBegun
    from tests.runtime.conftest import migrate

    for cls in (LooseEnd, LifeMoment, NudgeBegun):
        await migrate(cls, living_db)
    async with session_mod.get_session() as s:
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
) -> str:
    """``names_bot`` 写进 ``mentioned_common_user_ids``，不是往 ``content`` 里塞
    一条 mention item —— 公共层的内容契约没有那种片段（见 test_phone 的同名夹具）。

    ``mention_unrecorded`` 写 NULL：没人算过这条消息（存量行、QQ 的行）。
    """
    items = [{"kind": "text", "text": body}]
    mid = uuid.uuid4()
    async with session_mod.get_session() as s:
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


class FakeLife:
    """替身 life：她这一轮调了哪些工具、最后说了什么，由用例写死。

    ``prompts`` 收的是**这一轮新摆到她眼前那条**，也就是最后一条：喂给模型的列表是
    "连续上下文 + 这一轮的刺激"（:func:`app.living.moment.run_moment`），取第一条会
    取到几个 moment 之前的快照。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.said = "继续"
        self.prompts: list[str] = []
        # 模型那一步跑着的时候发生的事（比如又到了一条消息）。
        self.meanwhile = None
        # 她调完工具之后发生的事（比如这一轮在她已经做了些什么之后失败）。
        self.after = None

    async def run(self, messages, **kwargs):
        self.prompts.append(messages[-1].content)
        if self.meanwhile is not None:
            await self.meanwhile()
        with agent_context(kwargs["context"]):
            from app.living.moment import MOMENT_TOOLS

            tools = {t.name: t for t in MOMENT_TOOLS}
            for name, args in self.calls:
                await tools[name].invoke(args)
        if self.after is not None:
            await self.after()
        return Message(role=Role.ASSISTANT, content=self.said)


@pytest.fixture
def stub_life(monkeypatch):
    from app.living import moment as moment_mod
    from app.living import persona as persona_mod

    async def fake_find_persona(persona_id: str):
        return SimpleNamespace(display_name="赤尾", persona_core="她泡抹茶店。")

    monkeypatch.setattr(persona_mod, "find_persona", fake_find_persona)

    async def fixed_minutes() -> int:
        return DEFAULT_LIFE_MOMENT_MINUTES

    monkeypatch.setattr(moment_mod, "life_moment_minutes", fixed_minutes)

    fake = FakeLife()
    monkeypatch.setattr(moment_mod, "build_moment_runner", lambda: fake)
    return fake


async def _all_moments(persona_id: str = "akao") -> list[LifeMoment]:
    async with session_mod.get_session() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT * FROM data_life_moment WHERE lane = :l "
                    "AND persona_id = :p ORDER BY began_at ASC"
                ),
                {"l": LANE, "p": persona_id},
            )
        ).mappings().all()
    return [LifeMoment(**{k: r[k] for k in LifeMoment.model_fields}) for r in rows]


# --------------------------------------------------------------------------
# 一 · 她获得一次决策机会 —— 开不开口不进验收
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_direct_message_brings_her_to_that_moment(nudge_db, stub_life):
    await run_moment(lane=LANE, persona_id="akao", now=_at(21, 30))
    await _incoming(_DM, body="在吗，抹茶店那事", at=_at(21, 31))

    moment = await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 32))

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
    nudge_db, stub_life
):
    """**必须反证的方向**：被 @ 之后她没开口，而系统一切正常。

    "被叫到"和"开口"离得太近，一不小心 @ 就又变成了回复开关。这条用例存在的
    唯一目的，就是让"她没回"成为一个**正常轮次**，而不是一个失败。
    """
    await run_moment(lane=LANE, persona_id="akao", now=_at(21, 30))
    await _incoming(
        _GROUP, body=" 你说呢", at=_at(21, 31), sender=_SOMEONE,
        sender_name="路人", names_bot=_AKAO_BOT_UID,
    )
    stub_life.said = "继续"  # 她看了一眼信封，没说话

    moment = await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 32))

    assert moment is not None, "她连被带到那一刻的机会都没有"
    assert moment.switched is False and moment.recorded == 0
    assert moment.said == "继续"
    # 这一轮照常留痕、游标照常推进 —— 没开口不是异常状态。
    assert (await latest_moment(lane=LANE, persona_id="akao")).moment_id == \
        moment.moment_id


@pytest.mark.integration
async def test_group_chatter_that_does_not_name_her_waits_for_the_next_regular_moment(
    nudge_db, stub_life, pinned
):
    # 群固定加白，所以这个群**在**她视野里：这条用例要验的是"不点名不提前、但下一个
    # 常规轮次看得见"，不是白名单挡没挡住它（:mod:`app.living.whitelist`）。
    pinned(str(_GROUP))
    await run_moment(lane=LANE, persona_id="akao", now=_at(21, 30))
    await _incoming(
        _GROUP, body="今天好热", at=_at(21, 31), sender=_SOMEONE, sender_name="路人"
    )

    assert await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 32)) is None

    later = await run_moment(lane=LANE, persona_id="akao", now=_at(21, 40))
    assert later is not None
    assert "路人" in stub_life.prompts[-1], (
        f"不点名的消息不提前，但下一个常规轮次一定看得到。她看到的是：\n"
        f"{stub_life.prompts[-1]}"
    )


# --------------------------------------------------------------------------
# 二 · 坑 1：提前的那一轮不能把常规节奏往后推
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_an_early_moment_does_not_delay_the_regular_rhythm(nudge_db, stub_life):
    await run_moment(lane=LANE, persona_id="akao", now=_at(21, 30))
    await _incoming(_DM, body="在吗", at=_at(21, 33))
    early = await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 34))
    assert early is not None

    # 21:40 是原本就该来的那一轮。按"最近一轮"判间隔的话，21:34 到 21:40 只有
    # 六分钟，这一轮会被吞掉 —— 她的节奏就被每一条私聊往后拖。
    on_time = await run_moment(lane=LANE, persona_id="akao", now=_at(21, 40))

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
    nudge_db, stub_life
):
    regular = await run_moment(lane=LANE, persona_id="akao", now=_at(21, 30))
    await _incoming(_DM, body="在吗", at=_at(21, 30, 10))

    early = await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 30, 20))

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
    nudge_db, stub_life, named
):
    from app.living.received import receive

    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m0", place="家/客厅",
        doing="翻胶片", noted_at=_at(21, 20),
    )
    await run_moment(lane=LANE, persona_id="akao", now=_at(21, 30))
    await receive(_to_her("当面对你说：「姐我出门了。」", at=_at(21, 32), sender="绫奈"))
    await _incoming(_DM, body="在吗", at=_at(21, 33))

    early = await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 34))
    assert early is not None
    assert "姐我出门了。" in stub_life.prompts[-1], "提前轮次没看到刚传到她这里的话"

    await run_moment(lane=LANE, persona_id="akao", now=_at(21, 40))

    assert "姐我出门了。" not in stub_life.prompts[-1], "同一句话她看了两遍"


# --------------------------------------------------------------------------
# 五 · 同一条消息只提前一次（新消息才震）
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_same_message_only_pulls_her_early_once(nudge_db, stub_life):
    await run_moment(lane=LANE, persona_id="akao", now=_at(21, 30))
    await _incoming(_DM, body="在吗", at=_at(21, 31))

    first = await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 32))
    second = await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 33))
    third = await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 34))

    assert first is not None
    assert (second, third) == (None, None), (
        "她没看手机所以那条一直未读 —— 如果按「还有没有未读」判，她每分钟被震一次，"
        "一天一千多次模型调用"
    )
    assert len(await _all_moments()) == 2


@pytest.mark.integration
async def test_a_newer_message_pulls_her_early_again(nudge_db, stub_life):
    await run_moment(lane=LANE, persona_id="akao", now=_at(21, 30))
    await _incoming(_DM, body="在吗", at=_at(21, 31))
    assert await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 32)) is not None

    await _incoming(_DM, body="睡了？", at=_at(21, 35))

    assert await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 36)) is not None
    assert len(await _all_moments()) == 3


@pytest.mark.integration
async def test_nothing_new_means_no_early_moment_at_all(nudge_db, stub_life):
    await run_moment(lane=LANE, persona_id="akao", now=_at(21, 30))

    assert await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 31)) is None
    assert len(await _all_moments()) == 1


# --------------------------------------------------------------------------
# 六 · 游标跟着一轮落地；信封不漏掉在叫她的那条
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_moment_that_blew_up_did_not_read_her_phone(nudge_db, stub_life):
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

    original = moment_mod.insert_idempotent
    moment_mod.insert_idempotent = blow_up
    try:
        with pytest.raises(RuntimeError):
            await run_moment(lane=LANE, persona_id="akao", now=_at(21, 32))
    finally:
        moment_mod.insert_idempotent = original

    assert await read_through(
        lane=LANE, persona_id="akao", channel_id=str(_DM)
    ) == (0, ""), "这一轮没跑完，游标却推过去了 —— 那条消息就此永久消失"
    assert await _all_moments() == []


@pytest.mark.integration
async def test_a_finished_moment_did_read_her_phone(nudge_db, stub_life):
    """反面：这一轮跑完了，看过的就是看过了。"""
    from app.living.phone import read_through

    await _incoming(_DM, body="在吗", at=_at(21, 31))
    stub_life.calls = [("look_at_phone", {"channel_id": str(_DM)})]

    moment = await run_moment(lane=LANE, persona_id="akao", now=_at(21, 32))

    assert moment is not None
    assert (
        await read_through(lane=LANE, persona_id="akao", channel_id=str(_DM))
    )[0] == _ms(_at(21, 31))


@pytest.mark.integration
async def test_both_people_waiting_on_her_are_in_the_envelope(nudge_db, stub_life):
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

    moment = await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 33))

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
async def named(monkeypatch):
    """三姐妹的名字已经读好，``receive`` 才认得收件人。"""
    from app.living import participants as participants_mod

    names = {"akao": "赤尾", "ayana": "绫奈", "chinagi": "千凪"}

    async def find_persona(persona_id: str):
        return SimpleNamespace(persona_id=persona_id, display_name=names[persona_id])

    monkeypatch.setenv("LANE", LANE)
    monkeypatch.setattr(participants_mod, "find_persona", find_persona)
    monkeypatch.setattr(participants_mod, "_known", None)
    await participants_mod.load_residents()


def _to_her(body: str, *, at: dt.datetime, sender: str = "world"):
    from app.messaging.message import Kind, new_message

    return new_message(sender=sender, recipient="赤尾", body=body, kind=Kind.MESSAGE, time=at)


@pytest.mark.integration
async def test_a_received_message_brings_her_to_that_moment_once(
    nudge_db, stub_life, named
):
    from app.living.received import receive

    await run_moment(lane=LANE, persona_id="akao", now=_at(21, 30))
    rain = _to_her("窗外下起了雨。", at=_at(21, 31))
    await receive(rain)

    first = await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 32))
    second = await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 33))

    assert first is not None and first.nudged is True
    assert first.moment_id == f"nudge:inbox:{rain.message_id}", (
        "那一轮的身份不是叫醒她的那条消息"
    )
    assert "窗外下起了雨。" in stub_life.prompts[-1]
    assert second is None
    assert len(await _all_moments()) == 2


@pytest.mark.integration
async def test_a_woken_round_that_fails_is_the_same_moment_when_it_runs_again(
    nudge_db, stub_life, named
):
    """叫醒她的那一轮失败了：那条还没看过，下一拍再叫醒她，而且还是同一个 moment。

    身份由那条消息定、不由钟点定，所以重跑时她这一轮里所有派生 id 原样对上，失败之前
    已经做了的事重放一遍写不出第二行（见 :func:`app.living.moment.run_moment`）。
    """
    from app.living.received import receive

    await run_moment(lane=LANE, persona_id="akao", now=_at(21, 30))
    rain = _to_her("窗外下起了雨。", at=_at(21, 31))
    await receive(rain)

    async def blow_up():
        raise RuntimeError("这一轮的模型调用失败了")

    stub_life.meanwhile = blow_up
    with pytest.raises(RuntimeError):
        await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 32))
    stub_life.meanwhile = None
    again = await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 33))

    assert again is not None and again.moment_id == f"nudge:inbox:{rain.message_id}"
    assert "窗外下起了雨。" in stub_life.prompts[-1], "失败那一轮看到的，重跑时没再给她看"


@pytest.mark.integration
async def test_the_same_message_delivered_again_does_not_wake_her_again(
    nudge_db, stub_life, named
):
    from app.living.received import receive

    await run_moment(lane=LANE, persona_id="akao", now=_at(21, 30))
    rain = _to_her("窗外下起了雨。", at=_at(21, 31))
    await receive(rain)
    assert await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 32)) is not None

    await receive(rain)  # world 带着原来的 id 重发

    assert await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 33)) is None
    assert len(await _all_moments()) == 2


@pytest.mark.integration
async def test_a_message_that_arrived_during_her_round_wakes_her_after_it(
    nudge_db, stub_life, named
):
    """一轮跑着的时候到的那条没摆进那一轮，它还没叫醒过她：下一拍叫醒她一次。"""
    from app.living.received import receive

    await run_moment(lane=LANE, persona_id="akao", now=_at(21, 30))
    rain = _to_her("窗外下起了雨。", at=_at(21, 31))
    doorbell = _to_her("楼下有人按门铃。", at=_at(21, 32))
    await receive(rain)

    async def it_arrives():
        await receive(doorbell)

    stub_life.meanwhile = it_arrives
    woke_for_rain = await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 32))
    stub_life.meanwhile = None
    woke_for_doorbell = await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 33))

    assert woke_for_rain is not None and rain.message_id in woke_for_rain.moment_id
    assert woke_for_doorbell is not None
    assert doorbell.message_id in woke_for_doorbell.moment_id
    assert "楼下有人按门铃。" in stub_life.prompts[-1]
    assert await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 34)) is None


@pytest.mark.integration
async def test_a_phone_call_she_ignored_does_not_keep_a_received_message_from_waking_her(
    nudge_db, stub_life, named
):
    """手机上那条私聊已经叫醒过她、她没看手机所以一直未读；之后收到的消息照样叫醒她。"""
    from app.living.received import receive

    await run_moment(lane=LANE, persona_id="akao", now=_at(21, 30))
    await _incoming(_DM, body="在吗", at=_at(21, 31))
    assert await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 32)) is not None

    await receive(_to_her("千凪在厨房喊你吃饭。", at=_at(21, 33)))
    moment = await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 34))

    assert moment is not None, "一条躺着的未读私聊挡住了收件箱里的新消息"
    assert "千凪在厨房喊你吃饭。" in stub_life.prompts[-1]


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
    async with session_mod.get_session() as s:
        return (
            await s.execute(
                text(
                    "SELECT count(*) FROM data_happening "
                    "WHERE lane = :l AND actor = :p AND kind = 'act'"
                ),
                {"l": LANE, "p": persona_id},
            )
        ).scalar_one()


async def _unread_ids(persona_id: str = "akao") -> set[str]:
    from app.living.received import unread_received

    return {
        m.message_id
        for m in await unread_received(lane=LANE, persona_id=persona_id)
    }


async def _queued_behind(key: str) -> None:
    """等到有人排在这把占用后面（``asyncio.Lock`` 的等待队列不空）。"""
    from app.living.serial import _lock_for

    lock = _lock_for(key)
    for _ in range(500):
        if lock._waiters:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"没有人排在 {key} 后面")


async def _fails() -> None:
    raise RuntimeError("这一轮在她做完那些事之后失败了")


@pytest.mark.integration
async def test_the_message_that_woke_her_is_in_that_round_even_if_earlier_ones_pile_up(
    nudge_db, stub_life, named
):
    """她正在跑一轮，这条钟排在后面等；等的时候到了一批发生得更早的消息，多到一轮摆不下。
    醒来的那一轮是谁叫醒的，那条就得在那一轮里，落地后就算看过；剩下的接着一条一条叫醒她。"""
    from app.living.moment import life_moment_lock_key
    from app.living.received import RECEIVED_LIMIT, receive
    from app.living.serial import hold

    await run_moment(lane=LANE, persona_id="akao", now=_at(21, 30))
    doorbell = _to_her("楼下有人按门铃。", at=_at(21, 31))
    await receive(doorbell)
    bodies = {doorbell.message_id: doorbell.body}

    key = life_moment_lock_key(LANE, "akao")
    async with hold(key):  # 她正在跑的那一轮
        waiting = asyncio.create_task(
            nudge_once(lane=LANE, persona_id="akao", now=_at(21, 32))
        )
        await _queued_behind(key)
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
        assert await nudge_once(
            lane=LANE, persona_id="akao", now=_at(21, minute)
        ) is not None, "收件箱的提前叫醒卡住了：还有没看过的，却叫不醒她"
    assert await _unread_ids() == set()
    assert any("楼下有人按门铃。" in seen for seen in stub_life.prompts)
    assert await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 40)) is None


@pytest.mark.integration
async def test_two_ticks_queued_behind_her_round_wake_her_once(nudge_db, stub_life, named):
    """她正在跑一轮，这条钟的两拍叠在一起排在后面（钟每一拍都不等上一拍跑完）。叫醒她的那条
    只叫醒她一次：先轮到的那一拍跑那一轮，后轮到的那一拍什么都不做，也不出错。判"是什么叫醒
    了她"跟跑那一轮不在同一次占用里的话，两拍会各自判出同一条、各记一遍"开始了"。"""
    from app.living.moment import life_moment_lock_key
    from app.living.received import receive
    from app.living.serial import hold

    await run_moment(lane=LANE, persona_id="akao", now=_at(21, 30))
    doorbell = _to_her("楼下有人按门铃。", at=_at(21, 31))
    await receive(doorbell)

    async with hold(life_moment_lock_key(LANE, "akao")):  # 她正在跑的那一轮
        ticks = asyncio.gather(
            nudge_once(lane=LANE, persona_id="akao", now=_at(21, 32)),
            nudge_once(lane=LANE, persona_id="akao", now=_at(21, 33)),
            return_exceptions=True,
        )
        await asyncio.sleep(0.5)  # 两拍都走到占用门口
    outcomes = await ticks

    assert not [o for o in outcomes if isinstance(o, BaseException)], outcomes
    woke = [o for o in outcomes if o is not None]
    assert [o.moment_id for o in woke] == [f"nudge:inbox:{doorbell.message_id}"]
    assert len(await _all_moments()) == 2


@pytest.mark.integration
async def test_the_message_that_woke_her_is_in_that_round_even_if_earlier_ones_arrive_as_it_starts(
    nudge_db, stub_life, named, monkeypatch
):
    """那一轮已经定下是谁叫醒的、还没读收件箱的那一刻，到了一批发生得更早的消息，多到一轮
    摆不下：叫醒她的那条照样在那一轮里。"""
    from app.living import moment as moment_mod
    from app.living.received import RECEIVED_LIMIT, receive

    await run_moment(lane=LANE, persona_id="akao", now=_at(21, 30))
    doorbell = _to_her("楼下有人按门铃。", at=_at(21, 31))
    await receive(doorbell)

    real = moment_mod.read_snapshot

    async def a_pile_arrives_first(**kwargs):
        for i in range(RECEIVED_LIMIT):
            await receive(_to_her(f"第 {i} 件事。", at=_at(21, 0, i)))
        return await real(**kwargs)

    monkeypatch.setattr(moment_mod, "read_snapshot", a_pile_arrives_first)
    woken = await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 32))

    assert woken is not None
    assert woken.moment_id == f"nudge:inbox:{doorbell.message_id}"
    assert "楼下有人按门铃。" in stub_life.prompts[-1], "叫醒她的那条不在那一轮里"
    assert doorbell.message_id not in await _unread_ids()


@pytest.mark.integration
async def test_a_round_a_message_woke_that_failed_is_retried_as_itself_whatever_arrives(
    nudge_db, stub_life, named
):
    """收件箱里那条叫醒她，她做了一个动作，这一轮失败了。之后到了一批发生得更早的消息、手机
    上也有人叫她：下一拍重跑的还是那一轮，同一个动作不落第二遍；叫她的那些排在它之后。"""
    from app.living.received import RECEIVED_LIMIT, receive

    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m0", place="家/客厅",
        doing="翻胶片", noted_at=_at(21, 20),
    )
    await run_moment(lane=LANE, persona_id="akao", now=_at(21, 30))
    rain = _to_her("窗外下起了雨。", at=_at(21, 31))
    await receive(rain)

    stub_life.calls = [("act", {"what": "起身把窗关上了"})]
    stub_life.after = _fails
    with pytest.raises(RuntimeError, match="做完那些事之后失败"):
        await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 32))
    stub_life.after = None
    assert await _acts() == 1, "前提没造出来：失败之前那个动作要已经落下"

    for i in range(RECEIVED_LIMIT):
        await receive(_to_her(f"第 {i} 件事。", at=_at(21, 0, i)))
    summons = await _incoming(_DM, body="在吗", at=_at(21, 33))

    again = await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 34))

    assert again is not None
    assert again.moment_id == f"nudge:inbox:{rain.message_id}", (
        "没跑完的那一轮重跑时换了身份"
    )
    assert "窗外下起了雨。" in stub_life.prompts[-1], "重跑的那一轮里没有叫醒她的那条"
    assert await _acts() == 1, "失败之前做过的那个动作，重跑时又落了一遍"
    assert rain.message_id not in await _unread_ids()

    then = await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 35))
    assert then is not None and then.moment_id == f"nudge:{summons}", (
        "手机上叫她的那条被重跑那一轮挡掉了，没有轮到它"
    )


@pytest.mark.integration
async def test_a_round_a_summons_woke_that_failed_is_retried_as_itself_whatever_arrives(
    nudge_db, stub_life, named
):
    """手机上那条叫醒她，她做了一个动作，这一轮失败了。之后手机上来了更新的一条叫她、收件箱
    里也到了一条：下一拍重跑的还是那一轮，同一个动作不落第二遍。"""
    from app.living.received import receive

    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m0", place="家/客厅",
        doing="翻胶片", noted_at=_at(21, 20),
    )
    await run_moment(lane=LANE, persona_id="akao", now=_at(21, 30))
    first = await _incoming(_DM, body="在吗", at=_at(21, 31))

    stub_life.calls = [("act", {"what": "放下胶片去拿手机"})]
    stub_life.after = _fails
    with pytest.raises(RuntimeError, match="做完那些事之后失败"):
        await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 32))
    stub_life.after = None
    assert await _acts() == 1, "前提没造出来：失败之前那个动作要已经落下"

    newer = await _incoming(
        _GROUP, body=" 你说呢", at=_at(21, 33), sender=_SOMEONE,
        sender_name="路人", names_bot=_AKAO_BOT_UID,
    )
    await receive(_to_her("千凪在厨房喊你吃饭。", at=_at(21, 33)))

    again = await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 34))

    assert again is not None
    assert again.moment_id == f"nudge:{first}", "没跑完的那一轮重跑时换了身份"
    assert await _acts() == 1, "失败之前做过的那个动作，重跑时又落了一遍"

    then = await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 35))
    assert then is not None and then.moment_id == f"nudge:{newer}"
