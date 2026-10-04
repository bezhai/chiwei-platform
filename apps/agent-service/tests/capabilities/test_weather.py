"""和风天气这一层：地名换候选地点（城市优先、景点兜底），地点换实况、逐小时或逐日。

HTTP 换成替身，按和风文档的响应结构回话。钉的是：请求发对了（host、路径、坐标两位小数且
纬度在前、``localTime``、``lang``、key 只走 header）、发了几个；认得出"地名匹配不到"和
"上游出错"是两件事，上游出错不会去查景点；v1 的读数认对了（0 到 1 的小数换成百分数、
带单位的量带着单位、逐日分白天和夜里）；这一层不替调用方挑城市、不补默认城市。
"""
from __future__ import annotations

import dataclasses

import httpx
import pytest

from app.capabilities import weather
from app.capabilities._errors import CapabilityInvalidArg
from app.infra import config

from .qweather import (
    ATTRIBUTION,
    CITIES,
    CURRENT,
    DAILY,
    HOST,
    HOURLY,
    KEY,
    POIS,
    install,
    ok,
    problem,
)

CITY = "/geo/v2/city/lookup"
POI = "/geo/v2/poi/lookup"
PLACE = weather.Place(
    name="甲市", adm2="甲市", adm1="甲省", country="中国", lat="39.92", lon="116.42"
)
_WEATHER_PARAMS = {"localTime": "true", "lang": "zh"}


# ---------------------------------------------------------------------------
# 地点：城市优先，一个候选都没有才查景点
# ---------------------------------------------------------------------------


async def test_a_city_name_is_one_request_and_every_candidate_comes_back(monkeypatch):
    fake = install(monkeypatch, {CITY: ok(CITIES)})

    places = await weather.find_places("甲")

    assert places == [
        weather.Place(
            name="甲市", adm2="甲市", adm1="甲省", country="中国", lat="39.92", lon="116.42"
        ),
        weather.Place(
            name="甲区", adm2="乙市", adm1="乙省", country="中国", lat="31.16", lon="120.68"
        ),
    ]
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["url"] == f"https://{HOST}{CITY}"
    assert call["params"] == {"location": "甲", "number": 5, "lang": "zh"}
    assert call["headers"] == {"X-QW-Api-Key": KEY}
    assert KEY not in str(call["params"])


async def test_coordinates_are_rounded_half_up_to_two_decimals(monkeypatch):
    entry = dict(CITIES["location"][0], lat="23.125", lon="-122.415")
    install(monkeypatch, {CITY: ok({"code": "200", "location": [entry]})})

    (place,) = await weather.find_places("甲")

    assert (place.lat, place.lon) == ("23.13", "-122.42")


@pytest.mark.parametrize(
    "no_city",
    [
        problem(400, "NO SUCH LOCATION"),
        ok({"code": "404"}),
        ok({"code": "200", "location": []}),
    ],
    ids=["400-no-such-location", "body-code-404", "empty-list"],
)
async def test_a_name_no_city_matches_is_looked_up_once_more_as_a_scenic_spot(
    monkeypatch, no_city
):
    fake = install(monkeypatch, {CITY: no_city, POI: ok(POIS)})

    places = await weather.find_places("丙山")

    assert places == [
        weather.Place(
            name="丙山", adm2="丙市", adm1="丙省", country="中国", lat="39.92", lon="116.39"
        )
    ]
    assert fake.paths == [CITY, POI]
    assert fake.calls[1]["params"] == {
        "location": "丙山", "type": "scenic", "number": 5, "lang": "zh"
    }
    assert fake.calls[1]["headers"] == {"X-QW-Api-Key": KEY}


async def test_a_name_that_matches_neither_is_an_empty_list(monkeypatch):
    fake = install(
        monkeypatch,
        {CITY: problem(400, "NO SUCH LOCATION"), POI: problem(400, "NO SUCH LOCATION")},
    )

    assert await weather.find_places("不存在的地方") == []
    assert fake.paths == [CITY, POI]


@pytest.mark.parametrize(
    ("status", "title"),
    [(403, "INVALID HOST"), (429, "TOO MANY REQUESTS"), (400, "INVALID PARAMETER")],
)
async def test_an_upstream_error_is_not_mistaken_for_no_match(monkeypatch, status, title):
    fake = install(monkeypatch, {CITY: problem(status, title), POI: ok(POIS)})

    with pytest.raises(weather.WeatherUnavailable, match=f"{status} {title}") as caught:
        await weather.find_places("甲")

    assert KEY not in str(caught.value)
    assert fake.paths == [CITY]  # 不去查景点


