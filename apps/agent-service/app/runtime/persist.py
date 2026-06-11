"""Append-only persistence primitives for Data rows.

Four operations, deliberately minimal:

  - :func:`insert_append` — durable write with runtime-maintained ``Version``
    auto-increment. Concurrency-safe via ``pg_advisory_xact_lock`` keyed on
    the natural-key tuple. Optional ``expected_current_ver`` turns the append
    into an optimistic CAS (write only if ``MAX(ver)`` is still the version
    the caller loaded; the check rides inside the INSERT statement itself).
  - :func:`insert_idempotent` — ``INSERT ... ON CONFLICT (<target>) DO
    NOTHING RETURNING 1``. Conflict target is ``Meta.dedup_column`` when
    the Data class specifies one (adoption mode for pre-existing tables
    whose own PK / unique index enforces dedup), else the runtime-managed
    ``dedup_hash`` column. Returns the number of rows actually inserted
    (0 on collision, 1 otherwise); see function docstring for the full
    contract.
  - :func:`select_latest` — newest version per key using ``DISTINCT ON``.
  - :func:`select_all_versions` — full history for a key, ordered by version
    (or ``created_at`` when the Data class declares no ``Version``).

The dedup_hash is a sha256 over the ``DedupKey ∪ Key`` columns, serialized
via ``json.dumps(sort_keys=True, default=str)``. The class name is *not*
included — each Data class lives in its own table, so cross-class
collisions are impossible by construction.
"""

from __future__ import annotations

import hashlib
import json
from functools import cache
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import text

from app.data.session import get_session
from app.runtime.data import Data, dedup_fields, key_fields, version_field
from app.runtime.migrator import _table_name
from app.runtime.schema_types import is_jsonb_column


def _jsonb_columns(cls: type[Data]) -> frozenset[str]:
    """Names of fields whose declared annotation maps to a JSONB column.

    Reuses the runtime's annotation→pg-type judgement (:func:`is_jsonb_column`,
    which unwraps ``Optional[X]`` and recognizes ``dict`` / ``list`` /
    structured origins as ``JSONB``) so the write side and the table-creation
    side (``migrator``) agree on which columns are JSONB by construction.
    Decided on the *declared* type, never the runtime value — an
    ``Optional[dict]=None``, a nested pydantic model, or a dict subclass must
    still take the JSONB path the column was built for.
    """
    return frozenset(
        name
        for name, fi in cls.model_fields.items()
        if is_jsonb_column(fi)
    )


@cache
def _field_adapter(cls: type[Data], col: str) -> TypeAdapter[Any]:
    """Per-field ``TypeAdapter`` built from the field's declared annotation.

    Serializing each JSONB column through its *own* annotation (rather than
    the whole object) keeps scalar fields out of serialization entirely and
    makes nested pydantic models / datetimes inside the JSONB value follow
    pydantic's rules. Cached on ``(cls, col)`` because TypeAdapter construction
    compiles a serializer and is not free to build per write.
    """
    return TypeAdapter(cls.model_fields[col].annotation)


def _bind_value_sql(col: str, jsonb_cols: frozenset[str]) -> str:
    """The VALUES fragment for ``col``: ``CAST(:col AS jsonb)`` for JSONB,
    else a bare ``:col`` placeholder.

    asyncpg binds ``text()`` params without column-type context, so a dict /
    list bound at a JSONB column raises ``DataError``. The explicit ``CAST``
    tells postgres the target type and lets us bind the json *text* produced
    below in :func:`_encode_params`.
    """
    return f"CAST(:{col} AS jsonb)" if col in jsonb_cols else f":{col}"


