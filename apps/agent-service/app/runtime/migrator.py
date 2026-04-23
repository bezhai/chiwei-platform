"""Schema migrator: pydantic Data classes -> additive-only PostgreSQL DDL.

Generates ``CREATE TABLE IF NOT EXISTS`` for new Data classes and
``ALTER TABLE ADD COLUMN`` for additive changes on existing tables.

Breaking changes (column drop, type change, table drop) are *not* supported
and raise :class:`MigrationError`. Those require an explicit, human-written
migration script — the automatic migrator refuses to emit destructive DDL.

``AdminOnly`` Data classes are skipped entirely: their storage is owned by
platform admin tooling, not business code.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import get_args, get_origin

from pydantic.fields import FieldInfo

from app.runtime.data import (
    Data,
    is_admin_only,
    key_fields,
    version_field,
)


class MigrationError(Exception):
    """Raised when the requested schema change is destructive.

    The migrator is additive-only; breaking changes must be written as
    explicit migration scripts rather than inferred from model diffs.
    """


@dataclass(frozen=True)
class Stmt:
    sql: str
    params: tuple = ()


@dataclass
class Plan:
    stmts: list[Stmt] = field(default_factory=list)


_PY_TO_PG: dict[type, str] = {
    str: "TEXT",
    int: "BIGINT",
    float: "DOUBLE PRECISION",
    bool: "BOOLEAN",
    bytes: "BYTEA",
    dict: "JSONB",
    list: "JSONB",
}


def _camel_to_snake(name: str) -> str:
    if not name:
        return name
    chars = [name[0].lower()]
    for c in name[1:]:
        if c.isupper():
            chars.append("_")
            chars.append(c.lower())
        else:
            chars.append(c)
    return "".join(chars)


def _table_name(cls: type[Data]) -> str:
    meta = getattr(cls, "Meta", None)
    existing = getattr(meta, "existing_table", None) if meta else None
    if existing:
        return existing
    return f"data_{_camel_to_snake(cls.__name__)}"


def _pg_type(fi: FieldInfo) -> str:
    """Map a pydantic FieldInfo annotation to a PostgreSQL column type.

    Pydantic v2 already strips ``Annotated`` metadata before exposing the
    bare runtime type on ``FieldInfo.annotation``; the ``get_origin`` path
    below handles generic aliases (``list[str]``, ``dict[str, int]``).
    """
    t = fi.annotation
    if t in _PY_TO_PG:
        return _PY_TO_PG[t]
    origin = get_origin(t)
    if origin in _PY_TO_PG:
        return _PY_TO_PG[origin]
    if t is datetime.datetime:
        return "TIMESTAMPTZ"
    if t is datetime.date:
        return "DATE"
    # Unknown / union / user types — store as JSONB to preserve structure.
    if origin is not None:
        return "JSONB"
    return "TEXT"


# Columns owned by the append-only log convention, not declared on the
# pydantic class. Migrator is not allowed to drop these even if they are
# absent from the Data class.
_RESERVED_COLUMNS: frozenset[str] = frozenset(
    {"id", "created_at", "updated_at", "dedup_hash"}
)


def plan_migration(
    data_classes: list[type[Data]],
    existing_schema: dict[str, dict[str, str]],
) -> Plan:
    """Compare desired Data classes against existing schema; emit DDL.

    Args:
        data_classes: Data subclasses the application intends to persist.
        existing_schema: mapping ``table_name -> {column_name: pg_type}``
            reflecting the current database. Empty dict ``{}`` is treated
            as a fresh database.

    Returns:
        A :class:`Plan` of ordered DDL statements. Apply via
        :func:`apply_migration`.

    Raises:
        MigrationError: if any Data class requires a destructive change
            (column drop, etc.).
    """
    stmts: list[Stmt] = []

    for cls in data_classes:
        if is_admin_only(cls):
            continue

        table = _table_name(cls)
        desired_cols: dict[str, str] = {
            name: _pg_type(fi) for name, fi in cls.model_fields.items()
        }
        # Runtime-maintained columns for durable append-only writes.
        desired_cols["dedup_hash"] = "TEXT"
        desired_cols["created_at"] = "TIMESTAMPTZ DEFAULT now()"

        if table in existing_schema:
            existing_cols = existing_schema[table]
            for col in existing_cols:
                if col in desired_cols or col in _RESERVED_COLUMNS:
                    continue
                raise MigrationError(
                    f"column {table}.{col} dropped from "
                    f"{cls.__name__}; write explicit migration script"
                )
            for col, typ in desired_cols.items():
                if col in existing_cols:
                    continue
                if " DEFAULT " in typ:
                    base, default = typ.split(" DEFAULT ", 1)
                    stmts.append(
                        Stmt(
                            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "
                            f"{col} {base} DEFAULT {default}"
                        )
                    )
                else:
                    stmts.append(
                        Stmt(
                            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "
                            f"{col} {typ}"
                        )
                    )
            continue

        # Table missing. If the class explicitly maps onto an existing
        # table (``Meta.existing_table``), the database owner is elsewhere
        # — do not auto-create.
        meta = getattr(cls, "Meta", None)
        if meta and getattr(meta, "existing_table", None):
            continue

        col_ddl = ", ".join(f"{n} {t}" for n, t in desired_cols.items())
        stmts.append(
            Stmt(f"CREATE TABLE IF NOT EXISTS {table} ({col_ddl})")
        )
        stmts.append(
            Stmt(
                f"CREATE UNIQUE INDEX IF NOT EXISTS ix_{table}_dedup "
                f"ON {table}(dedup_hash)"
            )
        )
        ver = version_field(cls)
        if ver:
            keys = key_fields(cls)
            cols = ", ".join(keys) + f", {ver} DESC"
            stmts.append(
                Stmt(
                    f"CREATE INDEX IF NOT EXISTS ix_{table}_key_ver "
                    f"ON {table}({cols})"
                )
            )

    return Plan(stmts=stmts)


async def apply_migration(plan: Plan, conn) -> None:
    """Execute each statement in ``plan`` on an asyncpg-style connection."""
    for s in plan.stmts:
        await conn.execute(s.sql, *s.params)
