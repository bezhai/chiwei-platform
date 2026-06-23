"""时间归一 helper (``app.infra.cst_time``) 单测 —— 阶段 0 Task 1.

helper 的职责是把"喂给 agent 的时间"统一到 CST 一个口径：
  * ``parse(raw)`` 把当前代码实际产生的三种历史格式解析成 aware datetime
    （供按真实时刻比较，不再差 8 小时）：
      - world 写的 CST aware ISO（``...+08:00``）
      - life 写的 UTC aware ISO（``...+00:00``）
      - chat 写的 Unix 毫秒字符串（``"1717..."``）
  * ``to_cst_hm`` / ``to_cst_hms`` 把 aware datetime 或原始 raw 显示成 CST 的
    ``HH:MM`` / ``HH:MM:SS``（显示里带 CST 标识，让模型看得出是北京时间）。
  * ``now_cst_iso()`` 产当前 CST aware ISO（``...+08:00``）供新写。
  * ``CST`` 是权威 CST 偏移常量（一处定义）。

这里只测 helper 本身的纯函数行为，不碰任何 IO。最致命的两条：
跨格式比较不差 8 小时（同一真实时刻的 UTC 串 / CST 串 / Unix 毫秒解析后相等）、
显示统一到 CST。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.infra import cst_time


def test_cst_constant_is_utc_plus_8():
    """权威 CST 常量 = UTC+8（一处定义、其他出口都引这个）。"""
    assert cst_time.CST.utcoffset(None) == timedelta(hours=8)


# ---------------------------------------------------------------------------
# parse —— 三种历史格式各自解析成 aware datetime
# ---------------------------------------------------------------------------


def test_parse_cst_aware_iso():
    """world 写的 CST aware ISO（+08:00）解析成同一真实时刻的 aware datetime。"""
    dt = cst_time.parse("2026-06-03T20:30:00+08:00")
    assert dt.tzinfo is not None
    # 真实时刻 = UTC 12:30
    assert dt.astimezone(timezone.utc).hour == 12
    assert dt.astimezone(timezone.utc).minute == 30


def test_parse_utc_aware_iso():
    """life 写的 UTC aware ISO（+00:00）解析成 aware datetime。"""
    dt = cst_time.parse("2026-06-03T12:30:00+00:00")
    assert dt.tzinfo is not None
    assert dt.astimezone(timezone.utc).hour == 12
    assert dt.astimezone(timezone.utc).minute == 30


def test_parse_utc_z_suffix():
    """UTC 的 ``Z`` 后缀也是 life/历史会产生的形态，要能解析。"""
    dt = cst_time.parse("2026-06-03T12:30:00Z")
    assert dt.tzinfo is not None
    assert dt.astimezone(timezone.utc).hour == 12


def test_parse_unix_millis_string():
    """chat 历史写的 Unix 毫秒字符串解析成 aware datetime（真实时刻）。"""
    # 2026-06-03T12:30:00Z == 1780489800000 ms
    millis = int(
        datetime(2026, 6, 3, 12, 30, 0, tzinfo=timezone.utc).timestamp() * 1000
    )
    dt = cst_time.parse(str(millis))
    assert dt.tzinfo is not None
    assert dt.astimezone(timezone.utc).hour == 12
    assert dt.astimezone(timezone.utc).minute == 30


# ---------------------------------------------------------------------------
# 跨格式比较不差 8 小时（命门）
# ---------------------------------------------------------------------------


def test_cross_format_same_instant_compares_equal():
    """同一真实时刻的三种格式解析后必须是同一时刻（不差 8 小时）。"""
    millis = int(
        datetime(2026, 6, 3, 12, 30, 0, tzinfo=timezone.utc).timestamp() * 1000
    )
    cst = cst_time.parse("2026-06-03T20:30:00+08:00")  # world
    utc = cst_time.parse("2026-06-03T12:30:00+00:00")  # life
    unix = cst_time.parse(str(millis))  # chat

    assert cst == utc, "CST 串与 UTC 串是同一真实时刻，比较不该差 8 小时"
    assert cst == unix, "CST 串与 Unix 毫秒是同一真实时刻"
    assert utc == unix


def test_cross_format_ordering_by_real_instant():
    """跨格式排序按真实时刻：UTC 12:00 早于 CST 20:30（=UTC 12:30）。"""
    earlier = cst_time.parse("2026-06-03T12:00:00+00:00")  # 真实 UTC 12:00
    later = cst_time.parse("2026-06-03T20:30:00+08:00")  # 真实 UTC 12:30
    assert earlier < later


# ---------------------------------------------------------------------------
# CST 显示
# ---------------------------------------------------------------------------


def test_to_cst_hm_from_utc_iso_shifts_to_cst():
    """UTC 12:30 显示成 CST 应是 20:30（+8），且带 CST 标识。"""
    s = cst_time.to_cst_hm("2026-06-03T12:30:00+00:00")
    assert "20:30" in s
    assert "CST" in s, "显示要让模型看得出是 CST（北京时间）"


def test_to_cst_hms_from_unix_millis():
    """Unix 毫秒（真实 UTC 12:30:45）显示成 CST 20:30:45。"""
    millis = int(
        datetime(2026, 6, 3, 12, 30, 45, tzinfo=timezone.utc).timestamp() * 1000
    )
    s = cst_time.to_cst_hms(str(millis))
    assert "20:30:45" in s
    assert "CST" in s


def test_to_cst_hm_from_cst_iso_stays_cst():
    """已是 CST 的串显示仍是该 CST 钟点（不再二次偏移）。"""
    s = cst_time.to_cst_hm("2026-06-03T20:30:00+08:00")
    assert "20:30" in s


def test_to_cst_display_passthrough_on_unparseable():
    """无法解析的脏串：不抛、原样回显（向后兼容兜底，不静默吞）。"""
    s = cst_time.to_cst_hm("这不是时间")
    assert "这不是时间" in s


# ---------------------------------------------------------------------------
# to_cst_full —— 完整日期口径（年月日 + 星期 + 时分），喂 life 的「现在」时间锚
# ---------------------------------------------------------------------------


def test_to_cst_full_includes_date_weekday_and_time():
    """to_cst_full 给完整口径：年月日 + 星期 + 时分 + CST。

    life 的「现在是 X」时间锚要用它，而非只给时分的 to_cst_hm —— 她记日程 / 算
    remind_at 时必须知道「今天是几号、星期几」才能把「5 分钟后」「周五」这类相对
    时间换算成正确的绝对 ISO；只给时分她只能瞎填日期分量（线上 bug：remind_at
    日期被填到过去，提醒永远不在你等的那一刻触发）。
    """
    # 真实 UTC 12:30 → CST 20:30，日期同为 2026-06-03（+8 后仍在当天）。
    s = cst_time.to_cst_full("2026-06-03T12:30:00+00:00")
    assert "2026-06-03" in s, f"必须含年月日（她算 remind_at 靠它），实际 {s!r}"
    assert "20:30" in s
    assert "CST" in s
    # 星期从 datetime 推导（不手写，避免算错）：周一=0 … 周日=6。weekday 只看年月日、
    # 与时区无关，2026-06-03 不跨天，naive 即可。
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    expected_wd = weekdays[datetime(2026, 6, 3).weekday()]
    assert expected_wd in s, f"必须含星期 {expected_wd}，实际 {s!r}"


def test_to_cst_full_passthrough_on_unparseable():
    """无法解析的脏串：原样回显（同 to_cst_hm 的兜底，不静默吞）。"""
    assert "这不是时间" in cst_time.to_cst_full("这不是时间")


# ---------------------------------------------------------------------------
# now_cst_iso —— 新写时间一律 CST aware ISO
# ---------------------------------------------------------------------------


def test_now_cst_iso_is_cst_aware():
    """新写时间含 +08:00（CST aware ISO），可被 parse 回真实时刻。"""
    iso = cst_time.now_cst_iso()
    assert "+08:00" in iso, "新写时间必须是 CST aware ISO（带 +08:00）"
    dt = cst_time.parse(iso)
    assert dt.utcoffset() == timedelta(hours=8)


def test_now_cst_iso_roundtrips_close_to_now():
    """now_cst_iso 的真实时刻 ≈ 当前真实 UTC（同一个"现在"）。"""
    before = datetime.now(timezone.utc)
    parsed = cst_time.parse(cst_time.now_cst_iso())
    after = datetime.now(timezone.utc)
    assert before - timedelta(seconds=5) <= parsed <= after + timedelta(seconds=5)
