"""和风天气的替身：按和风文档的响应结构回话，记下每一次请求。

``app.capabilities.weather`` 和 world 的天气工具两边的测试共用。响应照文档里的示例构造；
地名换成占位的甲市、甲省（代码和测试都不认识真实的城市），天气接口的示例按请求带了
``lang=zh``、``localTime=true`` 的样子改成中文描述和当地时间。
"""
from __future__ import annotations

import dataclasses

import httpx

from app.capabilities import weather
from app.infra import config

HOST = "abc.qweatherapi.test"
KEY = "qweather-secret-key"
ATTRIBUTION = "https://developer.qweather.com/attribution.html"
_REFER = {"sources": [ATTRIBUTION], "license": ["QWeather Developers License"]}


class FakeQWeather:
    """按路径回话：路由的键是路径或路径前缀（天气接口的路径里带坐标）。值可以是异常。"""

    def __init__(self, routes: dict[str, httpx.Response | Exception]) -> None:
        self.routes = routes
        self.calls: list[dict] = []

    async def get(self, url: str, **kw) -> httpx.Response:
        self.calls.append({"url": url, **kw})
        path = url.removeprefix(f"https://{HOST}")
        for route, answer in self.routes.items():
            if path == route or path.startswith(route + "/"):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(f"unexpected request to {url}")

    @property
    def paths(self) -> list[str]:
        return [c["url"].removeprefix(f"https://{HOST}") for c in self.calls]


def install(monkeypatch, routes: dict[str, httpx.Response | Exception]) -> FakeQWeather:
    """配好 key 和 host，把和风换成替身。"""
    monkeypatch.setattr(
        config,
        "settings",
        dataclasses.replace(config.settings, qweather_api_key=KEY, qweather_api_host=HOST),
    )
    fake = FakeQWeather(routes)
    monkeypatch.setattr(weather, "_http", fake)
    return fake


def ok(body) -> httpx.Response:
    return httpx.Response(200, json=body)


def problem(status: int, title: str) -> httpx.Response:
    """和风文档"错误码"一页的出错格式：HTTP 状态码 + application/problem+json。"""
    return httpx.Response(
        status,
        json={
            "error": {
                "status": status,
                "type": "https://dev.qweather.com/docs/resource/error-code/",
                "title": title,
                "detail": "see the error code page",
            }
        },
        headers={"content-type": "application/problem+json"},
    )


def _geo_entry(name: str, adm2: str, adm1: str, lat: str, lon: str, kind: str) -> dict:
    return {
        "name": name, "id": "101011600", "lat": lat, "lon": lon, "adm2": adm2,
        "adm1": adm1, "country": "中国", "tz": "Asia/Shanghai", "utcOffset": "+08:00",
        "isDst": "0", "type": kind, "rank": "35", "fxLink": "https://www.qweather.com",
    }


# 城市查询：一个名字模糊匹配到两个地方。
CITIES = {
    "code": "200",
    "location": [
        _geo_entry("甲市", "甲市", "甲省", "39.91755", "116.41876", "city"),
        _geo_entry("甲区", "乙市", "乙省", "31.15999", "120.68000", "city"),
    ],
    "refer": _REFER,
}

# 景点查询（type=scenic）。
POIS = {
    "code": "200",
    "poi": [_geo_entry("丙山", "丙市", "丙省", "39.91999", "116.38999", "scenic")],
    "refer": _REFER,
}

_METADATA = {
    "tag": "03ec2ded05fa80a43df2664dd9e4a8f48f7cc4f97c6a81dfd736ae17098aba14",
    "attributions": [ATTRIBUTION],
}

# 实况：文档示例原样。v1 的实况里没有观测时间。
CURRENT = {
    "metadata": _METADATA,
    "condition": {"text": "少云", "code": "102"},
    "temperature": {"value": 31.71, "unit": "°C"},
    "feelsLike": {"value": 33.64, "unit": "°C"},
    "humidity": 0.69,
    "wind": {
        "direction": {"degree": 226, "compass": "sw"},
        "speed": {"value": 4.74, "unit": "m/s"},
        "scale": 3,
    },
    "windGust": {"value": 7.07, "unit": "m/s"},
    "precipitation": {
        "amount": {"value": 0, "unit": "mm"},
        "intensity": {"value": 0, "unit": "mm/h"},
        "type": "none",
    },
    "pressure": {"value": 1001.5, "unit": "hPa"},
    "visibility": {"value": 29020, "unit": "m"},
    "dewPoint": {"value": 25.36, "unit": "°C"},
    "cloudCover": 0.05,
    "uvIndex": 3,
}


