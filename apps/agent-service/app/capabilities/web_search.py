"""Web search + read webpage + rerank capability — Phase 7d Gap 16.

Hoists the inline httpx calls from ``app/agent/tools/search.py`` into a
single capability. Uses ``HTTPClient`` so lane/trace headers and the
method-aware retry matrix are applied uniformly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.capabilities.http import HTTPClient
from app.infra.config import settings

logger = logging.getLogger(__name__)


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str


# Module-level clients reused across calls; HTTPClient owns an httpx.AsyncClient.
_GET_CLIENT = HTTPClient(timeout=15.0)  # default GET retry=3
_POST_CLIENT = HTTPClient(timeout=30.0)  # POST retry_post=0 by default


async def web_search(
    query: str,
    *,
    count: int = 10,
    country: str = "CN",
    language: str = "ZH-HANS",
) -> list[SearchHit]:
    """Route to You Search / Google CSE based on settings; empty list if neither configured."""
    if settings.you_search_host and settings.you_search_api_key:
        return await _you_search(query, count, country, language)
    if settings.google_search_host and settings.google_search_api_key:
        return await _google_search(query, count)
    return []


async def _you_search(
    query: str, count: int, country: str, language: str
) -> list[SearchHit]:
    url = f"{settings.you_search_host}/v1/search"
    headers = {"X-API-Key": settings.you_search_api_key or ""}
    params: dict[str, str | int] = {
        "query": query,
        "count": count,
        "country": country,
        "language": language,
    }
    resp = await _GET_CLIENT.get(url, params=params, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return [
        SearchHit(
            title=r.get("title", ""),
            url=r.get("url", ""),
            snippet=r.get("description", ""),
        )
        for r in data.get("results", {}).get("web", [])
    ]


async def _google_search(query: str, count: int) -> list[SearchHit]:
    params = {
        "q": query,
        "ak": settings.google_search_api_key,
        "cx": settings.google_search_cx,
        "num": count,
    }
    resp = await _GET_CLIENT.get(settings.google_search_host or "", params=params)
    resp.raise_for_status()
    data = resp.json()
    return [
        SearchHit(
            title=item.get("title", ""),
            url=item.get("link", ""),
            snippet=item.get("snippet", ""),
        )
        for item in data.get("items", [])
    ]


async def read_webpage(url: str) -> str:
    """Fetch + html→markdown via You Contents API. Empty string if not configured."""
    if not settings.you_search_host or not settings.you_search_api_key:
        return ""
    api_url = f"{settings.you_search_host}/v1/contents"
    headers = {
        "X-API-Key": settings.you_search_api_key,
        "Content-Type": "application/json",
    }
    payload = {"urls": [url], "formats": ["markdown", "html"]}
    resp = await _POST_CLIENT.post(api_url, headers=headers, json=payload)
    resp.raise_for_status()
    data = resp.json()
    contents = data.get("contents") or data.get("results") or []
    if not contents:
        return ""
    item = contents[0]
    return item.get("markdown") or item.get("html") or ""


async def rerank(
    query: str,
    docs: list[str],
    *,
    top_k: int = 5,
    model: str = "Qwen/Qwen3-Reranker-4B",
) -> list[tuple[int, float]]:
    """Rerank docs against query via SiliconFlow API; return [(idx, score), ...].

    Falls back to identity ranking (1.0 score) if SiliconFlow is not configured.
    """
    if not settings.siliconflow_api_key or not docs:
        return [(i, 1.0) for i in range(min(top_k, len(docs)))]
    headers = {
        "Authorization": f"Bearer {settings.siliconflow_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "query": query,
        "documents": docs,
        "top_n": top_k,
    }
    resp = await _POST_CLIENT.post(
        f"{settings.siliconflow_base_url}/rerank", headers=headers, json=payload
    )
    resp.raise_for_status()
    data = resp.json()
    return [
        (item["index"], item.get("relevance_score", 0.0))
        for item in data.get("results", [])
    ]
