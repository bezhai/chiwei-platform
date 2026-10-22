"""her_records 插件对象（:class:`app.living.her_records.book.OwnRecords`）：别的功能经 ``her_records``
服务记下、读回她自己的经历和位置。

名字对照用替身（``residents`` 服务），库是用例的库。经历怎么存、怎么读、摆出来什么样各有自己的用例
（``test_happening.py``、``test_whereabouts.py``、``test_snapshot.py``）；这里只验服务这一面：交出去的
和那几处读到的是同一份、按一个人筛。
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.agent.round import Round, in_round
from app.living.her_records.records import Happening, Whereabouts
from app.living.services import KIND_ACT, KIND_SPEECH, MEDIUM_PHONE, Residents
from tests.living.conftest import NamesRead
from tests.runtime.conftest import migrate

pytestmark = pytest.mark.integration

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))


def _at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 7, 25, hour, minute, tzinfo=_CST)


@pytest.fixture
async def records_db(real_pg_required, test_db):  # noqa: F811
    await migrate(Happening, test_db)
    await migrate(Whereabouts, test_db)
    return test_db


def _records(names: dict[str, str] | None = None):
    from app.living.her_records.book import OwnRecords

    return OwnRecords(residents=NamesRead(Residents(names or {"akao": "赤尾", "ayana": "绫奈"})))


def _her_round(persona_id: str = "akao") -> Round:
    return Round(
        app="agent-service",
        lane=LANE,
        participant=persona_id,
        profile="resident",
        round_id="2026-07-25T21:30+08:00",
        now=_at(21, 30),
        reason="test",
        due=None,
    )


async def _say(records, happening_id: str, *, at: dt.datetime, actor: str = "akao", **extra):
    return await records.record_happening(
        lane=LANE,
        happening_id=happening_id,
        actor=actor,
        kind=extra.pop("kind", KIND_SPEECH),
        content=extra.pop("content", "嗯。"),
        occurred_at=at,
        **extra,
    )


async def test_what_she_records_counts_once_for_this_round(records_db):
    """记下一件事就记进这一轮做了几件；同一件重放不多算。"""
    records = _records()
    her_round = _her_round()
    with in_round(her_round):
        await _say(records, "h1", at=_at(21, 30))
        await _say(records, "h1", at=_at(21, 30))
        await _say(records, "h2", at=_at(21, 30), kind=KIND_ACT, content="泡了杯茶")

    assert records.recorded_this_round(her_round) == 2


async def test_her_line_names_her_sisters_by_the_names_the_residents_service_gives(records_db):
    """她记下的"说给谁"里是姐妹的 id 时，摆出来的是 ``residents`` 服务给的名字。"""
    records = _records({"akao": "赤尾", "ayana": "绫奈2"})
    with in_round(_her_round()):
        said = await _say(records, "h1", at=_at(21, 30), audience=["ayana", "许阿姨"])

    assert records.own_line(said) == "你对 绫奈2、许阿姨 说：「嗯。」"


async def test_what_comes_after_the_marks_is_hers_alone_in_commit_order(records_db):
    """往外讲她做了什么：号之后的经历和位置只有她自己的，按提交序；之前她最后在哪也交回来。"""
    records = _records()
    with in_round(_her_round()):
        await _say(records, "h1", at=_at(21, 0))
    await records.note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/客厅", doing="看书",
        noted_at=_at(21, 0),
    )
    marks = await records.marks(lane=LANE, persona_id="akao")
    with in_round(_her_round()):
        await _say(records, "h2", at=_at(21, 30))
    with in_round(_her_round("ayana")):
        await _say(records, "h3", at=_at(21, 30), actor="ayana")
    await records.note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m2", place="家/厨房", doing="看书",
        noted_at=_at(21, 30),
    )

    since = await records.after(lane=LANE, persona_id="akao", marks=marks)

    assert [d.happening_id for _, d in since.deeds] == ["h2"]
    assert [m.place for _, m in since.moves] == ["家/厨房"]
    assert since.before is not None and since.before.place == "家/客厅"


async def test_she_last_spoke_where_she_last_said_something_on_that_conversation(records_db):
    records = _records()
    with in_round(_her_round()):
        await _say(records, "h1", at=_at(20), medium=MEDIUM_PHONE, channel_id="c1")
        await _say(records, "h2", at=_at(21), medium=MEDIUM_PHONE, channel_id="c1")
    with in_round(_her_round("ayana")):
        await _say(records, "h3", at=_at(22), actor="ayana", medium=MEDIUM_PHONE, channel_id="c1")

    assert await records.last_spoke_in(lane=LANE, persona_id="akao", channel_id="c1") == _at(21)
    assert await records.last_spoke_in(lane=LANE, persona_id="akao", channel_id="c2") is None


async def test_the_her_records_plugin_places_its_two_sections_and_hands_over_the_service(
    app_host,
):
    from app.host import Plugin
    from app.living.her_records.book import OwnRecords
    from tests.living.conftest import her_plugins

    taken = []
    probe = Plugin(
        name="probe",
        setup=lambda ctx: taken.append(ctx.service("her_records")),
        requires=("her_records",),
    )
    host = await app_host("agent-service", [*her_plugins(), probe])

    assert isinstance(taken[0], OwnRecords)
    assert sorted(
        r.name for r in host.registered() if r.plugin == "her_records" and r.kind == "section"
    ) == ["her_records.recent", "her_records.whereabouts"]
