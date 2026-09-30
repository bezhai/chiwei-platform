"""和风天气（QWeather）：地名换候选地点，地点换此刻的天气、逐小时预报和逐日预报。

这一层只管两件事：请求发对，响应认对。它不认识任何城市——查哪儿由调用方传进来；它不
补默认城市，也不在同名的几个地方之间替调用方挑一个，候选全部交回去。

配置是两个环境变量：``QWEATHER_API_KEY`` 和 ``QWEATHER_API_HOST``（和风给每个账号一个
专属 host，统一域名对新 key 返回 403 INVALID HOST）。缺一个就抛
:class:`WeatherUnavailable`，一个请求都不发。key 只走 ``X-QW-Api-Key`` 这个 header，
不进 URL，所以报错原因里不会出现它。

结果分三种，不混：

* 地名匹配不到：:func:`find_places` 返回空列表。这不是故障，是没问对地方。和风现行文档
  写的是 HTTP 400 + ``error.title = "NO SUCH LOCATION"``，旧版是 body 里 ``code: "404"``，
  两种都认。
* 上游出错、或者响应不是认识的结构：抛 :class:`WeatherUnavailable`，原因里只有状态码、
  和风自己的错误标题（一张固定的枚举表）或 body 的 ``code``。
* 网络异常原样往外抛；:class:`app.capabilities.http.HTTPClient` 已经按 GET 的规则重试过。

交回去的读数是和风响应里原样的字符串，只挑了几项（图标编号这类对读的人没用的不要）。
怎么说给谁听是调用方的事。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.capabilities.http import HTTPClient
from app.infra import config

_GEO_PATH = "/geo/v2/city/lookup"
_NOW_PATH = "/v7/weather/now"
_HOURLY_PATH = "/v7/weather/24h"
_DAILY_PATH = "/v7/weather/3d"

# GeoAPI 是模糊搜索，一个名字可能匹配到好几个地方。多要几条是为了把同名的候选一并交回去，
# 让调用方看得出这次的名字有歧义。
_GEO_CANDIDATES = 5
_NO_SUCH_LOCATION = "NO SUCH LOCATION"

_NOW_FIELDS = (
    "obsTime", "text", "temp", "feelsLike", "humidity", "windDir", "windScale",
    "precip", "vis", "cloud",
)
_HOURLY_FIELDS = ("fxTime", "text", "temp", "pop", "precip", "windDir", "windScale")
_DAILY_FIELDS = (
    "fxDate", "sunrise", "sunset", "moonrise", "moonset", "moonPhase", "tempMax",
    "tempMin", "textDay", "textNight", "precip", "humidity", "windDirDay",
    "windScaleDay",
)

_http = HTTPClient(timeout=10.0)


class WeatherUnavailable(RuntimeError):
    """这次没拿到天气：没配置、上游出错、或者响应不是认识的样子。原因里不含 key。"""


@dataclass(frozen=True)
class Place:
    """和风认下来的一个地方：拿去查天气的 id，加上和风自己说的它叫什么、属于哪里。"""

    location_id: str
    name: str
    adm2: str
    adm1: str
    country: str


@dataclass(frozen=True)
class Weather:
    place: Place
    now: dict[str, str]
    hourly: list[dict[str, str]]
    daily: list[dict[str, str]]


def _endpoint() -> tuple[str, dict[str, str]]:
    settings = config.settings
    if not settings.qweather_api_key:
        raise WeatherUnavailable("QWEATHER_API_KEY is not configured")
    if not settings.qweather_api_host:
        raise WeatherUnavailable("QWEATHER_API_HOST is not configured")
    return f"https://{settings.qweather_api_host}", {
        "X-QW-Api-Key": settings.qweather_api_key
    }


def _error_title(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    title = error.get("title") if isinstance(error, dict) else None
    return title.strip() if isinstance(title, str) else ""


async def _get(path: str, params: dict[str, Any]) -> tuple[httpx.Response, dict | None]:
    """发一次 GET，交回响应和解析出来的 JSON 对象（不是对象就是 ``None``）。"""
    base, headers = _endpoint()
    resp = await _http.get(f"{base}{path}", params=params, headers=headers)
    try:
        body = resp.json()
    except ValueError:
        body = None
    return resp, body if isinstance(body, dict) else None


def _checked(path: str, resp: httpx.Response, body: dict | None) -> dict:
    if resp.status_code != 200:
        raise WeatherUnavailable(
            f"{path} returned HTTP {resp.status_code} {_error_title(resp)}".rstrip()
        )
    if body is None:
        raise WeatherUnavailable(f"{path} returned a body that is not a JSON object")
    if body.get("code") != "200":
        raise WeatherUnavailable(f"{path} returned code {body.get('code')}")
    return body


def _pick(entry: Any, fields: tuple[str, ...], path: str) -> dict[str, str]:
    if not isinstance(entry, dict):
        raise WeatherUnavailable(f"{path} returned an entry that is not an object")
    return {f: str(entry[f]) for f in fields if entry.get(f) not in (None, "")}


async def find_places(name: str) -> list[Place]:
    """和风认得的、叫这个名字的地方，按和风自己的相关性排序；一个都没有就是空列表。"""
    resp, body = await _get(_GEO_PATH, {"location": name, "number": _GEO_CANDIDATES})
    if resp.status_code != 200 and _error_title(resp) == _NO_SUCH_LOCATION:
        return []
    if body is not None and body.get("code") == "404":
        return []
    body = _checked(_GEO_PATH, resp, body)
    found = body.get("location")
    if not isinstance(found, list):
        raise WeatherUnavailable(f"{_GEO_PATH} returned no location list")
    places = []
    for entry in found:
        if not isinstance(entry, dict) or not entry.get("id") or not entry.get("name"):
            raise WeatherUnavailable(f"{_GEO_PATH} returned a location without id or name")
        places.append(
            Place(
                location_id=str(entry["id"]),
                name=str(entry["name"]),
                adm2=str(entry.get("adm2") or ""),
                adm1=str(entry.get("adm1") or ""),
                country=str(entry.get("country") or ""),
            )
        )
    return places


async def weather_at(place: Place) -> Weather:
    """这个地方此刻的天气、接下来 24 小时逐小时、接下来 3 天逐日（含日出日落）。"""
    params = {"location": place.location_id}

    resp, body = await _get(_NOW_PATH, params)
    now = _pick(_checked(_NOW_PATH, resp, body).get("now"), _NOW_FIELDS, _NOW_PATH)

    resp, body = await _get(_HOURLY_PATH, params)
    hours = _checked(_HOURLY_PATH, resp, body).get("hourly")
    if not isinstance(hours, list):
        raise WeatherUnavailable(f"{_HOURLY_PATH} returned no hourly list")

    resp, body = await _get(_DAILY_PATH, params)
    days = _checked(_DAILY_PATH, resp, body).get("daily")
    if not isinstance(days, list):
        raise WeatherUnavailable(f"{_DAILY_PATH} returned no daily list")

    return Weather(
        place=place,
        now=now,
        hourly=[_pick(h, _HOURLY_FIELDS, _HOURLY_PATH) for h in hours],
        daily=[_pick(d, _DAILY_FIELDS, _DAILY_PATH) for d in days],
    )
