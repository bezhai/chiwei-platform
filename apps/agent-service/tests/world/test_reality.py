"""插件 ``reality``：三个天气工具、网页搜索和主 agent 的 prompt 里讲现实的那一段。

工具在测试里直接调，天气一路走到 HTTP 替身，不起模型。
"""
from __future__ import annotations

import re

import pytest

from app.world import reality
from tests.capabilities import qweather as qw


async def _call(t, **arguments):
    return await t.invoke(arguments)


async def test_reality_registers_the_three_weather_tools_web_search_and_its_segment(app_host):
    from app.agent.tools.search import search_web

    host = await app_host("world")
    registered = [(r.kind, r.name, r.detail) for r in host.registered() if r.plugin == "reality"]

    assert [(kind, name) for kind, name, _ in registered] == [
        ("segment", "world_round/reality"),
        ("tool", "check_current_weather"),
        ("tool", "check_hourly_forecast"),
        ("tool", "check_daily_forecast"),
        ("tool", "search_web"),
    ]
    tools = [d["spec"] for kind, _, d in registered if kind == "tool"]
    assert [t.tool for t in tools] == [
        reality.check_current_weather,
        reality.check_hourly_forecast,
        reality.check_daily_forecast,
        search_web,
    ]
    assert {t.trim for t in tools} == {"material"}


# 天气工具一路走到 HTTP 替身：每次调用发几个请求、发到哪儿，交回来的读数长什么样。

CITY = "/geo/v2/city/lookup"
POI = "/geo/v2/poi/lookup"
_CLOCK = re.compile(r"\d{1,2}:\d{2}")


async def test_current_weather_is_two_requests_and_shows_the_readings(monkeypatch):
    fake = qw.install(
        monkeypatch, {CITY: qw.ok(qw.CITIES), "/weather/v1/current": qw.ok(qw.CURRENT)}
    )

    shown = await _call(reality.check_current_weather, place="甲")

    assert fake.paths == [CITY, "/weather/v1/current/39.92/116.42"]
    assert fake.calls[1]["params"] == {"localTime": "true", "lang": "zh"}
    lines = shown.splitlines()
    assert lines[0] == "和风天气认下的地方：甲市（甲省，中国）"
    assert "甲区（乙市，乙省，中国）" in lines[1]  # 同名的候选一并交回去
    for reading in (
        "少云", "气温 31.71°C", "体感 33.64°C", "湿度 69%", "西南风", "风力 3 级",
        "风速 4.74 m/s", "阵风 7.07 m/s", "降水 0 mm", "能见度 29020 m", "云量 5%",
        "紫外线指数 3",
    ):
        assert reading in shown, reading
    # v1 的实况没有观测时间：不写，也不拿查询的时刻冒充
    assert "观测" not in shown and not _CLOCK.search(shown)
    assert lines[-1] == f"数据来源：{qw.ATTRIBUTION}"


@pytest.mark.parametrize(("given", "sent"), [({}, 24), ({"hours": 6}, 6)])
async def test_hourly_forecast_is_two_requests_with_the_hours_passed_through(
    monkeypatch, given, sent
):
    fake = qw.install(
        monkeypatch, {CITY: qw.ok(qw.CITIES), "/weather/v1/hourly": qw.ok(qw.HOURLY)}
    )

    shown = await _call(reality.check_hourly_forecast, place="甲", **given)

    assert fake.paths == [CITY, "/weather/v1/hourly/39.92/116.42"]
    assert fake.calls[1]["params"] == {"hours": sent, "localTime": "true", "lang": "zh"}
    hours = [line for line in shown.splitlines() if line.startswith("- ")]
    assert len(hours) == 2
    for reading in ("2024-05-31T11:00+08:00", "阴", "31.12°C", "降水概率 31%", "降水 0.09 mm",
                    "西南风", "风力 3 级"):
        assert reading in hours[0], reading
    assert "北西北风" in hours[1] and "降水概率 30%" in hours[1]
    assert shown.splitlines()[-1] == f"数据来源：{qw.ATTRIBUTION}"


