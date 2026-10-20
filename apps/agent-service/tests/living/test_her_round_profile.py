"""她的一轮由 living 和已经拆出来的插件（loose_ends）登记的东西组成，按清单 ``resident`` 排的位置跑
（:func:`app.agent.round.run_round`）。

状态段和输入段分开登记、按清单的顺序拼：拼出来的文字和这些段拆开之前那两份（她此刻的样子、这一轮
的刺激）逐字相同。期望的文字是拆开之前的代码对同一份输入渲染出来的，原样写在这里。
"""

from __future__ import annotations

import datetime as dt

from app.agent.continuity import HistoryView
from app.agent.contracts import Continue, CostSpec
from app.agent.round import Round, in_round
from app.living.day_page.pages import LivingDayPage
from app.living.her_records.records import Happening, Whereabouts
from app.living.household.received import ReceivedMessage
from app.living.loose_ends.threads import LooseEnd
from app.living.moment import DueMoment
from app.living.phone.handset import Envelope
from app.living.services import Sender
from app.living.snapshot import MomentSnapshot

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))


def _at(hour: int, minute: int = 0, day: int = 25) -> dt.datetime:
    return dt.datetime(2026, 7, day, hour, minute, tzinfo=_CST)


NOW = _at(14, 10)

STATE = (
    "day_page.last_written",
    "loose_ends.open",
    "her_records.recent",
    "phone.unread",
)
INPUT = (
    "life.now",
    "her_records.whereabouts",
    "loose_ends.just_due",
    "household.received",
    "phone.arrived",
)


def test_living_places_every_part_of_her_round_the_manifest_lists(resident):
    assert [p.name for p in resident.round_start] == ["outgoing.before"]
    assert resident.profile.round_log == "life.moments"
    assert resident.round_log is not None
    assert [p.name for p in resident.state] == list(STATE)
    assert [p.name for p in resident.input] == list(INPUT)
    assert [p.name for p in resident.commit] == [
        "life.moment",
        "phone.glances",
        "household.read",
    ]
    assert [(p.name, p.value.on_failure) for p in resident.after_commit] == [
        ("history", Continue()),
        ("outgoing.after", Continue()),
    ]
    placed = (
        *resident.round_start,
        *resident.state,
        *resident.input,
        *resident.commit,
        *resident.after_commit,
    )
    assert {p.name: p.plugin for p in placed if p.plugin != "living"} == {
        "loose_ends.open": "loose_ends",
        "loose_ends.just_due": "loose_ends",
        "her_records.recent": "her_records",
        "her_records.whereabouts": "her_records",
        "day_page.last_written": "day_page",
        "phone.unread": "phone",
        "phone.arrived": "phone",
        "phone.glances": "phone",
        "outgoing.before": "household",
        "household.received": "household",
        "household.read": "household",
        "outgoing.after": "household",
    }
    assert resident.profile.history.key == "{lane}:{participant}"
    assert resident.profile.cost == CostSpec(
        actor="{participant}", round_id="{round_id}"
    )


_DOING = Whereabouts(
    lane=LANE,
    persona_id="akao",
    moment_id="2026-07-25T14:00+08:00",
    place="家/客厅",
    doing="看昨天拍的胶片",
    noted_at=_at(14),
)

_OWN_RECENT = [
    Happening(
        lane=LANE,
        happening_id="h1",
        actor="akao",
        kind="speech",
        medium="in_person",
        content="布丁我吃了。",
        occurred_at=_at(13, 50),
        audience=["ayana"],
    )
]


def _snapshot() -> MomentSnapshot:
    return MomentSnapshot(lane=LANE, persona_id="akao", now=NOW)


_DAY_PAGE = LivingDayPage(
    lane=LANE,
    persona_id="akao",
    day=dt.date(2026, 7, 24),
    text="胶片摊了一茶几，绫奈说要下雨。",
    written_at=_at(4, 30),
    happenings=3,
)


_OPEN_ENDS = [
    LooseEnd(
        lane=LANE,
        persona_id="akao",
        thread_id="t1",
        ver=1,
        what="周末陪绫奈去祭典",
        opened_at=_at(12),
        opened_moment_id="2026-07-25T12:00+08:00",
    ),
    LooseEnd(
        lane=LANE,
        persona_id="akao",
        thread_id="t2",
        ver=1,
        what="去洗衣房收衣服",
        due_at=_at(14, 5),
        opened_at=_at(13),
        opened_moment_id="2026-07-25T13:00+08:00",
    ),
]

_ENVELOPES = [
    Envelope(
        channel_id="c1",
        scope="group",
        title="胶片社",
        unread=2,
        senders=(Sender("小林", False),),
        earliest=_at(14, 3),
        latest=_at(14, 6),
        named_you=False,
        you_last_spoke_at=None,
    )
]

