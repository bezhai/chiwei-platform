"""Schema migrator: pydantic Data classes -> additive-only PostgreSQL DDL.

Generates ``CREATE TABLE IF NOT EXISTS`` for new Data classes and
``ALTER TABLE ADD COLUMN`` for additive changes on existing tables.

Breaking changes (column drop, type change, table drop) are *not* supported
and raise :class:`MigrationError`. Those require an explicit, human-written
migration script — the automatic migrator refuses to emit destructive DDL.

``AdminOnly`` Data classes are skipped entirely: their storage is owned by
platform admin tooling, not business code.

Data classes with ``Meta.existing_table`` are in *adoption mode*: the
migrator does not own their schema and emits no DDL (no CREATE, no ALTER,
no drop-check) regardless of what the existing schema looks like.
"""

from __future__ import annotations

import datetime
import types
import typing
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


def _pg_type_for_pytype(t: object) -> str:
    """Map a bare Python type (already Union-unwrapped) to a PG column type.

    Unwraps ``Optional[X]`` (``Union[X, None]``) by recursing on ``X``; a
    non-Optional Union cannot be mapped to a single column type and raises
    :class:`MigrationError`.
    """
    origin = get_origin(t)
    if origin is typing.Union or origin is types.UnionType:
        args = [a for a in get_args(t) if a is not type(None)]
        if len(args) == 1:
            return _pg_type_for_pytype(args[0])
        raise MigrationError(
            f"cannot map Union type {t!r} to postgres column type; "
            f"use a concrete type or Optional[X]"
        )
    if t in _PY_TO_PG:
        return _PY_TO_PG[t]
    if origin in _PY_TO_PG:
        return _PY_TO_PG[origin]
    if t is datetime.datetime:
        return "TIMESTAMPTZ"
    if t is datetime.date:
        return "DATE"
    # Unknown user type with a generic origin — preserve structure as JSONB.
    if origin is not None:
        return "JSONB"
    return "TEXT"


def _pg_type(fi: FieldInfo) -> str:
    """Map a pydantic FieldInfo annotation to a PostgreSQL column type.

    Pydantic v2 already strips ``Annotated`` metadata before exposing the
    bare runtime type on ``FieldInfo.annotation``. ``Optional[X]`` is
    unwrapped so nullable fields get the correct scalar column type;
    non-Optional unions are rejected.
    """
    return _pg_type_for_pytype(fi.annotation)


# Columns owned by the append-only log convention, not declared on the
# pydantic class. Migrator is not allowed to drop these even if they are
# absent from the Data class, and their runtime-reported pg types are not
# compared against the declared type (they are managed here).
_RESERVED_COLUMNS: frozenset[str] = frozenset(
    {"id", "created_at", "updated_at", "dedup_hash"}
)


# Postgres reports a canonical name per type that doesn't always match the
# DDL keyword the migrator emits (e.g. ``TIMESTAMPTZ`` ↔ ``timestamp with
# time zone``). Equivalence groups here keep the type-mismatch check from
# firing on mere naming differences.
_PG_TYPE_ALIASES: dict[str, set[str]] = {
    "TEXT": {"TEXT", "VARCHAR", "CHARACTER VARYING"},
    "BIGINT": {"BIGINT", "INT8"},
    "INTEGER": {"INTEGER", "INT", "INT4"},
    "DOUBLE PRECISION": {"DOUBLE PRECISION", "FLOAT8"},
    "BOOLEAN": {"BOOLEAN", "BOOL"},
    "BYTEA": {"BYTEA"},
    "JSONB": {"JSONB"},
    "TIMESTAMPTZ": {"TIMESTAMPTZ", "TIMESTAMP WITH TIME ZONE"},
}


def _pg_type_equivalent(declared: str, actual: str) -> bool:
    """Check whether two pg type strings name the same underlying type.

    ``declared`` may contain a trailing ``DEFAULT ...`` clause (the
    migrator composes column specs with defaults). ``actual`` is the
    introspected column type as reported by the database.
    """
    a = declared.split(" DEFAULT ")[0].strip().upper()
    b = actual.strip().upper()
    if a == b:
        return True
    for aliases in _PG_TYPE_ALIASES.values():
        if a in aliases and b in aliases:
            return True
    return False


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
            (column drop, column type change, etc.).
    """
    stmts: list[Stmt] = []

    for cls in data_classes:
        if is_admin_only(cls):
            continue

        # Adoption mode: the Data class declares it is backed by a
        # pre-existing legacy table. The migrator does not own the
        # schema of that table — emit no DDL at all, regardless of what
        # columns ``existing_schema`` reports.
        if getattr(getattr(cls, "Meta", None), "existing_table", None):
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
            for col, desired_typ in desired_cols.items():
                if col in existing_cols:
                    # Reserved columns are runtime-managed; their actual
                    # pg-reported types vary by environment and must not
                    # trigger a mismatch.
                    if col in _RESERVED_COLUMNS:
                        continue
                    if not _pg_type_equivalent(desired_typ, existing_cols[col]):
                        raise MigrationError(
                            f"column {table}.{col} type mismatch: "
                            f"existing={existing_cols[col]!r}, "
                            f"declared={desired_typ!r}; "
                            f"write explicit migration script"
                        )
                    continue
                if " DEFAULT " in desired_typ:
                    base, default = desired_typ.split(" DEFAULT ", 1)
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
                            f"{col} {desired_typ}"
                        )
                    )
            continue

        col_ddl = ", ".join(f"{n} {t}" for n, t in desired_cols.items())
        stmts.append(Stmt(f"CREATE TABLE IF NOT EXISTS {table} ({col_ddl})"))
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
                    f"CREATE INDEX IF NOT EXISTS ix_{table}_key_ver ON {table}({cols})"
                )
            )

    return Plan(stmts=stmts)


async def apply_migration(plan: Plan, conn) -> None:
    """Execute each statement in ``plan`` on an asyncpg-style connection.

    Caller is responsible for wrapping in a transaction.
    """
    for s in plan.stmts:
        await conn.execute(s.sql, *s.params)
