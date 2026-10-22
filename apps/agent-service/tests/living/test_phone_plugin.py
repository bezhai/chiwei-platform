"""phone 插件对象（:class:`app.living.phone.handset.Handset`）：她的手机 —— 三只手、两段、一轮落地时
记下她看过的，和别的功能经 ``phone`` 服务问的那几件事。

``her_records``、``pictures`` 两个服务用替身，库是用例的库。手机上摆什么、怎么摆在 ``test_phone.py``，
视野名单的判据在 ``test_whitelist.py``；这里只验插件这一面：登记了什么，经服务问到的、两段摆出来的
和三只手看到的是同一份视野，而且这一份在一轮之内不变。
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.agent.tooling import Tool
from app.data.tx import tx
from tests.living.test_phone import (
    _AKAO_BOT_UID,
    _DM,
    _GROUP,
    _SOMEONE,
    _at,
    _incoming,
    _seed_world,
)

LANE = "coe-living"
_NOW = _at(21, 0)


class _SpokeAt:
    """``her_records`` 服务的替身：她在每条会话上都是 ``at`` 那一刻最后开的口；问过哪几条记下来。"""

    def __init__(self, at: dt.datetime | None = None) -> None:
        self.at = at
        self.asked: list[str] = []

    async def last_spoke_in(self, *, lane: str, persona_id: str, channel_id: str):
        assert (lane, persona_id) == (LANE, "akao")
        self.asked.append(channel_id)
        return self.at


class _NoPictures:
    """``pictures`` 服务的替身：她手上一张图都没有。"""

    async def hers_among(self, *, lane, persona_id, file_names):
        return set()


def _phone(her_records=None):
    from app.living.phone.handset import Handset

    return Handset(her_records=her_records or _SpokeAt(), pictures=_NoPictures())


async def test_the_phone_plugin_places_her_three_hands_both_sections_and_her_glances(app_host):
    """phone 插件登记看手机的三只手（裁剪时都留着）、手机上没看的和刚来的两段、一轮落地时记下她看过的
    那一步，交出 ``phone`` 服务；清单 ``resident`` 把它们排在各自的位置。"""
    host = await app_host("agent-service")
    mine = [r for r in host.registered() if r.plugin == "phone"]

    assert {(r.kind, r.name) for r in mine} == {
        ("service", "phone"),
        ("tool", "look_at_phone"),
        ("tool", "look_through_your_phone"),
        ("tool", "look_up_contact"),
        ("section", "phone.unread"),
        ("section", "phone.arrived"),
        ("commit", "phone.glances"),
    }
    assert {
        r.name: (r.detail["spec"].trim, r.detail["spec"].ends_round)
        for r in mine
        if r.kind == "tool"
    } == {
        "look_at_phone": ("kept", False),
        "look_through_your_phone": ("kept", False),
        "look_up_contact": ("kept", False),
    }


@pytest.mark.integration
async def test_what_she_can_see_on_her_phone_is_fixed_for_the_whole_round(living_db, in_a_moment):
    """她这一轮看得见哪些会话，在这一轮里只定一次：经服务问的（发消息、读文件用的那份）、两段摆出来的、
    她自己翻手机看到的都是这一份。一个群在这一轮中途点了她的名，这一轮里它不出现在任何一处，下一轮才进来。"""
    await _seed_world()
    await _incoming(_DM, text_body="在吗", at=_NOW - dt.timedelta(minutes=10))
    phone = _phone()

    async with in_a_moment("akao", now=_NOW) as her_round:
        first = await phone.reachable_conversations(persona_id="akao", now=_NOW)
        assert [c.channel_id for c in first] == [str(_DM)]

        await _incoming(
            _GROUP,
            text_body="赤尾你看这个",
            at=_NOW - dt.timedelta(minutes=1),
            sender=_SOMEONE,
            sender_name="路人",
            names_bot=_AKAO_BOT_UID,
        )

        assert await phone.reachable_conversations(persona_id="akao", now=_NOW) == first
        assert (
            await phone.reachable_conversation(
                persona_id="akao", channel_id=str(_GROUP), now=_NOW
            )
            is None
        )
        arrived = await phone.arrived_section(her_round)
        listed = await Tool(phone.look_through_your_phone).invoke({})
        assert str(_GROUP) not in arrived, "这一轮中途点名的群出现在了手机上刚来的那段里"
        assert str(_GROUP) not in listed, "这一轮中途点名的群出现在了她翻到的会话列表里"

    async with in_a_moment("akao", now=_NOW + dt.timedelta(minutes=10)):
        later = await phone.reachable_conversations(
            persona_id="akao", now=_NOW + dt.timedelta(minutes=10)
        )
    assert {c.channel_id for c in later} == {str(_DM), str(_GROUP)}


@pytest.mark.integration
async def test_both_phone_sections_come_from_one_look_and_her_records_say_when_she_spoke(
    living_db, in_a_moment
):
    """手机上没看的（状态）和刚来的（输入）是同一次查到的信封；她上次在那条会话上什么时候开的口，问的是
    ``her_records`` 服务。"""
    await _seed_world()
    await _incoming(_DM, text_body="在吗", at=_NOW - dt.timedelta(minutes=10))
    records = _SpokeAt(_NOW - dt.timedelta(hours=1))
    phone = _phone(records)

    async with in_a_moment("akao", now=_NOW) as her_round:
        unread = await phone.unread_section(her_round)
        arrived = await phone.arrived_section(her_round)

    assert unread.startswith("手机上还没看的")
    assert arrived.startswith("手机上刚来的")
    for text in (unread, arrived):
        assert f"channel_id={_DM}" in text
        assert "你上次在这儿开口是 20:00 CST" in text
    assert records.asked == [str(_DM)], "两段各查了一遍信封"


@pytest.mark.integration
async def test_what_she_looked_at_counts_as_read_only_when_her_round_commits(
    living_db, in_a_moment
):
    """看手机只把读到哪攒进这一轮；落地那个事务里的 ``phone.glances`` 一步跑了，才算她看过。"""
    from app.living.phone.handset import NEVER_LOOKED, read_through

    await _seed_world()
    await _incoming(_DM, text_body="在吗", at=_NOW - dt.timedelta(minutes=10))
    phone = _phone()

    async with in_a_moment("akao", now=_NOW, finishes=False) as her_round:
        await Tool(phone.look_at_phone).invoke({"channel_id": str(_DM)})

    assert await read_through(lane=LANE, persona_id="akao", channel_id=str(_DM)) == NEVER_LOOKED

    async with tx():
        await phone.commit_glances(her_round)

    assert await read_through(lane=LANE, persona_id="akao", channel_id=str(_DM)) != NEVER_LOOKED
