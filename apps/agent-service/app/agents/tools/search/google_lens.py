"""Google Lens 以图搜图工具。"""

import logging
import time
from typing import Annotated, Any, Literal

import httpx
from langchain.tools import tool
from langgraph.runtime import get_runtime
from prometheus_client import Counter, Histogram
from pydantic import Field

from app.agents.core.context import AgentContext
from app.config import settings

logger = logging.getLogger(__name__)

_MAX_MATCHES = 5
_MAX_KNOWLEDGE = 3
_TYPE_TO_SERPAPI = {
    "all": "all",
    "visual_matches": "visual_matches",
    "exact_matches": "exact_matches",
    "about_this_image": "about_this_image",
}

LENS_SEARCH_DURATION = Histogram(
    "google_lens_search_duration_seconds",
    "Google Lens search duration in seconds",
)
LENS_SEARCH_TOTAL = Counter(
    "google_lens_search_requests_total",
    "Total Google Lens search requests",
    ["status"],
)


def _normalize_country(country: str) -> str:
    return country.lower()


def _trim_text(value: str, limit: int = 280) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


async def _resolve_image_input(image: str) -> str:
    """Resolve a user-supplied image input to a public URL."""
    if image.startswith(("http://", "https://")):
        return image

    if image.startswith("@"):
        image = image[1:]

    context = get_runtime(AgentContext).context
    registry = context.media.registry
    if not registry:
        raise ValueError("当前对话没有可用的图片上下文")

    resolved = await registry.resolve(image)
    if not resolved:
        raise ValueError(f"未找到图片: {image}")
    return resolved


def _build_params(
    *,
    image_url: str,
    search_type: Literal["all", "visual_matches", "exact_matches", "about_this_image"],
    q: str | None,
    hl: str,
    country: str,
) -> dict[str, str]:
    params = {
        "engine": "google_lens",
        "url": image_url,
        "type": _TYPE_TO_SERPAPI[search_type],
        "hl": hl,
        "country": _normalize_country(country),
        "api_key": settings.serpapi_api_key or "",
    }
    if q:
        params["q"] = q
    return params


def _build_http_client() -> httpx.AsyncClient:
    client_kwargs: dict[str, Any] = {"timeout": 20}
    if settings.forward_proxy_url:
        client_kwargs["proxy"] = settings.forward_proxy_url
    return httpx.AsyncClient(**client_kwargs)


def _format_matches(items: list[dict[str, Any]], *, label: str) -> list[str]:
    lines: list[str] = []
    for index, item in enumerate(items[:_MAX_MATCHES], start=1):
        title = item.get("title") or item.get("source") or "未命名结果"
        link = item.get("link") or item.get("thumbnail") or ""
        source = item.get("source") or item.get("domain") or ""
        snippet = item.get("snippet") or item.get("price") or ""

        parts = [f"{label} {index}. {title}"]
        if source:
            parts.append(f"来源: {source}")
        if snippet:
            parts.append(_trim_text(str(snippet), 180))
        if link:
            parts.append(link)
        lines.append(" | ".join(parts))
    return lines


def _format_knowledge(items: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for index, item in enumerate(items[:_MAX_KNOWLEDGE], start=1):
        title = item.get("title") or item.get("header") or "背景信息"
        snippet = item.get("snippet") or item.get("description") or ""
        lines.append(f"背景 {index}. {title}" + (f" | {_trim_text(str(snippet), 200)}" if snippet else ""))
    return lines


def _extract_response(data: dict[str, Any]) -> dict[str, Any]:
    visual_matches = data.get("visual_matches", [])
    exact_matches = data.get("exact_matches", [])
    knowledge = data.get("knowledge_graph") or data.get("about_this_image") or []
    if isinstance(knowledge, dict):
        knowledge = [knowledge]

    summary_parts: list[str] = []
    knowledge_lines = _format_knowledge(knowledge)
    visual_lines = _format_matches(visual_matches, label="相似图")
    exact_lines = _format_matches(exact_matches, label="原图")

    if knowledge_lines:
        summary_parts.append("背景信息：\n" + "\n".join(knowledge_lines))
    if visual_lines:
        summary_parts.append("相似图片：\n" + "\n".join(visual_lines))
    if exact_lines:
        summary_parts.append("原图线索：\n" + "\n".join(exact_lines))

    if not summary_parts:
        summary_parts.append("未找到明确的识图结果")

    return {
        "about_this_image": knowledge[:_MAX_KNOWLEDGE],
        "visual_matches": visual_matches[:_MAX_MATCHES],
        "exact_matches": exact_matches[:_MAX_MATCHES],
        "best_summary": "\n\n".join(summary_parts),
    }


@tool
async def search_by_image(
    image: Annotated[
        str,
        Field(description="公开图片 URL，或对话里已出现的图片引用，如 @3.png"),
    ],
    search_type: Annotated[
        Literal["all", "visual_matches", "exact_matches", "about_this_image"],
        Field(description="识图模式：all、visual_matches、exact_matches、about_this_image"),
    ] = "all",
    q: Annotated[
        str | None,
        Field(description="可选补充搜索词，用于收敛结果"),
    ] = None,
    hl: Annotated[
        str,
        Field(description="语言代码，如 zh-CN、en"),
    ] = "zh-CN",
    country: Annotated[
        str,
        Field(description="国家代码，如 cn、us"),
    ] = "cn",
) -> dict[str, Any] | str:
    """Google Lens 以图搜图。适合查原图、相似图、出处和背景信息。"""
    if not settings.serpapi_api_key:
        logger.error("SerpApi Google Lens not configured")
        return "Google Lens 搜图服务未配置"

    start = time.monotonic()
    status = "error"
    try:
        image_url = await _resolve_image_input(image)
        params = _build_params(
            image_url=image_url,
            search_type=search_type,
            q=q,
            hl=hl,
            country=country,
        )

        async with _build_http_client() as client:
            response = await client.get(settings.serpapi_google_lens_host, params=params)
            response.raise_for_status()
            data = response.json()

        status = "ok"
        return _extract_response(data)
    except ValueError as exc:
        status = "bad_input"
        logger.warning("Invalid image input for Google Lens: %s", exc)
        return str(exc)
    except httpx.TimeoutException:
        status = "timeout"
        logger.error("Timeout during Google Lens search")
        return "Google Lens 搜图超时"
    except httpx.HTTPStatusError as exc:
        status = f"http_{exc.response.status_code}"
        logger.error("Google Lens search HTTP error: %s", exc)
        return f"Google Lens 搜图失败: HTTP {exc.response.status_code}"
    except Exception as exc:
        logger.exception("Unexpected Google Lens search error")
        return f"Google Lens 搜图失败: {exc}"
    finally:
        LENS_SEARCH_TOTAL.labels(status=status).inc()
        LENS_SEARCH_DURATION.observe(time.monotonic() - start)
