"""Model provider / model mapping queries.

Operates on tables: ``ModelProvider``, ``ModelMapping``.
"""
from __future__ import annotations

from sqlalchemy.future import select

from app.data.models import ModelMapping, ModelProvider
from app.runtime.db import auto_tx, current_session

__all__ = [
    "parse_model_id",
    "find_model_mapping",
    "find_provider_by_name",
]


def parse_model_id(model_id: str) -> tuple[str, str]:
    """Parse ``"provider:model"`` into ``(provider_name, model_name)``.

    Falls back to ``"302.ai"`` as default provider when no colon present.
    """
    if ":" in model_id:
        provider_name, model_name = model_id.split(":", 1)
        return provider_name.strip(), model_name.strip()
    return "302.ai", model_id.strip()


async def find_model_mapping(alias: str) -> ModelMapping | None:
    """Look up a model mapping by alias."""
    async with auto_tx():
        result = await current_session().execute(
            select(ModelMapping).where(ModelMapping.alias == alias)
        )
        return result.scalar_one_or_none()


async def find_provider_by_name(name: str) -> ModelProvider | None:
    """Look up a model provider by name."""
    async with auto_tx():
        result = await current_session().execute(
            select(ModelProvider).where(ModelProvider.name == name)
        )
        return result.scalar_one_or_none()
