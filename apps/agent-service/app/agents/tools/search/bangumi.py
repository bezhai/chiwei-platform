"""Bangumi ACG 数据库搜索工具"""

import asyncio
import json
from collections.abc import Callable
from functools import wraps
from typing import Any

import httpx
from langchain.tools import tool
from pydantic import BaseModel

from app.agents.tools.search.models import (
    Career,
    Character,
    CharacterForAIResult,
    CharacterPerson,
    CharacterSearchResult,
    CharacterSubject,
    Person,
    PersonCharacter,
    PersonForAIResult,
    PersonSearchResult,
    PersonSubject,
    SimpleCharacter,
    SimpleCharacterPerson,
    SimpleCharacterSubject,
    SimplePerson,
    SimplePersonCharacter,
    SimplePersonSubject,
    SimpleSubject,
    SimpleSubjectCharacter,
    SimpleSubjectPerson,
    SimpleSubjectRelation,
    Subject,
    SubjectCharacter,
    SubjectForAIResult,
    SubjectPerson,
    SubjectRelation,
    SubjectSearchResult,
)
from app.config import settings
from app.utils.decorators import redis_cache


def auto_serialize_tool(func: Callable) -> Callable:
    """自动序列化Pydantic模型的装饰器"""

    @wraps(func)
    async def async_wrapper(*args, **kwargs):
        result = await func(*args, **kwargs)
        if isinstance(result, BaseModel):
            return result.model_dump()
        elif isinstance(result, list) and result and isinstance(result[0], BaseModel):
            return [item.model_dump() for item in result]
        return result

    @wraps(func)
    def sync_wrapper(*args, **kwargs):
        result = func(*args, **kwargs)
        if isinstance(result, BaseModel):
            return result.model_dump()
        elif isinstance(result, list) and result and isinstance(result[0], BaseModel):
            return [item.model_dump() for item in result]
        return result

    return async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper


