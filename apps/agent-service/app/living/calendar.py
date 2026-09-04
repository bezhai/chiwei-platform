"""日历 —— 这个家的客观时刻表，以及到期的东西怎么变成发生过的事。

**这一整个模块不花一分钱模型费。** 天亮、三餐、天黑、洗澡、睡前是**数据**，不是
推理：写下来、到点了拿出来，全程只有时刻表和比大小。世界里一直有会到期的东西，
靠的就是这条免费的腿；world 的模型轮次（:mod:`app.living.world`）只在它之上稀疏
地补新东西。

两件事，两处幂等，都必须真的成立：

**一、排今天的日历**（:func:`plan_day`）。每一拍都重排一次——这就是重启后的自愈
方式，不需要一个"今天排过没有"的标记。幂等落在 ``item_id`` 上：
``day:<日期>:<槽标识>``，同一天同一个槽永远是同一个 id，
:func:`~app.living.upcoming.schedule_upcoming` 的 CAS 让第二次是 no-op（**已经被
消费掉的项也不会被复活**，理由在它自己的 docstring 里）。只排**还没到点**的槽：
中午才起来的进程不该在 12:05 给她端上"早饭做好了"。

**二、到期交付**（:func:`deliver_due`）。这是账上所有东西——日历项和 world 排的
新东西——变成她感知得到的 :class:`~app.living.records.Happening` 的**唯一出口**。
``Upcoming`` 的消费是**至少一次**：拿到手了但还没来得及标 consumed 就崩，下一拍
会把同一条再交出来一次。所以 ``happening_id`` 必须从 ``item_id`` 派生
（:func:`due_happening_id`）——重放落回同一行，她不会把一件事感知两遍。顺序也是
这个语义的一半：**先写 Happening，再销账**。反过来会在崩溃窗口里把一件事彻底吞掉。

时刻表放 Dynamic Config（:data:`LIVING_DAY_SCHEDULE_KEY`）而不是写死在代码里：
"这个家几点吃晚饭"是会被调的剧情参数，不是常量。代码里那份
:data:`DEFAULT_DAY_SCHEDULE` 是**兜底**，配置缺失 / 配脏时退回它并 warning——这跟
``world_daylight_coords`` 配脏就不拼日照那条不一样：那边编一个日落时刻是撒谎，这边
退回默认作息只是回到默认值，而"今天一件会到期的事都没有"会把整个实验静默弄死。

**不算日出日落。** 第一版用这个家的作息就够（起床、早饭、午饭、天黑、晚饭、洗澡、
睡前），要季节变化就去改配置里那几个粗略钟点，不引天文库、更不手搓天文算法——
误差半小时的天黑对"她感知到天暗了"没有任何差别。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time

from inner_shared.dynamic_config import dynamic_config

from app.infra.cst_time import CST
from app.living.happening import record_happening
from app.living.records import KIND_ACT, MEDIUM_IN_PERSON, Happening
from app.living.upcoming import (
    list_due_upcoming,
    mark_upcoming_consumed,  # module-level so tests can monkeypatch
    schedule_upcoming,
)

logger = logging.getLogger(__name__)

# 日历项和 world 排的新东西都不是谁做的，是世界自己发生的。用一个绝不会跟
# persona_id 撞的 actor，让回声抑制（``_perceive`` 里 ``actor == persona_id``
# 那一条）永远不会把世界的事从谁眼前抹掉。
WORLD_ACTOR = "world"

# 没绑地点的事（天黑、停电）发生在**这个家这一整片**上。屋里每个人都在这片里面，
# 所以按 :func:`app.living.place.reach_between` 的包含档都拿得到原话，在学校的
# 拿不到。写成路径的第一段，跟 whereabouts 用的是同一套路径词汇。
AMBIENT_PLACE = "家"

# Dynamic Config key：值 = JSON 数组，每项 {"key","at","what","place"?}。
# ``at`` 是 CST 的 ``HH:MM``；``place`` 省略 = 不绑地点（走 AMBIENT_PLACE）。
LIVING_DAY_SCHEDULE_KEY = "living_day_schedule"


@dataclass(frozen=True)
class DaySlot:
    """时刻表上的一格：几点、发生什么、在哪。

    ``key`` 是**槽标识**，不是文案：它进 ``item_id``，所以改 ``what`` 的措辞不会
    让今天那条项重新发一遍，而改 ``key`` 会。加一个新槽用一个新 key 就行。

    ``at`` 是真正的 :class:`datetime.time`，不是 ``"07:30"`` 这种串——脏值在解析
    配置那一刻就被挡住，而不是等到写库时把整批日历一起炸掉。
    """

    key: str
    at: time
    what: str
    place: str | None = None


# 兜底的一天：这个家的暑假作息。粗略、固定、够用——它要回答的只有"世界里一直有
# 会到期的东西吗"，不是"今天广州几点日落"。要调就去改 Dynamic Config，别改这里。
DEFAULT_DAY_SCHEDULE: tuple[DaySlot, ...] = (
    DaySlot(key="daybreak", at=time(6, 20), what="天亮了", place=None),
    DaySlot(key="wake", at=time(8, 0), what="家里开始有人起床走动", place="家"),
    DaySlot(key="breakfast", at=time(8, 40), what="早饭做好了", place="家/餐厅"),
    DaySlot(key="lunch", at=time(12, 30), what="午饭做好了", place="家/餐厅"),
    DaySlot(key="nightfall", at=time(19, 10), what="天黑了", place=None),
    DaySlot(key="dinner", at=time(19, 30), what="晚饭做好了", place="家/餐厅"),
    DaySlot(key="bath", at=time(21, 30), what="浴室的热水烧好了", place="家/浴室"),
    DaySlot(key="bedtime", at=time(23, 30), what="夜深了，家里安静下来", place="家"),
)


def parse_schedule(raw: str) -> tuple[DaySlot, ...]:
    """把配置串解析成时刻表；空 / 坏一律退回 :data:`DEFAULT_DAY_SCHEDULE`。

    整份要么全对要么全退——半份时刻表比默认那份更难查：她会莫名其妙不吃晚饭，而
    日志里只有一行"某一项跳过了"。退回时打 warning：配置写错了不能静默降级成
    "今天按默认作息过"，那样改配置的人永远不知道自己改的东西没生效。
    """
    if not raw or not raw.strip():
        return DEFAULT_DAY_SCHEDULE
    try:
        payload = json.loads(raw)
        if not isinstance(payload, list):
            raise TypeError(f"期望一个数组，拿到 {type(payload).__name__}")
        parsed = tuple(_parse_slot(item) for item in payload)
    except Exception as exc:
        logger.warning(
            "dynamic config %s 解析不出时刻表（%s）；本次退回内置作息 —— "
            "配置没有生效，去看一眼它的值",
            LIVING_DAY_SCHEDULE_KEY,
            exc,
        )
        return DEFAULT_DAY_SCHEDULE
    return parsed or DEFAULT_DAY_SCHEDULE


def _parse_slot(item: object) -> DaySlot:
    if not isinstance(item, dict):
        raise TypeError(f"每一项要是对象，拿到 {item!r}")
    key, what = item.get("key"), item.get("what")
    if not key or not what:
        raise ValueError(f"{item!r} 缺 key 或 what")
    at = time.fromisoformat(str(item.get("at", "")))
    place = item.get("place") or None
    return DaySlot(key=str(key), at=at, what=str(what), place=place)


async def load_day_schedule() -> tuple[DaySlot, ...]:
    """读这个家的时刻表。

    Dynamic Config 的拉取是同步 httpx（10s 缓存），走 ``asyncio.to_thread`` 避免
    缓存刷新那一次阻塞事件循环（与包里别处读 Dynamic Config 同口径）。
    """
    raw = await asyncio.to_thread(
        dynamic_config.get, LIVING_DAY_SCHEDULE_KEY, default=""
    )
    return parse_schedule(raw)


def day_item_id(day: date, key: str) -> str:
    """这一天这个槽的日历项 id —— 幂等的全部依据，同一天同一槽永远是它。"""
    return f"day:{day.isoformat()}:{key}"


async def plan_day(
    *, lane: str, now: datetime, schedule: Sequence[DaySlot]
) -> list[str]:
    """把 ``now`` 那天还没到点的槽写上账；返回**本次真的新写**的 item_id。

    每一拍都可以照跑：已经写过的槽是 no-op，已经发生过的槽不会被复活。跨天也不
    用管——第一拍进入新的一天时自然会写新一天的项。

    只排还没到点的：中午才起来的进程不该立刻给她端上早饭。所以一天里比较晚才
    起的进程，那天早上的项就是没有——这比补发一堆已经过去的时刻正确。
    """
    today = now.astimezone(CST).date()
    written: list[str] = []
    for slot in schedule:
        due_at = datetime.combine(today, slot.at, tzinfo=CST)
        if due_at <= now:
            continue
        item_id = day_item_id(today, slot.key)
        if await schedule_upcoming(
            lane=lane,
            item_id=item_id,
            what=slot.what,
            due_at=due_at,
            place=slot.place,
        ):
            written.append(item_id)
    return written


def due_happening_id(item_id: str) -> str:
    """一条日历项到期之后那件事的 id —— **从 item_id 派生，这是幂等的命门**。

    ``Upcoming`` 的消费是至少一次：交付了但还没销账就崩，下一拍会把同一条再交
    一次。id 从 ``item_id`` 派生，重放就落回同一行；随手一个 uuid 的话她会把同
    一件事感知两遍，而且两遍都长得一模一样、事后根本查不出来是重复。
    """
    return f"due:{item_id}"


async def deliver_due(*, lane: str, now: datetime) -> list[Happening]:
    """把账上所有到期还没销的东西变成已经发生的事；返回这一拍发生的事。

    **先写 Happening，再销账。** 反过来（先销账再写）会在崩溃窗口里把一件事彻底
    吞掉——账销了、事没发生、下一拍也拿不到它了。现在这个顺序最坏是"同一条交付
    两次"，而那个由 :func:`due_happening_id` 挡住。

    出错就往上抛，不吞：这一拍失败，下一拍会把没销账的重新拿到。

    ``occurred_at`` 用的是 ``due_at`` 而不是 ``now``——事情发生在它该发生的那一刻，
    交付晚了几十秒是钟的粒度问题，不该写进世界的记录里。
    """
    happened: list[Happening] = []
    for item in await list_due_upcoming(lane=lane, until=now):
        happened.append(
            await record_happening(
                lane=lane,
                happening_id=due_happening_id(item.item_id),
                actor=WORLD_ACTOR,
                place=item.place or AMBIENT_PLACE,
                kind=KIND_ACT,
                medium=MEDIUM_IN_PERSON,
                content=item.what,
                occurred_at=item.due_at,
            )
        )
        await mark_upcoming_consumed(
            lane=lane, item_id=item.item_id, consumed_at=now
        )
    return happened
