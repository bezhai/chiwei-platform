"""Web search, image search, webpage reading, and reranking.

Merges the old search/web.py, search/image.py, search/reader.py,
and search/reranker.py into a single module.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Annotated, Any

import httpx
from bs4 import BeautifulSoup
from langchain.tools import tool
from langgraph.runtime import get_runtime
from prometheus_client import Counter, Histogram
from pydantic import Field

from app.agent.context import AgentContext
from app.infra.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prometheus metrics (reuse existing collectors to avoid double-registration)
# ---------------------------------------------------------------------------


def _counter(name: str, doc: str, labels: list[str]) -> Counter:
    try:
        return Counter(name, doc, labels)
    except ValueError:
        # Already registered — grab the existing one from the default registry
        from prometheus_client import REGISTRY

        return REGISTRY._names_to_collectors[name.removesuffix("_total")]  # type: ignore[return-value]


def _histogram(name: str, doc: str, labels: list[str] | None = None) -> Histogram:
    try:
        return Histogram(name, doc, labels or [])
    except ValueError:
        from prometheus_client import REGISTRY

        return REGISTRY._names_to_collectors[name]  # type: ignore[return-value]


WEB_SEARCH_REQUESTS = _counter(
    "web_search_requests_total", "Web search API requests", ["status"]
)
WEB_SEARCH_DURATION = _histogram(
    "web_search_duration_seconds", "Web search request duration"
)
RERANK_DURATION = _histogram("search_rerank_duration_seconds", "Rerank duration")
IMAGE_SEARCH_DURATION = _histogram(
    "image_search_step_duration_seconds", "Image search step duration", ["step"]
)
IMAGE_SEARCH_TOTAL = _counter(
    "image_search_requests_total", "Image search requests", ["status"]
)
IMAGE_SEARCH_UPLOADS = _counter(
    "image_search_upload_results_total", "Image upload outcomes", ["status"]
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PAGE_MAX_CHARS = 16_000
CHUNK_SIZE = 2_000
CHUNK_OVERLAP = 200
RERANK_TOP_K = 5
MIN_RELEVANCE_SCORE = 0.1
RERANK_MODEL = "Qwen/Qwen3-Reranker-4B"
IMAGE_MAX_RESULTS = 5


# ---------------------------------------------------------------------------
# Internal helpers — webpage reading
# ---------------------------------------------------------------------------


def _html_to_text(html: str) -> str:
    """Strip HTML to plain text, removing scripts and styles."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        return "\n".join(line.strip() for line in text.split("\n") if line.strip())
    except Exception as exc:
        logger.error("HTML→text conversion failed: %s", exc)
        return ""


