"""Model info resolution — resolve a model_id to validated provider config.

Responsibilities:
  - TTL cache for DB lookups (5 min, asyncio-safe without locks)
  - resolve_model_info: validate provider config (active / required fields)

Construction of the actual client lives in ``app.agent.client`` (the neutral
``ModelClient`` + per-provider adapters). This module is the shared resolution
seam: ``client`` / ``embedding`` / ``image_gen`` all call ``resolve_model_info``.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.runtime.db import tx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TTL cache (asyncio single-threaded, no lock needed)
# ---------------------------------------------------------------------------

_CACHE_TTL_SECONDS: int = 300  # 5 minutes
_SENTINEL = object()

# { model_id: (value, expire_at) }
_model_info_cache: dict[str, tuple[Any, float]] = {}


def clear_model_info_cache() -> None:
    """Clear the model info cache (for tests and admin endpoints)."""
    _model_info_cache.clear()


# ---------------------------------------------------------------------------
# DB lookup (with TTL cache)
# ---------------------------------------------------------------------------


async def _get_model_and_provider_info(model_id: str) -> dict[str, Any] | None:
    """Resolve *model_id* to provider config via DB, with TTL cache.

    Lookup strategy:
      1. Try ModelMapping by alias.
      2. Fall back to parsing ``"provider:model"`` (default provider ``302.ai``).
      3. Query ModelProvider by name (fall back to ``302.ai`` if missing).

    Cache policy:
      - Hit and fresh -> return cached value.
      - Miss or stale -> query DB -> cache (including None, to prevent stampede).
      - DB exception -> propagate to caller (no cache write — next call retries).
        Per dataflow contract §4.6: nodes do not log+raise as a courtesy; the
        wire-level on_error decides DLQ / review / swallow_and_log. The caller
        (resolve_model_info) wraps this in ModelBuildError, preserving the
        original exception via __cause__.
    """
    now = time.monotonic()

    cached = _model_info_cache.get(model_id, _SENTINEL)
    if cached is not _SENTINEL:
        value, expire_at = cached  # type: ignore[misc]
        if now < expire_at:
            return dict(value) if value is not None else None

    from app.data.queries import (
        find_model_mapping,
        find_provider_by_name,
        parse_model_id,
    )

    async with tx():
        mapping = await find_model_mapping(model_id)

        if mapping:
            provider_name = mapping.provider_name
            actual_model_name = mapping.real_model_name
        else:
            provider_name, actual_model_name = parse_model_id(model_id)

        provider = await find_provider_by_name(provider_name)

        if not provider:
            provider = await find_provider_by_name("302.ai")

        if not provider:
            _model_info_cache[model_id] = (None, now + _CACHE_TTL_SECONDS)
            return None

        result: dict[str, Any] = {
            "model_name": actual_model_name,
            "api_key": provider.api_key,
            "base_url": provider.base_url,
            "is_active": provider.is_active,
            "client_type": provider.client_type or "openai",
            "use_proxy": provider.use_proxy,
        }

    _model_info_cache[model_id] = (result, now + _CACHE_TTL_SECONDS)
    return dict(result)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ModelBuildError(Exception):
    """Raised when model construction fails."""

    def __init__(self, model_id: str, detail: str):
        self.model_id = model_id
        super().__init__(f"[{model_id}] {detail}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def resolve_model_info(
    model_id: str,
    *,
    required_fields: tuple[str, ...] = ("api_key", "model_name"),
) -> dict[str, Any]:
    """Resolve model_id to validated provider info.

    Shared by the model client and embedding/image_gen modules.
    Raises ModelBuildError on missing / inactive / incomplete config.
    """
    try:
        info = await _get_model_and_provider_info(model_id)
    except Exception as exc:
        raise ModelBuildError(model_id, "model info lookup failed") from exc
    if info is None:
        raise ModelBuildError(model_id, "model info not found")
    if not info.get("is_active", True):
        raise ModelBuildError(model_id, "model is disabled")
    missing = [f for f in required_fields if not info.get(f)]
    if missing:
        raise ModelBuildError(model_id, f"missing fields: {', '.join(missing)}")
    return info