async def test_a_geo_body_code_other_than_200_is_unavailable(monkeypatch):
    fake = install(monkeypatch, {CITY: ok({"code": "402"}), POI: ok(POIS)})

    with pytest.raises(weather.WeatherUnavailable, match="402"):
        await weather.find_places("甲")
    assert fake.paths == [CITY]


@pytest.mark.parametrize(
    "entry",
    [{"name": "甲市"}, dict(CITIES["location"][0], lat="north"), "甲市"],
    ids=["no-coordinates", "bad-coordinates", "not-an-object"],
)
async def test_a_place_of_an_unknown_shape_is_unavailable(monkeypatch, entry):
    install(monkeypatch, {CITY: ok({"code": "200", "location": [entry]})})

    with pytest.raises(weather.WeatherUnavailable):
        await weather.find_places("甲")


async def test_a_network_error_while_looking_up_propagates(monkeypatch):
    fake = install(monkeypatch, {CITY: httpx.ConnectError("boom"), POI: ok(POIS)})

    with pytest.raises(httpx.ConnectError):
        await weather.find_places("甲")
    assert fake.paths == [CITY]


# ---------------------------------------------------------------------------
# 天气：v1 的三个接口
# ---------------------------------------------------------------------------


async def test_current_weather_is_one_v1_request_latitude_first(monkeypatch):
    fake = install(monkeypatch, {"/weather/v1/current": ok(CURRENT)})

    current = await weather.current_weather(PLACE)

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["url"] == f"https://{HOST}/weather/v1/current/39.92/116.42"
    assert call["params"] == _WEATHER_PARAMS
    assert call["headers"] == {"X-QW-Api-Key": KEY}
    assert current.readings == {
        "condition": "少云",
        "temperature": "31.71°C",
        "feelsLike": "33.64°C",
        "humidity": "69%",
        "windDirection": "sw",
        "windScale": "3",
        "windSpeed": "4.74 m/s",
        "windGust": "7.07 m/s",
        "precipitation": "0 mm",
        "visibility": "29020 m",
        "cloudCover": "5%",
        "uvIndex": "3",
    }
    assert current.attributions == (ATTRIBUTION,)


@pytest.mark.parametrize("hours", [1, 24, 240])
async def test_hourly_forecast_passes_the_hours_through(monkeypatch, hours):
    fake = install(monkeypatch, {"/weather/v1/hourly": ok(HOURLY)})

    hourly = await weather.hourly_forecast(PLACE, hours)

    assert len(fake.calls) == 1
    assert fake.calls[0]["url"] == f"https://{HOST}/weather/v1/hourly/39.92/116.42"
    assert fake.calls[0]["params"] == {"hours": hours, **_WEATHER_PARAMS}
    assert hourly.hours[0] == {
        "time": "2024-05-31T11:00+08:00",
        "condition": "阴",
        "temperature": "31.12°C",
        "precipitationProbability": "31%",
        "precipitation": "0.09 mm",
        "windDirection": "sw",
        "windScale": "3",
    }
    assert [h["precipitationProbability"] for h in hourly.hours] == ["31%", "30%"]
    assert hourly.attributions == (ATTRIBUTION,)


@pytest.mark.parametrize("days", [1, 7, 10])
async def test_daily_forecast_passes_the_days_through_and_keeps_day_and_night(
    monkeypatch, days
):
    fake = install(monkeypatch, {"/weather/v1/daily": ok(DAILY)})

    daily = await weather.daily_forecast(PLACE, days)

    assert len(fake.calls) == 1
    assert fake.calls[0]["url"] == f"https://{HOST}/weather/v1/daily/39.92/116.42"
    assert fake.calls[0]["params"] == {"days": days, **_WEATHER_PARAMS}
    (day,) = daily.days
    assert day.readings == {
        "date": "2024-08-11",
        "temperatureMax": "29.94°C",
        "temperatureMin": "20.93°C",
        "uvIndexMax": "6",
        "sunrise": "2024-08-11T06:22+08:00",
        "sunset": "2024-08-11T19:34+08:00",
        "moonrise": "2024-08-11T22:00+08:00",
        "moonset": "2024-08-11T06:57+08:00",
        "moonPhase": "waning-gibbous",
    }
    assert day.daytime == {
        "condition": "小雨",
        "temperatureMax": "29.94°C",
        "temperatureMin": "20.93°C",
        "precipitationProbability": "64%",
        "precipitation": "0.75 mm",
        "windDirection": "w",
        "windScale": "2",
        "humidity": "52%",
        "cloudCover": "32%",
    }
    assert day.nighttime["condition"] == "晴间多云"
    assert day.nighttime["precipitationProbability"] == "0%"
    assert day.nighttime["humidity"] == "56%"
    assert daily.attributions == (ATTRIBUTION,)