def _hour(time: str, text: str, temp: float, pop: float, amount: float, compass: str) -> dict:
    return {
        "forecastTime": time,
        "condition": {"text": text, "code": "104"},
        "temperature": {"value": temp, "unit": "°C"},
        "feelsLike": {"value": 34.31, "unit": "°C"},
        "humidity": 0.76,
        "wind": {
            "direction": {"degree": 215, "compass": compass},
            "speed": {"value": 3.42, "unit": "m/s"},
            "scale": 3,
        },
        "windGust": {"value": 8.16, "unit": "m/s"},
        "precipitation": {
            "amount": {"value": amount, "unit": "mm"},
            "intensity": {"value": amount, "unit": "mm/h"},
            "probability": pop,
            "type": "rain",
        },
        "pressure": {"value": 1001.5, "unit": "hPa"},
        "visibility": {"value": 14780, "unit": "m"},
        "dewPoint": {"value": 26.44, "unit": "°C"},
        "cloudCover": 0.92,
        "uvIndex": 6,
    }


HOURLY = {
    "metadata": _METADATA,
    "hours": [
        _hour("2024-05-31T11:00+08:00", "阴", 31.12, 0.31, 0.09, "sw"),
        _hour("2024-05-31T12:00+08:00", "多云", 31.01, 0.3, 0.21, "nnw"),
    ],
}


def _half_day(start: str, end: str, text: str, high: float, low: float, pop: float,
              amount: float, compass: str, cloud: float, humidity: float) -> dict:
    return {
        "forecastStartTime": start,
        "forecastEndTime": end,
        "condition": {"text": text, "code": "305"},
        "temperatureMax": {"value": high, "unit": "°C"},
        "temperatureMin": {"value": low, "unit": "°C"},
        "wind": {
            "direction": {"degree": 270, "compass": compass},
            "speed": {"value": 1.87, "unit": "m/s"},
            "scale": 2,
        },
        "windGustMax": {"value": 6.64, "unit": "m/s"},
        "precipitation": {
            "amount": {"value": amount, "unit": "mm"},
            "probability": pop,
            "type": "rain" if amount else "none",
        },
        "cloudCover": cloud,
        "humidity": humidity,
    }


DAILY = {
    "metadata": _METADATA,
    "days": [
        {
            "forecastStartTime": "2024-08-11T00:00+08:00",
            "forecastEndTime": "2024-08-12T00:00+08:00",
            "astro": {
                "sunrise": "2024-08-11T06:22+08:00",
                "sunset": "2024-08-11T19:34+08:00",
                "civilDawn": "2024-08-11T05:44+08:00",
                "civilDusk": "2024-08-11T20:12+08:00",
                "solarNoon": "2024-08-11T12:58+08:00",
                "moonrise": "2024-08-11T22:00+08:00",
                "moonset": "2024-08-11T06:57+08:00",
                "moonPhase": "waning-gibbous",
            },
            "temperatureMax": {"value": 29.94, "unit": "°C"},
            "temperatureMin": {"value": 20.93, "unit": "°C"},
            "temperatureAvg": {"value": 25.57, "unit": "°C"},
            "uvIndexMax": 6,
            "daytime": _half_day(
                "2024-08-11T07:00+08:00", "2024-08-11T19:00+08:00", "小雨",
                29.94, 20.93, 0.64, 0.75, "w", 0.32, 0.52,
            ),
            "nighttime": _half_day(
                "2024-08-11T19:00+08:00", "2024-08-12T07:00+08:00", "晴间多云",
                29.8, 19.95, 0, 0, "nnw", 0.34, 0.56,
            ),
        },
    ],
}
