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

Data classes with ``Meta.transient = True`` are never persisted to pg
(they flow in-process to a non-durable Sink). The
migrator skips them entirely: no CREATE, no ALTER, no drop-check.

Data classes may declare ``Meta.indexes`` — a tuple of column-name tuples —
to get plain secondary indexes (``CREATE INDEX IF NOT EXISTS``, idempotent)
supporting their read shapes (e.g. cursor-window scans over ``created_at``).
Declared indexes are emitted for **both** new and already-existing tables,
so a table created by an earlier deploy gets backfilled on the next migrate.
Index columns must be declared fields or the runtime columns the migrator
itself creates (``created_at`` / ``dedup_hash``); anything else fails the
plan fast (a typo surfacing only at DDL time points far away from the
declaration).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic.fields import FieldInfo

from app.runtime.data import (
    Data,
    is_admin_only,
    key_fields,
    version_field,
)
from app.runtime.naming import to_snake
from app.runtime.schema_types import UnmappablePgType, pg_type_for_annotation


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


def _table_name(cls: type[Data]) -> str:
    meta = getattr(cls, "Meta", None)
    existing = getattr(meta, "existing_table", None) if meta else None
    if existing:
        return existing
    return f"data_{to_snake(cls.__name__)}"


def _pg_type(fi: FieldInfo) -> str:
    """Map a pydantic FieldInfo annotation to a PostgreSQL column type.

    Delegates to the shared :func:`pg_type_for_annotation` judgement (the
    persist write path uses the same one) and re-raises an unmappable type
    as a :class:`MigrationError` so DDL callers see a migration-shaped error.
    """
    try:
        return pg_type_for_annotation(fi.annotation)
    except UnmappablePgType as exc:
        raise MigrationError(str(exc)) from exc


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


def _declared_index_stmts(
    cls: type[Data], table: str, desired_cols: dict[str, str]
) -> list[Stmt]:
    """DDL for the class's ``Meta.indexes`` declarations (idempotent).

    One plain ``CREATE INDEX IF NOT EXISTS ix_{table}_{cols}`` per declared
    column tuple, column order preserved (order is the read shape — a
    cursor-window scan wants keys first, then the cursor columns). Emitted
    unconditionally by :func:`plan_migration` for new *and* existing tables:
    ``IF NOT EXISTS`` makes re-runs free, and existing tables created by an
    earlier deploy must be backfilled — the CREATE-branch-only treatment of
    the dedup/ver indexes would leave them unindexed forever.

    Columns are validated against ``desired_cols`` (declared fields plus the
    runtime columns the migrator actually creates: ``created_at`` /
    ``dedup_hash``). An unknown column raises :class:`MigrationError` at
    plan time, naming class and column — not at DDL time, far from the
    declaration.
    """
    meta = getattr(cls, "Meta", None)
    indexes = getattr(meta, "indexes", ()) if meta else ()
    stmts: list[Stmt] = []
    for cols in indexes:
        for col in cols:
            if col not in desired_cols:
                raise MigrationError(
                    f"{cls.__name__}: Meta.indexes column {col!r} is neither "
                    f"a declared field nor a migrator-managed runtime column"
                )
        name = f"ix_{table}_{'_'.join(cols)}"
        col_sql = ", ".join(f'"{c}"' for c in cols)
        stmts.append(
            Stmt(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({col_sql})")
        )
    return stmts


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

        meta = getattr(cls, "Meta", None)

        # Transient Data never reaches pg — it flows in-process to a
        # non-durable Sink. No CREATE, no ALTER, no drop-check; the
        # class is invisible to the migrator.
        if meta and getattr(meta, "transient", False):
            continue

        # Adoption mode: the Data class declares it is backed by a
        # pre-existing legacy table. The migrator does not own the
        # schema of that table — emit no DDL at all, regardless of what
        # columns ``existing_schema`` reports.
        if meta and getattr(meta, "existing_table", None):
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
                            f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS '
                            f'"{col}" {base} DEFAULT {default}'
                        )
                    )
                else:
                    stmts.append(
                        Stmt(
                            f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS '
                            f'"{col}" {desired_typ}'
                        )
                    )
            # Declared secondary indexes are backfilled on existing tables
            # too (IF NOT EXISTS — no-op when already present).
            stmts.extend(_declared_index_stmts(cls, table, desired_cols))
            continue

        # Quote column names so reserved words (e.g. ``limit``) compile.
        # PostgreSQL preserves quoted lowercase identifiers as the same
        # canonical name an unquoted identifier folds to, so existing
        # information_schema lookups still match.
        col_ddl = ", ".join(f'"{n}" {t}' for n, t in desired_cols.items())
        stmts.append(Stmt(f"CREATE TABLE IF NOT EXISTS {table} ({col_ddl})"))
        stmts.append(
            Stmt(
                f"CREATE UNIQUE INDEX IF NOT EXISTS ix_{table}_dedup "
                f'ON {table}("dedup_hash")'
            )
        )
        ver = version_field(cls)
        if ver:
            keys = key_fields(cls)
            cols = ", ".join(f'"{k}"' for k in keys) + f', "{ver}" DESC'
            stmts.append(
                Stmt(
                    f"CREATE INDEX IF NOT EXISTS ix_{table}_key_ver ON {table}({cols})"
                )
            )
        stmts.extend(_declared_index_stmts(cls, table, desired_cols))

    return Plan(stmts=stmts)


async def apply_migration(plan: Plan, conn) -> None:
    """Execute each statement in ``plan`` on an asyncpg-style connection.

    Caller is responsible for wrapping in a transaction.
    """
    for s in plan.stmts:
        await conn.execute(s.sql, *s.params)