def _encode_params(
    obj: Data,
    cols_map: dict[str, Any],
    jsonb_cols: frozenset[str],
) -> dict[str, Any]:
    """Return a params map where JSONB columns carry json *text*.

    Each JSONB column is serialized **on its own**, through a TypeAdapter
    built from that field's declared annotation
    (:func:`_field_adapter`), so nested pydantic models / datetimes inside
    the value follow pydantic's json rules. The result is bound at
    ``CAST(:col AS jsonb)``.

    Scalar columns (incl. ``datetime`` / ``date`` / ``bytes`` / the
    runtime-managed ``dedup_hash``) never touch serialization at all — their
    native Python value asyncpg already maps correctly. We deliberately do
    **not** dump the whole object: a blanket ``model_dump`` would (a) stringify
    scalar datetimes / bytes and break their bindings, and (b) raise on a
    non-JSON-safe scalar (e.g. non-UTF-8 ``bytes``) even when no JSONB field is
    at fault.

    Serialization is fail-fast and per-field: a non-JSON-safe value in a JSONB
    field raises here, naming the Data class and the specific field, with no
    fallback or placeholder.
    """
    if not jsonb_cols:
        return cols_map
    cls = type(obj)
    out = dict(cols_map)
    for col in jsonb_cols:
        if col not in out:
            continue
        try:
            out[col] = _field_adapter(cls, col).dump_json(out[col]).decode()
        except Exception as exc:
            raise ValueError(
                f"{cls.__name__}.{col}: value is not JSON-serializable "
                f"for the JSONB column ({exc})"
            ) from exc
    return out


def _dedup_hash(obj: Data) -> str:
    """SHA-256 of the obj's dedup columns, deterministic across processes.

    For append-only Data (those declaring a ``Version`` field), the version
    column is folded into the hash so each append produces a unique row
    under the ``UNIQUE (dedup_hash)`` index. Without this, two appends
    under the same natural key would collide — defeating versioning.
    """
    cls = type(obj)
    cols = list(dedup_fields(cls))
    ver = version_field(cls)
    if ver and ver not in cols:
        cols.append(ver)
    payload = json.dumps(
        {c: getattr(obj, c) for c in cols},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


async def insert_append(
    obj: Data, *, expected_current_ver: int | None = None
) -> int:
    """Append ``obj`` as a new row; auto-assign ``Version`` if declared.

    Holds a per-key ``pg_advisory_xact_lock`` during ``MAX(ver)`` read and
    the subsequent ``INSERT`` so that concurrent writers against the same
    key produce monotonic, gapless versions. The lock key is an MD5 of the
    natural-key tuple, truncated into a 31-bit int (fits pg int4); MD5 is
    not cryptographic here — only a stable bucket. Collisions between
    unrelated keys are benign (they serialize more than strictly necessary,
    never miss a lock).

    ``expected_current_ver`` (optimistic concurrency, requires a ``Version``
    column): append only if the key's current ``MAX(ver)`` still equals this
    value, i.e. nobody appended since the caller read that version. The check
    rides **inside the INSERT itself** (``INSERT ... SELECT ... WHERE (SELECT
    MAX(ver) ...) = :expected``) so check and write are one atomic statement —
    no TOCTOU window even against a writer that bypassed the advisory lock.
    Returns ``1`` when written, ``0`` when stale (someone advanced the
    version); the caller decides what a lost race means.

    Without ``expected_current_ver`` the behavior is unchanged: always
    returns ``1`` on success. A ``UniqueViolation`` on ``dedup_hash`` is
    raised (not swallowed): because the version column is folded into the
    hash, a collision means two writers slipped past the advisory lock — an
    upstream bug worth surfacing loudly.
    """
    cls = type(obj)
    table = _table_name(cls)
    ver_col = version_field(cls)
    keys = key_fields(cls)
    if expected_current_ver is not None and not ver_col:
        raise ValueError(
            f"{cls.__name__}: expected_current_ver requires a Version column"
        )

    cols_map: dict[str, Any] = {c: getattr(obj, c) for c in cls.model_fields}
    key_tuple = tuple(getattr(obj, k) for k in keys)
    lock_key = int(hashlib.md5(str(key_tuple).encode()).hexdigest()[:15], 16) % (2**31)

    async with get_session() as s:
        await s.execute(
            text("SELECT pg_advisory_xact_lock(:k)"),
            {"k": lock_key},
        )
        where = " AND ".join(f"{k} = :{k}" for k in keys)
        if expected_current_ver is not None:
            # CAS path: the version to write is pinned to expected+1; whether
            # expected still holds is judged by the INSERT's own WHERE below.
            cols_map[ver_col] = expected_current_ver + 1
        elif ver_col:
            r = await s.execute(
                text(
                    f"SELECT COALESCE(MAX({ver_col}), 0) FROM {table} "
                    f"WHERE {where}"
                ),
                {k: getattr(obj, k) for k in keys},
            )
            cols_map[ver_col] = r.scalar() + 1

        # dedup_hash must reflect the assigned version, so it is computed
        # after the ver column is resolved. Build a transient obj of the
        # same class carrying the final values so the hash is consistent
        # with the row we actually insert.
        hashed = cls.model_construct(**cols_map)
        cols_map["dedup_hash"] = _dedup_hash(hashed)

        jsonb_cols = _jsonb_columns(cls)
        cols = list(cols_map.keys())
        placeholders = ", ".join(_bind_value_sql(c, jsonb_cols) for c in cols)
        if expected_current_ver is not None:
            # Atomic check-and-insert: key columns are part of cols_map, so the
            # subquery's :{k} binds reuse the same params (same values).
            sql = (
                f"INSERT INTO {table} ({', '.join(cols)}) "
                f"SELECT {placeholders} "
                f"WHERE (SELECT COALESCE(MAX({ver_col}), 0) FROM {table} "
                f"WHERE {where}) = :_expected_ver RETURNING 1"
            )
            params = _encode_params(obj, cols_map, jsonb_cols)
            params["_expected_ver"] = expected_current_ver
            r = await s.execute(text(sql), params)
            return len(r.fetchall())
        sql = (
            f"INSERT INTO {table} ({', '.join(cols)}) "
            f"VALUES ({placeholders})"
        )
        await s.execute(text(sql), _encode_params(obj, cols_map, jsonb_cols))
    return 1


async def insert_idempotent(obj: Data) -> int:
    """Insert ``obj`` if its dedup key has not been seen; return rows inserted.

    Uses ``ON CONFLICT (<dedup_target>) DO NOTHING RETURNING 1``. The dedup
    target is either:

      * ``Meta.dedup_column`` when the Data class specifies one (used when
        adopting a pre-existing table whose PK / unique index already
        enforces dedup — that table typically has no ``dedup_hash``
        column, so we also omit it from the INSERT column list), or
      * ``dedup_hash`` (the runtime-managed hash column) otherwise.

    We count ``len(fetchall())`` because asyncpg does not reliably populate
    ``rowcount`` for ``DO NOTHING``.
    """
    cls = type(obj)
    table = _table_name(cls)

    meta = getattr(cls, "Meta", None)
    dedup_col = getattr(meta, "dedup_column", None) if meta else None

    cols_map: dict[str, Any] = {c: getattr(obj, c) for c in cls.model_fields}
    if not dedup_col:
        cols_map["dedup_hash"] = _dedup_hash(obj)

    jsonb_cols = _jsonb_columns(cls)
    cols = list(cols_map.keys())
    placeholders = ", ".join(_bind_value_sql(c, jsonb_cols) for c in cols)
    conflict_target = dedup_col or "dedup_hash"
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict_target}) DO NOTHING RETURNING 1"
    )
    async with get_session() as s:
        r = await s.execute(text(sql), _encode_params(obj, cols_map, jsonb_cols))
        return len(r.fetchall())


