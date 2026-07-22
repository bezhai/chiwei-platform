"""Trusted identity-label queries."""

from __future__ import annotations

from sqlalchemy.future import select

from app.data.models import CommonUser
from app.runtime.db import auto_tx, current_session

__all__ = ["find_owner_common_user_ids"]


async def find_owner_common_user_ids() -> set[str]:
    """Return every common_user_id explicitly marked as an owner."""
    async with auto_tx():
        result = await current_session().execute(
            select(CommonUser.common_user_id).where(CommonUser.is_owner.is_(True))
        )
        return {str(common_user_id) for common_user_id in result.scalars().all()}
