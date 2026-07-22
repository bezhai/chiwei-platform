"""外部查询工具：天气 / 番剧 / 节假日。

赤尾世界的"外部干预"信息源。每个工具内部是**写定的代码**——调对应官方 API、按已知
响应结构解析、返回一份**结构化数据**（@tool 返回 dict，框架会 JSON 序列化喂给
agent）。不让 LLM 去解析原始 JSON，也不在这层把数据拼成人话——拼人话、组织底料是
抓取 agent 的事，工具只负责返回**准的结构化事实**。

成功返回带字段的结构（天气：城市/温度/体感/天气/湿度/风；番剧：今天周几/番剧名列表；
节假日：日期/周几/类型/节日名），并带 ``"ok": True``。

失败降级契约（三个工具一致）：网络失败 / 坏 key / 解析失败 / API 返回错误码时，返回
``{"ok": False, "reason": "..."}``（绝不返回空、绝不返回半截或脏数据、绝不冒充成功）。
``reason`` 里**绝不含 key 明文、不含带 key 的完整 url 或 header**——异常对象只暴露类型
名、不拼进可能含敏感信息的 url（key 永远走 header / 不入 url，见 ``query_weather``）。

agent 别瞎编：某个工具返回 ``ok=False`` 就如实说那项今天没拿到，绝不编一个顶上——这
靠抓取 agent 的 prompt 管，工具这层只保证数据准。

时间一律用 :func:`now_cst`（CST 北京时间），不用 UTC——"今天"对赤尾是北京的今天。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import cnlunar

from app.agent.tooling import tool
from app.agent.tools._common import tool_error
from app.capabilities.external_facts import (
    fetch_anime_calendar,
    fetch_holiday,
    fetch_sun_times,
    fetch_weather,
)
from app.infra.config import settings
from app.infra.cst_time import now_cst

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 广州坐标（经度在前，和风口径 location=lon,lat）。
_GUANGZHOU_LON = "113.27"
_GUANGZHOU_LAT = "23.13"
_GUANGZHOU_NAME = "广州"

# 和风天气的 API Host 不写死：2024 改版后每个账号有专属 host（统一 devapi/api 域名
# 对新 key 返 Invalid Host 403），host 从 settings.qweather_api_host 走 env 注入。


def _failed(reason: str) -> dict[str, Any]:
    """统一的失败结构。``reason`` 已由调用处保证不含 key 明文 / 带 key 的 url。"""
    return {"ok": False, "reason": reason}


# ===========================================================================
# 天气 —— 和风 QWeather
# ===========================================================================


@tool
@tool_error("天气查询失败")
async def query_weather() -> dict[str, Any]:
    """查询广州当前实时天气，返回结构化天气数据。

    Returns:
        成功时返回 ``{"ok": True, "city": "广州", "temp": "24", "feels_like":
        "26", "weather": "小雨", "humidity": "80", "wind": "南风2级"}``（字段值
        是从和风响应里取出的原始字符串，不拼成人话）；查询失败时返回
        ``{"ok": False, "reason": "..."}``（reason 不含任何密钥）。
    """
    api_key = settings.qweather_api_key
    if not api_key:
        # 不把 key（None）拼进文本，只给人话原因。
        return _failed("未配置和风天气 API Key")
    api_host = settings.qweather_api_host
    if not api_host:
        # host 没配就直接降级，不去打统一域名（必被 Invalid Host 403 拒）。
        return _failed("未配置和风天气 API Host")

    return await fetch_weather(
        api_host=api_host,
        api_key=api_key,
        location=f"{_GUANGZHOU_LON},{_GUANGZHOU_LAT}",
        city=_GUANGZHOU_NAME,
    )


# ===========================================================================
# 番剧 —— Bangumi 放送日历（必走 forward_proxy）
# ===========================================================================


@tool
@tool_error("番剧查询失败")
async def query_anime_calendar() -> dict[str, Any]:
    """查询今天正在更新的番剧，返回结构化番剧列表。

    Returns:
        成功时返回 ``{"ok": True, "weekday": "星期日", "anime": ["Re:Zero 第三
        季", ...]}``（``anime`` 是今天在更新的番剧名列表，今天没番时是空列表
        ``[]`` —— 没番仍算查询成功）；查询失败时返回 ``{"ok": False, "reason":
        "..."}``。
    """
    # Bangumi weekday.id：周一=1 ... 周日=7，与 datetime.isoweekday() 同口径。
    return await fetch_anime_calendar(
        today_isoweekday=now_cst().isoweekday(),
        proxy_url=settings.forward_proxy_url,
    )


# ===========================================================================
# 节假日 —— timor
# ===========================================================================


@tool
@tool_error("节假日查询失败")
async def query_holiday() -> dict[str, Any]:
    """查询今天的节假日状态，返回结构化节假日数据。

    Returns:
        成功时返回 ``{"ok": True, "date": "2026-06-08", "weekday": "周日",
        "kind": "工作日"|"周末休息"|"法定节假日"|"周末调休补班", "holiday_name":
        "端午节"|None}``（``kind`` 是节假日类型标签，``holiday_name`` 仅法定节假
        日 / 调休补班时有值，否则为 ``None``）；查询失败时返回 ``{"ok": False,
        "reason": "..."}``。
    """
    today = now_cst().strftime("%Y-%m-%d")
    return await fetch_holiday(today=today)


# ===========================================================================
# 日出日落 —— 和风 QWeather astronomy
# ===========================================================================


@tool
@tool_error("日出日落查询失败")
async def query_sun_times() -> dict[str, Any]:
    """查询广州今天的日出 / 日落时刻，返回结构化数据。

    Returns:
        成功时返回 ``{"ok": True, "city": "广州", "sunrise": "05:41", "sunset":
        "19:12"}``（``sunrise`` / ``sunset`` 是广州当地 ``HH:MM``，从和风
        astronomy 返回的带 ``+08:00`` 偏移时刻里取出）；查询失败时返回
        ``{"ok": False, "reason": "..."}``（reason 不含任何密钥）。
    """
    api_key = settings.qweather_api_key
    if not api_key:
        return _failed("未配置和风天气 API Key")
    api_host = settings.qweather_api_host
    if not api_host:
        return _failed("未配置和风天气 API Host")

    # date 是 CST 当天 YYYYMMDD（"今天"对赤尾是北京的今天）。
    today = now_cst().strftime("%Y%m%d")
    return await fetch_sun_times(
        api_host=api_host,
        api_key=api_key,
        location=f"{_GUANGZHOU_LON},{_GUANGZHOU_LAT}",
        city=_GUANGZHOU_NAME,
        date=today,
    )


# ===========================================================================
# 节气农历 —— cnlunar 本地天文计算（无网络）
# ===========================================================================


@tool
@tool_error("节气农历查询失败")
async def query_lunar_term() -> dict[str, Any]:
    """计算今天的农历日期 + 节气，返回结构化时令数据（本地算，不走网络）。

    用 :mod:`cnlunar` 对今天（CST）做确定性农历 / 节气天文计算——给世界引擎一份
    "现在是农历几月几、生肖年、今天是否某节气、临近哪个节气"的时令底料。

    Returns:
        成功时返回 ``{"ok": True, "lunar_date": "四月廿三", "zodiac_year":
        "丙午马年", "solar_term": "夏至"|None, "next_solar_term": "夏至",
        "days_to_next_term": 13}``——``solar_term`` 仅当**今天正好是**某个节气时
        有值（否则 ``None``），``next_solar_term`` / ``days_to_next_term`` 是临近
        的下一个节气名和还差几天；本地计算失败时返回
        ``{"ok": False, "reason": "..."}``。
    """
    today = now_cst()
    # now_cst() 是 CST aware datetime；cnlunar 内部对 date 做 naive 减法、喂 aware
    # 会 TypeError。剥掉 tzinfo 拿"广州当地的那一天"——civil 日期正是农历 / 节气
    # 计算要的口径。
    civil = today.replace(tzinfo=None)
    lunar = cnlunar.Lunar(civil, godType="8char")

    # 农历月日去掉"大/小"月标记，留干净的"四月廿三"。
    lunar_month = (lunar.lunarMonthCn or "").rstrip("大小")
    lunar_date = f"{lunar_month}{lunar.lunarDayCn or ''}"

    # 生肖年：干支 + 生肖（丙午马年）。
    zodiac_year = f"{lunar.year8Char}{lunar.chineseYearZodiac}年"

    # todaySolarTerms 为 "无" 表示今天不是节气日；否则就是今天的节气名。
    today_term = lunar.todaySolarTerms
    solar_term = today_term if today_term and today_term != "无" else None

    # 临近的下一个节气 + 还差几天（nextSolarTermDate 是 (月, 日)）。
    next_term = lunar.nextSolarTerm
    next_date = date(lunar.nextSolarTermYear, *lunar.nextSolarTermDate)
    days_to_next = (next_date - civil.date()).days

    return {
        "ok": True,
        "lunar_date": lunar_date,
        "zodiac_year": zodiac_year,
        "solar_term": solar_term,
        "next_solar_term": next_term,
        "days_to_next_term": days_to_next,
    }
