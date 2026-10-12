"""Reading back what a test wrote to a Data table.

:mod:`app.data.persist` reads only the latest version of a key; no production code reads a key's
whole history. Tests that check what was written (a version chain, the single row an idempotent
insert kept) read it here, through ``app.data.session``, which the ``test_db`` fixture points at
the test database.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.data.schema import Data, key_fields, table_name, version_field
from app.data.session import get_session


async def all_versions(cls: type[Data], keys_values: dict[str, Any]) -> list[Data]:
    """Every row of ``cls`` with these Key values, oldest version first (by ``created_at`` when
    ``cls`` declares no ``Version``)."""
    where = " AND ".join(f"{k} = :{k}" for k in key_fields(cls))
    order = version_field(cls) or "created_at"
    sql = f"SELECT * FROM {table_name(cls)} WHERE {where} ORDER BY {order}"
    async with get_session() as s:
        rows = (await s.execute(text(sql), keys_values)).mappings().all()
    return [cls(**{k: row[k] for k in cls.model_fields}) for row in rows]
