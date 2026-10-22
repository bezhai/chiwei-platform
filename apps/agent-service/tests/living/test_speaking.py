"""speaking 插件：她在手机上开口和撤回 —— 两只手（:class:`app.living.speaking.mouth.Mouth` 和
:class:`app.living.speaking.takeback.TakeBack` 的方法）、嘴交出去之前那道检查的段落、事后核对落没落地
的那条钟、两条出站。

``phone``、``her_records``、``pictures`` 服务用替身。开口和撤回本身在 ``test_mouth.py`` 和
``test_takeback.py``；这里只验插件这一面：登记了什么，两只手问的是 ``phone`` 服务的哪一道闸。
"""
from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.agent.round import Round, in_round
from app.agent.tooling import Tool
from app.data.persist import insert_append

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))
_NOW = dt.datetime(2026, 7, 25, 21, 30, tzinfo=_CST)
_DM = str(uuid.uuid5(uuid.NAMESPACE_OID, "conv-dm-bezhai-akao"))


class _PhoneWithout:
    """``phone`` 服务的替身：她手机上没有这条会话，她的 bot 也不在里面；问过什么记下来。"""

    def __init__(self) -> None:
        self.asked: list[tuple] = []

    async def reachable_conversation(self, *, persona_id: str, channel_id: str, now):
        self.asked.append(("in sight", persona_id, channel_id, now))
        return None

    async def conversation_her_bot_is_in(self, *, persona_id: str, channel_id: str):
        self.asked.append(("her bot is in", persona_id, channel_id))
        return None


class _NothingRecorded:
    """``her_records`` 服务的替身：一被问到就说明这一步不该走到这儿。"""

    async def record_happening(self, **kwargs):
        raise AssertionError("没发出去的话不该记成她说过")


class _NoPictures:
    """``pictures`` 服务的替身：一被问到就说明这一步不该走到这儿。"""


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


async def test_the_speaking_plugin_places_both_hands_the_check_and_the_landing(app_host):
    """speaking 插件登记开口和撤回两只手（裁剪时都留着）、嘴交出去之前那道检查的段落、核对落没落地的
    那条钟，和她说的话、撤回各自的出站。"""
    host = await app_host("agent-service")
    mine = [r for r in host.registered() if r.plugin == "speaking"]

    assert {(r.kind, r.name) for r in mine} == {
        ("tool", "send_message"),
        ("tool", "take_back_message"),
        ("segment", "guard_output_safety"),
        ("clock", "landing"),
        ("outbound", "ChatResponseSegment->chat_response"),
        ("outbound", "Recall->recall"),
    }
    assert {
        r.name: (r.detail["spec"].trim, r.detail["spec"].ends_round)
        for r in mine
        if r.kind == "tool"
    } == {"send_message": ("kept", False), "take_back_message": ("kept", False)}


async def test_she_speaks_only_into_a_conversation_the_phone_says_she_can_see():
    """开口问的是 ``phone`` 服务过闸的那一份（这一轮的视野 ∩ bot 还在），按这一轮的身份和『现在』问；
    它说没有就不发，什么都不记。"""
    from app.living.speaking.mouth import Mouth

    phone = _PhoneWithout()
    mouth = Mouth(phone=phone, her_records=_NothingRecorded(), pictures=_NoPictures())

    with in_round(_round()):
        outcome = await Tool(mouth.send_message).invoke({"what": "在吗", "channel_id": f" {_DM} "})

    assert "不是你手机上的会话" in str(outcome)
    assert phone.asked == [("in sight", "akao", _DM, _NOW)]


@pytest.mark.integration
async def test_taking_back_asks_whether_her_bot_is_still_there_not_whether_she_sees_it(living_db):
    """撤回问的是 ``phone`` 服务不过视野的那一份：她撤的是自己已经发出去的话，跟她现在还看不看得见那条
    会话是两件事。她的 bot 不在了就撤不了，如实说。"""
    from app.living.speaking.mouth import STATE_HANDED_OFF, SpokenOutbound
    from app.living.speaking.takeback import TakeBack

    spoken = SpokenOutbound(
        lane=LANE,
        outbound_id=uuid.uuid5(uuid.NAMESPACE_OID, "she-said-it").hex,
        ver=1,
        persona_id="akao",
        channel_id=_DM,
        moment_id="2026-07-25T21:00+08:00",
        said="我先睡了",
        state=STATE_HANDED_OFF,
        claimed_at=_NOW - dt.timedelta(minutes=30),
        settled_at=_NOW - dt.timedelta(minutes=30),
    )
    assert await insert_append(spoken, expected_current_ver=0) == 1
    phone = _PhoneWithout()
    taking_back = TakeBack(phone=phone, her_records=_NothingRecorded())

    with in_round(_round()):
        outcome = await Tool(taking_back.take_back_message).invoke(
            {"message_id": spoken.outbound_id}
        )

    assert "撤不了" in str(outcome)
    assert phone.asked == [("her bot is in", "akao", _DM)]
