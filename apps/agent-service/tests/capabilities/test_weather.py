"""和风天气这一层：地名换候选地点、地点换此刻 + 逐小时 + 逐日的天气。

HTTP 换成替身，按和风文档的响应结构回话。钉的是：请求发对了（host、路径、参数、key 只
走 header）；认得出"地名匹配不到"和"上游出错"是两件事；这一层不替调用方挑城市、不补
默认城市。
"""
from __future__ import annotations

import dataclasses

import httpx
import pytest

from app.capabilities import weather
from app.infra import config

HOST = "abc.qweatherapi.test"
KEY = "qweather-secret-key"


class _FakeHTTP:
    def __init__(self, routes: dict[str, httpx.Response]) -> None:
        self.routes = routes
        self.calls: list[dict] = []

    async def get(self, url: str, **kw) -> httpx.Response:
        self.calls.append({"url": url, **kw})
        path = url.split(HOST, 1)[1]
        return self.routes[path]


def _ok(body: dict) -> httpx.Response:
    return httpx.Response(200, json=body)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(
        config,
        "settings",
        dataclasses.replace(config.settings, qweather_api_key=KEY, qweather_api_host=HOST),
    )


def _install(monkeypatch, routes) -> _FakeHTTP:
    fake = _FakeHTTP(routes)
    monkeypatch.setattr(weather, "_http", fake)
    return fake


_GEO = {
    "code": "200",
    "location": [
        {"name": "甲市", "id": "101", "adm2": "甲市", "adm1": "甲省", "country": "中国"},
        {"name": "甲区", "id": "102", "adm2": "乙市", "adm1": "乙省", "country": "中国"},
    ],
}


async def test_find_places_returns_every_candidate_as_the_upstream_names_it(
    configured, monkeypatch
):
    fake = _install(monkeypatch, {"/geo/v2/city/lookup": _ok(_GEO)})

    places = await weather.find_places("甲")

    assert [p.location_id for p in places] == ["101", "102"]
    assert places[1] == weather.Place(
        location_id="102", name="甲区", adm2="乙市", adm1="乙省", country="中国"
    )
    call = fake.calls[0]
    assert call["url"] == f"https://{HOST}/geo/v2/city/lookup"
    assert call["params"]["location"] == "甲"
    assert call["headers"] == {"X-QW-Api-Key": KEY}
    assert KEY not in str(call["params"])


async def test_a_name_that_matches_nothing_is_an_empty_list(configured, monkeypatch):
    _install(
        monkeypatch,
        {
            "/geo/v2/city/lookup": httpx.Response(
                400, json={"error": {"status": 400, "title": "NO SUCH LOCATION"}}
            )
        },
    )

    assert await weather.find_places("不存在的地方") == []


async def test_an_upstream_error_is_not_mistaken_for_no_match(configured, monkeypatch):
    _install(
        monkeypatch,
        {
            "/geo/v2/city/lookup": httpx.Response(
                403, json={"error": {"status": 403, "title": "INVALID HOST"}}
            )
        },
    )

    with pytest.raises(weather.WeatherUnavailable, match="403 INVALID HOST"):
        await weather.find_places("甲")


async def test_weather_at_a_place_has_now_hours_and_days(configured, monkeypatch):
    fake = _install(
        monkeypatch,
        {
            "/v7/weather/now": _ok(
                {
                    "code": "200",
                    "now": {
                        "obsTime": "2026-09-30T14:00+08:00",
                        "text": "小雨",
                        "temp": "24",
                        "feelsLike": "26",
                        "humidity": "88",
                        "windDir": "东南风",
                        "windScale": "2",
                        "precip": "0.6",
                        "icon": "305",
                    },
                }
            ),
            "/v7/weather/24h": _ok(
                {
                    "code": "200",
                    "hourly": [
                        {"fxTime": "2026-09-30T15:00+08:00", "text": "中雨", "temp": "23", "pop": "80", "precip": "2.1", "icon": "306"},
                    ],
                }
            ),
            "/v7/weather/3d": _ok(
                {
                    "code": "200",
                    "daily": [
                        {"fxDate": "2026-09-30", "sunrise": "06:12", "sunset": "18:05", "tempMax": "27", "tempMin": "22", "textDay": "小雨", "textNight": "阴", "iconDay": "305"},
                    ],
                }
            ),
        },
    )
    place = weather.Place(location_id="101", name="甲市", adm2="甲市", adm1="甲省", country="中国")

    reading = await weather.weather_at(place)

    assert reading.place == place
    assert reading.now["text"] == "小雨" and reading.now["temp"] == "24"
    assert "icon" not in reading.now
    assert reading.hourly == [
        {"fxTime": "2026-09-30T15:00+08:00", "text": "中雨", "temp": "23", "pop": "80", "precip": "2.1"}
    ]
    assert reading.daily[0]["sunset"] == "18:05"
    assert "iconDay" not in reading.daily[0]
    assert {c["params"]["location"] for c in fake.calls} == {"101"}
    assert all(c["headers"] == {"X-QW-Api-Key": KEY} for c in fake.calls)


async def test_a_body_code_other_than_200_is_unavailable(configured, monkeypatch):
    _install(
        monkeypatch,
        {"/v7/weather/now": _ok({"code": "402"})},
    )
    place = weather.Place(location_id="101", name="甲市", adm2="", adm1="", country="")

    with pytest.raises(weather.WeatherUnavailable, match="402"):
        await weather.weather_at(place)


async def test_a_response_of_an_unknown_shape_is_unavailable(configured, monkeypatch):
    _install(
        monkeypatch,
        {"/v7/weather/now": _ok({"code": "200", "now": "sunny"})},
    )
    place = weather.Place(location_id="101", name="甲市", adm2="", adm1="", country="")

    with pytest.raises(weather.WeatherUnavailable):
        await weather.weather_at(place)


@pytest.mark.parametrize("missing", ["qweather_api_key", "qweather_api_host"])
async def test_without_configuration_nothing_is_sent(monkeypatch, missing):
    values = {"qweather_api_key": KEY, "qweather_api_host": HOST, missing: None}
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, **values))
    fake = _install(monkeypatch, {})

    with pytest.raises(weather.WeatherUnavailable, match=missing.upper()):
        await weather.find_places("甲")
    assert fake.calls == []


def test_the_settings_read_the_qweather_env(monkeypatch):
    monkeypatch.setenv("QWEATHER_API_KEY", "k")
    monkeypatch.setenv("QWEATHER_API_HOST", "h")

    fresh = config.Settings()

    assert (fresh.qweather_api_key, fresh.qweather_api_host) == ("k", "h")
