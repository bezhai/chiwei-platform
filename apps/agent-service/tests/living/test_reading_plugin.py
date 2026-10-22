"""reading 插件：她读别人发到她手机上的文件 —— 两只手（:class:`app.living.reading.files.Reader` 的方法）、
读书收件箱（她拿起一个文件时发一条进来，处理函数读那一程）、把旧读书队列里剩下的搬进读书收件箱的那条
运维路由。

``phone``、``residents`` 服务用替身。读什么、怎么读、读完记下什么在 ``test_reading.py``、
``test_reading_agent.py``、``test_reading_inbox.py``；这里只验插件这一面：登记了什么，她能读的就是
``phone`` 服务给的那些会话里的。
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.agent.round import Round, in_round
from app.agent.tooling import Tool
from tests.living.conftest import NamesRead

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))
_NOW = dt.datetime(2026, 7, 25, 21, 30, tzinfo=_CST)


class _PhoneWithNothing:
    """``phone`` 服务的替身：她手机上一条会话都没有；问过什么记下来。"""

    def __init__(self) -> None:
        self.asked: list[tuple] = []

    async def reachable_conversations(self, *, persona_id: str, now: dt.datetime):
        self.asked.append((persona_id, now))
        return []


def _round() -> Round:
    return Round(
        app="agent-service",
        lane=LANE,
        participant="akao",
        profile="resident",
        round_id="2026-07-25T21:30+08:00",
        now=_NOW,
        reason="test",
        due=None,
    )


async def test_the_reading_plugin_places_both_hands_its_inbox_and_the_carry_over(app_host):
    """reading 插件登记读东西的两只手（裁剪时都留着）、读书收件箱、搬运旧读书队列的那条运维路由（三项
    检查都开着）。"""
    host = await app_host("agent-service")
    mine = [r for r in host.registered() if r.plugin == "reading"]

    assert {(r.kind, r.name) for r in mine} == {
        ("tool", "look_for_something_to_read"),
        ("tool", "read_a_bit"),
        ("inbox", "reading"),
        ("route", "POST /admin/reading/carry-over"),
    }
    assert {
        r.name: (r.detail["spec"].trim, r.detail["spec"].ends_round)
        for r in mine
        if r.kind == "tool"
    } == {"look_for_something_to_read": ("kept", False), "read_a_bit": ("kept", False)}
    (route,) = [r for r in mine if r.kind == "route"]
    assert (
        route.detail["inner_secret"],
        route.detail["lane_match"],
        route.detail["answers_with_lane"],
    ) == (True, True, True)


@pytest.mark.integration
async def test_what_she_can_read_comes_from_the_conversations_the_phone_gives(living_db):
    """她能读的严格等于她看得见的：会话集合就是 ``phone`` 服务按这一轮的身份和『现在』交回的那一份，它说
    一条都没有，她就没有能读的。"""
    from app.living.reading.files import Reader

    phone = _PhoneWithNothing()
    reader = Reader(phone=phone, residents=NamesRead())

    with in_round(_round()):
        listed = await Tool(reader.look_for_something_to_read).invoke({})

    assert listed == "没有谁给你发过什么可以读的东西。"
    assert phone.asked == [("akao", _NOW)]