@pytest.mark.parametrize(
    ("ask", "bounds"),
    [
        (lambda: weather.hourly_forecast(PLACE, 0), "1 到 240"),
        (lambda: weather.hourly_forecast(PLACE, 241), "1 到 240"),
        (lambda: weather.daily_forecast(PLACE, 0), "1 到 10"),
        (lambda: weather.daily_forecast(PLACE, 11), "1 到 10"),
    ],
    ids=["hours-0", "hours-241", "days-0", "days-11"],
)
async def test_an_out_of_range_span_is_refused_without_a_request(monkeypatch, ask, bounds):
    fake = install(monkeypatch, {})

    with pytest.raises(CapabilityInvalidArg, match=bounds):
        await ask()
    assert fake.calls == []


@pytest.mark.parametrize(
    ("status", "title"),
    [(403, "NO CREDIT"), (429, "TOO MANY REQUESTS"), (400, "NO SUCH LOCATION")],
)
async def test_a_v1_error_is_unavailable_with_its_status_and_title(monkeypatch, status, title):
    install(monkeypatch, {"/weather/v1/current": problem(status, title)})

    with pytest.raises(weather.WeatherUnavailable, match=f"{status} {title}") as caught:
        await weather.current_weather(PLACE)
    assert KEY not in str(caught.value)


@pytest.mark.parametrize(
    ("route", "answer"),
    [
        # v7 的样子：顶层 code + now，v1 的实况不长这样
        ("/weather/v1/current", ok({"code": "200", "now": {"text": "晴", "temp": "31"}})),
        ("/weather/v1/current", ok({"condition": {"text": "晴"}, "temperature": 31})),
        ("/weather/v1/current", ok(["晴"])),
        ("/weather/v1/current", httpx.Response(200, text="<html>gateway</html>")),
        ("/weather/v1/hourly", ok({"metadata": {}, "hours": "soon"})),
        ("/weather/v1/hourly", ok({"metadata": {}, "hours": [{"condition": {"text": "晴"}}]})),
        ("/weather/v1/daily", ok({"metadata": {}, "days": ["sunny"]})),
        ("/weather/v1/daily", ok({"code": "200", "daily": []})),
    ],
)
async def test_a_response_of_an_unknown_shape_is_unavailable(monkeypatch, route, answer):
    install(monkeypatch, {route: answer})
    ask = {
        "/weather/v1/current": lambda: weather.current_weather(PLACE),
        "/weather/v1/hourly": lambda: weather.hourly_forecast(PLACE, 24),
        "/weather/v1/daily": lambda: weather.daily_forecast(PLACE, 7),
    }[route]

    with pytest.raises(weather.WeatherUnavailable):
        await ask()


async def test_a_network_error_while_asking_the_weather_propagates(monkeypatch):
    install(monkeypatch, {"/weather/v1/current": httpx.ReadTimeout("slow")})

    with pytest.raises(httpx.ReadTimeout):
        await weather.current_weather(PLACE)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["qweather_api_key", "qweather_api_host"])
async def test_without_configuration_nothing_is_sent(monkeypatch, missing):
    fake = install(monkeypatch, {})
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, **{missing: None}))

    with pytest.raises(weather.WeatherUnavailable, match=missing.upper()):
        await weather.find_places("甲")
    with pytest.raises(weather.WeatherUnavailable, match=missing.upper()):
        await weather.current_weather(PLACE)
    assert fake.calls == []


def test_the_settings_read_the_qweather_env(monkeypatch):
    monkeypatch.setenv("QWEATHER_API_KEY", "k")
    monkeypatch.setenv("QWEATHER_API_HOST", "h")

    fresh = config.Settings()

    assert (fresh.qweather_api_key, fresh.qweather_api_host) == ("k", "h")
