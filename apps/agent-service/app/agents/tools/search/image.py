"""图片搜索工具"""

import asyncio
import logging
from typing import Annotated, Any

import httpx
from langchain.tools import tool
from langgraph.runtime import get_runtime
from pydantic import Field

from app.agents.core.context import AgentContext
from app.config import settings

logger = logging.getLogger(__name__)

_MAX_RESULTS = 5


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
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()

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

        # Upload each to TOS and register
        from app.clients.image_client import image_client

        context = get_runtime(AgentContext).context
        registry = context.media.registry

        # Upload concurrently (API returns image_url field)
        upload_tasks = [
            image_client.upload_to_tos("url", img.get("image_url") or img.get("url", ""))
            for img in images
            if img.get("image_url") or img.get("url")
        ]
        tos_urls = await asyncio.gather(*upload_tasks, return_exceptions=True)

        content_blocks: list[dict[str, Any]] = []
        result_lines: list[str] = []

        for i, tos_url in enumerate(tos_urls):
            if isinstance(tos_url, Exception) or not tos_url:
                logger.warning(f"图片 {i} 上传 TOS 失败")
                continue

            if registry:
                filename = await registry.register(tos_url)
                result_lines.append(f"@{filename}")
                content_blocks.append({"type": "text", "text": f"@{filename}:"})
                content_blocks.append({"type": "image_url", "image_url": {"url": tos_url}})

        if not content_blocks:
            return "图片搜索成功但上传失败，请稍后重试"

        # Prepend summary text
        summary = f"搜索到 {len(result_lines)} 张图片: {', '.join(result_lines)}"
        content_blocks.insert(0, {"type": "text", "text": summary})

        return content_blocks

    except httpx.TimeoutException:
        logger.error("Timeout during image search")
        return "图片搜索超时"
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error during image search: {e}")
        return f"图片搜索失败: HTTP {e.response.status_code}"
    except Exception as e:
        logger.error(f"Unexpected error during image search: {e}")
        return f"图片搜索失败: {str(e)}"
