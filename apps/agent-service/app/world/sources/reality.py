"""知识来源"现实"：真实世界此刻的样子——天气、网页上能搜到的事。

* 天气（:mod:`app.capabilities.weather`，和风天气）分三个工具：
  :func:`check_current_weather` 查此刻的实况，:func:`check_hourly_forecast` 查逐小时预报，
  :func:`check_daily_forecast` 查逐日预报。和风的实况、逐小时、逐日本来就是三个接口，拆成
  三个工具，调用的 agent 从工具名就看得出该用哪个，每次也只发它要的那一个天气请求（加一次
  地名查询）；只想知道现在下没下雨，不用连预报一起拿。查哪儿由调用的 agent 自己传，它从
  世界的记录里知道这个世界坐落在哪个真实的地方；代码和配置里没有任何地名。什么时候查、
  查哪样，也由它自己判断，这里不设次数限制。
* :func:`app.agent.tools.search.search_web` —— 基础层现成的网页搜索，原样复用。

都只读，没有收件处理。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from pydantic import Field

from app.agent.tooling import tool
from app.agent.tools._common import tool_error
from app.agent.tools.search import search_web
from app.capabilities import weather
from app.world.sources import Source

# 和风 v1 的风向是方位代码，月相是英文代码；按和风文档里的中文名说出来，不认识的代码原样给。
_COMPASS = {
    "n": "北风", "nne": "北东北风", "ne": "东北风", "ene": "东东北风",
    "e": "东风", "ese": "东东南风", "se": "东南风", "sse": "南东南风",
    "s": "南风", "ssw": "南西南风", "sw": "西南风", "wsw": "西西南风",
    "w": "西风", "wnw": "西西北风", "nw": "西北风", "nnw": "北西北风",
    "none": "无持续风向", "vrb": "风向不定",
}
_MOON_PHASES = {
    "new-moon": "新月", "waxing-crescent": "蛾眉月", "first-quarter": "上弦月",
    "waxing-gibbous": "盈凸月", "full-moon": "满月", "waning-gibbous": "亏凸月",
    "last-quarter": "下弦月", "waning-crescent": "残月",
}


def _wind(code: str) -> str:
    return _COMPASS.get(code, f"风向 {code}")


def _moon_phase(code: str) -> str:
    return _MOON_PHASES.get(code, f"月相 {code}")


_Labels = tuple[tuple[str, Callable[[str], str]], ...]

_CURRENT_LABELS: _Labels = (
    ("condition", "{}".format), ("temperature", "气温 {}".format),
    ("feelsLike", "体感 {}".format), ("humidity", "湿度 {}".format),
    ("windDirection", _wind), ("windScale", "风力 {} 级".format),
    ("windSpeed", "风速 {}".format), ("windGust", "阵风 {}".format),
    ("precipitation", "近一小时降水 {}".format), ("visibility", "能见度 {}".format),
    ("cloudCover", "云量 {}".format), ("uvIndex", "紫外线指数 {}".format),
)
_HOUR_LABELS: _Labels = (
    ("time", "{}".format), ("condition", "{}".format), ("temperature", "{}".format),
    ("precipitationProbability", "降水概率 {}".format), ("precipitation", "降水 {}".format),
    ("windDirection", _wind), ("windScale", "风力 {} 级".format),
)
_DAY_LABELS: _Labels = (
    ("date", "{}".format), ("temperatureMin", "最低 {}".format),
    ("temperatureMax", "最高 {}".format), ("uvIndexMax", "紫外线指数最高 {}".format),
    ("sunrise", "日出 {}".format), ("sunset", "日落 {}".format),
    ("moonrise", "月出 {}".format), ("moonset", "月落 {}".format),
    ("moonPhase", _moon_phase),
)
_HALF_DAY_LABELS: _Labels = (
    ("condition", "{}".format), ("temperatureMin", "最低 {}".format),
    ("temperatureMax", "最高 {}".format),
    ("precipitationProbability", "降水概率 {}".format), ("precipitation", "降水 {}".format),
    ("windDirection", _wind), ("windScale", "风力 {} 级".format),
    ("humidity", "湿度 {}".format), ("cloudCover", "云量 {}".format),
)

_Place = Annotated[str, Field(description="要查的那个真实地方的名字")]


def _readings(values: dict[str, str], labels: _Labels) -> str:
    return "，".join(show(values[key]) for key, show in labels if key in values)


def _place_label(place: weather.Place) -> str:
    around = "，".join(x for x in (place.adm2, place.adm1, place.country) if x and x != place.name)
    return f"{place.name}（{around}）" if around else place.name


def _not_found(place: str) -> str:
    return f"和风天气查不到叫「{place}」的地方。"


def _report(places: list[weather.Place], lines: list[str], attributions: tuple[str, ...]) -> str:
    """和风认下的地方、同名的其他候选、读数，最后一行是和风要求一起展示的数据来源。"""
    head = [f"和风天气认下的地方：{_place_label(places[0])}"]
    if len(places) > 1:
        head.append(
            "同名的还有：" + "、".join(_place_label(p) for p in places[1:])
            + "。要查的不是上面那个的话，换一个更完整的名字再查。"
        )
    tail = ["数据来源：" + " ".join(attributions)] if attributions else []
    return "\n".join(head + lines + tail)


@tool
@tool_error("查天气实况失败")
async def check_current_weather(place: _Place) -> str:
    """查一个真实地方此刻的天气实况：天气现象、气温、体感、湿度、风、近一小时降水、能见度、云量、紫外线。

    交回来的是和风天气认下的那个地方和它的读数。名字有歧义时会列出同名的其他地方。
    """
    places = await weather.find_places(place)
    if not places:
        return _not_found(place)
    current = await weather.current_weather(places[0])
    return _report(places, ["实况：" + _readings(current.readings, _CURRENT_LABELS)],
                   current.attributions)


@tool
@tool_error("查逐小时预报失败")
async def check_hourly_forecast(
    place: _Place,
    hours: Annotated[
        int,
        Field(
            ge=weather.FORECAST_HOURS[0],
            le=weather.FORECAST_HOURS[1],
            description="要看接下来几个小时",
        ),
    ] = weather.DEFAULT_HOURS,
) -> str:
    """查一个真实地方接下来逐小时的天气预报：每小时的天气现象、气温、降水概率和降水量、风。

    交回来的是和风天气认下的那个地方和它的预报。名字有歧义时会列出同名的其他地方。
    """
    weather.check_hours(hours)
    places = await weather.find_places(place)
    if not places:
        return _not_found(place)
    forecast = await weather.hourly_forecast(places[0], hours)
    lines = ["逐小时预报："] + ["- " + _readings(h, _HOUR_LABELS) for h in forecast.hours]
    return _report(places, lines, forecast.attributions)


@tool
@tool_error("查逐日预报失败")
async def check_daily_forecast(
    place: _Place,
    days: Annotated[
        int,
        Field(
            ge=weather.FORECAST_DAYS[0],
            le=weather.FORECAST_DAYS[1],
            description="要看接下来几天",
        ),
    ] = weather.DEFAULT_DAYS,
) -> str:
    """查一个真实地方接下来几天的逐日预报：每天的最高最低气温、日出日落、月出月落和月相，白天和夜里分开说天气现象、降水、风、湿度、云量。

    交回来的是和风天气认下的那个地方和它的预报。名字有歧义时会列出同名的其他地方。
    """
    weather.check_days(days)
    places = await weather.find_places(place)
    if not places:
        return _not_found(place)
    forecast = await weather.daily_forecast(places[0], days)
    lines = ["逐日预报："]
    for day in forecast.days:
        lines.append("- " + _readings(day.readings, _DAY_LABELS))
        if day.daytime:
            lines.append("  白天：" + _readings(day.daytime, _HALF_DAY_LABELS))
        if day.nighttime:
            lines.append("  夜里：" + _readings(day.nighttime, _HALF_DAY_LABELS))
    return _report(places, lines, forecast.attributions)


SOURCE = Source(
    name="reality",
    tools=(check_current_weather, check_hourly_forecast, check_daily_forecast, search_web),
)
