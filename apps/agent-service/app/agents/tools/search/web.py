"""Web 搜索工具"""

import asyncio

import httpx
from langchain.tools import tool

from app.agents.tools.search.reader import read_webpage
from app.config import settings
from app.utils.decorators import dict_serialize, log_io


async def _fetch_content(result: dict) -> dict:
    """为单个搜索结果抓取网页内容。"""
    link = result.get("link", "")
    if not link:
        return result

    try:
        content = await read_webpage(link)
        result["content"] = content
    except Exception:
        # 抓取失败时降级到 snippet
        result["content"] = result.get("snippet", "")

    return result


@tool
@log_io
@dict_serialize
async def search_web(
    query: str,
    gl: str = "cn",
    hl: str = "zh-cn",
    num: int = 5,
) -> list[dict]:
    """Google 网页搜索，返回搜索结果及其网页内容。

    Args:
        query: 搜索关键词。
        gl: 结果地域代码，默认 "cn"。
        hl: 界面语言代码，默认 "zh-cn"。
        num: 返回结果条数，默认 5。

    Returns:
        搜索结果列表，每个结果包含 title, link, snippet, content。
    """
    url = "https://api.302.ai/serpapi/search"

    params: dict[str, str | int] = {
        "engine": "google_light",
        "api_key": settings.search_api_key or "",
        "q": query,
        "hl": hl,
        "gl": gl,
        "num": num,
    }

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        data = response.json()

    # 只保留 organic_results
    organic_results = data.get("organic_results", [])

    # 并发抓取每个结果的网页内容
    tasks = [_fetch_content(result) for result in organic_results]
    results = await asyncio.gather(*tasks)

    return list(results)