async def _read_webpage(url: str) -> str:
    """Fetch webpage content via You Search Contents API, return markdown."""
    if not settings.you_search_host or not settings.you_search_api_key:
        return ""

    api_url = f"{settings.you_search_host}/v1/contents"
    headers = {
        "X-API-Key": settings.you_search_api_key,
        "Content-Type": "application/json",
    }
    payload = {"urls": [url], "formats": ["markdown", "html"]}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(api_url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        if isinstance(data, dict):
            data = data.get("contents") or data.get("results") or []
        if not data:
            return ""

        result = data[0]
        if result.get("markdown"):
            return result["markdown"]
        if result.get("html"):
            return _html_to_text(result["html"])
        return ""
    except Exception as exc:
        logger.error("read_webpage(%s) failed: %s", url, exc)
        return ""


async def _fetch_content(result: dict) -> dict:
    """Fetch page content for a single search result dict."""
    link = result.get("link", "")
    if not link:
        return result
    try:
        content = await _read_webpage(link)
        result["content"] = content[:PAGE_MAX_CHARS]
    except Exception:
        result["content"] = result.get("snippet", "")
    return result


# ---------------------------------------------------------------------------
# Internal helpers — reranking
# ---------------------------------------------------------------------------


def _chunk_text(
    text: str,
    chunk_size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
) -> list[str]:
    """Split text at paragraph boundaries with overlap."""
    if len(text) <= chunk_size:
        return [text] if text.strip() else []

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        if end >= len(text):
            tail = text[start:]
            if tail.strip():
                chunks.append(tail)
            break

        window = text[start:end]
        split_pos = window.rfind("\n\n")
        if split_pos == -1 or split_pos < chunk_size // 2:
            split_pos = window.rfind("\n")
        if split_pos == -1 or split_pos < chunk_size // 2:
            split_pos = chunk_size

        chunk = text[start : start + split_pos]
        if chunk.strip():
            chunks.append(chunk)
        start = max(0, start + split_pos - overlap)

    return chunks


def _rerank_fallback(results: list[dict], top_k: int) -> list[dict]:
    """Fallback: truncate each result's content to CHUNK_SIZE."""
    out = []
    for r in results[:top_k]:
        content = r.get("content", "") or r.get("snippet", "")
        out.append(
            {
                "title": r.get("title", ""),
                "link": r.get("link", ""),
                "content": content[:CHUNK_SIZE],
            }
        )
    return out


async def _rerank_chunks(
    query: str,
    results: list[dict],
    top_k: int = RERANK_TOP_K,
) -> list[dict]:
    """Chunk-level reranking via SiliconFlow cross-encoder."""
    if not settings.siliconflow_api_key:
        return _rerank_fallback(results, top_k)

    all_chunks: list[dict] = []
    for r in results:
        content = r.get("content", "")
        if not content:
            continue
        for idx, chunk in enumerate(_chunk_text(content)):
            all_chunks.append(
                {
                    "title": r.get("title", ""),
                    "link": r.get("link", ""),
                    "chunk": chunk,
                    "chunk_idx": idx,
                }
            )

    if not all_chunks:
        return _rerank_fallback(results, top_k)

    try:
        documents = [c["chunk"] for c in all_chunks]
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{settings.siliconflow_base_url}/rerank",
                headers={
                    "Authorization": f"Bearer {settings.siliconflow_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": RERANK_MODEL,
                    "query": query,
                    "documents": documents,
                    "top_n": top_k,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        ranked = []
        for item in data.get("results", []):
            score = item.get("relevance_score", 0)
            if score < MIN_RELEVANCE_SCORE:
                continue
            idx = item["index"]
            c = all_chunks[idx]
            ranked.append(
                {
                    "title": c["title"],
                    "link": c["link"],
                    "content": c["chunk"],
                    "score": score,
                }
            )
        return ranked

    except Exception:
        logger.exception("rerank_chunks failed, using truncation fallback")
        return _rerank_fallback(results, top_k)


# ---------------------------------------------------------------------------
# Internal helpers — search providers
# ---------------------------------------------------------------------------


async def _google_search(query: str, num: int) -> list[dict]:
    """Google Custom Search via proxy."""
    params = {
        "q": query,
        "ak": settings.google_search_api_key,
        "cx": settings.google_search_cx,
        "num": num,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(settings.google_search_host, params=params)
        resp.raise_for_status()
        data = resp.json()

    items = data.get("items", [])
    logger.info("Google search returned %d items", len(items))
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
    """You Search API (fallback provider)."""
    params: dict[str, str | int] = {
        "query": query,
        "count": num,
        "country": gl,
        "language": hl,
    }
    headers = {"X-API-Key": settings.you_search_api_key or ""}
    async with httpx.AsyncClient(timeout=15) as client:
        url = f"{settings.you_search_host}/v1/search"
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()

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


# ---------------------------------------------------------------------------
# Internal helpers — image upload
# ---------------------------------------------------------------------------


async def _upload_and_register(
    source_type: str,
    data: str,
    registry: Any,
) -> tuple[str, str | None]:
    """Upload an image to TOS and optionally register in ImageRegistry.

    Returns ``(tos_url, filename)`` on success, ``(data, None)`` on failure.
    """
    from app.infra.image import image_client

    try:
        tos_url = await image_client.upload_to_tos(source_type, data)
        if not tos_url:
            return data, None
        filename: str | None = None
        if registry:
            filename = await registry.register(tos_url)
        return tos_url, filename
    except Exception:
        logger.warning("upload_and_register failed", exc_info=True)
        return data, None


# =========================================================================
# Public tools
# =========================================================================


def _tool_error(error_message: str):
    """Decorator: catch exceptions, log, and return a friendly error string."""
    import functools

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except Exception as exc:
                logger.error("%s failed: %s", func.__name__, exc, exc_info=True)
                return f"{error_message}: {exc}"

        return wrapper

    return decorator


@tool
@_tool_error("网页搜索失败")
async def search_web(
    query: str,
    gl: str = "CN",
    hl: str = "ZH-HANS",
    num: int = 5,
) -> str:
    """网页搜索，返回搜索结果及其网页内容。

    Args:
        query: 搜索关键词。
        gl: 结果地域代码，默认 "CN"。
        hl: 界面语言代码，默认 "ZH-HANS"。
        num: 返回结果条数，默认 5。

    Returns:
        搜索结果的文本摘要。
    """
    start = time.monotonic()
    status = "error"
    try:
        if settings.you_search_host and settings.you_search_api_key:
            organic_results = await _you_search(query, num, gl, hl)
        elif settings.google_search_host and settings.google_search_api_key:
            organic_results = await _google_search(query, num)
        else:
            logger.error("No search provider configured")
            return "搜索服务未配置"
        status = "ok"
    except httpx.TimeoutException:
        status = "timeout"
        logger.error("Web search timed out")
        return "网页搜索超时"
    except httpx.HTTPStatusError as exc:
        status = f"http_{exc.response.status_code}"
        logger.error("Web search HTTP error: %s", exc)
        return f"网页搜索失败: HTTP {exc.response.status_code}"
    except Exception as exc:
        logger.error("Web search unexpected error: %s", exc)
        return f"网页搜索失败: {exc}"
    finally:
        duration = time.monotonic() - start
        WEB_SEARCH_REQUESTS.labels(status=status).inc()
        WEB_SEARCH_DURATION.observe(duration)

    # Fetch page content concurrently
    enriched = await asyncio.gather(*[_fetch_content(r) for r in organic_results])

    # Chunk-level rerank
    rerank_start = time.monotonic()
    try:
        ranked = await _rerank_chunks(query, list(enriched))
    except Exception:
        logger.exception("rerank_chunks failed in search_web")
        ranked = list(enriched)
    finally:
        RERANK_DURATION.observe(time.monotonic() - rerank_start)

    # Format as text
    if not ranked:
        return "未搜索到相关结果"

    lines = []
    for i, r in enumerate(ranked, 1):
        title = r.get("title", "")
        link = r.get("link", "")
        content = r.get("content", "")[:800]
        lines.append(f"[{i}] {title}\n    {link}\n    {content}")
    return "\n\n".join(lines)


@tool
@_tool_error("图片搜索失败")
async def search_images(
    query: Annotated[
        str,
        Field(description="搜索关键词，用英文效果更好"),
    ],
) -> str | list[dict[str, Any]]:
    """搜索网络图片。返回搜索到的图片（自动注册为 @N.png 可供后续引用）。

    Args:
        query: 搜索关键词。
    """
    if not settings.you_search_host or not settings.you_search_api_key:
        return "图片搜索服务未配置"

    url = f"{settings.you_search_host}/images"
    params = {"q": query}
    headers = {"X-API-Key": settings.you_search_api_key}

    try:
        t_start = time.monotonic()

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        t_search = time.monotonic() - t_start
        IMAGE_SEARCH_DURATION.labels(step="search_api").observe(t_search)

        raw_images = data.get("images", [])
        if isinstance(raw_images, dict):
            images = raw_images.get("results", [])
        else:
            images = raw_images
        if not images:
            return "未搜索到相关图片"

        images = images[:IMAGE_MAX_RESULTS]

        # Upload each to TOS and register
        context = get_runtime(AgentContext).context
        registry = context.media.registry

        t0 = time.monotonic()
        upload_tasks = [
            _upload_and_register(
                source_type="url",
                data=img.get("image_url") or img.get("url", ""),
                registry=registry,
            )
            for img in images
            if img.get("image_url") or img.get("url")
        ]
        results = await asyncio.gather(*upload_tasks, return_exceptions=True)
        t_upload = time.monotonic() - t0

        content_blocks: list[dict[str, Any]] = []
        result_lines: list[str] = []
        failed = 0

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                failed += 1
                logger.warning("Image %d upload failed: %s", i, result)
                continue
            tos_url, filename = result
            if not filename:
                failed += 1
                continue
            result_lines.append(f"@{filename}")
            content_blocks.append({"type": "text", "text": f"@{filename}:"})
            content_blocks.append({"type": "image_url", "image_url": {"url": tos_url}})

        IMAGE_SEARCH_DURATION.labels(step="upload_pipeline").observe(t_upload)
        IMAGE_SEARCH_UPLOADS.labels(status="success").inc(len(result_lines))
        IMAGE_SEARCH_UPLOADS.labels(status="failed").inc(failed)

        t_total = time.monotonic() - t_start
        logger.info(
            "search_images done: query=%r search=%.2fs upload=%.2fs total=%.2fs "
            "results=%d/%d failed=%d",
            query,
            t_search,
            t_upload,
            t_total,
            len(result_lines),
            len(upload_tasks),
            failed,
        )

        if not content_blocks:
            IMAGE_SEARCH_TOTAL.labels(status="upload_failed").inc()
            return "图片搜索成功但上传失败，请稍后重试"

        IMAGE_SEARCH_TOTAL.labels(status="success").inc()
        summary = f"搜索到 {len(result_lines)} 张图片: {', '.join(result_lines)}"
        content_blocks.insert(0, {"type": "text", "text": summary})
        return content_blocks

    except httpx.TimeoutException:
        IMAGE_SEARCH_TOTAL.labels(status="timeout").inc()
        return "图片搜索超时"
    except httpx.HTTPStatusError as exc:
        IMAGE_SEARCH_TOTAL.labels(status="http_error").inc()
        return f"图片搜索失败: HTTP {exc.response.status_code}"
    except Exception as exc:
        IMAGE_SEARCH_TOTAL.labels(status="error").inc()
        return f"图片搜索失败: {exc}"
