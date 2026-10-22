"""household 插件对象（:class:`app.living.household.mailbox.Mailbox`）：传到她这里的消息和她往外发的
消息 —— 收件箱里还没看过的那一段、一轮落地时记成看过、一轮开始前和落地之后把她做了的事发出去，和
别的功能经 ``received`` 服务问的那几件事。

``residents``、``her_records`` 两个服务用替身，库是用例的库。收件怎么存在 ``test_received.py``，往外
发什么、怎么发在 ``test_outgoing.py``；这里只验插件这一面：登记了什么、服务交出去的是什么、往外发只在
她的轮次队列上跑。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pytest

from app.agent.round import Round, in_round
from app.data.persist import fetch_all
from app.data.tx import tx
from app.host import Coalesce, Signal, WakeSource
from tests.living.conftest import NamesRead, clock_at, her_queue

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))
_NOW = dt.datetime(2026, 7, 25, 21, 30, tzinfo=_CST)


class _NothingDoneYet:
    """``her_records`` 服务的替身：她还什么都没做过（两个号都是 0）。"""

    async def marks(self, *, lane: str, persona_id: str) -> tuple[int, int]:
        assert (lane, persona_id) == (LANE, "akao")
        return (0, 0)


def _mailbox():
    from app.living.household.mailbox import Mailbox

    return Mailbox(residents=NamesRead(), her_records=_NothingDoneYet())


def _round(*, must_show: str | None = None) -> Round:
    @dataclass(frozen=True)
    class _Due:
        reason: str = "test"
        must_show: str | None = None

    return Round(
        app="agent-service",
        lane=LANE,
        participant="akao",
        profile="resident",
        round_id="2026-07-25T21:30+08:00",
        now=_NOW,
        reason="test",
        due=_Due(must_show=must_show),
    )


async def _received(message_id: str, body: str, *, at: dt.datetime, wakes: bool = True) -> None:
    from app.data.persist import insert_idempotent
    from app.living.household.received import ReceivedMessage

    await insert_idempotent(
        ReceivedMessage(
            lane=LANE,
            persona_id="akao",
            message_id=message_id,
            sender="world",
            body=body,
            message_time=at,
            wakes_recipient=wakes,
        )
    )


async def test_the_household_plugin_places_her_inbox_and_what_she_sends_out(app_host):
    """household 插件登记收件箱里还没看过的那一段、一轮开始前和落地之后往外发、落地时记成看过，开始
    接收时开三姐妹的收件箱，交出 ``received`` 服务；往外发失败只记日志，这一轮照样算数。"""
    from app.agent.contracts import Continue

    host = await app_host("agent-service")
    mine = [r for r in host.registered() if r.plugin == "household"]

    assert {(r.kind, r.name) for r in mine} == {
        ("service", "received"),
        ("section", "household.received"),
        ("round_start", "outgoing.before"),
        ("commit", "household.read"),
        ("after_commit", "outgoing.after"),
        ("inboxes_at_start", "Mailbox.open_inboxes"),
    }
    (after,) = [r for r in mine if r.kind == "after_commit"]
    assert after.detail["on_failure"] == Continue()


@pytest.mark.integration
async def test_sending_what_she_did_outside_her_turns_refuses(living_db):
    """往外讲她做了什么，靠的是"她的经历这一刻全都落了库"：只有她的轮次队列上一次只跑一轮。从她的
    队列之外调（哪怕这一轮绑着），当场报错，一行都不写。"""
    mailbox = _mailbox()

    with in_round(_round()), pytest.raises(RuntimeError, match="轮次队列"):
        await mailbox.send_what_she_did(_round())

    assert await fetch_all("SELECT * FROM data_outgoing_up_to") == []


@pytest.mark.integration
async def test_sending_what_she_did_on_her_turns_tells_from_where_she_is_now(living_db):
    """在她的队列上跑的那一次照常讲：第一次只记下从她现在最新的经历讲起（``her_records`` 服务给的号）。"""
    mailbox = _mailbox()

    async def due(participant, batch, clock):
        batch.close()
        return _round()

    async def run(her_round: Round) -> None:
        with in_round(her_round):
            await mailbox.send_what_she_did(her_round)

    await her_queue("akao", clock=clock_at(_NOW)).take(
        WakeSource("telling", Coalesce.QUEUE_EACH, due, run), Signal(at=_NOW)
    )

    rows = await fetch_all("SELECT persona_id, happening_seq, whereabouts_seq FROM data_outgoing_up_to")
    assert [dict(r) for r in rows] == [
        {"persona_id": "akao", "happening_seq": 0, "whereabouts_seq": 0}
    ]


@pytest.mark.integration
async def test_what_is_shown_this_round_is_what_the_round_marks_read(living_db):
    """收件箱那一段这一轮只读一次；落地那一步记成看过的就是摆出来的那几条，叫醒她这一轮的那条一定在
    里面。这一轮跑着的时候新到的留给下一轮。"""
    mailbox = _mailbox()
    await _received("m1", "窗外下起了雨。", at=_NOW - dt.timedelta(minutes=5))
    her_round = _round(must_show="m1")

    with in_round(her_round):
        shown = await mailbox.received_section(her_round)
        await _received("m2", "有人敲门。", at=_NOW - dt.timedelta(minutes=10))
        assert await mailbox.received_section(her_round) == shown
    async with tx():
        await mailbox.mark_read(her_round)

    assert "窗外下起了雨。" in shown and "有人敲门。" not in shown
    read = await fetch_all("SELECT message_id FROM data_received_read")
    assert [r["message_id"] for r in read] == ["m1"]


@pytest.mark.integration
async def test_the_received_service_names_the_earliest_waking_one_and_reads_a_day(living_db):
    """``received`` 服务：还没看过、要叫醒她的最早那条（不叫醒的不挡在前面）；一段时间里收到的，按消息
    自带的时间排，摆出来跟她一轮里读到的是同一个样子。"""
    mailbox = _mailbox()
    await _received("quiet", "楼下有人走过。", at=_NOW - dt.timedelta(minutes=30), wakes=False)
    await _received("loud", "有人敲门。", at=_NOW - dt.timedelta(minutes=20))
    await _received("later", "门开了。", at=_NOW - dt.timedelta(minutes=10))

    assert await mailbox.earliest_waking_unread(lane=LANE, persona_id="akao") == "loud"
    day = await mailbox.between(
        lane=LANE,
        persona_id="akao",
        since=_NOW - dt.timedelta(minutes=25),
        until=_NOW,
    )
    assert [mailbox.line(m) for m in day] == ["有人敲门。", "门开了。"]
