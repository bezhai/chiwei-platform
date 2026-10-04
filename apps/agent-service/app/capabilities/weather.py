"""和风天气（QWeather）：地名换候选地点，地点换此刻的实况、逐小时预报或逐日预报。

这一层只管两件事：请求发对，响应认对。它不认识任何城市——查哪儿由调用方传进来；它不
补默认城市，也不在同名的几个地方之间替调用方挑一个，候选全部交回去。

地点走 GeoAPI，先按城市查（``/geo/v2/city/lookup``）；城市一个候选都没有，再按景点查一次
（``/geo/v2/poi/lookup``，``type=scenic``），补上山、公园这类城市查询查不到的地方。景点只是
兜底：城市查询是模糊搜索，景点名也可能先匹配到某个城市，这时不会再查景点；上游出错也
不会。和风的条款不许缓存 GeoAPI 的数据，所以每次都现查。

天气走 v1 的三个接口（``/weather/v1/{current|hourly|daily}/{纬度}/{经度}``），实况、逐小时、
逐日分开查，要哪样只发哪样的请求：和风的额度按请求数算，地名查询也算在里面，只想看一眼
此刻下没下雨，不该连预报一起拿。v1 只收经纬度，纬度在前，最多两位小数；GeoAPI 交回的坐标
是字符串，四舍五入到两位再用（每个方向最多偏 0.005 度，相对 1 公里分辨率的天气数据可以
接受）。请求都带 ``lang=zh`` 和 ``localTime=true``，描述是中文、时间是当地时间，这一层不翻译
天气现象，也不换算时区。逐小时看几小时、逐日看几天由调用方定，原样交给和风；超出和风
支持的范围就抛 :class:`CapabilityInvalidArg`，一个请求都不发。

配置是两个环境变量：``QWEATHER_API_KEY`` 和 ``QWEATHER_API_HOST``（和风给每个账号一个
专属 host，统一域名对新 key 返回 403 INVALID HOST）。缺一个就抛
:class:`WeatherUnavailable`，一个请求都不发。key 只走 ``X-QW-Api-Key`` 这个 header，
不进 URL，所以报错原因里不会出现它。

结果分三种，不混：

* 地名匹配不到：:func:`find_places` 返回空列表。这不是故障，是没问对地方。和风现行文档
  写的是 HTTP 400 + ``error.title = "NO SUCH LOCATION"``，旧版是 body 里 ``code: "404"``，
  两种都认；成功响应里列表是空的也算。
* 上游出错、或者响应不是认识的结构：抛 :class:`WeatherUnavailable`，原因里只有状态码、
  和风自己的错误标题（一张固定的枚举表）或 GeoAPI body 的 ``code``。两边的约定不一样：
  GeoAPI 的成功响应带顶层 ``code``，v1 没有这个字段，出错只看 HTTP 状态码和
  ``application/problem+json`` 里的 ``error.title``。
* 网络异常原样往外抛；:class:`app.capabilities.http.HTTPClient` 已经按 GET 的规则重试过
  （429 和部分 5xx 也在它那里重试，所以实际发出的请求可能比调用次数多）。

交回去的读数是逐项挑出来的字符串（天气现象代码、气压这类对读的人没用的不要）：带单位的
量 ``{value, unit}`` 拼上它的单位；湿度、云量、降水概率在 v1 里是 0 到 1 的小数，换成
百分数；其余照和风给的原样。v1 的实况没有观测时间，这里也不补。和风要求数据来源和数据
一起展示，``metadata.attributions`` 原样交回。怎么说给谁听是调用方的事。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import httpx

from app.capabilities._errors import CapabilityInvalidArg
from app.capabilities.http import HTTPClient
from app.infra import config

_CITY_PATH = "/geo/v2/city/lookup"
_POI_PATH = "/geo/v2/poi/lookup"
_CURRENT_PATH = "/weather/v1/current"
_HOURLY_PATH = "/weather/v1/hourly"
_DAILY_PATH = "/weather/v1/daily"

# GeoAPI 是模糊搜索，一个名字可能匹配到好几个地方。多要几条是为了把同名的候选一并交回去，
# 让调用方看得出这次的名字有歧义。
_GEO_CANDIDATES = 5
_NO_SUCH_LOCATION = "NO SUCH LOCATION"
_LANG = "zh"
_TWO_PLACES = Decimal("0.01")

# 和风文档写的取值范围和默认值。
FORECAST_HOURS = (1, 240)
FORECAST_DAYS = (1, 10)
DEFAULT_HOURS = 24
DEFAULT_DAYS = 7

_http = HTTPClient(timeout=10.0)


class WeatherUnavailable(RuntimeError):
    """这次没拿到天气：没配置、上游出错、或者响应不是认识的样子。原因里不含 key。"""


@dataclass(frozen=True)
class Place:
    """和风认下来的一个地方：它自己说的名字和归属，加上拿去查天气的坐标（两位小数）。"""

    name: str
    adm2: str
    adm1: str
    country: str
    lat: str
    lon: str


@dataclass(frozen=True)
class Current:
    readings: dict[str, str]
    attributions: tuple[str, ...]


@dataclass(frozen=True)
class Hourly:
    hours: list[dict[str, str]]
    attributions: tuple[str, ...]


@dataclass(frozen=True)
class Day:
    """一天：全天的读数（日期、最高最低、日月出没），加上白天、夜里两段各自的读数。"""

    readings: dict[str, str]
    daytime: dict[str, str]
    nighttime: dict[str, str]


@dataclass(frozen=True)
class Daily:
    days: list[Day]
    attributions: tuple[str, ...]


# ---------------------------------------------------------------------------
# 认读数
# ---------------------------------------------------------------------------


def _is_number(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _text(v: Any) -> str | None:
    return v if isinstance(v, str) else None


def _number(v: Any) -> str | None:
    return str(v) if _is_number(v) else None


def _fraction(v: Any) -> str | None:
    return f"{round(v * 100)}%" if _is_number(v) else None


def _quantity(v: Any) -> str | None:
    if not isinstance(v, dict):
        return None
    value, unit = v.get("value"), v.get("unit")
    if not _is_number(value) or not isinstance(unit, str):
        return None
    return f"{value}{unit}" if unit.startswith("°") else f"{value} {unit}"


def _date(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v).date().isoformat()
    except ValueError:
        return None


# 一项读数：交回去叫什么，在 v1 响应里的路径，怎么认。
_Reading = tuple[str, tuple[str, ...], Callable[[Any], str | None]]

_WIND: tuple[_Reading, ...] = (
    ("windDirection", ("wind", "direction", "compass"), _text),
    ("windScale", ("wind", "scale"), _number),
)
_CURRENT: tuple[_Reading, ...] = (
    ("condition", ("condition", "text"), _text),
    ("temperature", ("temperature",), _quantity),
    ("feelsLike", ("feelsLike",), _quantity),
    ("humidity", ("humidity",), _fraction),
    *_WIND,
    ("windSpeed", ("wind", "speed"), _quantity),
    ("windGust", ("windGust",), _quantity),
    ("precipitation", ("precipitation", "amount"), _quantity),
    ("visibility", ("visibility",), _quantity),
    ("cloudCover", ("cloudCover",), _fraction),
    ("uvIndex", ("uvIndex",), _number),
)
_HOUR: tuple[_Reading, ...] = (
    ("time", ("forecastTime",), _text),
    ("condition", ("condition", "text"), _text),
    ("temperature", ("temperature",), _quantity),
    ("precipitationProbability", ("precipitation", "probability"), _fraction),
    ("precipitation", ("precipitation", "amount"), _quantity),
    *_WIND,
)
_DAY: tuple[_Reading, ...] = (
    ("date", ("forecastStartTime",), _date),
    ("temperatureMax", ("temperatureMax",), _quantity),
    ("temperatureMin", ("temperatureMin",), _quantity),
    ("uvIndexMax", ("uvIndexMax",), _number),
    ("sunrise", ("astro", "sunrise"), _text),
    ("sunset", ("astro", "sunset"), _text),
    ("moonrise", ("astro", "moonrise"), _text),
    ("moonset", ("astro", "moonset"), _text),
    ("moonPhase", ("astro", "moonPhase"), _text),
)
_HALF_DAY: tuple[_Reading, ...] = (
    ("condition", ("condition", "text"), _text),
    ("temperatureMax", ("temperatureMax",), _quantity),
    ("temperatureMin", ("temperatureMin",), _quantity),
    ("precipitationProbability", ("precipitation", "probability"), _fraction),
    ("precipitation", ("precipitation", "amount"), _quantity),
    *_WIND,
    ("humidity", ("humidity",), _fraction),
    ("cloudCover", ("cloudCover",), _fraction),
)


def _pick(
    entry: Any, readings: tuple[_Reading, ...], where: str, *, required: str
) -> dict[str, str]:
    """按表挑读数。没给的项跳过；给了却不是文档说的样子，或者缺了 ``required``，就是不认识。"""
    if not isinstance(entry, dict):
        raise WeatherUnavailable(f"{where} returned an entry that is not an object")
    picked: dict[str, str] = {}
    for name, path, read in readings:
        node: Any = entry
        for key in path:
            if node is None:
                break
            if not isinstance(node, dict):
                raise WeatherUnavailable(f"{where} returned {'.'.join(path)} in an unknown shape")
            node = node.get(key)
        if node is None or node == "":
            continue
        shown = read(node)
        if shown is None:
            raise WeatherUnavailable(f"{where} returned {'.'.join(path)} in an unknown shape")
        picked[name] = shown
    if required not in picked:
        raise WeatherUnavailable(f"{where} returned an entry without {required}")
    return picked


def _entries(body: dict, key: str, where: str) -> list:
    found = body.get(key)
    if not isinstance(found, list):
        raise WeatherUnavailable(f"{where} returned no {key} list")
    return found


def _attributions(body: dict, where: str) -> tuple[str, ...]:
    metadata = body.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise WeatherUnavailable(f"{where} returned metadata in an unknown shape")
    found = metadata.get("attributions") or []
    if not isinstance(found, list) or not all(isinstance(a, str) for a in found):
        raise WeatherUnavailable(f"{where} returned metadata in an unknown shape")
    return tuple(found)


# ---------------------------------------------------------------------------
# 发请求
# ---------------------------------------------------------------------------


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


def _http_error(where: str, resp: httpx.Response) -> WeatherUnavailable:
    return WeatherUnavailable(
        f"{where} returned HTTP {resp.status_code} {_error_title(resp)}".rstrip()
    )


def _coordinate(v: Any) -> str | None:
    """GeoAPI 的坐标字符串四舍五入到两位小数；认不出来就是 ``None``。"""
    if not isinstance(v, str | int | float) or isinstance(v, bool):
        return None
    try:
        exact = Decimal(str(v))
    except InvalidOperation:
        return None
    if not exact.is_finite():
        return None
    return str(exact.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP))


def _place(entry: Any, path: str) -> Place:
    if not isinstance(entry, dict) or not entry.get("name"):
        raise WeatherUnavailable(f"{path} returned a place without a name")
    lat, lon = _coordinate(entry.get("lat")), _coordinate(entry.get("lon"))
    if lat is None or lon is None:
        raise WeatherUnavailable(f"{path} returned a place without coordinates")
    return Place(
        name=str(entry["name"]),
        adm2=str(entry.get("adm2") or ""),
        adm1=str(entry.get("adm1") or ""),
        country=str(entry.get("country") or ""),
        lat=lat,
        lon=lon,
    )


async def _lookup(path: str, found_in: str, params: dict[str, str]) -> list[Place]:
    resp, body = await _get(path, {**params, "number": _GEO_CANDIDATES, "lang": _LANG})
    if resp.status_code != 200 and _error_title(resp) == _NO_SUCH_LOCATION:
        return []
    if body is not None and body.get("code") == "404":
        return []
    if resp.status_code != 200:
        raise _http_error(path, resp)
    if body is None:
        raise WeatherUnavailable(f"{path} returned a body that is not a JSON object")
    if body.get("code") != "200":
        raise WeatherUnavailable(f"{path} returned code {body.get('code')}")
    return [_place(entry, path) for entry in _entries(body, found_in, path)]


async def find_places(name: str) -> list[Place]:
    """和风认得的、叫这个名字的地方，按和风自己的相关性排序；一个都没有就是空列表。

    先按城市查；城市一个候选都没有，才按景点再查一次。
    """
    places = await _lookup(_CITY_PATH, "location", {"location": name})
    if places:
        return places
    return await _lookup(_POI_PATH, "poi", {"location": name, "type": "scenic"})


def _check_within(name: str, value: Any, bounds: tuple[int, int]) -> None:
    low, high = bounds
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise CapabilityInvalidArg(
            f"{name} 只能是 {low} 到 {high} 之间的整数，这次给的是 {value!r}",
            meta={"param": name},
        )


def check_hours(hours: Any) -> None:
    """逐小时预报看几小时：和风支持 1 到 240，越界抛 :class:`CapabilityInvalidArg`。"""
    _check_within("hours", hours, FORECAST_HOURS)


def check_days(days: Any) -> None:
    """逐日预报看几天：和风支持 1 到 10，越界抛 :class:`CapabilityInvalidArg`。"""
    _check_within("days", days, FORECAST_DAYS)


async def _weather(path: str, place: Place, params: dict[str, Any]) -> dict:
    """v1 的路径里带坐标：纬度在前。"""
    resp, body = await _get(
        f"{path}/{place.lat}/{place.lon}", {**params, "localTime": "true", "lang": _LANG}
    )
    if resp.status_code != 200:
        raise _http_error(path, resp)
    if body is None:
        raise WeatherUnavailable(f"{path} returned a body that is not a JSON object")
    return body


async def current_weather(place: Place) -> Current:
    """这个地方此刻的实况。"""
    body = await _weather(_CURRENT_PATH, place, {})
    return Current(
        readings=_pick(body, _CURRENT, _CURRENT_PATH, required="condition"),
        attributions=_attributions(body, _CURRENT_PATH),
    )


async def hourly_forecast(place: Place, hours: int) -> Hourly:
    """这个地方接下来 ``hours`` 个小时的逐小时预报。"""
    check_hours(hours)
    body = await _weather(_HOURLY_PATH, place, {"hours": hours})
    return Hourly(
        hours=[
            _pick(h, _HOUR, _HOURLY_PATH, required="time")
            for h in _entries(body, "hours", _HOURLY_PATH)
        ],
        attributions=_attributions(body, _HOURLY_PATH),
    )


async def daily_forecast(place: Place, days: int) -> Daily:
    """这个地方接下来 ``days`` 天的逐日预报，每天分白天、夜里两段。"""
    check_days(days)
    body = await _weather(_DAILY_PATH, place, {"days": days})

    def half(day: dict, key: str) -> dict[str, str]:
        block = day.get(key)
        if block is None:
            return {}
        return _pick(block, _HALF_DAY, _DAILY_PATH, required="condition")

    found = []
    for day in _entries(body, "days", _DAILY_PATH):
        readings = _pick(day, _DAY, _DAILY_PATH, required="date")
        found.append(
            Day(readings=readings, daytime=half(day, "daytime"), nighttime=half(day, "nighttime"))
        )
    return Daily(days=found, attributions=_attributions(body, _DAILY_PATH))
