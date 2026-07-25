"""今天几点天亮天黑——喂给 world 的日照锚点（idle-deadlock spec Task 2）。

prod 实证（2026-07-24）：广州当天真实日落约 19:13，但 world **17:01 就写「傍晚
前段」**、17:56「傍晚后段的自然光继续退下去」、18:30「更深的蓝灰」——把天黑提前
了 1.5–2 小时；三姐妹从 17:00 起读到的就是「夜晚的家」，这是早睡的一段独立成因。

根因：world 每轮只拿到【现实此刻】这个裸时刻，而循环指令反复拿「天色暗下来」当
时间推进的范例，模型只能用通用先验去渲染黄昏。补法是**给它当天的客观事实**——
今天几点日出、几点日落——而不是给它「几点之后算晚上」这种判断规则（那是替 agent
做世界内容决策，赤尾宪法不允许）。日出日落是确定性天文事实，算出来喂给她属于
「给 agent 客观事实」，边界守在这里。

这些用例钉死三件事：
  * 正常算得出（拿 prod 那天的真实日落对表）；
  * 坐标配置缺失 / 脏 → **如实不拼，绝不编造**；
  * 跨季节两个日期的日照差异符合常识（冬至比夏至晚亮、早黑、白天短）。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.infra.cst_time import CST
from app.world import daylight as daylight_mod

# 广州（越秀区一带）——prod 实证用的坐标，只在测试里写死当样本，生产走 Dynamic Config。
_GZ_LAT = 23.1291
_GZ_LON = 113.2644


# ---------------------------------------------------------------------------
# parse_coords：配置串 -> (纬度, 经度)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("23.1291,113.2644", (23.1291, 113.2644)),   # 正常
        (" 23.1291 , 113.2644 ", (23.1291, 113.2644)),  # 带空格
        ("-33.87,151.21", (-33.87, 151.21)),         # 南纬（负数）
        ("", None),                                   # 空串 = 没配
        ("   ", None),                                # 全空白
        ("23.1291", None),                            # 少一半
        ("23.1291,113.2644,7", None),                 # 多一截
        ("广州,113.2644", None),                       # 不是数字
        ("91.0,113.2644", None),                      # 纬度越界
        ("23.1291,181.0", None),                      # 经度越界
    ],
)
def test_parse_coords(raw: str, expected):
    assert daylight_mod.parse_coords(raw) == expected


# ---------------------------------------------------------------------------
# compute_daylight：纯函数，输入日期 + 坐标 -> 当天日出 / 日落
# ---------------------------------------------------------------------------


def test_compute_daylight_matches_real_guangzhou_day():
    """对表 prod 实证那天：2026-07-24 广州真实日落约 19:13（world 却 17:01 写傍晚）。"""
    result = daylight_mod.compute_daylight(
        date(2026, 7, 24), latitude=_GZ_LAT, longitude=_GZ_LON
    )
    assert result is not None
    # 输出是 CST aware（喂 prompt 的时间口径全项目统一 CST）
    assert result.sunrise.utcoffset() == CST.utcoffset(None)
    assert result.sunset.utcoffset() == CST.utcoffset(None)
    assert result.sunrise.strftime("%H:%M") == "05:54"
    assert result.sunset.strftime("%H:%M") == "19:12", (
        "必须算出当天真实日落（prod 那天约 19:13），而不是通用先验里的「傍晚≈天黑」"
    )
    assert result.sunrise.date() == date(2026, 7, 24)
    assert result.sunset.date() == date(2026, 7, 24)


def test_compute_daylight_is_pure_and_deterministic():
    """纯函数：同样输入永远同样输出，不读配置、不读当前时间。"""
    args = {"latitude": _GZ_LAT, "longitude": _GZ_LON}
    first = daylight_mod.compute_daylight(date(2026, 3, 1), **args)
    second = daylight_mod.compute_daylight(date(2026, 3, 1), **args)
    assert first == second


def test_compute_daylight_differs_across_seasons():
    """跨季节的日照差异符合常识：冬至比夏至晚亮、早黑、白天短。"""
    summer = daylight_mod.compute_daylight(
        date(2026, 6, 21), latitude=_GZ_LAT, longitude=_GZ_LON
    )
    winter = daylight_mod.compute_daylight(
        date(2026, 12, 21), latitude=_GZ_LAT, longitude=_GZ_LON
    )
    assert summer is not None and winter is not None

    assert winter.sunrise.time() > summer.sunrise.time(), "冬至天亮得比夏至晚"
    assert winter.sunset.time() < summer.sunset.time(), "冬至天黑得比夏至早"

    summer_span = summer.sunset - summer.sunrise
    winter_span = winter.sunset - winter.sunrise
    assert summer_span > winter_span, "夏至白天比冬至长"
    # 广州这个纬度，夏至比冬至长 3 小时左右（不是 0、也不是极昼那种量级）
    assert 2 * 3600 < (summer_span - winter_span).total_seconds() < 4 * 3600


def test_compute_daylight_returns_none_where_sun_never_sets():
    """极昼 / 极夜（算不出日出日落）→ 返回 None，不编一个时刻出来。"""
    # 北纬 78°（斯瓦尔巴），夏至没有日落。
    assert (
        daylight_mod.compute_daylight(date(2026, 6, 21), latitude=78.0, longitude=15.0)
        is None
    )


def test_compute_daylight_returns_none_where_sun_never_rises():
    """真极夜同样返回 None——降级的另一半，跟极昼一个口径。"""
    # 北纬 78°（斯瓦尔巴），冬至太阳整天在地平线以下。
    assert (
        daylight_mod.compute_daylight(date(2026, 12, 21), latitude=78.0, longitude=15.0)
        is None
    )


def test_compute_daylight_survives_missing_civil_twilight():
    """有日出日落、只是没有曙暮光的地方，必须照实算出来，不许当极昼降级。

    北纬 65°（雅库特一带，经度取 120°E 让本地时间跟 CST 大致对齐）在北极圈
    （约 66.56°N）以南，夏至这天太阳确实会落下、也确实会升起——只是整夜都停在
    民用曙暮光里，``dawn`` / ``dusk`` 算不出来。

    ``astral.sun.sun()`` 是聚合函数，一次算 dawn/sunrise/noon/sunset/dusk 五个
    时刻，**任何一个算不出来就整体抛 ValueError**；照单接住就会把这一天误判成
    极昼、连真实存在的日出日落一起丢掉。降级只允许发生在真的没有日出或没有日落
    的时候（见下方两个 78° 用例）。
    """
    result = daylight_mod.compute_daylight(
        date(2026, 6, 21), latitude=65.0, longitude=120.0
    )

    assert result is not None, (
        "65°N 在北极圈以南，夏至有日出也有日落；没有曙暮光不等于极昼"
    )
    assert result.sunrise.utcoffset() == CST.utcoffset(None)
    assert result.sunset.utcoffset() == CST.utcoffset(None)
    assert result.sunrise.strftime("%H:%M") == "01:02"
    assert result.sunset.strftime("%H:%M") == "23:01"
    # 高纬夏至的白昼长得离谱但确实存在——这正是它跟极昼的区别。
    assert result.sunset > result.sunrise
    assert (result.sunset - result.sunrise).total_seconds() > 20 * 3600


# ---------------------------------------------------------------------------
# today_daylight_text：读 Dynamic Config 坐标 -> 拼给 world 的一段；缺失时如实不拼
# ---------------------------------------------------------------------------


def _patch_coords(monkeypatch, value: str) -> list[str]:
    """把 Dynamic Config 的 get 换成固定返回值，记录被读的 key。"""
    calls: list[str] = []

    def fake_get(key: str, *, default: str = "") -> str:
        calls.append(key)
        return value

    monkeypatch.setattr(daylight_mod.dynamic_config, "get", fake_get)
    return calls


@pytest.mark.asyncio
async def test_today_daylight_text_renders_sunrise_and_sunset(monkeypatch):
    """配了坐标 → 拼出当天日出 / 日落时刻（客观事实，不含任何「几点算晚上」的判断）。"""
    calls = _patch_coords(monkeypatch, f"{_GZ_LAT},{_GZ_LON}")

    text = await daylight_mod.today_daylight_text(date(2026, 7, 24))

    assert calls == [daylight_mod.WORLD_DAYLIGHT_COORDS_KEY]
    assert "05:54" in text and "19:12" in text
    assert "日出" in text and "日落" in text
    # 只给客观事实，不给「几点之后算晚上」这类替 agent 做决策的规则（赤尾宪法）。
    for verdict in ("算晚上", "算傍晚", "之后就是夜", "视为夜晚"):
        assert verdict not in text


@pytest.mark.asyncio
async def test_today_daylight_text_empty_when_coords_missing(monkeypatch):
    """坐标没配 → 如实不拼（空串），绝不编一个坐标 / 编一个日落时刻。"""
    _patch_coords(monkeypatch, "")
    assert await daylight_mod.today_daylight_text(date(2026, 7, 24)) == ""


@pytest.mark.asyncio
async def test_today_daylight_text_empty_when_coords_malformed(monkeypatch, caplog):
    """坐标配脏了 → 同样如实不拼，并打 warning（配置坏了要可感知，不能静默降级）。"""
    import logging

    _patch_coords(monkeypatch, "广州")
    with caplog.at_level(logging.WARNING):
        assert await daylight_mod.today_daylight_text(date(2026, 7, 24)) == ""
    assert any(
        daylight_mod.WORLD_DAYLIGHT_COORDS_KEY in r.getMessage() for r in caplog.records
    ), "配置脏了必须留下可查的 warning"


@pytest.mark.asyncio
async def test_today_daylight_text_empty_where_sun_never_sets(monkeypatch):
    """配了极区坐标、当天算不出日出日落 → 如实不拼，不报错、不编造。"""
    _patch_coords(monkeypatch, "78.0,15.0")
    assert await daylight_mod.today_daylight_text(date(2026, 6, 21)) == ""


@pytest.mark.asyncio
async def test_today_daylight_text_renders_without_civil_twilight(monkeypatch):
    """高纬但不是极昼（只是没曙暮光）→ 照拼真实日出日落，别顺手降级成空串。

    降级是「这天真的没有日出 / 没有日落」才做的事；缺曙暮光不是不拼的理由。
    """
    _patch_coords(monkeypatch, "65.0,120.0")

    text = await daylight_mod.today_daylight_text(date(2026, 6, 21))

    assert "01:02" in text and "23:01" in text
