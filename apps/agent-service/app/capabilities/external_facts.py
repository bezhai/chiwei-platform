"""Typed HTTP capability for weather, anime, holiday, and sun-time facts.

Business tools choose the configured inputs and current CST date.  This module
owns external URLs, HTTP client configuration, response parsing, and the
provider-specific failure payloads returned to those tools.  Calls remain
one-shot: routing them through the generic retrying HTTP client would change
the established observable behavior of these tools.
"""

from __future__ import annotations

import html
import logging
from typing import Any, Literal, NotRequired, TypedDict

import httpx

logger = logging.getLogger(__name__)

_BANGUMI_CALENDAR_URL = "https://api.bgm.tv/calendar"
_TIMOR_HOLIDAY_BASE = "https://timor.tech/api/holiday/info"
_HTTP_TIMEOUT_SECONDS = 10.0

_HOLIDAY_TYPE_LABEL = {
    0: "工作日",
    1: "周末休息",
    2: "法定节假日",
    3: "周末调休补班",
}


class ExternalFactFailure(TypedDict):
    ok: Literal[False]
    reason: str


class WeatherFact(TypedDict):
    ok: Literal[True]
    city: str
    temp: Any
    weather: str
    feels_like: NotRequired[Any]
    humidity: NotRequired[Any]
    wind: NotRequired[str]


class AnimeCalendarFact(TypedDict):
    ok: Literal[True]
    weekday: str
    anime: list[str]


class HolidayFact(TypedDict):
    ok: Literal[True]
    date: str
    weekday: str
    kind: str
    holiday_name: Any


class SunTimesFact(TypedDict):
    ok: Literal[True]
    city: str
    sunrise: str
    sunset: str


def _failed(reason: str) -> ExternalFactFailure:
    return {"ok": False, "reason": reason}


async def fetch_weather(
    *,
    api_host: str,
    api_key: str,
    location: str,
    city: str,
) -> WeatherFact | ExternalFactFailure:
    """Fetch and parse QWeather current conditions without exposing the key."""
    url = f"https://{api_host}/v7/weather/now"
    params = {"location": location}
    headers = {"X-QW-Api-Key": api_key}

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("query_weather connect error: %s", type(exc).__name__)
        return _failed(f"无法连接和风天气({type(exc).__name__})")

    if response.status_code != 200:
        logger.warning("query_weather http %d", response.status_code)
        return _failed(f"和风天气返回 HTTP {response.status_code}")

    try:
        data = response.json()
    except ValueError:
        logger.warning("query_weather body not json")
        return _failed("响应解析失败")

    if data.get("code") != "200":
        logger.warning("query_weather api code=%s", data.get("code"))
        return _failed(f"和风天气返回错误码 {data.get('code')}")

    now = data.get("now") or {}
    text = now.get("text")
    temp = now.get("temp")
    if not text or temp is None:
        return _failed("响应缺少天气字段")

    result: WeatherFact = {
        "ok": True,
        "city": city,
        "temp": temp,
        "weather": text,
    }
    feels = now.get("feelsLike")
    if feels is not None:
        result["feels_like"] = feels
    if (humidity := now.get("humidity")) is not None:
        result["humidity"] = humidity
    wind_dir = now.get("windDir")
    wind_scale = now.get("windScale")
    if wind_dir and wind_scale is not None:
        result["wind"] = f"{wind_dir}{wind_scale}级"
    return result