@pytest.mark.parametrize(("given", "sent"), [({}, 7), ({"days": 3}, 3)])
async def test_daily_forecast_is_two_requests_and_shows_day_and_night(
    monkeypatch, given, sent
):
    fake = qw.install(
        monkeypatch, {CITY: qw.ok(qw.CITIES), "/weather/v1/daily": qw.ok(qw.DAILY)}
    )

    shown = await _call(reality.check_daily_forecast, place="甲", **given)

    assert fake.paths == [CITY, "/weather/v1/daily/39.92/116.42"]
    assert fake.calls[1]["params"] == {"days": sent, "localTime": "true", "lang": "zh"}
    lines = shown.splitlines()
    (day,) = [line for line in lines if line.startswith("- ")]
    for reading in ("2024-08-11", "最低 20.93°C", "最高 29.94°C", "日出 2024-08-11T06:22+08:00",
                    "日落 2024-08-11T19:34+08:00", "亏凸月"):
        assert reading in day, reading
    (daytime,) = [line for line in lines if "白天：" in line]
    (night,) = [line for line in lines if "夜里：" in line]
    for reading in ("小雨", "降水概率 64%", "降水 0.75 mm", "西风", "湿度 52%", "云量 32%"):
        assert reading in daytime, reading
    for reading in ("晴间多云", "降水概率 0%", "北西北风", "湿度 56%", "最低 19.95°C"):
        assert reading in night, reading
    assert lines[-1] == f"数据来源：{qw.ATTRIBUTION}"


async def test_a_name_no_city_matches_is_found_as_a_scenic_spot(monkeypatch):
    fake = qw.install(
        monkeypatch,
        {
            CITY: qw.problem(400, qw.NO_SUCH_LOCATION),
            POI: qw.ok(qw.POIS),
            "/weather/v1/current": qw.ok(qw.CURRENT),
        },
    )

    shown = await _call(reality.check_current_weather, place="丙山")

    assert fake.paths == [CITY, POI, "/weather/v1/current/39.92/116.39"]
    assert shown.splitlines()[0] == "和风天气认下的地方：丙山（丙市，丙省，中国）"


async def test_a_name_that_matches_nothing_sends_no_weather_request(monkeypatch):
    fake = qw.install(
        monkeypatch,
        {
            CITY: qw.problem(400, qw.NO_SUCH_LOCATION),
            POI: qw.problem(400, qw.NO_SUCH_LOCATION),
        },
    )

    shown = await _call(reality.check_current_weather, place="不存在")

    assert "查不到" in shown and "不存在" in shown
    assert fake.paths == [CITY, POI]


@pytest.mark.parametrize(
    ("tool_name", "argument", "value", "bounds"),
    [
        ("check_hourly_forecast", "hours", 0, "1 到 240"),
        ("check_hourly_forecast", "hours", 241, "1 到 240"),
        ("check_daily_forecast", "days", 0, "1 到 10"),
        ("check_daily_forecast", "days", 11, "1 到 10"),
    ],
)
async def test_an_out_of_range_span_tells_the_range_without_a_request(
    monkeypatch, tool_name, argument, value, bounds
):
    fake = qw.install(monkeypatch, {})

    outcome = await _call(getattr(reality, tool_name), place="甲", **{argument: value})

    assert outcome["kind"] == "invalid_args"
    assert bounds in outcome["message"]
    assert fake.calls == []


async def test_an_upstream_error_is_a_tool_error_without_the_key(monkeypatch):
    fake = qw.install(
        monkeypatch, {CITY: qw.problem(403, "Invalid Host"), POI: qw.ok(qw.POIS)}
    )

    outcome = await _call(reality.check_current_weather, place="甲")

    assert outcome["kind"] == "tool_error"
    assert "403 Invalid Host" in outcome["message"]
    assert qw.KEY not in str(outcome)
    assert fake.paths == [CITY]
