"""强提醒可以提前一缝，但不代她回复。

私聊来了、群里被点名 → 她被带到那一刻、看得到信封。**回不回是她的输出**，不进验收
条件；所以这里必须同时反证相反方向：存在被 @ 之后她没开口、而系统一切正常的轮次。

只加一个"跳过间隔"的开关是不够的，三个坑各有一条用例：

  1. 提前的那缝会成为"最近一缝"，把常规节奏往后推 —— 常规的间隔判断只认常规缝；
  2. 同一分钟会跟常规缝撞 ``moment_id`` —— 提前缝的身份是**把她叫来的那条消息**，
     不是钟点，天然撞不上；
  3. 提前缝推进共享感知游标是对的，不该让常规缝重复感知 —— 游标在同一张表上续接。

顺带还有一条不是坑但会烧钱的：**同一条消息只提前一次**。真人手机是新消息才震，
躺着的未读不会一直震；提前缝的身份就是那条消息，所以"只震一次"是结构，不是冷却。
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.agent.neutral import Message, Role
from app.agent.runtime_context import agent_context
from app.data import session as session_mod
from app.living.happening import record_happening
from app.living.moment import (
    DEFAULT_LIFE_MOMENT_MINUTES,
    LifeMoment,
    latest_moment,
    run_moment,
)
from app.living.nudge import nudge_once
from app.living.records import KIND_SPEECH, MEDIUM_IN_PERSON
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
    from tests.runtime.conftest import migrate

    for cls in (LooseEnd, LifeMoment):
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
    """替身 life：她这一缝调了哪些工具、最后说了什么，由用例写死。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.said = "继续"
        self.prompts: list[str] = []

    async def run(self, messages, **kwargs):
        self.prompts.append(messages[0].content)
        with agent_context(kwargs["context"]):
            from app.living.moment import MOMENT_TOOLS

            tools = {t.name: t for t in MOMENT_TOOLS}
            for name, args in self.calls:
                await tools[name].invoke(args)
        return Message(role=Role.ASSISTANT, content=self.said)


@pytest.fixture
def stub_life(monkeypatch):
    from app.living import moment as moment_mod

    async def fake_find_persona(persona_id: str):
        return SimpleNamespace(display_name="赤尾", persona_core="她泡抹茶店。")

    monkeypatch.setattr(moment_mod, "find_persona", fake_find_persona)

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
    # 这一缝照常留痕、游标照常推进 —— 没开口不是异常状态。
    assert (await latest_moment(lane=LANE, persona_id="akao")).moment_id == \
        moment.moment_id


@pytest.mark.integration
async def test_group_chatter_that_does_not_name_her_waits_for_the_next_regular_moment(
    nudge_db, stub_life
):
    await run_moment(lane=LANE, persona_id="akao", now=_at(21, 30))
    await _incoming(
        _GROUP, body="今天好热", at=_at(21, 31), sender=_SOMEONE, sender_name="路人"
    )

    assert await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 32)) is None

    later = await run_moment(lane=LANE, persona_id="akao", now=_at(21, 40))
    assert later is not None
    assert "路人" in stub_life.prompts[-1], (
        f"不点名的消息不提前缝，但下一个常规缝一定看得到。她看到的是：\n"
        f"{stub_life.prompts[-1]}"
    )


# --------------------------------------------------------------------------
# 二 · 坑 1：提前的那缝不能把常规节奏往后推
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_an_early_moment_does_not_delay_the_regular_rhythm(nudge_db, stub_life):
    await run_moment(lane=LANE, persona_id="akao", now=_at(21, 30))
    await _incoming(_DM, body="在吗", at=_at(21, 33))
    early = await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 34))
    assert early is not None

    # 21:40 是原本就该来的那一缝。按"最近一缝"判间隔的话，21:34 到 21:40 只有
    # 六分钟，这一缝会被吞掉 —— 她的节奏就被每一条私聊往后拖。
    on_time = await run_moment(lane=LANE, persona_id="akao", now=_at(21, 40))

    assert on_time is not None, "提前的那一缝把常规节奏往后推了"
    assert on_time.nudged is False
    assert [m.began_at for m in await _all_moments()] == [
        _at(21, 30), _at(21, 34), _at(21, 40)
    ]


# --------------------------------------------------------------------------
# 三 · 坑 2：同一分钟不能跟常规缝撞身份
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_an_early_moment_in_the_same_minute_is_still_its_own_moment(
    nudge_db, stub_life
):
    regular = await run_moment(lane=LANE, persona_id="akao", now=_at(21, 30))
    await _incoming(_DM, body="在吗", at=_at(21, 30, 10))

    early = await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 30, 20))

    assert early is not None, (
        "提前缝跟常规缝撞了 moment_id —— 自然键相同，这一缝被当成重放丢掉了"
    )
    assert early.moment_id != regular.moment_id
    assert len(await _all_moments()) == 2


# --------------------------------------------------------------------------
# 四 · 坑 3：提前缝推进游标，常规缝不重复感知
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_regular_moment_does_not_re_perceive_what_the_early_one_read(
    nudge_db, stub_life
):
    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m0", place="家/客厅",
        doing="翻胶片", noted_at=_at(21, 20),
    )
    await run_moment(lane=LANE, persona_id="akao", now=_at(21, 30))
    await record_happening(
        lane=LANE, happening_id="h1", actor="ayana", place="家/客厅",
        kind=KIND_SPEECH, content="姐我出门了。", occurred_at=_at(21, 32),
        audience=["akao"], medium=MEDIUM_IN_PERSON,
    )
    await _incoming(_DM, body="在吗", at=_at(21, 33))

    early = await nudge_once(lane=LANE, persona_id="akao", now=_at(21, 34))
    assert early.perceived == 1, "提前缝没读到刚发生的事"

    on_time = await run_moment(lane=LANE, persona_id="akao", now=_at(21, 40))

    assert on_time.after_seq == early.next_seq, (
        "常规缝的起点没接上提前缝 —— 游标各推各的"
    )
    assert on_time.perceived == 0, "同一句话她听见了两遍"


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
# 六 · 游标跟着一缝落地；信封不漏掉在叫她的那条
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_moment_that_blew_up_did_not_read_her_phone(nudge_db, stub_life):
    """这一缝崩了 = 她没看过 = 下一缝原样再看到。

    工具返回不等于她看见了。游标跟 ``LifeMoment`` 在同一个事务里落库，缝没落地就
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
    ) == (0, ""), "缝没跑完，游标却推过去了 —— 那条消息就此永久消失"
    assert await _all_moments() == []


@pytest.mark.integration
async def test_a_finished_moment_did_read_her_phone(nudge_db, stub_life):
    """反面：缝跑完了，看过的就是看过了。"""
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
    """一个轮询间隔里来了两条私聊 —— 提前的那一缝里两条都得在信封上。

    每拍只取**最新**那条召唤把她带过来，是有意的（一缝把她带到就够了，带两次是
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
