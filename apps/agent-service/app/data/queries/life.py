"""Typed read queries for life page and persona version chains."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from sqlalchemy import text

from app.runtime.data import Data
from app.runtime.db import auto_tx, current_session
from app.runtime.migrator import _table_name

__all__ = [
    "day_page_exists_for_date",
    "find_day_page_before",
    "find_latest_day_pages",
    "find_latest_relationship_pages",
    "find_latest_persona_review_written_at",
]


PageT = TypeVar("PageT", bound=Data)


def _from_row(data_type: type[PageT], row: Mapping[str, Any]) -> PageT:
    return data_type(**{name: row[name] for name in data_type.model_fields})


async def day_page_exists_for_date(
    data_type: type[PageT],
    *,
    lane: str,
    persona_id: str,
    date: str,
) -> bool:
    """Return whether the exact lane/persona/living-day chain has any row."""
    sql = (
        f"SELECT 1 FROM {_table_name(data_type)} "
        f"WHERE lane = :lane AND persona_id = :persona_id AND date = :date "
        f"LIMIT 1"
    )
    async with auto_tx():
        result = await current_session().execute(
            text(sql), {"lane": lane, "persona_id": persona_id, "date": date}
        )
        return result.first() is not None


async def find_day_page_before(
    data_type: type[PageT],
    *,
    lane: str,
    persona_id: str,
    before_date: str,
) -> PageT | None:
    """Return the newest version on the latest day strictly before the bound."""
    sql = (
        f"SELECT * FROM {_table_name(data_type)} "
        f"WHERE lane = :lane AND persona_id = :persona_id AND date < :before_date "
        f"ORDER BY date DESC, version DESC LIMIT 1"
    )
    async with auto_tx():
        result = await current_session().execute(
            text(sql),
            {"lane": lane, "persona_id": persona_id, "before_date": before_date},
        )
        row = result.mappings().first()
        return _from_row(data_type, row) if row is not None else None


async def find_latest_day_pages(
    data_type: type[PageT],
    *,
    lane: str,
    persona_id: str,
) -> list[PageT]:
    """Return each living day's latest version in ascending date order."""
    sql = (
        f"SELECT DISTINCT ON (date) * FROM {_table_name(data_type)} "
        f"WHERE lane = :lane AND persona_id = :persona_id "
        f"ORDER BY date ASC, version DESC"
    )
    async with auto_tx():
        result = await current_session().execute(
            text(sql), {"lane": lane, "persona_id": persona_id}
        )
        return [_from_row(data_type, row) for row in result.mappings()]


async def find_latest_relationship_pages(
    data_type: type[PageT],
    *,
    lane: str,
    persona_id: str,
) -> list[PageT]:
    """Return each other user's latest relationship page in user-id order."""
    sql = (
        f"SELECT DISTINCT ON (other_user_id) * "
        f"FROM {_table_name(data_type)} "
        f"WHERE lane = :lane AND persona_id = :persona_id "
        f"ORDER BY other_user_id ASC, version DESC"
    )
    async with auto_tx():
        result = await current_session().execute(
            text(sql), {"lane": lane, "persona_id": persona_id}
        )
        return [_from_row(data_type, row) for row in result.mappings()]


async def find_latest_persona_review_written_at(
    data_type: type[PageT],
    *,
    lane: str,
    persona_id: str,
) -> str | None:
    """Return the newest review-source version's evidence cursor."""
    sql = (
        f"SELECT written_at FROM {_table_name(data_type)} "
        f"WHERE lane = :lane AND persona_id = :persona_id "
        f"AND source = 'review' ORDER BY version DESC LIMIT 1"
    )
    async with auto_tx():
        result = await current_session().execute(
            text(sql), {"lane": lane, "persona_id": persona_id}
        )
        return result.scalar_one_or_none()
