"""生活日 — 睡前回顾的钟的约定（[当日 04:00, 次日 04:00) 的日期标签）.

「入睡时间 ≠ 回顾覆盖哪天」：23:30 入睡回看的是当日、熬夜 01:30 入睡回看的是
前一日、起夜 03:50 再睡仍是同一个生活日（marker 同日挡重跑）、凌晨补班对账的
是「刚结束的那个生活日」。这个判断是钟的事、不是 agent 的事——回顾的
target_date、marker 比对、昨天页的 ``date`` 键、证据窗口全部用本模块的口径，
与眼睛的晨界（fetch cron 04:00 第一班）同一约定。

边界钉死：时刻 < 04:00 归前一日；>= 04:00 归当日（04:00 整属新的一天）。
输入时区归一：不论传 CST 还是 UTC 的 aware datetime，先归一到 CST 再判钟点
（生活日是北京时间的钟）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from app.infra.cst_time import CST

# 生活日晨界（CST 钟点）：与眼睛的 04:00 第一班同一口径。
LIVING_DAY_BOUNDARY_HOUR = 4


def living_day(moment: datetime) -> str:
    """某个真实时刻属于哪个生活日（``YYYY-MM-DD`` 标签）。

    先归一到 CST 再判钟点：< 04:00 归前一日，>= 04:00 归当日。
    """
    cst = moment.astimezone(CST)
    if cst.hour < LIVING_DAY_BOUNDARY_HOUR:
        cst -= timedelta(days=1)
    return cst.strftime("%Y-%m-%d")


def previous_living_day(moment: datetime) -> str:
    """``moment`` 时刻「刚结束的那个生活日」标签（凌晨补班对账用）。

    05:00 跑补班时 ``living_day(now)`` 已经是新的一天，刚收尾的是它的前一日：
    [前一日 04:00, 当日 04:00) 这一段。
    """
    current = date.fromisoformat(living_day(moment))
    return (current - timedelta(days=1)).strftime("%Y-%m-%d")


def evidence_window(target_date: str, trigger_at: datetime) -> tuple[datetime, datetime]:
    """目标生活日的证据窗口 ``[生活日 04:00, 触发时刻]``（两端 CST aware）。

    起点是该生活日的 04:00 晨界；终点是触发回顾的真实时刻（快班 = 她宣布入睡
    那刻、补班 = 凌晨对账那刻），归一到 CST。熬夜 / 起夜 / 补班的窗口尾巴落在
    第二个自然日——窗口本身可跨两个自然日，这是合同的一部分。
    """
    start = datetime.strptime(target_date, "%Y-%m-%d").replace(
        hour=LIVING_DAY_BOUNDARY_HOUR, tzinfo=CST
    )
    return start, trigger_at.astimezone(CST)


def window_session_dates(target_date: str) -> tuple[str, str]:
    """目标生活日窗口覆盖的两个自然日（意识流 session 按自然日滚动，取这两天）。

    生活日 [04:00, 次日 04:00) 天然横跨 target 与 target+1 两个自然日：23:30
    入睡只用得到第一天，熬夜 / 起夜 / 凌晨补班的尾巴在第二天的 session 里。
    固定取两天，读不到的那天是空 session（``load_session`` 冷启返回空，无害）。
    """
    first = date.fromisoformat(target_date)
    second = first + timedelta(days=1)
    return target_date, second.strftime("%Y-%m-%d")
