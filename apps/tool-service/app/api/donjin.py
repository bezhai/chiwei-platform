"""同人展搜索 API（供 sandbox skill 脚本调用）

包装 AllCpp.cn 的活动搜索 API，提供结构化的搜索结果。
"""

import asyncio
import logging
import random
from datetime import datetime

import arrow
import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.middleware.auth import verify_bearer_token

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(verify_bearer_token)])

# ─── AllCpp API 配置 ──────────────────────────────────────

_ALLCPP_URL = "https://www.allcpp.cn/allcpp/event/eventMainListV2.do"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "sec-ch-ua": '"Not)A;Brand";v="8", "Chromium";v="138", "Google Chrome";v="138"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}
_TYPE_MAP = {
    "茶会": 1,
    "综合同人展": 2,
    "ONLY": 3,
    "线上活动": 6,
    "官方活动": 7,
    "综合展": 8,
    "同好包场": 10,
}
_MAX_RETRIES = 3


# ─── 请求/响应模型 ────────────────────────────────────────


class DonjinSearchRequest(BaseModel):
    query: str | None = None
    is_online: bool | None = None
    recent_days: int | None = None
    activity_status: str | None = None  # ongoing / ended
    activity_type: str | None = None
    ticket_status: int | None = None


class DonjinEvent(BaseModel):
    event_url: str
    name: str
    type: str
    tag: str
    enter_time: str
    end_time: str
    wanna_go_count: int
    prov_name: str
    city_name: str
    area_name: str
    enter_address: str
    ended: bool
    is_online: bool


# ─── 端点 ─────────────────────────────────────────────────


@router.post("/donjin-search")
async def search_donjin_events(req: DonjinSearchRequest):
    """搜索同人展活动"""
    recent_days = req.recent_days
    if req.activity_status == "ongoing":
        recent_days = -1
    elif req.activity_status == "ended":
        recent_days = -2

    payload = {
        "keyword": req.query,
        "is_online": req.is_online,
        "day": recent_days,
        "sort": 1,
        "page": 1,
        "page_size": 100,
        "ticketStatus": req.ticket_status,
        "type": _TYPE_MAP.get(req.activity_type) if req.activity_type else None,
    }

    # 带重试的 AllCpp API 调用
    data = {}
    for attempt in range(_MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=15, headers=_HEADERS) as client:
                if attempt > 0:
                    await asyncio.sleep(random.uniform(1, 3))
                response = await client.get(_ALLCPP_URL, params=payload)
                response.raise_for_status()
                data = response.json()
                break
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            if attempt == _MAX_RETRIES - 1:
                logger.error("AllCpp API failed after %d retries: %s", _MAX_RETRIES, e)
                return {"success": False, "error": f"AllCpp API 请求失败: {e}"}
            continue

    result = data.get("result", {})
    raw_events = result.get("list", [])

    events = []
    for item in raw_events:
        enter_time = _format_time(item.get("enterTime"))
        end_time = _format_time(item.get("endTime"))
        events.append(DonjinEvent(
            event_url=f"https://www.allcpp.cn/allcpp/event/event.do?event={item['id']}",
            name=item.get("name", ""),
            type=item.get("type", ""),
            tag=item.get("tag", ""),
            enter_time=enter_time,
            end_time=end_time,
            wanna_go_count=item.get("wannaGoCount", 0),
            prov_name=item.get("provName", "") or "",
            city_name=item.get("cityName", "") or "",
            area_name=item.get("areaName", "") or "",
            enter_address=item.get("enterAddress", ""),
            ended=item.get("ended", False) or False,
            is_online=item.get("isOnline", 0) == 1,
        ))

    return {
        "success": True,
        "data": {
            "total": result.get("total", len(events)),
            "events": [e.model_dump() for e in events],
        },
    }


def _format_time(ts: int | None) -> str:
    if not ts:
        return ""
    try:
        return arrow.get(datetime.fromtimestamp(ts / 1000)).format("YYYY-MM-DD")
    except Exception:
        return ""
