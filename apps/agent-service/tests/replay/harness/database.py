"""The database: a real Postgres (testcontainers, the same ``test_db`` the other suites use), with
the full schema both apps run on, and two recordings per step.

* **Write transactions.** SQLAlchemy engine events on the test engine record every write
  statement (INSERT / UPDATE / DELETE, by table) and which transaction it ran in. When a
  transaction that wrote something ends, one entry goes onto the step's effects timeline:
  ``{"db": "commit" | "rollback", "writes": [...]}``. Transactions that only read are left out.
  This is where "which tables are written, and where the transaction boundaries are" comes
  from: the writes in one entry committed together, and the entry's place in the timeline says
  what happened before and after it.
* **Row changes.** Every table in ``public`` is read before and after the step; the baseline
  gets the rows added and removed (and, for tables with a primary key, the columns changed).

Values the database itself fills in are masked: columns whose default is the server clock
(``now()``, ``clock_timestamp()``) become ``<db-clock>``, serial / identity columns ``<serial>``,
and ``dedup_hash`` (a framework hash of the row's Key columns, which are compared verbatim)
``<dedup>``. Timestamps are shown in CST; JSON stored as text is shown parsed.

Commit failure is injected here too (:meth:`WriteLog.fail_commits`): a transaction whose writes
a predicate accepts fails at COMMIT with the error the driver would raise, and is rolled back.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError

from app.infra.cst_time import CST

_DB_CLOCK = re.compile(r"now\(\)|clock_timestamp\(\)|current_timestamp", re.IGNORECASE)
# A statement-level write and its table. ``ON CONFLICT ... DO UPDATE`` and ``FOR UPDATE`` are
# not writes of their own (the statement is matched on whitespace-collapsed text).
_WRITE = re.compile(
    r"\b(INSERT INTO|DELETE FROM|(?<!DO )(?<!FOR )UPDATE) (?:ONLY )?(\"?[\w.]+\"?)",
    re.IGNORECASE,
)
# Transaction structure recorded alongside the writes; a transaction made only of these wrote
# nothing.
_STRUCTURE = {"SAVEPOINT", "ROLLBACK TO SAVEPOINT", "RELEASE SAVEPOINT"}


async def create_schema(engine) -> None:
    """Every table either app runs on, the way a coe lane builds them at startup.

    * the SQLAlchemy models (``ensure_business_schema``),
    * every ``Data`` class defined under ``app`` (the runtime migrator; both apps' wiring is
      imported so all of them are defined),
    * the runtime's own tables (inflight, dlq audit),
    * the two tables channel-server owns that her phone reads (``bot_config``,
      ``common_bot_presence``; same DDL as ``tests/living``).
    """
    import app.wiring  # noqa: F401  (defines agent-service's Data classes)
    import app.world.wiring  # noqa: F401  (defines world's Data classes)
    from app.data.models import Base
    from app.runtime.data import DATA_REGISTRY
    from app.runtime.dlq_audit import RUNTIME_DLQ_AUDIT_DDL
    from app.runtime.inflight import RUNTIME_INFLIGHT_DDL
    from app.runtime.migrator import plan_migration
    from tests.living.conftest import _BOT_CONFIG_DDL, _BOT_PRESENCE_DDL

    data_classes = sorted(
        (c for c in DATA_REGISTRY if c.__module__.startswith("app.")),
        key=lambda c: (c.__module__, c.__qualname__),
    )
    plan = plan_migration(data_classes, existing_schema={})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for stmt in plan.stmts:
            await conn.execute(text(stmt.sql))
        for ddl in (*RUNTIME_INFLIGHT_DDL, *RUNTIME_DLQ_AUDIT_DDL):
            await conn.execute(text(ddl))
        await conn.execute(text(_BOT_CONFIG_DDL))
        await conn.execute(text(_BOT_PRESENCE_DDL))


def _writes_in(statement: str) -> list[str]:
    flat = " ".join(statement.split())
    head = flat.split(" ", 1)[0].upper() if flat else ""
    if head in {"CREATE", "ALTER", "DROP", "TRUNCATE"}:
        return [f"DDL {head}"]
    if head not in {"INSERT", "UPDATE", "DELETE", "WITH"}:
        return []
    return [
        f"{op.split()[0].upper()} {table.strip(chr(34)).split('.')[-1]}"
        for op, table in _WRITE.findall(flat)
    ]


def _wrote(writes: list[str]) -> bool:
    return any(w not in _STRUCTURE for w in writes)


class WriteLog:
    """Write statements grouped by the transaction they ran in, while :attr:`active`; each
    transaction that wrote goes onto ``effects`` when it ends."""

    def __init__(self, engine, effects) -> None:
        self._engine = engine.sync_engine
        self.active = False
        self._open: dict[int, dict[str, Any]] = {}
        self._effects = effects
        self._failing: list[list] = []
        self._listeners = [
            ("begin", self._begin),
            ("commit", self._commit),
            ("rollback", self._rollback),
            ("savepoint", self._savepoint),
            ("rollback_savepoint", self._rollback_savepoint),
            ("release_savepoint", self._release_savepoint),
            ("before_cursor_execute", self._execute),
        ]
        for name, fn in self._listeners:
            event.listen(self._engine, name, fn)

    def close(self) -> None:
        for name, fn in self._listeners:
            event.remove(self._engine, name, fn)

    def flush_open(self) -> None:
        """Transactions still open when a step ends go onto the timeline as ``open``."""
        for tx in self._open.values():
            if self.active and _wrote(tx["writes"]):
                self._effects.append({"db": "open", "writes": tx["writes"]})
        self._open.clear()

    def _tx(self, conn) -> dict[str, Any]:
        return self._open.setdefault(id(conn), {"writes": []})

    def _begin(self, conn) -> None:
        self._open[id(conn)] = {"writes": []}

    def _end(self, conn, how: str) -> None:
        tx = self._open.pop(id(conn), None)
        if self.active and tx is not None and _wrote(tx["writes"]):
            self._effects.append({"db": how, "writes": tx["writes"]})

    def fail_commits(self, matches, *, times: int = 1) -> None:
        """The next ``times`` write transactions whose writes ``matches`` accepts fail at COMMIT
        (``OperationalError``, as the driver raises it) and are rolled back."""
        self._failing.append([matches, times])

    def _commit(self, conn) -> None:
        tx = self._open.get(id(conn))
        if self.active and tx is not None and _wrote(tx["writes"]):
            for rule in self._failing:
                if rule[1] > 0 and rule[0](tx["writes"]):
                    rule[1] -= 1
                    del self._open[id(conn)]
                    self._effects.append(
                        {"db": "commit failed (injected)", "writes": tx["writes"]}
                    )
                    raise OperationalError(
                        "COMMIT", {}, ConnectionError("replay: injected commit failure")
                    )
        self._end(conn, "commit")

    def _rollback(self, conn) -> None:
        self._end(conn, "rollback")

    def _mark(self, conn, what: str) -> None:
        if self.active:
            self._tx(conn)["writes"].append(what)

    def _savepoint(self, conn, name) -> None:
        self._mark(conn, "SAVEPOINT")

    def _rollback_savepoint(self, conn, name, context) -> None:
        self._mark(conn, "ROLLBACK TO SAVEPOINT")

    def _release_savepoint(self, conn, name, context) -> None:
        self._mark(conn, "RELEASE SAVEPOINT")

    def _execute(
        self, conn, cursor, statement, parameters, context, executemany
    ) -> None:
        if not self.active:
            return
        self._effects.alive()
        for write in _writes_in(statement):
            if executemany and isinstance(parameters, (list, tuple)):
                write = f"{write} ×{len(parameters)}"
            self._tx(conn)["writes"].append(write)


# --------------------------------------------------------------------------- row snapshots


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(CST).isoformat() if value.tzinfo else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if isinstance(value, (bytes, memoryview)):
        return f"<bytes:{len(bytes(value))}>"
    if isinstance(value, str) and value[:1] in "[{":
        try:
            return json.loads(value)
        except ValueError:
            return value
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


class Tables:
    """Reads every table in ``public`` with the masks applied."""

    def __init__(self, engine) -> None:
        self._engine = engine

    async def read(
        self,
    ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[str]]]:
        async with self._engine.connect() as conn:
            columns = (
                await conn.execute(
                    text(
                        "SELECT table_name, column_name, column_default, is_identity "
                        "FROM information_schema.columns WHERE table_schema = 'public' "
                        "ORDER BY table_name, ordinal_position"
                    )
                )
            ).all()
            primary = (
                await conn.execute(
                    text(
                        "SELECT k.table_name, k.column_name FROM "
                        "information_schema.table_constraints c JOIN "
                        "information_schema.key_column_usage k ON "
                        "c.constraint_name = k.constraint_name AND c.table_name = k.table_name "
                        "WHERE c.table_schema = 'public' AND c.constraint_type = 'PRIMARY KEY' "
                        "ORDER BY k.table_name, k.ordinal_position"
                    )
                )
            ).all()
            masks: dict[str, dict[str, str]] = {}
            for table, column, default, identity in columns:
                mask = None
                if column == "dedup_hash":
                    mask = "<dedup>"
                elif identity == "YES" or (default and "nextval(" in default):
                    mask = "<serial>"
                elif default and _DB_CLOCK.search(default):
                    mask = "<db-clock>"
                masks.setdefault(table, {})
                if mask:
                    masks[table][column] = mask
            keys: dict[str, list[str]] = {}
            for table, column in primary:
                if column not in masks.get(table, {}):
                    keys.setdefault(table, []).append(column)
            rows: dict[str, list[dict[str, Any]]] = {}
            for table in sorted(masks):
                result = await conn.execute(text(f'SELECT * FROM "{table}"'))
                rows[table] = [
                    {
                        column: masks[table].get(column, _plain(value))
                        if value is not None
                        else None
                        for column, value in sorted(row.items())
                    }
                    for row in result.mappings().all()
                ]
            return rows, keys


def _canonical(row: dict[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True)


def row_changes(
    before: dict[str, list[dict[str, Any]]],
    after: dict[str, list[dict[str, Any]]],
    keys: dict[str, list[str]],
) -> dict[str, dict[str, list[Any]]]:
    """Per table, the rows the step added and removed; updates paired by primary key."""
    changes: dict[str, dict[str, list[Any]]] = {}
    for table in sorted(set(before) | set(after)):
        old = Counter(_canonical(r) for r in before.get(table, []))
        new = Counter(_canonical(r) for r in after.get(table, []))
        added = [json.loads(r) for r in sorted((new - old).elements())]
        removed = [json.loads(r) for r in sorted((old - new).elements())]
        changed = []
        key = keys.get(table)
        if key:
            for gone in list(removed):
                match = next(
                    (a for a in added if all(a.get(k) == gone.get(k) for k in key)),
                    None,
                )
                if match is None:
                    continue
                removed.remove(gone)
                added.remove(match)
                differs = [
                    c
                    for c in sorted(set(gone) | set(match))
                    if gone.get(c) != match.get(c)
                ]
                changed.append(
                    {
                        "key": {k: gone.get(k) for k in key},
                        "before": {c: gone.get(c) for c in differs},
                        "after": {c: match.get(c) for c in differs},
                    }
                )
        entry: dict[str, list[Any]] = {}
        if added:
            entry["added"] = added
        if changed:
            entry["changed"] = changed
        if removed:
            entry["removed"] = removed
        if entry:
            changes[table] = entry
    return changes
