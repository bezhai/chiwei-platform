"""Model provider / model mapping queries.

Operates on tables: ``ModelProvider``, ``ModelMapping``.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.data.models import ModelMapping, ModelProvider

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


async def find_model_mapping(session: AsyncSession, alias: str) -> ModelMapping | None:
    """Look up a model mapping by alias."""
    result = await session.execute(
        select(ModelMapping).where(ModelMapping.alias == alias)
    )
    return result.scalar_one_or_none()


async def find_provider_by_name(
    session: AsyncSession, name: str
) -> ModelProvider | None:
    """Look up a model provider by name."""
    result = await session.execute(
        select(ModelProvider).where(ModelProvider.name == name)
    )
    return result.scalar_one_or_none()
