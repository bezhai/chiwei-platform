"""Langfuse 集成

惰性初始化单例客户端 + prompt 缓存（SDK 原生 cache_ttl_seconds）
"""

from langfuse import Langfuse

from app.config import settings

_client: Langfuse | None = None

_PROMPT_CACHE_TTL_SECONDS: int = 300  # 5 分钟


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
    """获取 Langfuse prompt（带 SDK 原生缓存）

    Args:
        prompt_id: Prompt 标识
        label: 可选标签（如 production/staging）
        cache_ttl_seconds: 缓存 TTL 秒数，默认 300s（Langfuse SDK >=3.3.4 支持）
    """
    return get_client().get_prompt(
        prompt_id, label=label, cache_ttl_seconds=cache_ttl_seconds
    )
