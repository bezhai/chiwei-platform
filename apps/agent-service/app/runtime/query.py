"""Generic read-side query builder for Data rows.

``query(T)`` returns a chainable ``Query`` that produces SQL against the
Data class's backing table. Minimal on purpose:

  - :meth:`Query.where` — equality filters joined with ``AND``
  - :meth:`Query.limit` — ``LIMIT N``
  - :meth:`Query.order_by_asc` / :meth:`Query.order_by_desc` — single-column ordering
  - :meth:`Query.all_versions` — for Versioned Data, return every row instead
    of collapsing to the latest version per key
  - :meth:`Query.all` — materialize to ``list[T]``

For Versioned Data without ``all_versions()``, the builder emits
``DISTINCT ON (keys) ... ORDER BY keys, ver DESC`` so each key collapses to
its latest row — the same semantics as :func:`select_latest`, just expressed
via the builder.

Intentionally not supported: JOINs, aggregations, OR/IN filters, raw SQL.
Callers who need more reach should drop to ``get_session()`` + ``text()``
directly rather than grow this builder into a half-baked ORM.
"""

from __future__ import annotations

from sqlalchemy import text

from app.data.session import get_session
from app.runtime.data import Data, key_fields, version_field
from app.runtime.migrator import _table_name


class Query:
    """Chainable query builder for a single Data class.

    Do not instantiate directly; use :func:`query`.
    """

    def __init__(self, cls: type[Data]) -> None:
        self.cls = cls
        self._where: dict[str, object] = {}
        self._limit: int | None = None
        self._order: tuple[str, bool] | None = None  # (column, desc?)
        self._all_versions: bool = False

    def where(self, **kv: object) -> "Query":
        self._where.update(kv)
        return self

    def limit(self, n: int) -> "Query":
        self._limit = n
        return self

    def order_by_desc(self, col: str) -> "Query":
        self._order = (col, True)
        return self

    def order_by_asc(self, col: str) -> "Query":
        self._order = (col, False)
        return self

    def all_versions(self) -> "Query":
        self._all_versions = True
        return self

    async def all(self) -> list[Data]:
        table = _table_name(self.cls)
        keys = key_fields(self.cls)
        ver = version_field(self.cls)
        where_sql = " AND ".join(f"{k} = :{k}" for k in self._where) or "TRUE"

        if ver and not self._all_versions:
            base = (
                f"SELECT DISTINCT ON ({', '.join(keys)}) * FROM {table} "
                f"WHERE {where_sql} ORDER BY {', '.join(keys)}, {ver} DESC"
            )
        else:
            base = f"SELECT * FROM {table} WHERE {where_sql}"

        if self._order:
            col, desc = self._order
            base += f" ORDER BY {col} {'DESC' if desc else 'ASC'}"
        if self._limit is not None:
            base += f" LIMIT {self._limit}"

        async with get_session() as s:
            r = await s.execute(text(base), self._where)
            return [
                self.cls(**{k: row[k] for k in self.cls.model_fields})
                for row in r.mappings().all()
            ]


def query(cls: type[Data]) -> Query:
    """Start a query for ``cls``. Chain ``.where(...).limit(...).all()``."""
    return Query(cls)
