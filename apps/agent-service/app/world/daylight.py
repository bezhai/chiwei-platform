"""今天几点天亮、几点天黑——喂给 world 的日照锚点（idle-deadlock spec Task 2）。

**为什么要有这个模块**：prod 实证 2026-07-24，广州当天真实日落约 19:13，但 world
17:01 就写「傍晚前段」、17:56 写「傍晚后段的自然光继续退下去」、18:30 写「更深的
蓝灰」——把天黑提前了 1.5–2 小时。三姐妹从 17:00 起，感知到的就是一间「夜晚的
家」，这是她们早睡的一段独立成因。

根因不在模型，在它的输入：world 每轮只拿到【现实此刻】这一个裸时刻，而
:func:`app.world.engine.world_loop_instruction` 里反复拿「天色暗下来」当时间推进的
范例——没有当天的日照事实，它只能用「傍晚≈天黑」这种通用先验去渲染黄昏。

**边界（赤尾宪法）**：这里算的是**确定性天文事实**（今天几点日出、几点日落），
算出来喂给她属于「给 agent 客观事实」，不是替她做世界内容决策。所以这个模块
**只给两个时刻**，绝不给「几点之后算晚上」「天黑了就该睡了」这类判断规则——那会
变成用代码替 agent 决定世界该长什么样，是宪法禁止的。

**为什么不解析底料**：``DailyMaterials`` 只有自由文本 ``briefing`` 一个字段，日出
日落只是那段中文话里的一句，没有结构化契约——正则解析脆弱、失败语义不明。独立
计算零 schema 变更、可单测、没有解析失败路径。日出日落是标准天文算法，用成熟库
（``astral``，纯 Python、零重依赖）算，不手搓。

**坐标从哪来**：Dynamic Config key :data:`WORLD_DAYLIGHT_COORDS_KEY`
（``world_daylight_coords``），值 = ``"纬度,经度"`` 十进制度、北纬 / 东经为正，
例如广州 ``"23.1291,113.2644"``。走运行时配置而不是硬编码，是因为「这家人住在
哪」是剧情事实、属于业务参数（项目规范：业务行为参数走 Dynamic Config）。

**降级语义（硬约束）**：配置缺失 / 配脏 / 该坐标当天真的没有日出或没有日落（极昼
极夜）→ :func:`today_daylight_text` 返回**空串**，调用方如实不拼这一段。**绝不编
一个坐标、绝不编一个日落时刻**——喂给 world 的每一个客观事实都必须是真的，宁可
没有。反过来也要守住：只要当天真有日出日落就必须照实给，缺曙暮光那类高纬现象不算
降级理由（见 :func:`compute_daylight`）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime

from astral import Observer
from astral.sun import sunrise as astral_sunrise
from astral.sun import sunset as astral_sunset
from inner_shared.dynamic_config import dynamic_config

from app.infra.cst_time import CST

logger = logging.getLogger(__name__)

# Dynamic Config key：值 = "纬度,经度"（十进制度，北纬 / 东经为正）。
# 例：广州越秀 "23.1291,113.2644"。没配 / 配脏 → 不拼日照段（见模块 docstring）。
WORLD_DAYLIGHT_COORDS_KEY = "world_daylight_coords"


@dataclass(frozen=True)
class Daylight:
    """某一天在某个坐标上的日出 / 日落时刻（都是 CST aware ``datetime``）。"""

    sunrise: datetime
    sunset: datetime


def parse_coords(raw: str | None) -> tuple[float, float] | None:
    """把配置串 ``"纬度,经度"`` 解析成 ``(latitude, longitude)``。

    解析不出（空 / 少一半 / 多一截 / 不是数字 / 超出地理范围）一律返回 ``None``
    ——调用方据此如实不拼，不猜一个坐标出来。纬度限 ``[-90, 90]``、经度限
    ``[-180, 180]``：越界值 astral 也算得出个数，但那是个没有意义的数，宁可当没配。
    """
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 2:
        return None
    try:
        latitude, longitude = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if not (-90.0 <= latitude <= 90.0) or not (-180.0 <= longitude <= 180.0):
        return None
    return latitude, longitude


def compute_daylight(
    day: date, *, latitude: float, longitude: float
) -> Daylight | None:
    """纯函数：算 ``day`` 这天在给定坐标的日出 / 日落（CST aware）。

    不读配置、不读当前时间——同样输入永远同样输出，可单测、可对表。

    高纬度地区某些日子太阳整天不落或整天不升（极昼 / 极夜），``astral`` 对此抛
    ``ValueError``；这里如实返回 ``None``（没有这回事就是没有，不编一个时刻）。

    **只单算 sunrise / sunset，不用聚合的** :func:`astral.sun.sun`：后者一次算
    dawn / sunrise / noon / sunset / dusk 五个时刻，其中**任何一个**算不出来就整体
    抛 ``ValueError``。北极圈以南的高纬夏天（如 65°N 夏至）太阳确实会落也确实会
    升，只是整夜停在民用曙暮光里、``dawn`` / ``dusk`` 无解——走聚合函数就会把这天
    误判成极昼，连真实存在的日出日落一起丢掉，降级语义名不副实。这里只要两个时刻
    就只算两个时刻，``None`` 因此只在真的没有日出或没有日落时出现。
    """
    observer = Observer(latitude=latitude, longitude=longitude)
    try:
        return Daylight(
            sunrise=astral_sunrise(observer, date=day, tzinfo=CST),
            sunset=astral_sunset(observer, date=day, tzinfo=CST),
        )
    except ValueError:
        # 真极昼 / 真极夜：当天太阳整天在地平线以上或以下，没有日出或没有日落。
        return None


async def today_daylight_text(day: date) -> str:
    """拼给 world 的日照锚点；坐标没配 / 配脏 / 算不出 → 空串（如实不拼）。

    Dynamic Config 的拉取是同步 httpx（10s 缓存），走 ``asyncio.to_thread`` 避免
    缓存刷新那一次阻塞事件循环（与 :mod:`app.life.feed_whitelist` 同口径）。

    只给两个客观时刻，不附带任何「所以现在算白天还是晚上」的判断——那是 world
    自己看着现实此刻去推的事。
    """
    raw = await asyncio.to_thread(
        dynamic_config.get, WORLD_DAYLIGHT_COORDS_KEY, default="",
    )
    coords = parse_coords(raw)
    if coords is None:
        if raw and raw.strip():
            # 配了但解析不出：这是配置写错了，不能静默降级成「今天没有日照」。
            logger.warning(
                "dynamic config %s 解析不出坐标（拿到 %r，期望 \"纬度,经度\"）；"
                "本轮不给 world 拼日照锚点",
                WORLD_DAYLIGHT_COORDS_KEY,
                raw,
            )
        return ""
    latitude, longitude = coords
    daylight = compute_daylight(day, latitude=latitude, longitude=longitude)
    if daylight is None:
        logger.warning(
            "坐标 (%s, %s) 在 %s 当天算不出日出日落（极昼 / 极夜）；本轮不拼日照锚点",
            latitude,
            longitude,
            day,
        )
        return ""
    return (
        f"（今天日出 {daylight.sunrise.strftime('%H:%M')}、"
        f"日落 {daylight.sunset.strftime('%H:%M')}）"
    )