@redis_cache(expire_seconds=86400)  # 24小时缓存
async def send_bangumi_request(
    path: str,
    params: dict[str, Any] | None = None,
    method: str = "GET",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """发送 Bangumi API 请求"""
    headers = {
        "Authorization": f"Bearer {settings.bangumi_access_token}",
        "Content-Type": "application/json",
        "User-Agent": "panda1234/search",  # 参考 https://github.com/bangumi/api/blob/master/docs-raw/user%20agent.md
    }

    # 构建完整的 URL
    base_url = "https://api.bgm.tv"
    url = f"{base_url}{path}"

    # 准备请求数据
    json_data = json.dumps(data) if data is not None else None

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.request(
            method=method,
            url=url,
            params=params or {},
            content=json_data,
            headers=headers,
        )
        response.raise_for_status()
        return response.json()


def append_element(array, element):
    if array is None:
        array = []
    # 返回拼接后的新数组，不改变原来的array
    return array + [element]


# 工具函数定义


@tool
@auto_serialize_tool
async def search_subjects(
    types: list[str] | None = None,
    keyword: str | None = None,
    sort: str = "match",
    limit: int = 10,
    offset: int = 0,
    tags: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    min_rating: int | None = None,
    max_rating: int | None = None,
) -> SubjectForAIResult:
    """
    搜索条目, 包含书籍, 动画, 音乐, 游戏, 三次元

    Args:
        keyword: 搜索关键词
        sort: 排序方式，可选值：match(匹配度)/heat(热度)/score(评分)，默认match
        limit: 分页限制，默认10，最大50, 范围比较明确时建议设置为5以下甚至1
        offset: 分页偏移，默认0
        types: 条目类型筛选，可选值包含"书籍", "动画", "音乐", "游戏", "三次元"，
               多值为"或"关系
        tags: 标签筛选，多值为"且"关系
        start_date: 开始日期(包含)筛选，格式如"YYYY-MM-DD"
        end_date: 结束日期(不包含)筛选，格式如"YYYY-MM-DD"
        min_rating: 最小评分，取值范围为1-10
        max_rating: 最大评分，取值范围为1-10

    Returns:
        条目搜索结果，包含总数、分页信息和条目列表
    """
    # 构建筛选条件
    search_filter: dict[str, Any] = {}
    if types is not None:
        search_filter["type"] = list(
            filter(
                lambda x: x is not None,
                [
                    {
                        "书籍": 1,
                        "动画": 2,
                        "音乐": 3,
                        "游戏": 4,
                        "三次元": 6,
                    }.get(sub_type)
                    for sub_type in types
                ],
            )
        )
    if tags is not None:
        search_filter["tag"] = tags
    if start_date is not None:
        search_filter["air_date"] = append_element(
            search_filter.get("air_date", []), f">={start_date}"
        )
    if end_date is not None:
        search_filter["air_date"] = append_element(
            search_filter.get("air_date", []), f"<{end_date}"
        )
    if min_rating is not None:
        search_filter["rating"] = append_element(
            search_filter.get("rating", []), f">={min_rating}"
        )
    if max_rating is not None:
        search_filter["rating"] = append_element(
            search_filter.get("rating", []), f"<={max_rating}"
        )

    # 构建请求体
    request_body: dict[str, Any] = {"keyword": keyword, "sort": sort}
    if search_filter:
        request_body["filter"] = search_filter

    # 构建查询参数
    params: dict[str, Any] = {}
    if limit != 30:
        params["limit"] = limit
    if offset != 0:
        params["offset"] = offset

    response = await send_bangumi_request(
        path="/v0/search/subjects", params=params, method="POST", data=request_body
    )
    mid_result = SubjectSearchResult(**response)
    return SubjectForAIResult(
        total=mid_result.total,
        limit=mid_result.limit,
        offset=mid_result.offset,
        data=[subject.to_simple() for subject in mid_result.data],
    )


@tool
@auto_serialize_tool
async def search_characters(
    keyword: str,
    limit: int = 30,
    offset: int = 0,
) -> CharacterForAIResult:
    """
    搜索角色

    Args:
        keyword: 搜索关键词，必填
        limit: 分页限制，默认30，最大50, 范围比较明确时建议设置为5以下甚至1
        offset: 分页偏移，默认0

    Returns:
        角色搜索结果，包含总数、分页信息和角色列表
    """

    # 构建请求体
    request_body = {"keyword": keyword}

    # 构建查询参数
    params = {}
    if limit != 30:
        params["limit"] = limit
    if offset != 0:
        params["offset"] = offset

    response = await send_bangumi_request(
        path="/v0/search/characters", params=params, method="POST", data=request_body
    )
    mid_result = CharacterSearchResult(**response)
    return CharacterForAIResult(
        total=mid_result.total,
        limit=mid_result.limit,
        offset=mid_result.offset,
        data=[character.to_simple() for character in mid_result.data],
    )


@tool
@auto_serialize_tool
async def search_persons(
    keyword: str,
    careers: list[str] | None = None,
    limit: int = 10,
    offset: int = 0,
) -> PersonForAIResult:
    """
    搜索与ACG相关的现实人物

    Args:
        keyword: 搜索关键词，必填
        career: 职业筛选，可选值包含"制作人员", "漫画家", "音乐人", "声优", "作家",
                "绘师", "演员"，多值为"或"关系
        limit: 分页限制，默认10，最大50, 范围比较明确时建议设置为5以下甚至1
        offset: 分页偏移，默认0

    Returns:
        人物搜索结果，包含总数、分页信息和人物列表
    """
    # 构建请求体
    request_body: dict[str, Any] = {"keyword": keyword}
    if careers is not None:
        request_body["career"] = list(
            filter(
                lambda x: x is not None,
                [
                    {
                        "制作人员": Career.PRODUCER,
                        "漫画家": Career.MANGAKA,
                        "音乐人": Career.ARTIST,
                        "声优": Career.SEIYU,
                        "作家": Career.WRITER,
                        "绘师": Career.ILLUSTRATOR,
                        "演员": Career.ACTOR,
                    }.get(career)
                    for career in careers
                ],
            )
        )

    # 构建查询参数
    params: dict[str, Any] = {}
    if limit != 10:
        params["limit"] = limit
    if offset != 0:
        params["offset"] = offset

    response = await send_bangumi_request(
        path="/v0/search/persons", params=params, method="POST", data=request_body
    )
    mid_result = PersonSearchResult(**response)
    return PersonForAIResult(
        total=mid_result.total,
        limit=mid_result.limit,
        offset=mid_result.offset,
        data=[person.to_simple() for person in mid_result.data],
    )


@tool
@auto_serialize_tool
async def get_subject_characters(
    subject_id: int,
) -> list[SimpleSubjectCharacter]:
    """
    获取条目关联的角色

    Args:
        subject_id: 条目ID

    Returns:
        角色列表
    """
    response = await send_bangumi_request(
        path=f"/v0/subjects/{subject_id}/characters", method="GET"
    )
    mid_result = [SubjectCharacter(**item).to_simple() for item in response]  # pyright: ignore[reportCallIssue]
    for item in mid_result:
        item.detail = await _get_character_info(item.id)
    return mid_result


@tool
@auto_serialize_tool
async def get_subject_relations(
    subject_id: int,
) -> list[SimpleSubjectRelation]:
    """
    获取条目关联的条目
    """
    response = await send_bangumi_request(
        path=f"/v0/subjects/{subject_id}/relations", method="GET"
    )
    mid_result = [SubjectRelation(**item).to_simple() for item in response]  # pyright: ignore[reportCallIssue]
    for item in mid_result:
        item.detail = await _get_subject_info(item.id)
    return mid_result


@tool
@auto_serialize_tool
async def get_subject_persons(
    subject_id: int,
) -> list[SimpleSubjectPerson]:
    """
    获取条目关联的人物

    Args:
        subject_id: 条目ID

    Returns:
        人物列表
    """
    response = await send_bangumi_request(
        path=f"/v0/subjects/{subject_id}/persons", method="GET"
    )
    mid_result = [SubjectPerson(**item).to_simple() for item in response]  # pyright: ignore[reportCallIssue]
    for item in mid_result:
        item.detail = await _get_person_info(item.id)
    return mid_result


@tool
@auto_serialize_tool
async def get_character_subjects(
    character_id: int,
) -> list[SimpleCharacterSubject]:
    """
    获取角色关联的条目

    Args:
        character_id: 角色ID

    Returns:
        条目列表
    """
    response = await send_bangumi_request(
        path=f"/v0/characters/{character_id}/subjects", method="GET"
    )
    mid_result = [CharacterSubject(**item).to_simple() for item in response]  # pyright: ignore[reportCallIssue]
    for item in mid_result:
        item.detail = await _get_subject_info(item.id)
    return mid_result


@tool
@auto_serialize_tool
async def get_character_persons(
    character_id: int,
) -> list[SimpleCharacterPerson]:
    """
    获取角色关联的人物

    Args:
        character_id: 角色ID

    Returns:
        人物列表
    """
    response = await send_bangumi_request(
        path=f"/v0/characters/{character_id}/persons", method="GET"
    )
    mid_result = [CharacterPerson(**item).to_simple() for item in response]  # pyright: ignore[reportCallIssue]
    for item in mid_result:
        item.detail = await _get_person_info(item.id)
    return mid_result


@tool
@auto_serialize_tool
async def get_person_characters(
    person_id: int,
) -> list[SimplePersonCharacter]:
    """
    获取人物关联的角色

    Args:
        person_id: 人物ID

    Returns:
        角色列表
    """
    response = await send_bangumi_request(
        path=f"/v0/persons/{person_id}/characters", method="GET"
    )
    mid_result = [PersonCharacter(**item).to_simple() for item in response]  # pyright: ignore[reportCallIssue]
    for item in mid_result:
        item.detail = await _get_character_info(item.id)
    return mid_result


@tool
@auto_serialize_tool
async def get_person_subjects(
    person_id: int,
) -> list[SimplePersonSubject]:
    """
    获取人物关联的条目

    Args:
        person_id: 人物ID

    Returns:
        条目列表
    """
    response = await send_bangumi_request(
        path=f"/v0/persons/{person_id}/subjects", method="GET"
    )
    mid_result = [PersonSubject(**item).to_simple() for item in response]  # pyright: ignore[reportCallIssue]
    for item in mid_result:
        item.detail = await _get_subject_info(item.id)
    return mid_result


async def _get_subject_info(subject_id: int) -> SimpleSubject:
    response = await send_bangumi_request(
        path=f"/v0/subjects/{subject_id}", method="GET"
    )
    return Subject(**response).to_simple()


async def _get_character_info(character_id: int) -> SimpleCharacter:
    response = await send_bangumi_request(
        path=f"/v0/characters/{character_id}", method="GET"
    )
    return Character(**response).to_simple()


async def _get_person_info(person_id: int) -> SimplePerson:
    response = await send_bangumi_request(path=f"/v0/persons/{person_id}", method="GET")
    return Person(**response).to_simple()
