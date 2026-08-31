"""时间锚 —— 一缝 / 一轮的身份，必须跨重试保持稳定。

这是"重试不重复执行动作"的地基。派生 id 都带着这个锚（``moment_id``、
``happening_id``、``due_at``），锚一动，同一件事就会落成两行、同一句话就会被说两遍，
而且事后根本看不出来是重复。

崩在收尾之前 → 下一拍的 ``now`` 已经不是原来那个 → 不落到同一格上的话，幂等全废。
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.living.anchor import anchor_on_grid

_CST = dt.timezone(dt.timedelta(hours=8))


def _at(hour: int, minute: int = 0, second: int = 0) -> dt.datetime:
    return dt.datetime(2026, 7, 25, hour, minute, second, tzinfo=_CST)


@pytest.mark.parametrize(
    "moment,minutes,expected",
    [
        (_at(14, 0), 10, _at(14, 0)),
        (_at(14, 1), 10, _at(14, 0)),
        (_at(14, 9, 59), 10, _at(14, 0)),
        (_at(14, 10), 10, _at(14, 10)),
        (_at(14, 59), 60, _at(14, 0)),
        (_at(15, 0), 60, _at(15, 0)),
        (_at(0, 3), 10, _at(0, 0)),
    ],
)
def test_a_moment_falls_onto_its_grid_cell(moment, minutes, expected):
    assert anchor_on_grid(moment, minutes=minutes) == expected


def test_two_ticks_inside_one_cell_are_the_same_moment():
    """崩在收尾之前、下一拍一分钟后再来 —— 必须还是同一缝，否则动作会被做两遍。"""
    assert anchor_on_grid(_at(14, 0), minutes=10) == anchor_on_grid(
        _at(14, 1), minutes=10
    )


def test_the_seconds_are_dropped_so_the_id_is_stable():
    assert anchor_on_grid(_at(14, 3, 47), minutes=10) == _at(14, 0)


def test_the_grid_starts_at_local_midnight_not_at_the_epoch():
    """按当地午夜起格，跨天不会漂 —— 不然同一个钟点每天落在不同格上。"""
    tomorrow = _at(0, 7) + dt.timedelta(days=1)
    assert anchor_on_grid(tomorrow, minutes=10) == tomorrow.replace(
        minute=0, second=0, microsecond=0
    )


def test_a_naive_moment_is_refused():
    """naive 落进派生 id 会静默偏几小时，整条幂等链跟着错位。"""
    with pytest.raises(ValueError, match="时区"):
        anchor_on_grid(dt.datetime(2026, 7, 25, 14, 0), minutes=10)


@pytest.mark.parametrize("minutes", [0, -10])
def test_a_nonpositive_grid_is_refused(minutes):
    with pytest.raises(ValueError):
        anchor_on_grid(_at(14, 0), minutes=minutes)
