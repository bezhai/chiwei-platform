"""Jina Reader - 网页转 Markdown 格式"""

import httpx

from app.config import settings


async def read_webpage(url: str) -> str:
    """将网页内容转换为干净的 Markdown 格式。

    使用 Jina Reader API 从 URL 中提取核心内容，去除广告、脚本等干扰元素，
    返回结构化的 Markdown 文本。

    Args:
        url: 要读取的网页 URL，例如 "https://example.com/article"

    Returns:
        Markdown 格式的网页内容文本。
    """
    api_url = f"https://api.302.ai/jina/reader/{url}"

    headers = {}
    if settings.search_api_key:
        headers["Authorization"] = f"Bearer {settings.search_api_key}"

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(api_url, headers=headers)
        response.raise_for_status()

    return response.text
