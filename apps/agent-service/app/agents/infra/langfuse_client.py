"""Langfuse 集成

惰性初始化单例客户端 + prompt 缓存（SDK 原生 cache_ttl_seconds）
泳道支持：非 prod 泳道自动尝试 label=lane，失败 fallback production
"""

import logging

from langfuse import Langfuse

from app.config import settings
from app.utils.middlewares.trace import get_lane

_client: Langfuse | None = None

_PROMPT_CACHE_TTL_SECONDS: int = 10

logger = logging.getLogger(__name__)


def get_client() -> Langfuse:
    """获取 Langfuse 客户端（惰性单例）"""
    global _client
    if _client is None:
        _client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
    return _client


def get_prompt(
    prompt_id: str,
    label: str | None = None,
    cache_ttl_seconds: int = _PROMPT_CACHE_TTL_SECONDS,
):
    """获取 Langfuse prompt（带 SDK 原生缓存 + 泳道路由）

    非 prod 泳道时先尝试 label=lane，找不到则 fallback production。
    """
    lane = get_lane() or settings.lane
    effective_label = label
    if not effective_label and lane and lane != "prod":
        try:
            return get_client().get_prompt(
                prompt_id, label=lane, cache_ttl_seconds=cache_ttl_seconds
            )
        except Exception:
            logger.debug("prompt %s 无泳道 label=%s，fallback production", prompt_id, lane)

    return get_client().get_prompt(
        prompt_id, label=effective_label, cache_ttl_seconds=cache_ttl_seconds
    )
