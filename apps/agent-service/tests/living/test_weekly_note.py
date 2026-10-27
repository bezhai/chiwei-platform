"""她每周回头看时写下的「这阵子」：一张自己的 append-only 表，跟「她是谁」那条链分开存。

「她是谁」只有 bezhai 手写（链上的 owner 版本，或者 ``bot_persona`` 那一列），代码不往里写；每周回看
写的是这一段。所以它有自己的表（:class:`app.living.persona.notes.WeeklyNote`），经 persona 服务读写：

  * 写：追加一段，带着它说的是哪一周（那一周的周一，和下一周的周一）、什么时候写下的；
  * 每周回看的幂等：``now`` 所在那一周写过没有（周一 00:00 CST 起），只看这张表，不另设标记。

持久化用真实 Postgres（testcontainers）。
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.data.schema import key_fields, version_field
from app.infra.cst_time import CST
from app.living.persona.chain import PersonaChain
from app.living.persona.notes import WeeklyNote
from tests.data.read_back import all_versions
from tests.runtime.conftest import migrate

CHAIN = PersonaChain()

# 固定"现在"= 2026-06-09（周二）12:00 CST，本周一 = 2026-06-08。回看的是 06-01 ～ 06-07 那一周。
_NOW = datetime(2026, 6, 9, 12, 0, tzinfo=CST)
_WEEK = {"week_since": date(2026, 6, 1), "week_until": date(2026, 6, 8)}


@pytest.fixture
async def note_db(test_db):
    await migrate(WeeklyNote, test_db)
    yield test_db


async def _write(
    note: str, *, at: datetime, lane: str = "coe-t1", persona_id: str = "akao"
) -> None:
    await CHAIN.write_note(
        lane=lane, persona_id=persona_id, note=note, written_at=at, **_WEEK
    )


def test_one_line_of_notes_per_lane_and_persona_each_a_new_version():
    """自然键 (lane, persona_id)：泳道隔离、每个人一条；每写一段是一个新版本，不覆盖。"""
    assert set(key_fields(WeeklyNote)) == {"lane", "persona_id"}
    assert version_field(WeeklyNote) == "version"


@pytest.mark.integration
async def test_each_note_is_appended_with_the_week_it_covers(note_db):
    await _write("这阵子在准备考试。", at=datetime(2026, 6, 8, 6, 30, tzinfo=CST))
    await _write("这阵子考完了。", at=datetime(2026, 6, 15, 6, 30, tzinfo=CST))

    notes = await all_versions(WeeklyNote, {"lane": "coe-t1", "persona_id": "akao"})

    assert [(n.version, n.note) for n in notes] == [
        (1, "这阵子在准备考试。"),
        (2, "这阵子考完了。"),
    ]
    assert (notes[0].week_since, notes[0].week_until) == (date(2026, 6, 1), date(2026, 6, 8))
    assert notes[0].written_at == datetime(2026, 6, 8, 6, 30, tzinfo=CST)


@pytest.mark.integration
async def test_a_note_written_this_week_counts(note_db):
    await _write("这阵子。", at=datetime(2026, 6, 8, 6, 30, tzinfo=CST))

    assert await CHAIN.note_written_this_week(lane="coe-t1", persona_id="akao", now=_NOW)


@pytest.mark.integration
async def test_a_note_written_last_sunday_night_is_not_this_weeks(note_db):
    """周界是周一 00:00 CST：周日 23:59 写下的那段算上一周的。"""
    await _write("这阵子。", at=datetime(2026, 6, 7, 23, 59, tzinfo=CST))

    assert not await CHAIN.note_written_this_week(
        lane="coe-t1", persona_id="akao", now=_NOW
    )


@pytest.mark.integration
async def test_monday_just_after_midnight_is_this_week(note_db):
    await _write("这阵子。", at=datetime(2026, 6, 8, 0, 1, tzinfo=CST))

    assert await CHAIN.note_written_this_week(lane="coe-t1", persona_id="akao", now=_NOW)


@pytest.mark.integration
async def test_no_note_yet_is_not_written(note_db):
    assert not await CHAIN.note_written_this_week(
        lane="coe-t1", persona_id="akao", now=_NOW
    )


@pytest.mark.integration
async def test_notes_are_kept_per_lane_and_per_person(note_db):
    """coe 里写下的那段不算 prod 的，赤尾的不算绫奈的。"""
    await _write("coe 里赤尾的这阵子。", at=datetime(2026, 6, 8, 6, 30, tzinfo=CST))

    assert not await CHAIN.note_written_this_week(lane="prod", persona_id="akao", now=_NOW)
    assert not await CHAIN.note_written_this_week(
        lane="coe-t1", persona_id="ayana", now=_NOW
    )
