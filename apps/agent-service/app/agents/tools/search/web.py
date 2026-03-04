"""Web 搜索工具"""

import asyncio
import logging
import time

import httpx
from langchain.tools import tool
from prometheus_client import Counter, Histogram

from app.agents.tools.search.reader import read_webpage
from app.config import settings
from app.utils.decorators import dict_serialize, log_io

logger = logging.getLogger(__name__)

YOU_SEARCH_REQUESTS_TOTAL = Counter(
    "you_search_requests_total",
    "Total You Search API requests",
    ["status"],
)
YOU_SEARCH_DURATION = Histogram(
    "you_search_duration_seconds",
    "You Search API request duration in seconds",
)


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
    gl: str = "CN",
    hl: str = "ZH-HANS",
    num: int = 5,
) -> list[dict]:
    """网页搜索，返回搜索结果及其网页内容。

    Args:
        query: 搜索关键词。
        gl: 结果地域代码，默认 "CN"。
        hl: 界面语言代码，默认 "ZH-HANS"。
        num: 返回结果条数，默认 5。

    Returns:
        搜索结果列表，每个结果包含 title, link, snippet, content。
    """
    if not settings.you_search_host or not settings.you_search_api_key:
        logger.error("You Search not configured")
        return []

    url = f"{settings.you_search_host}/v1/search"

    params: dict[str, str | int] = {
        "query": query,
        "count": num,
        "country": gl,
        "language": hl,
    }

    headers = {
        "X-API-Key": settings.you_search_api_key,
    }

    start = time.monotonic()
    status = "error"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()
        status = "ok"
    except httpx.TimeoutException:
        status = "timeout"
        logger.error("Timeout during web search")
        return []
    except httpx.HTTPStatusError as e:
        status = f"http_{e.response.status_code}"
        logger.error(f"HTTP error during web search: {e}")
        return []
    except Exception as e:
        logger.error(f"Unexpected error during web search: {e}")
        return []
    finally:
        duration = time.monotonic() - start
        YOU_SEARCH_REQUESTS_TOTAL.labels(status=status).inc()
        YOU_SEARCH_DURATION.observe(duration)

    # 转换响应结构
    web_results = data.get("results", {}).get("web", [])
    organic_results = [
        {
            "link": r.get("url", ""),
            "title": r.get("title", ""),
            "snippet": r.get("description", ""),
        }
        for r in web_results
    ]

    # 并发抓取每个结果的网页内容
    tasks = [_fetch_content(result) for result in organic_results]
    results = await asyncio.gather(*tasks)

    return list(results)
