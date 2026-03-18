"""图片搜索工具"""

import asyncio
import logging
import time
from typing import Annotated, Any

import httpx
from langchain.tools import tool
from langgraph.runtime import get_runtime
from prometheus_client import Counter, Histogram
from pydantic import Field

from app.agents.core.context import AgentContext
from app.config import settings

logger = logging.getLogger(__name__)

_MAX_RESULTS = 5

IMAGE_SEARCH_DURATION = Histogram(
    "image_search_step_duration_seconds",
    "Duration of each image search step",
    ["step"],  # search_api, upload_pipeline
)
IMAGE_SEARCH_TOTAL = Counter(
    "image_search_requests_total",
    "Total image search requests",
    ["status"],  # success, no_results, error
)
IMAGE_SEARCH_UPLOAD_RESULTS = Counter(
    "image_search_upload_results_total",
    "Image search upload outcomes",
    ["status"],  # success, failed
)


@tool
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
        logger.error("You Search not configured")
        return "图片搜索服务未配置"

    url = f"{settings.you_search_host}/images"
    params = {"q": query}
    headers = {"X-API-Key": settings.you_search_api_key}

    try:
        t_start = time.monotonic()

        # 1. Search API
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()
        t_search = time.monotonic() - t_start
        IMAGE_SEARCH_DURATION.labels(step="search_api").observe(t_search)

        raw_images = data.get("images", [])
        # You Search API may return {"results": [...]} or a plain list
        if isinstance(raw_images, dict):
            images = raw_images.get("results", [])
        else:
            images = raw_images
        if not images:
            return "未搜索到相关图片"

        # Take top N results
        images = images[:_MAX_RESULTS]

        # 2. Upload each to TOS and register
        from app.clients.image_client import image_client

        context = get_runtime(AgentContext).context
        registry = context.media.registry

        t0 = time.monotonic()
        # Upload concurrently (API returns image_url field)
        upload_tasks = [
            image_client.upload_to_tos("url", img.get("image_url") or img.get("url", ""))
            for img in images
            if img.get("image_url") or img.get("url")
        ]
        tos_urls = await asyncio.gather(*upload_tasks, return_exceptions=True)
        t_upload = time.monotonic() - t0

        content_blocks: list[dict[str, Any]] = []
        result_lines: list[str] = []
        failed = 0

        for i, tos_url in enumerate(tos_urls):
            if isinstance(tos_url, Exception) or not tos_url:
                failed += 1
                logger.warning(f"图片 {i} 上传 TOS 失败: {tos_url if isinstance(tos_url, Exception) else 'empty'}")
                continue

            if registry:
                filename = await registry.register(tos_url)
                result_lines.append(f"@{filename}")
                content_blocks.append({"type": "text", "text": f"@{filename}:"})
                content_blocks.append({"type": "image_url", "image_url": {"url": tos_url}})

        t_total = time.monotonic() - t_start
        IMAGE_SEARCH_DURATION.labels(step="upload_pipeline").observe(t_upload)
        IMAGE_SEARCH_UPLOAD_RESULTS.labels(status="success").inc(len(result_lines))
        IMAGE_SEARCH_UPLOAD_RESULTS.labels(status="failed").inc(failed)
        logger.info(
            "search_images done: query=%r search=%.2fs upload=%.2fs total=%.2fs "
            "results=%d/%d failed=%d",
            query, t_search, t_upload, t_total,
            len(result_lines), len(upload_tasks), failed,
        )

        if not content_blocks:
            IMAGE_SEARCH_TOTAL.labels(status="upload_failed").inc()
            return "图片搜索成功但上传失败，请稍后重试"

        # Prepend summary text
        IMAGE_SEARCH_TOTAL.labels(status="success").inc()
        summary = f"搜索到 {len(result_lines)} 张图片: {', '.join(result_lines)}"
        content_blocks.insert(0, {"type": "text", "text": summary})

        return content_blocks

    except httpx.TimeoutException:
        IMAGE_SEARCH_TOTAL.labels(status="timeout").inc()
        logger.error("Timeout during image search")
        return "图片搜索超时"
    except httpx.HTTPStatusError as e:
        IMAGE_SEARCH_TOTAL.labels(status="http_error").inc()
        logger.error(f"HTTP error during image search: {e}")
        return f"图片搜索失败: HTTP {e.response.status_code}"
    except Exception as e:
        IMAGE_SEARCH_TOTAL.labels(status="error").inc()
        logger.error(f"Unexpected error during image search: {e}")
        return f"图片搜索失败: {str(e)}"
