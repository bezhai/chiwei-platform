"""图片搜索工具（内部使用，暂不对外暴露）"""

import httpx
from langchain.tools import tool

from app.config import settings
from app.utils.decorators import dict_serialize, log_io


@tool
@log_io
@dict_serialize
async def search_images(
    query: str,
    gl: str = "cn",
    hl: str = "zh-cn",
    num: int = 10,
    page: int | None = None,
) -> list[dict]:
    """Google 图片搜索。

    Args:
        query: 搜索关键词。
        gl: 结果地域代码，默认 "cn"。
        hl: 界面语言代码，默认 "zh-cn"。
        num: 返回结果条数，默认 10。
        page: 页码（1-based），用于翻页。

    Returns:
        图片搜索结果列表。
    """
    url = "https://api.302.ai/serpapi/search"

    params: dict[str, str | int] = {
        "engine": "google_images_light",
        "api_key": settings.search_api_key or "",
        "q": query,
        "hl": hl,
        "gl": gl,
        "num": num,
    }

    if page is not None and page > 1:
        params["ijn"] = page - 1

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        data = response.json()

    return data.get("images_results", [])


@tool
@log_io
@dict_serialize
async def search_by_image(
    image_url: str,
    gl: str = "cn",
    hl: str = "zh-cn",
) -> dict:
    """Google Lens 以图搜图。

    Args:
        image_url: 要搜索的图片 URL。
        gl: 结果地域代码，默认 "cn"。
        hl: 界面语言代码，默认 "zh-cn"。

    Returns:
        包含 visual_matches 和 knowledge_graph 的搜索结果。
    """
    url = "https://api.302.ai/serpapi/search"

    params: dict[str, str | int] = {
        "engine": "google_lens",
        "api_key": settings.search_api_key or "",
        "url": image_url,
        "hl": hl,
        "gl": gl,
    }

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        data = response.json()

    return {
        "visual_matches": data.get("visual_matches", []),
        "knowledge_graph": data.get("knowledge_graph", {}),
    }
