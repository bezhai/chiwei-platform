"""图片搜索工具（内部使用，暂不对外暴露）"""

import logging

import httpx
from langchain.tools import tool

from app.config import settings
from app.utils.decorators import dict_serialize, log_io

logger = logging.getLogger(__name__)


@tool
@log_io
@dict_serialize
async def search_images(
    query: str,
) -> list[dict]:
    """图片搜索。

    Args:
        query: 搜索关键词。

    Returns:
        图片搜索结果列表。
    """
    if not settings.you_search_host or not settings.you_search_api_key:
        logger.error("You Search not configured")
        return []

    url = f"{settings.you_search_host}/images"

    params = {
        "q": query,
    }

    headers = {
        "X-API-Key": settings.you_search_api_key,
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()

        return data.get("images", [])

    except httpx.TimeoutException:
        logger.error("Timeout during image search")
        return []
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error during image search: {e}")
        return []
    except Exception as e:
        logger.error(f"Unexpected error during image search: {e}")
        return []


@tool
@log_io
@dict_serialize
async def search_by_image(
    image_url: str,
) -> dict:
    """以图搜图（功能不可用）。

    注意：You Search 不提供反向图片搜索功能。

    Args:
        image_url: 要搜索的图片 URL。

    Returns:
        空结果字典。
    """
    logger.warning("Reverse image search is not available with You Search")
    return {
        "visual_matches": [],
        "knowledge_graph": {},
    }