async def fetch_anime_calendar(
    *,
    today_isoweekday: int,
    proxy_url: str | None,
) -> AnimeCalendarFact | ExternalFactFailure:
    """Fetch and parse today's Bangumi calendar through the configured proxy."""
    client_kwargs: dict[str, Any] = {"timeout": _HTTP_TIMEOUT_SECONDS}
    if proxy_url:
        client_kwargs["proxy"] = proxy_url

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.get(_BANGUMI_CALENDAR_URL)
    except httpx.HTTPError as exc:
        logger.warning("query_anime_calendar connect error: %s", type(exc).__name__)
        return _failed(f"无法连接 Bangumi({type(exc).__name__})")

    if response.status_code != 200:
        logger.warning("query_anime_calendar http %d", response.status_code)
        return _failed(f"Bangumi 返回 HTTP {response.status_code}")

    try:
        week = response.json()
    except ValueError:
        logger.warning("query_anime_calendar body not json")
        return _failed("响应解析失败")

    if not isinstance(week, list):
        return _failed("响应结构异常")

    today_block = next(
        (
            day
            for day in week
            if isinstance(day, dict)
            and (day.get("weekday") or {}).get("id") == today_isoweekday
        ),
        None,
    )
    weekday_cn = (
        (today_block.get("weekday") or {}).get("cn") if today_block else None
    ) or "今天"

    items = (today_block or {}).get("items") or []
    names: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        raw = item.get("name_cn") or item.get("name") or ""
        name = html.unescape(raw).strip()
        if name:
            names.append(name)

    return {"ok": True, "weekday": weekday_cn, "anime": names}


async def fetch_holiday(*, today: str) -> HolidayFact | ExternalFactFailure:
    """Fetch and parse timor's holiday classification for a CST date."""
    url = f"{_TIMOR_HOLIDAY_BASE}/{today}"

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        logger.warning("query_holiday connect error: %s", type(exc).__name__)
        return _failed(f"无法连接 timor({type(exc).__name__})")

    if response.status_code != 200:
        logger.warning("query_holiday http %d", response.status_code)
        return _failed(f"timor 返回 HTTP {response.status_code}")

    try:
        data = response.json()
    except ValueError:
        logger.warning("query_holiday body not json")
        return _failed("响应解析失败")

    if data.get("code") != 0:
        logger.warning("query_holiday api code=%s", data.get("code"))
        return _failed(f"timor 返回错误码 {data.get('code')}")

    type_block = data.get("type") or {}
    type_code = type_block.get("type")
    weekday_name = type_block.get("name") or ""
    label = _HOLIDAY_TYPE_LABEL.get(type_code)
    if label is None:
        return _failed("响应缺少节假日类型")

    holiday = data.get("holiday") or {}
    holiday_name = holiday.get("name") if isinstance(holiday, dict) else None
    return {
        "ok": True,
        "date": today,
        "weekday": weekday_name,
        "kind": label,
        "holiday_name": holiday_name,
    }


def _qweather_local_hm(value: str | None) -> str | None:
    if not value or "T" not in value:
        return None
    hour_minute = value.split("T", 1)[1][:5]
    if len(hour_minute) == 5 and hour_minute[2] == ":":
        return hour_minute
    return None


async def fetch_sun_times(
    *,
    api_host: str,
    api_key: str,
    location: str,
    city: str,
    date: str,
) -> SunTimesFact | ExternalFactFailure:
    """Fetch and parse QWeather sunrise/sunset data without exposing the key."""
    url = f"https://{api_host}/v7/astronomy/sun"
    params = {"location": location, "date": date}
    headers = {"X-QW-Api-Key": api_key}

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("query_sun_times connect error: %s", type(exc).__name__)
        return _failed(f"无法连接和风天气({type(exc).__name__})")

    if response.status_code != 200:
        logger.warning("query_sun_times http %d", response.status_code)
        return _failed(f"和风天气返回 HTTP {response.status_code}")

    try:
        data = response.json()
    except ValueError:
        logger.warning("query_sun_times body not json")
        return _failed("响应解析失败")

    if data.get("code") != "200":
        logger.warning("query_sun_times api code=%s", data.get("code"))
        return _failed(f"和风天气返回错误码 {data.get('code')}")

    sunrise = _qweather_local_hm(data.get("sunrise"))
    sunset = _qweather_local_hm(data.get("sunset"))
    if not sunrise or not sunset:
        return _failed("响应缺少日出日落字段")

    return {
        "ok": True,
        "city": city,
        "sunrise": sunrise,
        "sunset": sunset,
    }
