"""生活日合同 — 睡前回顾的钟的约定（前置验收点，表驱动钉死）.

「入睡时间 ≠ 回顾覆盖哪天」（codex T1 必改）：生活日 = [当日 04:00, 次日 04:00)
的日期标签，与眼睛的晨界同一口径。这是钟的约定、不是 agent 规则——回顾的
target_date、marker 比对、昨天页 date 键、证据窗口全部用它。本文件用表驱动单测
把映射函数和证据窗口的每个 case 钉死，**先于回顾本体**存在：

  * ``living_day``：时刻 < 04:00 归前一日；>= 04:00 归当日（边界 04:00 属新的一天）。
  * ``previous_living_day``：凌晨补班对账的「刚结束的那个生活日」（05:00 时 = 前一日标签）。
  * ``evidence_window``：[生活日 04:00, 触发时刻]，可跨两个自然日。
  * ``window_session_dates``：窗口覆盖哪两个自然日的意识流 session（按天滚动的
    session key 用自然日，所以跨自然日的窗口要取两天）。

输入时区归一：传进来的 datetime 不论 CST / UTC，都先归一到 CST 再判钟点
（生活日是北京时间的钟）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.life.living_day import (
    evidence_window,
    living_day,
    previous_living_day,
    window_session_dates,
)

_CST = timezone(timedelta(hours=8))
_UTC = UTC


# ---------------------------------------------------------------------------
# living_day：04:00 边界映射（表驱动）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("moment", "expected", "case"),
    [
        # 23:30 入睡：还在当日生活日内 → 当日标签。
        (datetime(2026, 6, 10, 23, 30, tzinfo=_CST), "2026-06-10", "23:30 入睡"),
        # 熬夜 01:30 入睡：已过自然日零点但没过 04:00 晨界 → 前一日标签。
        (datetime(2026, 6, 11, 1, 30, tzinfo=_CST), "2026-06-10", "熬夜 01:30 入睡"),
        # 起夜 03:50 再睡：仍在 04:00 之前 → 还是同一个生活日（marker 同日挡重跑靠它）。
        (datetime(2026, 6, 11, 3, 50, tzinfo=_CST), "2026-06-10", "起夜 03:50 再睡"),
        # 03:59:59 边界内侧：仍归前一日。
        (
            datetime(2026, 6, 11, 3, 59, 59, tzinfo=_CST),
            "2026-06-10",
            "03:59:59 边界内侧",
        ),
        # 04:00:00 整：新生活日开始（边界属新的一天，与眼睛晨界同口径）。
        (datetime(2026, 6, 11, 4, 0, 0, tzinfo=_CST), "2026-06-11", "04:00 整点"),
        # 05:00（凌晨补班跑的时刻）：属于新生活日——「刚结束的」是前一日，见
        # previous_living_day 的用例。
        (datetime(2026, 6, 11, 5, 0, tzinfo=_CST), "2026-06-11", "05:00 补班时刻"),
        # 正午白天：当日。
        (datetime(2026, 6, 10, 12, 0, tzinfo=_CST), "2026-06-10", "正午"),
        # UTC 输入归一：2026-06-10T17:30Z == 2026-06-11T01:30 CST → 生活日 06-10。
        (
            datetime(2026, 6, 10, 17, 30, tzinfo=_UTC),
            "2026-06-10",
            "UTC 输入按 CST 钟点判",
        ),
        # 跨月边界：07-01 02:00 CST → 生活日 06-30。
        (datetime(2026, 7, 1, 2, 0, tzinfo=_CST), "2026-06-30", "跨月凌晨"),
    ],
)
def test_living_day_table(moment: datetime, expected: str, case: str):
    assert living_day(moment) == expected, f"case: {case}"


# ---------------------------------------------------------------------------
# previous_living_day：凌晨补班对账「刚结束的生活日」
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("moment", "expected", "case"),
    [
        # 05:00 补班：刚结束的生活日是前一日标签（[06-10 04:00, 06-11 04:00) 刚收尾）。
        (datetime(2026, 6, 11, 5, 0, tzinfo=_CST), "2026-06-10", "05:00 补班"),
        # 04:00 整点跑（理论边界）：刚结束的同样是前一日。
        (datetime(2026, 6, 11, 4, 0, tzinfo=_CST), "2026-06-10", "04:00 边界"),
        # 03:50（还没过晨界就问"刚结束的"）：当前生活日是 06-10，刚结束的是 06-09。
        (datetime(2026, 6, 11, 3, 50, tzinfo=_CST), "2026-06-09", "晨界前问刚结束的"),
    ],
)
def test_previous_living_day_table(moment: datetime, expected: str, case: str):
    assert previous_living_day(moment) == expected, f"case: {case}"


# ---------------------------------------------------------------------------
# evidence_window：[生活日 04:00, 触发时刻]
# ---------------------------------------------------------------------------


def test_window_fast_shift_same_natural_day():
    """快班 23:30 入睡：窗口 [当日 04:00, 23:30]，起止都在同一自然日。"""
    trigger = datetime(2026, 6, 10, 23, 30, tzinfo=_CST)
    start, end = evidence_window("2026-06-10", trigger)
    assert start == datetime(2026, 6, 10, 4, 0, tzinfo=_CST)
    assert end == trigger


def test_window_late_night_spans_two_natural_days():
    """熬夜 01:30 入睡：窗口 [06-10 04:00, 06-11 01:30] 跨两个自然日。"""
    trigger = datetime(2026, 6, 11, 1, 30, tzinfo=_CST)
    start, end = evidence_window("2026-06-10", trigger)
    assert start == datetime(2026, 6, 10, 4, 0, tzinfo=_CST)
    assert end == trigger
    assert start.date() != end.date(), "熬夜窗口必须跨自然日"


def test_window_settlement_shift_at_five():
    """05:00 补班对账刚结束的生活日：窗口 [前一日 04:00, 今日 05:00]。"""
    trigger = datetime(2026, 6, 11, 5, 0, tzinfo=_CST)
    target = previous_living_day(trigger)
    start, end = evidence_window(target, trigger)
    assert target == "2026-06-10"
    assert start == datetime(2026, 6, 10, 4, 0, tzinfo=_CST)
    assert end == trigger


def test_window_end_normalised_to_cst():
    """触发时刻不论传 UTC 还是 CST，窗口端点都归一成 CST aware（钟是北京时间的钟）。"""
    trigger_utc = datetime(2026, 6, 10, 15, 30, tzinfo=_UTC)  # == 23:30 CST
    start, end = evidence_window("2026-06-10", trigger_utc)
    assert end.utcoffset() == timedelta(hours=8)
    assert end == trigger_utc  # 同一真实时刻
    assert start.utcoffset() == timedelta(hours=8)


# ---------------------------------------------------------------------------
# window_session_dates：窗口跨自然日 → 取两天的意识流 session
# ---------------------------------------------------------------------------


def test_window_session_dates_two_natural_days():
    """生活日窗口最多横跨 target 与 target+1 两个自然日 → session 取这两天。

    意识流 session 按 (lane, actor, 自然日) 滚动，而生活日 [04:00, 次日 04:00)
    天然跨两个自然日：23:30 入睡只用得到第一天，但熬夜 / 起夜 / 凌晨补班的窗口
    尾巴落在第二个自然日的 session 里——固定取两天、读不到的那天是空 session
    （load_session 冷启返回 []，无害）。
    """
    assert window_session_dates("2026-06-10") == ("2026-06-10", "2026-06-11")


def test_window_session_dates_cross_month():
    """跨月生活日：06-30 的窗口第二天是 07-01（不是字符串加一）。"""
    assert window_session_dates("2026-06-30") == ("2026-06-30", "2026-07-01")
