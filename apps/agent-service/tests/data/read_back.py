"""Reading back what a test wrote to a Data table.

:mod:`app.data.persist` reads only the latest version of a key; no production code reads a key's
whole history. Tests that check what was written (a version chain, the single row an idempotent
insert kept) read it here, the way every other read goes: :func:`app.data.persist.fetch_all` and
:func:`app.data.persist.from_row`, on the database the ``test_db`` fixture points storage at.
"""

from __future__ import annotations

from typing import Any

from app.data.persist import fetch_all, from_row
from app.data.schema import Data, key_fields, table_name, version_field


async def all_versions(cls: type[Data], keys_values: dict[str, Any]) -> list[Data]:
    """Every row of ``cls`` with these Key values, oldest version first (by ``created_at`` when
    ``cls`` declares no ``Version``)."""
    where = " AND ".join(f"{k} = :{k}" for k in key_fields(cls))
    order = version_field(cls) or "created_at"
    sql = f"SELECT * FROM {table_name(cls)} WHERE {where} ORDER BY {order}"
    return [from_row(cls, row) for row in await fetch_all(sql, **keys_values)]
