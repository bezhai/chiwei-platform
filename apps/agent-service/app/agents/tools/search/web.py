"""Web 搜索工具"""

import asyncio
import logging
import time

import httpx
from langchain.tools import tool
from prometheus_client import Counter, Histogram

from app.agents.tools.search.reader import read_webpage
from app.agents.tools.search.reranker import rerank_chunks
from app.config import settings
from app.utils.decorators import dict_serialize, log_io

logger = logging.getLogger(__name__)

WEB_SEARCH_REQUESTS_TOTAL = Counter(
    "web_search_requests_total",
    "Total web search API requests",
    ["status"],
)
WEB_SEARCH_DURATION = Histogram(
    "web_search_duration_seconds",
    "Web search API request duration in seconds",
)
RERANK_DURATION = Histogram(
    "search_rerank_duration_seconds",
    "Search rerank duration in seconds",
)

PAGE_MAX_CHARS = 16000


async def _fetch_content(result: dict) -> dict:
    """为单个搜索结果抓取网页内容。"""
    link = result.get("link", "")
    if not link:
        return result

    try:
        content = await read_webpage(link)
        result["content"] = content[:PAGE_MAX_CHARS]
    except Exception:
        # 抓取失败时降级到 snippet
        result["content"] = result.get("snippet", "")

    return result


async def _google_search(query: str, num: int) -> list[dict]:
    """通过 Google Custom Search 代理搜索。"""
    params = {
        "q": query,
        "ak": settings.google_search_api_key,
        "cx": settings.google_search_cx,
        "num": num,
    }

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(settings.google_search_host, params=params)
        response.raise_for_status()
        data = response.json()

    items = data.get("items", [])
    logger.info("Google Custom Search returned %d items", len(items))
    return [
        {
            "link": item.get("link", ""),
            "title": item.get("title", ""),
            "snippet": item.get("snippet", ""),
            "displayLink": item.get("displayLink", ""),
        }
        for item in items
    ]


async def _you_search(query: str, num: int, gl: str, hl: str) -> list[dict]:
    """通过 You Search API 搜索（fallback）。"""
    params: dict[str, str | int] = {
        "query": query,
        "count": num,
        "country": gl,
        "language": hl,
    }
    headers = {"X-API-Key": settings.you_search_api_key or ""}

    async with httpx.AsyncClient(timeout=15) as client:
        url = f"{settings.you_search_host}/v1/search"
        response = await client.get(url, params=params, headers=headers)
        response.raise_for_status()
        data = response.json()

    web_results = data.get("results", {}).get("web", [])
    logger.info("You Search returned %d results", len(web_results))
    return [
        {
            "link": r.get("url", ""),
            "title": r.get("title", ""),
            "snippet": r.get("description", ""),
        }
        for r in web_results
    ]


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
    start = time.monotonic()
    status = "error"
    try:
        # 优先 You Search，fallback Google Custom Search
        if settings.you_search_host and settings.you_search_api_key:
            organic_results = await _you_search(query, num, gl, hl)
        elif settings.google_search_host and settings.google_search_api_key:
            organic_results = await _google_search(query, num)
        else:
            logger.error("No search provider configured")
            return []
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
        WEB_SEARCH_REQUESTS_TOTAL.labels(status=status).inc()
        WEB_SEARCH_DURATION.observe(duration)

    # 并发抓取每个结果的网页内容
    tasks = [_fetch_content(result) for result in organic_results]
    results = await asyncio.gather(*tasks)

    # 切片级 rerank 重排
    rerank_start = time.monotonic()
    try:
        ranked = await rerank_chunks(query, list(results))
    except Exception:
        logger.exception("rerank_chunks failed in search_web")
        ranked = list(results)
    finally:
        RERANK_DURATION.observe(time.monotonic() - rerank_start)

    return ranked
