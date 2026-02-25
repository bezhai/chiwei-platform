import asyncio
import random
from datetime import datetime

import arrow
import httpx
from langchain.tools import tool
from pydantic import BaseModel, field_validator

from app.utils.decorators.log_decorator import log_io
from app.utils.decorators.serializer import dict_serialize


class CppSearchResult(BaseModel):
    event_url: str  # 活动链接
    name: str  # 活动名称
    type: str  # 活动类型
    tag: str  # 活动标签
    enter_time: str  # 活动开始时间
    end_time: str  # 活动结束时间
    wanna_go_count: int  # 想参加人数
    prov_name: str  # 省份
    city_name: str  # 城市
    area_name: str  # 地区
    enter_address: str  # 活动地址
    ended: bool  # 是否已结束
    is_online: bool  # 是否为线上


class CppSearchResultList(BaseModel):
    list: list[CppSearchResult]  # 活动列表


# 中间结果
class CppSearchMidSingleResult(BaseModel):
    id: int  # 活动id
    name: str  # 活动名称
    type: str  # 活动类型
    tag: str  # 活动标签
    enterTime: int | None = None  # 活动开始时间（毫秒）
    endTime: int | None = None  # 活动结束时间（毫秒）
    wannaGoCount: int  # 想参加人数
    provName: str | None = ""  # 省份
    cityName: str | None = ""  # 城市
    areaName: str | None = ""  # 地区
    enterAddress: str  # 活动地址
    ended: bool | None = False  # 是否已结束
    isOnline: int  # 是否为线上

    @field_validator("enterTime", "endTime")
    @classmethod
    def convert_timestamp_to_string(cls, v):
        """将时间戳转换为字符串格式"""
        if v is None:
            return ""
        if isinstance(v, int):
            # 将毫秒时间戳转换为秒时间戳，然后格式化
            timestamp = v / 1000
            return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
        return str(v)


class CppSearchMidResult(BaseModel):
    total: int  # 总活动数
    list: list[CppSearchMidSingleResult]  # 活动列表


@tool
@log_io
@dict_serialize
async def search_donjin_event(
    query: str | None = None,
    is_online: bool | None = None,
    recent_days: int | None = None,
    activity_status: str | None = None,
    activity_type: str | None = None,
    ticket_status: int | None = None,
) -> CppSearchResultList:
    """
    搜索同人展活动, 返回结构化的活动列表

    Args:
        query: 搜索关键词, 默认为空
        is_online: 是否为线上活动, 默认不限制
        recent_days: 最近几天内的活动, 默认为全部
        activity_status: 活动状态, 默认不限制, 可选值: ongoing(未结束), ended(已结束), 优先级比recent_days高
        activity_type: 活动类型, 默认不限制, 可选值: "茶会", "综合同人展", "ONLY", "线上活动", "官方活动", "综合展", "同好包场"
        ticket_status: 售票状态, 默认不限制, 可选值: {"暂未开票":1, "即将开票":2, "正在售票":3, "售票结束":4, "站外售票":5}
    """
    url = "https://www.allcpp.cn/allcpp/event/eventMainListV2.do"

    if activity_status == "ongoing":
        recent_days = -1
    elif activity_status == "ended":
        recent_days = -2

    payload = {
        "keyword": query,
        "is_online": is_online,
        "day": recent_days,
        "sort": 1,
        "page": 1,
        "page_size": 100,
        "ticketStatus": ticket_status,
        "type": {
            "茶会": 1,
            "综合同人展": 2,
            "ONLY": 3,
            "线上活动": 6,
            "官方活动": 7,
            "综合展": 8,
            "同好包场": 10,
        }.get(activity_type, None)
        if activity_type
        else None,
    }

    # 构建浏览器请求头来绕过反爬检测
    headers = {
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

    # 添加重试机制
    data = {}
    max_retries = 3
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=15, headers=headers) as client:
                # 添加随机延迟避免频繁请求
                if attempt > 0:
                    await asyncio.sleep(random.uniform(1, 3))

                response = await client.get(url, params=payload)
                response.raise_for_status()
                data = response.json()
                break  # 成功则跳出重试循环

        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            if attempt == max_retries - 1:  # 最后一次尝试失败
                raise e
            continue

    mid_result = CppSearchMidResult(**data["result"])

    def transform_time(input):
        """
        将时间转换为'YYYY-MM-DD'格式（本地时间）。
        :param input: 时间
        :return: str，格式为'YYYY-MM-DD'的日期字符串
        """
        return arrow.get(input).format("YYYY-MM-DD")

    return CppSearchResultList(
        list=[
            CppSearchResult(
                event_url=f"https://www.allcpp.cn/allcpp/event/event.do?event={item.id}",
                name=item.name,
                type=item.type,
                tag=item.tag,
                enter_time=transform_time(item.enterTime) if item.enterTime else "",
                end_time=transform_time(item.endTime) if item.endTime else "",
                wanna_go_count=item.wannaGoCount,
                prov_name=item.provName or "",
                city_name=item.cityName or "",
                area_name=item.areaName or "",
                enter_address=item.enterAddress,
                ended=item.ended or False,
                is_online=item.isOnline == 1,
            )
            for item in mid_result.list
        ]
    )
