"""知识来源"现实"：真实世界此刻的样子——天气、网页上能搜到的事。

* :func:`check_weather` —— 天气（:mod:`app.capabilities.weather`）。查哪儿由调用的 agent 自己
  传，它从世界的记录里知道这个世界坐落在哪个真实的地方；代码和配置里没有任何地名。
* :func:`app.agent.tools.search.search_web` —— 基础层现成的网页搜索，原样复用。

两样都只读，没有收件处理。
"""
from __future__ import annotations

from typing import Annotated

from pydantic import Field

from app.agent.tooling import tool
from app.agent.tools._common import tool_error
from app.agent.tools.search import search_web
from app.capabilities import weather
from app.world.sources import Source

_NOW_LABELS = (
    ("obsTime", "观测于 {}"), ("text", "{}"), ("temp", "气温 {}°C"),
    ("feelsLike", "体感 {}°C"), ("humidity", "湿度 {}%"), ("windDir", "{}"),
    ("windScale", "风力 {} 级"), ("precip", "降水 {} mm"), ("vis", "能见度 {} km"),
    ("cloud", "云量 {}%"),
)
_HOURLY_LABELS = (
    ("fxTime", "{}"), ("text", "{}"), ("temp", "{}°C"), ("pop", "降水概率 {}%"),
    ("precip", "降水 {} mm"), ("windDir", "{}"), ("windScale", "风力 {} 级"),
)
_DAILY_LABELS = (
    ("fxDate", "{}"), ("textDay", "白天{}"), ("textNight", "夜里{}"),
    ("tempMin", "最低 {}°C"), ("tempMax", "最高 {}°C"), ("precip", "降水 {} mm"),
    ("humidity", "湿度 {}%"), ("windDirDay", "白天{}"), ("windScaleDay", "风力 {} 级"),
    ("sunrise", "日出 {}"), ("sunset", "日落 {}"), ("moonrise", "月出 {}"),
    ("moonset", "月落 {}"), ("moonPhase", "{}"),
)


def _readings(values: dict[str, str], labels) -> str:
    return "，".join(label.format(values[key]) for key, label in labels if key in values)


def _place_label(place: weather.Place) -> str:
    around = "，".join(x for x in (place.adm2, place.adm1, place.country) if x and x != place.name)
    return f"{place.name}（{around}）" if around else place.name


@tool
@tool_error("查天气失败")
async def check_weather(
    place: Annotated[str, Field(description="要查的那个真实地方的名字")],
) -> str:
    """查一个真实地方此刻的天气、接下来逐小时的预报、接下来几天的预报（含日出日落）。

    交回来的是和风天气认下的那个地方和它的原始读数。名字有歧义时会列出同名的其他地方。
    """
    places = await weather.find_places(place)
    if not places:
        return f"和风天气查不到叫「{place}」的地方。"
    reading = await weather.weather_at(places[0])
    lines = [f"和风天气认下的地方：{_place_label(reading.place)}"]
    if len(places) > 1:
        lines.append(
            "同名的还有：" + "、".join(_place_label(p) for p in places[1:])
            + "。要查的不是上面那个的话，换一个更完整的名字再查。"
        )
    lines.append("此刻：" + _readings(reading.now, _NOW_LABELS))
    lines.append("接下来逐小时：")
    lines += ["- " + _readings(h, _HOURLY_LABELS) for h in reading.hourly]
    lines.append("接下来几天：")
    lines += ["- " + _readings(d, _DAILY_LABELS) for d in reading.daily]
    return "\n".join(lines)


SOURCE = Source(name="reality", tools=(check_weather, search_web))
