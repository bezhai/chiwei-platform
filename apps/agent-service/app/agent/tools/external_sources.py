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

import html
import logging
from typing import Any

import httpx

from app.agent.tooling import tool
from app.agent.tools._common import tool_error
from app.infra.config import settings
from app.infra.cst_time import now_cst

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 广州坐标（经度在前，和风口径 location=lon,lat）。
_GUANGZHOU_LON = "113.27"
_GUANGZHOU_LAT = "23.13"
_GUANGZHOU_NAME = "广州"

# 免费版必须打专属域名，打 api.qweather.com 会 403。
_QWEATHER_BASE = "https://devapi.qweather.com"
_BANGUMI_CALENDAR_URL = "https://api.bgm.tv/calendar"
_TIMOR_HOLIDAY_BASE = "https://timor.tech/api/holiday/info"

_HTTP_TIMEOUT = 10.0

# timor type.type → 节假日类型人话标签（结构化字段 ``kind``，不是拼好的整句）。
_HOLIDAY_TYPE_LABEL = {
    0: "工作日",
    1: "周末休息",
    2: "法定节假日",
    3: "周末调休补班",
}


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

    url = f"{_QWEATHER_BASE}/v7/weather/now"
    params = {"location": f"{_GUANGZHOU_LON},{_GUANGZHOU_LAT}"}
    # key 只走 header，绝不进 url query —— reason 里贴 url 也不会泄露。
    headers = {"X-QW-Api-Key": api_key}

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("query_weather connect error: %s", type(exc).__name__)
        return _failed(f"无法连接和风天气({type(exc).__name__})")

    if resp.status_code != 200:
        logger.warning("query_weather http %d", resp.status_code)
        return _failed(f"和风天气返回 HTTP {resp.status_code}")

    try:
        data = resp.json()
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

    result: dict[str, Any] = {
        "ok": True,
        "city": _GUANGZHOU_NAME,
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
    proxy = settings.forward_proxy_url
    client_kwargs: dict[str, object] = {"timeout": _HTTP_TIMEOUT}
    if proxy:
        client_kwargs["proxy"] = proxy

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:  # type: ignore[arg-type]
            resp = await client.get(_BANGUMI_CALENDAR_URL)
    except httpx.HTTPError as exc:
        logger.warning("query_anime_calendar connect error: %s", type(exc).__name__)
        return _failed(f"无法连接 Bangumi({type(exc).__name__})")

    if resp.status_code != 200:
        logger.warning("query_anime_calendar http %d", resp.status_code)
        return _failed(f"Bangumi 返回 HTTP {resp.status_code}")

    try:
        week = resp.json()
    except ValueError:
        logger.warning("query_anime_calendar body not json")
        return _failed("响应解析失败")

    if not isinstance(week, list):
        return _failed("响应结构异常")

    # Bangumi weekday.id：周一=1 ... 周日=7，与 datetime.isoweekday() 同口径。
    today_id = now_cst().isoweekday()
    today_block = next(
        (
            d
            for d in week
            if isinstance(d, dict)
            and (d.get("weekday") or {}).get("id") == today_id
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
        # name_cn 是 HTML 转义的，要 unescape；没有中文名退回原名。
        raw = item.get("name_cn") or item.get("name") or ""
        name = html.unescape(raw).strip()
        if name:
            names.append(name)

    # 今天没有在更新的番剧仍是一次**成功**查询（空列表，不是失败）——agent 据此如实说
    # 今天没番，而不是把它当查询失败。
    return {"ok": True, "weekday": weekday_cn, "anime": names}


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
    url = f"{_TIMOR_HOLIDAY_BASE}/{today}"

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        logger.warning("query_holiday connect error: %s", type(exc).__name__)
        return _failed(f"无法连接 timor({type(exc).__name__})")

    if resp.status_code != 200:
        logger.warning("query_holiday http %d", resp.status_code)
        return _failed(f"timor 返回 HTTP {resp.status_code}")

    try:
        data = resp.json()
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
        # 仅法定节假日 / 调休补班时 timor 才给 holiday.name；其余为 None。
        "holiday_name": holiday_name,
    }