_RECEIVED = [
    ReceivedMessage(
        lane=LANE,
        persona_id="akao",
        message_id="m1",
        sender="world",
        body="窗外下起了雨。",
        message_time=_at(14, 4),
        wakes_recipient=False,
    ),
    ReceivedMessage(
        lane=LANE,
        persona_id="akao",
        message_id="m2",
        sender="绫奈",
        body="冰箱里还有布丁",
        message_time=_at(14, 8),
        wakes_recipient=True,
    ),
]


def _reads(
    monkeypatch, *, snapshot, day_page, doing, own_recent, open_ends, envelopes, received
) -> dict[str, int]:
    """这一轮读的七样（快照、上一次写下的那一页、她在哪、她刚做过说过的、心里挂着的、手机信封、收件箱）
    换成写死的，并数每样读了几次。"""
    from app.living import moment as moment_mod
    from app.living.day_page import pages as pages_mod
    from app.living.her_records import book as book_mod
    from app.living.household import mailbox as mailbox_mod
    from app.living.loose_ends import mind as mind_mod
    from app.living.phone import handset as handset_mod

    reads = {
        "snapshot": 0,
        "day_page": 0,
        "doing": 0,
        "own_recent": 0,
        "open_ends": 0,
        "envelopes": 0,
        "received": 0,
    }

    async def current_whereabouts(*, lane, persona_id):
        assert (lane, persona_id) == (LANE, "akao")
        reads["doing"] += 1
        return doing

    async def recent_own_happenings(*, lane, persona_id):
        assert (lane, persona_id) == (LANE, "akao")
        reads["own_recent"] += 1
        return own_recent

    async def list_open_loose_ends(*, lane, persona_id):
        assert (lane, persona_id) == (LANE, "akao")
        reads["open_ends"] += 1
        return open_ends

    async def read_snapshot(*, lane, persona_id, now):
        assert (lane, persona_id, now) == (LANE, "akao", NOW)
        reads["snapshot"] += 1
        return snapshot

    async def read_day_page_before(*, lane, persona_id, day):
        assert (lane, persona_id, day) == (LANE, "akao", dt.date(2026, 7, 25))
        reads["day_page"] += 1
        return day_page

    async def envelopes_for(*, lane, persona_id, now, her_records):
        assert (lane, persona_id, now) == (LANE, "akao", NOW)
        reads["envelopes"] += 1
        return envelopes

    async def unread_received(*, lane, persona_id, including):
        assert (lane, persona_id, including) == (LANE, "akao", "m2")
        reads["received"] += 1
        return received

    monkeypatch.setattr(moment_mod, "read_snapshot", read_snapshot)
    monkeypatch.setattr(pages_mod, "read_day_page_before", read_day_page_before)
    monkeypatch.setattr(book_mod, "current_whereabouts", current_whereabouts)
    monkeypatch.setattr(book_mod, "recent_own_happenings", recent_own_happenings)
    monkeypatch.setattr(mind_mod, "list_open_loose_ends", list_open_loose_ends)
    monkeypatch.setattr(handset_mod, "envelopes_for", envelopes_for)
    monkeypatch.setattr(mailbox_mod, "unread_received", unread_received)
    return reads


def _round(*, previous_at, continues_unbroken: bool) -> Round:
    due = DueMoment(
        reason="常规节奏",
        lane=LANE,
        persona_id="akao",
        moment_id="2026-07-25T14:10+08:00",
        now=NOW,
        must_show="m2",
    )
    return Round(
        app="agent-service",
        lane=LANE,
        participant="akao",
        profile="resident",
        round_id=due.moment_id,
        now=NOW,
        reason=due.reason,
        due=due,
        previous_at=previous_at,
        history=HistoryView(
            messages=[],
            version=0,
            lost_last_round=False,
            continues_unbroken=continues_unbroken,
        ),
    )


async def _texts(resident, round: Round) -> tuple[list, list]:
    with in_round(round):
        state = [await p.value(round) for p in resident.state]
        stimulus = [await p.value(round) for p in resident.input]
    return state, stimulus


def _joined(resident, texts: list) -> str:
    return resident.profile.join.join(t for t in texts if t is not None)


