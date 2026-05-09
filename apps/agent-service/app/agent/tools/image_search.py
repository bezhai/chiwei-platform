"""Image search tool — search for images and register them for agent use."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Annotated, Any

from langchain.tools import tool
from langgraph.runtime import get_runtime
from pydantic import Field

from app.agent.context import AgentContext
from app.agent.tools._common import (
    get_or_create_counter,
    get_or_create_histogram,
    tool_error,
    upload_and_register,
)
from app.capabilities.image_search import image_search as _image_search_capability

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

IMAGE_SEARCH_DURATION = get_or_create_histogram(
    "image_search_step_duration_seconds", "Image search step duration", ["step"]
)
IMAGE_SEARCH_TOTAL = get_or_create_counter(
    "image_search_requests_total", "Image search requests", ["status"]
)
IMAGE_SEARCH_UPLOADS = get_or_create_counter(
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
    try:
        t_start = time.monotonic()
        hits = await _image_search_capability(query, count=IMAGE_MAX_RESULTS)
        t_search = time.monotonic() - t_start
        IMAGE_SEARCH_DURATION.labels(step="search_api").observe(t_search)

        if not hits:
            IMAGE_SEARCH_TOTAL.labels(status="empty").inc()
            return "图片搜索服务未配置或未搜索到相关图片"

        # Upload each to TOS and register
        context = get_runtime(AgentContext).context
        registry = context.image_registry

        t0 = time.monotonic()
        upload_tasks = [
            upload_and_register(
                source_type="url",
                data=h.image_url,
                registry=registry,
            )
            for h in hits
            if h.image_url
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

    except Exception as exc:
        IMAGE_SEARCH_TOTAL.labels(status="error").inc()
        return f"图片搜索失败: {exc}"