async def select_latest(
    cls: type[Data], keys_values: dict[str, Any]
) -> Data | None:
    """Return the highest-version row matching ``keys_values``, or ``None``."""
    table = _table_name(cls)
    keys = key_fields(cls)
    ver = version_field(cls)
    where = " AND ".join(f"{k} = :{k}" for k in keys)
    order = (
        f"{', '.join(keys)}, {ver} DESC"
        if ver
        else f"{', '.join(keys)}"
    )
    sql = (
        f"SELECT DISTINCT ON ({', '.join(keys)}) * FROM {table} "
        f"WHERE {where} ORDER BY {order}"
    )
    async with get_session() as s:
        r = await s.execute(text(sql), keys_values)
        row = r.mappings().first()
        if not row:
            return None
        return cls(**{k: row[k] for k in cls.model_fields})


async def select_all_versions(
    cls: type[Data], keys_values: dict[str, Any]
) -> list[Data]:
    """Return all rows matching ``keys_values``, ordered by version ASC.

    Falls back to ``created_at`` ordering for Data classes without a
    ``Version`` column.
    """
    table = _table_name(cls)
    keys = key_fields(cls)
    ver = version_field(cls)
    where = " AND ".join(f"{k} = :{k}" for k in keys)
    order = ver if ver else "created_at"
    sql = f"SELECT * FROM {table} WHERE {where} ORDER BY {order}"
    async with get_session() as s:
        r = await s.execute(text(sql), keys_values)
        return [cls(**{k: row[k] for k in cls.model_fields}) for row in r.mappings().all()]