async def test_a_round_whose_history_broke_shows_everything_section_by_section(
    resident, monkeypatch
):
    reads = _reads(
        monkeypatch,
        snapshot=_snapshot(),
        day_page=_DAY_PAGE,
        doing=_DOING,
        own_recent=_OWN_RECENT,
        open_ends=_OPEN_ENDS,
        envelopes=_ENVELOPES,
        received=_RECEIVED,
    )
    round = _round(previous_at=_at(14), continues_unbroken=False)

    state, stimulus = await _texts(resident, round)

    assert state == [
        "昨天（07-24）你写下的：\n胶片摊了一茶几，绫奈说要下雨。",
        "心里挂着没了结的：\n"
        "- 周末陪绫奈去祭典 · 从 2026-07-25T12:00+08:00 那一刻起挂着\n"
        "- [2026-07-25 14:05] 去洗衣房收衣服 · 到点了 · 从 2026-07-25T13:00+08:00 那一刻起挂着",
        "你刚做过、说过：\n- 13:50 CST 你对 绫奈 说：「布丁我吃了。」",
        "手机上还没看的（想知道说了什么，得自己拿起来看）：\n"
        '- 群「胶片社」channel_id=c1 · 2 条没看 · <who from="小林"/> · 14:03 CST–14:06 CST'
        " · 你还没在这儿说过话",
    ]
    assert stimulus == [
        "现在 2026-07-25 周六 14:10 CST，离上一次过了 10 分钟。",
        "手上：你在 家/客厅，正在 看昨天拍的胶片。",
        "刚到点的：\n- [2026-07-25 14:05] 去洗衣房收衣服",
        "这段时间传到你这里的：\n- 14:04 CST 窗外下起了雨。\n- 14:08 CST 绫奈：冰箱里还有布丁",
        "手机上刚来的（想知道说了什么，得自己拿起来看）：\n"
        '- 群「胶片社」channel_id=c1 · 2 条没看 · <who from="小林"/> · 14:03 CST–14:06 CST'
        " · 你还没在这儿说过话",
    ]
    assert _joined(resident, state) == (
        "昨天（07-24）你写下的：\n胶片摊了一茶几，绫奈说要下雨。\n\n心里挂着没了结的：\n"
        "- 周末陪绫奈去祭典 · 从 2026-07-25T12:00+08:00 那一刻起挂着\n"
        "- [2026-07-25 14:05] 去洗衣房收衣服 · 到点了 · 从 2026-07-25T13:00+08:00 那一刻起挂着"
        "\n\n你刚做过、说过：\n- 13:50 CST 你对 绫奈 说：「布丁我吃了。」\n\n"
        "手机上还没看的（想知道说了什么，得自己拿起来看）：\n"
        '- 群「胶片社」channel_id=c1 · 2 条没看 · <who from="小林"/> · 14:03 CST–14:06 CST'
        " · 你还没在这儿说过话"
    )
    assert _joined(resident, stimulus) == (
        "现在 2026-07-25 周六 14:10 CST，离上一次过了 10 分钟。\n\n"
        "手上：你在 家/客厅，正在 看昨天拍的胶片。\n\n"
        "刚到点的：\n- [2026-07-25 14:05] 去洗衣房收衣服\n\n"
        "这段时间传到你这里的：\n- 14:04 CST 窗外下起了雨。\n- 14:08 CST 绫奈：冰箱里还有布丁\n\n"
        "手机上刚来的（想知道说了什么，得自己拿起来看）：\n"
        '- 群「胶片社」channel_id=c1 · 2 条没看 · <who from="小林"/> · 14:03 CST–14:06 CST'
        " · 你还没在这儿说过话"
    )
    # 快照、心里挂着的、手机、收件箱各读一次：两个手机段用的是同一次查到的信封，挂念和刚到点的
    # 两段用的是同一次读到的清单。
    assert reads == {
        "snapshot": 1,
        "day_page": 1,
        "doing": 1,
        "own_recent": 1,
        "open_ends": 1,
        "envelopes": 1,
        "received": 1,
    }


async def test_a_first_round_whose_history_continues_leaves_out_what_is_not_there(
    resident, monkeypatch
):
    empty = MomentSnapshot(lane=LANE, persona_id="akao", now=NOW)
    _reads(
        monkeypatch,
        snapshot=empty,
        day_page=None,
        doing=None,
        own_recent=[],
        open_ends=[],
        envelopes=[],
        received=[],
    )
    round = _round(previous_at=None, continues_unbroken=True)

    state, stimulus = await _texts(resident, round)

    assert state == [
        "你上一次写下的那一天：（还没有）",
        "心里挂着没了结的：（没有）",
        None,
        "手机上：（没有没看的）",
    ]
    assert stimulus == [
        "现在 2026-07-25 周六 14:10 CST。",
        "手上：你还没定下自己在哪、在做什么。",
        None,
        "这段时间传到你这里的：（没有）",
        "手机上：（这一阵没新动静）",
    ]
    assert _joined(resident, state) == (
        "你上一次写下的那一天：（还没有）\n\n心里挂着没了结的：（没有）\n\n手机上：（没有没看的）"
    )
    assert _joined(resident, stimulus) == (
        "现在 2026-07-25 周六 14:10 CST。\n\n手上：你还没定下自己在哪、在做什么。\n\n"
        "这段时间传到你这里的：（没有）\n\n手机上：（这一阵没新动静）"
    )
