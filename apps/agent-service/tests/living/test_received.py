"""她收到的消息：三姐妹的收件箱只存储，她下一轮醒来时才读。

收件处理只做一件事：按 (泳道, 人, 消息 id) 存一行，存和去重是同一条语句。通信机制的投递是
至少一次，world 会带着原来的 id 重发没发完的告知，所以同一条到两遍、存到一半进程没了，都是
正常运转里会发生的事——这里逐一走一遍。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.agent.neutral import Message as Said
from app.agent.neutral import Role
from app.data import session as session_mod
from app.living import participants as participants_mod
from app.living import received as received_mod
from app.living.received import (
    ReceivedMessage,
    open_inboxes,
    receive,
    render_received,
    unread_received,
)
from app.messaging.message import Kind, Message, new_message
from app.messaging.receiving import INBOX_REGISTRY

# 真 broker（带延时插件）+ 通信机制那几张表，跟 ``tests/messaging`` 用同一份。
from tests.messaging.conftest import (  # noqa: F401 — 形参名就是 fixture 名
    broker,
    delayed_broker,
    messaging_db,
)

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))
_NAMES = {"akao": "赤尾", "ayana": "绫奈", "chinagi": "千凪"}


def _at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 7, 25, hour, minute, tzinfo=_CST)


class _Crash(Exception):
    """存储那一步出事：进程在这一刻没了，或者库连不上。"""


@pytest.fixture
async def inboxes(living_db, monkeypatch):
    """三姐妹的收件箱已经按人设表开好（跟线上开始接收时同一个函数）。"""
    monkeypatch.setenv("LANE", LANE)

    async def find_persona(persona_id: str):
        return SimpleNamespace(persona_id=persona_id, display_name=_NAMES[persona_id])

    monkeypatch.setattr(participants_mod, "find_persona", find_persona)
    monkeypatch.setattr(participants_mod, "_known", None)
    await open_inboxes()
    return living_db


def _from(
    sender: str,
    body: str,
    *,
    to: str = "绫奈",
    at: dt.datetime | None = None,
    message_id: str | None = None,
    kind: Kind = Kind.MESSAGE,
) -> Message:
    return new_message(
        sender=sender,
        recipient=to,
        body=body,
        kind=kind,
        time=at or _at(21, 30),
        message_id=message_id,
    )


async def _stored(persona_id: str = "ayana") -> list[ReceivedMessage]:
    async with session_mod.get_session() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT * FROM data_received_message "
                    "WHERE lane = :l AND persona_id = :p ORDER BY created_at"
                ),
                {"l": LANE, "p": persona_id},
            )
        ).mappings().all()
    return [ReceivedMessage(**{k: r[k] for k in ReceivedMessage.model_fields}) for r in rows]


# ---------------------------------------------------------------------------
# 收件箱
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_each_resident_has_an_inbox_under_her_name_that_takes_no_questions(inboxes):
    assert sorted(INBOX_REGISTRY) == sorted(_NAMES.values())
    for spec in INBOX_REGISTRY.values():
        assert spec.on_message is receive
        assert spec.on_question is None, "这一期她还不回答谁的提问"


@pytest.mark.integration
async def test_a_message_is_stored_for_the_resident_it_was_sent_to(inboxes):
    message = _from("world", "窗外下起了雨。", at=_at(21, 41))

    await receive(message)

    (row,) = await _stored("ayana")
    assert (row.lane, row.persona_id, row.message_id) == (LANE, "ayana", message.message_id)
    assert (row.sender, row.body, row.message_time) == ("world", "窗外下起了雨。", _at(21, 41))
    assert await _stored("akao") == []


@pytest.mark.integration
async def test_the_same_message_delivered_twice_is_stored_once(inboxes):
    message = _from("world", "窗外下起了雨。")

    await receive(message)
    await receive(message)

    assert len(await _stored()) == 1


@pytest.mark.integration
async def test_a_crash_before_the_row_is_written_loses_nothing(inboxes, monkeypatch):
    """存之前出事：这次处理失败，通信机制会再投；再投的那一次照常存下。"""
    real = received_mod.insert_idempotent

    async def crash(*args, **kwargs):
        raise _Crash("写之前没了")

    monkeypatch.setattr(received_mod, "insert_idempotent", crash)
    message = _from("world", "窗外下起了雨。")
    with pytest.raises(_Crash):
        await receive(message)
    assert await _stored() == []

    monkeypatch.setattr(received_mod, "insert_idempotent", real)
    await receive(message)

    assert [r.message_id for r in await _stored()] == [message.message_id]


@pytest.mark.integration
async def test_a_crash_after_the_row_is_written_duplicates_nothing(inboxes, monkeypatch):
    """存下之后、确认之前出事：通信机制会再投同一条，再投的那一次不会多存一行。"""
    real = received_mod.insert_idempotent

    async def write_then_crash(*args, **kwargs):
        await real(*args, **kwargs)
        raise _Crash("写完、还没确认就没了")

    monkeypatch.setattr(received_mod, "insert_idempotent", write_then_crash)
    message = _from("world", "窗外下起了雨。")
    with pytest.raises(_Crash):
        await receive(message)

    monkeypatch.setattr(received_mod, "insert_idempotent", real)
    await receive(message)

    assert [r.message_id for r in await _stored()] == [message.message_id]


@pytest.mark.integration
async def test_a_not_delivered_notice_is_logged_not_stored(inboxes, caplog):
    """她不定时发消息，"没有送达"的告知不该出现；真来了也不是她的经历，只留一条日志。"""
    notice = _from("绫奈", "你定的那条没有送达。", to="绫奈", kind=Kind.NOT_DELIVERED)

    with caplog.at_level(logging.WARNING, logger=received_mod.__name__):
        await receive(notice)

    assert await _stored() == []
    assert notice.message_id in caplog.text


@pytest.mark.integration
async def test_a_message_for_a_name_she_does_not_go_by_fails_and_is_retried(inboxes):
    """收件人不是三姐妹之一：收件箱是按这份对照开的，到这里只能是哪里错了。失败交给通信机制
    重试、最后进死信，不能悄悄存到谁名下或者丢掉。"""
    with pytest.raises(RuntimeError, match="赤尾酱"):
        await receive(_from("world", "下雨了。", to="赤尾酱"))
    assert await _stored("akao") == []


# ---------------------------------------------------------------------------
# 她下一轮读到：按消息自带的时间排，只把放进这一轮的那几条记成看过
# ---------------------------------------------------------------------------


def test_world_reads_as_what_she_perceives_and_anyone_else_carries_a_name():
    """world 发来的是她察觉到的事，不标是谁说的；别人发来的带着发送方的名字。"""
    def item(sender: str, body: str, at: dt.datetime) -> ReceivedMessage:
        return ReceivedMessage(
            lane=LANE,
            persona_id="ayana",
            message_id=f"{sender}-{at:%H%M}",
            sender=sender,
            body=body,
            message_time=at,
        )

    shown = render_received(
        [
            item("world", "窗外下起了雨。", _at(21, 25)),
            item("千凪", "姐姐，饭好了。", _at(21, 26)),
            item("world", "楼下有人按门铃。", dt.datetime(2026, 7, 24, 23, 50, tzinfo=_CST)),
        ],
        now=_at(21, 30),
    )

    assert shown == (
        "这段时间传到你这里的：\n"
        "- 21:25 CST 窗外下起了雨。\n"
        "- 21:26 CST 千凪：姐姐，饭好了。\n"
        "- 07-24 23:50 CST 楼下有人按门铃。"
    )
    assert render_received([], now=_at(21, 30)) == "这段时间传到你这里的：（没有）"


@pytest.mark.integration
async def test_unread_messages_come_in_the_order_they_happened_not_the_order_they_arrived(
    inboxes,
):
    """world 的告知和姐妹的话走两条路，后到的可能先发生。"""
    later = _from("千凪", "姐姐，饭好了。", at=_at(21, 28))
    earlier = _from("world", "窗外下起了雨。", at=_at(21, 20))
    await receive(later)
    await receive(earlier)

    unread = await unread_received(lane=LANE, persona_id="ayana")

    assert [m.message_id for m in unread] == [earlier.message_id, later.message_id]


@pytest.mark.integration
async def test_a_round_takes_the_oldest_few_and_leaves_the_rest_for_the_next(inboxes):
    messages = [_from("world", f"第 {i} 件事。", at=_at(21, i)) for i in (3, 1, 2)]
    for m in messages:
        await receive(m)

    unread = await unread_received(lane=LANE, persona_id="ayana", limit=2)

    assert [m.body for m in unread] == ["第 1 件事。", "第 2 件事。"]


class _Round:
    """替身 life：记下每一轮新摆到她眼前的那条，可以在模型那一步做点什么、或者失败。"""

    def __init__(self) -> None:
        self.seen: list[str] = []
        self.meanwhile = None
        self.fails = False

    async def run(self, messages, **kwargs):
        self.seen.append(messages[-1].content)
        if self.meanwhile is not None:
            await self.meanwhile()
        if self.fails:
            raise RuntimeError("这一轮的模型调用失败了")
        return Said(role=Role.ASSISTANT, content="继续")


@pytest.fixture
async def her_round(inboxes, monkeypatch):
    """绫奈的一轮：真的 ``run_moment``，只有模型那一步是替身。"""
    from app.living import moment as moment_mod
    from app.living import persona as persona_mod
    from app.living.loose_ends import LooseEnd
    from app.living.moment import DEFAULT_LIFE_MOMENT_MINUTES, LifeMoment
    from tests.runtime.conftest import migrate

    for cls in (LooseEnd, LifeMoment):
        await migrate(cls, inboxes)

    async def find_persona(persona_id: str):
        return SimpleNamespace(display_name="绫奈", persona_core="她在念初二。")

    async def fixed_minutes() -> int:
        return DEFAULT_LIFE_MOMENT_MINUTES

    monkeypatch.setattr(persona_mod, "find_persona", find_persona)
    monkeypatch.setattr(moment_mod, "life_moment_minutes", fixed_minutes)
    runner = _Round()
    monkeypatch.setattr(moment_mod, "build_moment_runner", lambda: runner)
    return runner


async def _her_round(at: dt.datetime):
    from app.living.moment import run_moment

    return await run_moment(lane=LANE, persona_id="ayana", now=at)


@pytest.mark.integration
async def test_what_she_received_is_in_her_next_round_once(her_round):
    rain = _from("world", "窗外下起了雨。", at=_at(21, 25))
    dinner = _from("千凪", "姐姐，饭好了。", at=_at(21, 26))
    await receive(rain)
    await receive(dinner)

    await _her_round(_at(21, 30))
    await _her_round(_at(21, 40))

    first, second = her_round.seen
    assert "- 21:25 CST 窗外下起了雨。\n- 21:26 CST 千凪：姐姐，饭好了。" in first
    assert "这段时间传到你这里的：（没有）" in second, (
        f"看过的又摆了一遍：\n{second}"
    )
    assert await unread_received(lane=LANE, persona_id="ayana") == []


@pytest.mark.integration
async def test_a_round_that_fails_shows_them_again(her_round):
    """她跑失败的那一轮里看到的消息，下一轮再给她看一次：没跑完就不算看过。"""
    rain = _from("world", "窗外下起了雨。", at=_at(21, 25))
    await receive(rain)

    her_round.fails = True
    with pytest.raises(RuntimeError, match="模型调用失败"):
        await _her_round(_at(21, 30))
    her_round.fails = False
    await _her_round(_at(21, 31))

    assert len(her_round.seen) == 2
    assert all("窗外下起了雨。" in seen for seen in her_round.seen)
    assert await unread_received(lane=LANE, persona_id="ayana") == []


@pytest.mark.integration
async def test_what_arrives_while_she_is_in_a_round_stays_unread_for_the_next(her_round):
    """这一轮跑着的时候新到的消息，这一轮没给她看，就不能跟着记成看过。"""
    rain = _from("world", "窗外下起了雨。", at=_at(21, 25))
    doorbell = _from("world", "楼下有人按门铃。", at=_at(21, 31))
    await receive(rain)

    async def it_arrives():
        await receive(doorbell)

    her_round.meanwhile = it_arrives
    await _her_round(_at(21, 30))
    her_round.meanwhile = None

    assert [m.message_id for m in await unread_received(lane=LANE, persona_id="ayana")] == [
        doorbell.message_id
    ]
    await _her_round(_at(21, 40))

    first, second = her_round.seen
    assert "窗外下起了雨。" in first and "楼下有人按门铃。" not in first
    assert "楼下有人按门铃。" in second and "窗外下起了雨。" not in second


# ---------------------------------------------------------------------------
# 走一遍真的通信机制：world 按名字发给她
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_world_reaches_her_by_name_and_a_resend_is_stored_once(
    broker, messaging_db, living_db, monkeypatch  # noqa: F811 — 形参名就是 fixture 名
):
    """名字从人设表来，收件箱在开始接收时按名字开好；world 发给"绫奈"的那条存到 ayana 名下，
    带着同一个 id 重发一次（world 补发没发完的告知就是这样）也只有一行。"""
    from app.data.models import Base, BotPersona
    from app.living.records import living_lane
    from app.messaging.lifecycle import start_messaging
    from app.messaging.receiving import inboxes_at_start
    from app.messaging.sending import send
    from tests.messaging.helpers import eventually, outcomes_become

    async with living_db.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(c, tables=[BotPersona.__table__])
        )
    async with session_mod.get_session() as s:
        for persona_id, name in _NAMES.items():
            s.add(
                BotPersona(
                    persona_id=persona_id,
                    display_name=name,
                    persona_core="",
                    persona_lite="",
                    default_reply_style="",
                    error_messages={},
                )
            )
    monkeypatch.setattr(participants_mod, "_known", None)

    inboxes_at_start(open_inboxes)
    await start_messaging()
    delivery = await send(sender="world", recipient="绫奈", body="窗外下起了雨。")

    assert delivery.delivered
    assert await outcomes_become(delivery.message_id, ["sending", "delivered"]) == [
        "sending",
        "delivered",
    ]

    async def stored() -> list[str]:
        async with session_mod.get_session() as s:
            return list(
                (
                    await s.execute(
                        text(
                            "SELECT persona_id FROM data_received_message "
                            "WHERE lane = :l AND message_id = :m"
                        ),
                        {"l": living_lane(), "m": delivery.message_id},
                    )
                ).scalars()
            )

    await eventually(stored)
    resent = await send(
        sender="world",
        recipient="绫奈",
        body="窗外下起了雨。",
        message_id=delivery.message_id,
    )
    assert resent.delivered
    await outcomes_become(
        delivery.message_id, ["sending", "delivered", "sending", "delivered"]
    )
    await asyncio.sleep(1.0)  # 让重发的那一份被处理完
    assert await stored() == ["ayana"]
