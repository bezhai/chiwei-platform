"""看到别人的有效占位时隔多久再看（:func:`app.messaging.receiving._held_check_delay_ms`）。

纯函数，不碰 broker 和库：延时序列在这里逐项钉住；真 broker 上只验结果（租约到期前不处理、到期后
处理一次），见 ``test_contract.py``。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.messaging import receiving
from app.messaging.receiving import HELD_CHECKS_HEADER

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
FAR = NOW + timedelta(hours=1)


def _delay(checks: int, lease_left: timedelta) -> int:
    return receiving._held_check_delay_ms(checks, NOW + lease_left, NOW)


def test_the_looks_double_from_one_second_and_stop_growing_at_a_minute():
    assert [_delay(checks, timedelta(hours=1)) for checks in range(9)] == [
        1_000,
        2_000,
        4_000,
        8_000,
        16_000,
        32_000,
        60_000,
        60_000,
        60_000,
    ]


def test_a_copy_that_has_looked_very_often_still_waits_a_minute():
    assert receiving._held_check_delay_ms(10_000, FAR, NOW) == 60_000


@pytest.mark.parametrize(
    ("checks", "lease_left", "expected"),
    [
        (2, timedelta(milliseconds=2_500), 3_500),  # 4 秒截到租约到期后 1 秒
        (6, timedelta(seconds=10), 11_000),  # 一分钟截到租约到期后 1 秒
        (0, timedelta(milliseconds=300), 1_000),  # 第一次的 1 秒本来就不晚于到期后 1 秒
        (3, timedelta(0), 1_000),  # 正好到期
        (5, timedelta(seconds=-30), 1_000),  # 已经过期：到期后 1 秒已经过了，按 1 秒算
    ],
)
def test_a_look_is_never_later_than_a_second_after_the_lease_ends(checks, lease_left, expected):
    assert _delay(checks, lease_left) == expected


@pytest.mark.parametrize("checks", range(20))
@pytest.mark.parametrize("lease_left_ms", [0, 1, 999, 1_000, 3_000, 59_000, 600_000, 900_000])
def test_every_look_is_within_the_cap_and_the_lease(checks, lease_left_ms):
    delay = _delay(checks, timedelta(milliseconds=lease_left_ms))
    assert receiving.HELD_CHECK_FIRST_MS <= delay <= receiving.HELD_CHECK_CAP_MS
    assert delay <= lease_left_ms + 1_000
    assert delay <= receiving.HELD_CHECK_FIRST_MS * 2**checks


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({}, 0),
        ({HELD_CHECKS_HEADER: 3}, 3),
        ({HELD_CHECKS_HEADER: -1}, 0),
        ({HELD_CHECKS_HEADER: "2"}, 0),
        ({HELD_CHECKS_HEADER: None}, 0),
    ],
)
def test_how_many_looks_a_copy_has_had_is_read_from_its_header(headers, expected):
    assert receiving._held_checks(headers) == expected
