"""Image search tool — search for images and register them for agent use."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Annotated, Any

import httpx
from langchain.tools import tool
from langgraph.runtime import get_runtime
from prometheus_client import Counter, Histogram
from pydantic import Field

from app.agent.context import AgentContext
from app.agent.tools._common import tool_error, upload_and_register
from app.infra.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prometheus metrics (reuse existing collectors to avoid double-registration)
# ---------------------------------------------------------------------------

# prometheus_client has no public API to retrieve an already-registered collector
# by name. _names_to_collectors is the only option; tracked in
# https://github.com/prometheus/client_python/issues/546


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


IMAGE_SEARCH_DURATION = _histogram(
    "image_search_step_duration_seconds", "Image search step duration", ["step"]
)
IMAGE_SEARCH_TOTAL = _counter(
    "image_search_requests_total", "Image search requests", ["status"]
)
IMAGE_SEARCH_UPLOADS = _counter(
    "image_search_upload_results_total", "Image upload outcomes", ["status"]
)

IMAGE_MAX_RESULTS = 5


# =========================================================================
# Public tool
# =========================================================================


@tool
@tool_error("图片搜索失败")
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
        registry = context.image_registry

        t0 = time.monotonic()
        upload_tasks = [
            upload_and_register(
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
