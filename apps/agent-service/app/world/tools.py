"""world 主 agent 手里的工具：看记录、写记录、查现实、定下次醒来的时刻。

六件，尽量少：

* :func:`list_records` / :func:`read_record` / :func:`write_record` —— 它自己的记录
  （:mod:`app.world.records`）。没有删除：一样东西没了，改写那份记录说它没了。人工接口
  可以删。
* :func:`app.agent.tools.search.search_web` —— 基础层现成的网页搜索，原样复用。
* :func:`check_weather` —— 天气（:mod:`app.capabilities.weather`）。查哪儿由它自己传，
  它从自己的记录里知道这个世界在哪；代码和配置里没有任何地名。
* :func:`wake_me_at` —— 定下次醒来的时刻。每一轮必须调，没调的一轮算失败
  （:mod:`app.world.round`）。

一轮里工具之间共享的东西放在 :class:`RoundScope` 里，由这一轮的 ``AgentContext`` 带着：
这一轮读过哪几份记录（读到时的指纹）、定下的下次醒来。

**改写一份已有的记录，必须在这一轮里读过它现在的样子。** 模型不用自己搬指纹：读的时候
记下指纹，写的时候拿它去做 :func:`app.world.records.write` 的指纹检查。它读过之后别人
（人工接口）又改过，这次写就不落盘，告诉它重新读。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated

from pydantic import Field

from app.agent.runtime_context import get_context
from app.agent.tooling import tool
from app.agent.tools._common import tool_error
from app.agent.tools.search import search_web
from app.capabilities import weather
from app.capabilities._errors import CapabilityInvalidArg, CapabilityNotFound
from app.infra.cst_time import CST, now_cst, to_cst_full
from app.world import records

ROUND_SCOPE = "world_round"


@dataclass(frozen=True)
class WakeChoice:
    at: datetime
    reason: str


@dataclass
class RoundScope:
    """一轮里工具之间共享的东西。每一轮新建一个，放进 ``AgentContext.features``。"""

    # 这一轮读过（或者自己刚写下）的记录 → 那时的指纹。
    read: dict[str, str] = field(default_factory=dict)
    # 这一轮写下的记录，按写的先后（只用来记日志）。
    written: list[str] = field(default_factory=list)
    # 这一轮定下的下次醒来；调过几次以最后一次为准。
    next_wake: WakeChoice | None = None


def _scope() -> RoundScope:
    return get_context().features[ROUND_SCOPE]


def _when(moment: datetime) -> str:
    return to_cst_full(moment.isoformat())


# ---------------------------------------------------------------------------
# 记录
# ---------------------------------------------------------------------------

RecordPath = Annotated[
    str, Field(description="记录的路径，相对记录根目录，形如 目录/名字.md")
]


@tool
@tool_error("列记录失败")
async def list_records() -> str:
    """列出你的全部记录：每一份的路径、字数、最后改动的时间。"""
    entries = records.listing()
    if not entries:
        return "你还没有任何记录。"
    lines = [f"你的记录（{len(entries)} 份）："]
    lines += [
        f"- {e.path}（{e.chars} 字，最后改动 {_when(e.updated_at)}）" for e in entries
    ]
    return "\n".join(lines)


@tool
@tool_error("读记录失败")
async def read_record(path: RecordPath) -> str:
    """读一份记录的全文。"""
    try:
        record = records.read(path)
    except records.RecordNotFound as exc:
        raise CapabilityNotFound(str(exc)) from exc
    except records.InvalidRecordPath as exc:
        raise CapabilityInvalidArg(str(exc)) from exc
    _scope().read[record.path] = record.fingerprint
    return (
        f"《{record.path}》（{len(record.text)} 字，最后改动 {_when(record.updated_at)}）"
        f"\n\n{record.text}"
    )


@tool
@tool_error("写记录失败")
async def write_record(
    path: RecordPath,
    text: Annotated[str, Field(description="这一份记录的全文，整份替换原来的")],
) -> str:
    """整份写下一份记录：没有就新建，有就整份替换。

    改写一份已经有的记录之前，要在这一轮里读过它现在的样子。
    """
    scope = _scope()
    try:
        written = records.write(path, text, expected=scope.read.get(path))
    except (records.InvalidRecordPath, records.InvalidRecordText) as exc:
        raise CapabilityInvalidArg(str(exc)) from exc
    except records.RecordConflict:
        if path in scope.read:
            return (
                f"没有写：「{path}」在你读过之后被改过了。重新读一遍（read_record）"
                f"再决定怎么写。"
            )
        return (
            f"没有写：「{path}」已经有了，你这一轮还没读过它现在的样子。先读一遍"
            f"（read_record）再写。"
        )
    scope.read[written.path] = written.fingerprint
    scope.written.append(written.path)
    return f"写好了：「{written.path}」（{len(written.text)} 字）。"


# ---------------------------------------------------------------------------
# 天气
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# 下次醒来
# ---------------------------------------------------------------------------


@tool
@tool_error("没有定下")
async def wake_me_at(
    at: Annotated[
        str,
        Field(description="下次醒来的时刻，ISO 8601，形如 YYYY-MM-DDTHH:MM；不带时区的按北京时间算"),
    ],
    reason: Annotated[
        str, Field(description="为什么定这个时刻。到时候这句话会原样摆在你眼前")
    ],
) -> str:
    """定下次醒来的时刻。每一轮结束前必须定一次；调了几次以最后一次为准。

    有人找你、或者有消息来，你会被提前叫醒，不用为等消息定时刻。
    """
    try:
        moment = datetime.fromisoformat(at.strip())
    except ValueError as exc:
        raise CapabilityInvalidArg(
            "时刻写成 ISO 8601，形如 YYYY-MM-DDTHH:MM；不带时区的按北京时间算"
        ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=CST)
    now = now_cst()
    if moment <= now:
        raise CapabilityInvalidArg(
            f"这个时刻已经过去了（现在是 {_when(now)}）：下次醒来要定在现在之后"
        )
    if not reason.strip():
        raise CapabilityInvalidArg("写一句为什么定这个时刻：到时候它会原样摆在你眼前")
    _scope().next_wake = WakeChoice(at=moment, reason=reason.strip())
    minutes = int((moment - now).total_seconds() // 60)
    return (
        f"定好了：{_when(moment)}（离现在约 {minutes} 分钟）。这一轮结束时生效；"
        f"再调一次就改成新的时刻。"
    )


WORLD_TOOLS = [
    list_records,
    read_record,
    write_record,
    search_web,
    check_weather,
    wake_me_at,
]

# 这些工具的返回是读到的材料：过了保留期就换成一句"不在眼前了"（:mod:`app.agent.continuity`），
# 要用再去读一次。写记录和定时刻的返回是它自己做过的事，跟着它自己的话一起留。
MATERIAL_TOOLS = frozenset({"list_records", "read_record", "search_web", "check_weather"})
