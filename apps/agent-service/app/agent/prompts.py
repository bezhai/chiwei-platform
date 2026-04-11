"""Langfuse prompt management.

Thin wrapper around the Langfuse SDK providing:
  - ``get_prompt``   — fetch a cached Langfuse prompt object (with lane routing)
  - ``compile_prompt`` — fetch + compile with template variables
"""

from __future__ import annotations

import logging
from typing import Any

from langfuse import Langfuse

from app.api.middleware import get_lane
from app.infra.config import settings

logger = logging.getLogger(__name__)

_client: Langfuse | None = None
_PROMPT_CACHE_TTL_SECONDS: int = 10


def _get_client() -> Langfuse:
    """Lazily initialise and return the Langfuse singleton client."""
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
    *,
    label: str | None = None,
    cache_ttl_seconds: int = _PROMPT_CACHE_TTL_SECONDS,
) -> Any:
    """Fetch a Langfuse prompt with SDK-level caching and lane routing.

    For non-prod lanes, first attempts ``label=lane``; on failure, falls back
    to the production label.
    """
    lane = get_lane() or settings.lane
    effective_label = label

    if not effective_label and lane and lane != "prod":
        try:
            return _get_client().get_prompt(
                prompt_id, label=lane, cache_ttl_seconds=cache_ttl_seconds
            )
        except Exception:
            logger.debug(
                "prompt %s has no lane label=%s, fallback to production",
                prompt_id,
                lane,
            )

    return _get_client().get_prompt(
        prompt_id, label=effective_label, cache_ttl_seconds=cache_ttl_seconds
    )


def compile_prompt(prompt_id: str, **variables: Any) -> str:
    """Fetch a Langfuse prompt and compile it with *variables*.

    Convenience shortcut for ``get_prompt(id).compile(**vars)``.
    """
    prompt = get_prompt(prompt_id)
    return prompt.compile(**variables)
