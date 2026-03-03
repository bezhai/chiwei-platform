"""You Search - 网页内容提取"""

import logging

import httpx
from bs4 import BeautifulSoup

from app.config import settings

logger = logging.getLogger(__name__)


async def read_webpage(url: str) -> str:
    """将网页内容转换为干净的 Markdown 格式。

    使用 You Search Contents API 从 URL 中提取核心内容，去除广告、脚本等干扰元素，
    返回结构化的 Markdown 文本。

    Args:
        url: 要读取的网页 URL，例如 "https://example.com/article"

    Returns:
        Markdown 格式的网页内容文本。如果提取失败返回空字符串。
    """
    if not settings.you_search_host or not settings.you_search_api_key:
        logger.error("You Search not configured")
        return ""

    api_url = f"{settings.you_search_host}/v1/contents"

    headers = {
        "X-API-Key": settings.you_search_api_key,
        "Content-Type": "application/json",
    }

    payload = {
        "urls": [url],
        "formats": ["markdown", "html"],
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(api_url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()

        if not data or len(data) == 0:
            return ""

        result = data[0]

        # 优先使用 markdown
        if result.get("markdown"):
            return result["markdown"]

        # fallback 到 html 转文本
        if result.get("html"):
            return _html_to_text(result["html"])

        return ""

    except httpx.TimeoutException:
        logger.error(f"Timeout reading webpage: {url}")
        return ""
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error reading webpage {url}: {e}")
        return ""
    except Exception as e:
        logger.error(f"Unexpected error reading webpage {url}: {e}")
        return ""


def _html_to_text(html: str) -> str:
    """将 HTML 转换为纯文本。

    Args:
        html: HTML 字符串

    Returns:
        纯文本内容
    """
    try:
        soup = BeautifulSoup(html, "html.parser")

        # 移除 script 和 style 标签
        for tag in soup(["script", "style"]):
            tag.decompose()

        # 提取文本
        text = soup.get_text(separator="\n", strip=True)

        # 清理多余空行
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        return "\n".join(lines)

    except Exception as e:
        logger.error(f"Error converting HTML to text: {e}")
        return ""
