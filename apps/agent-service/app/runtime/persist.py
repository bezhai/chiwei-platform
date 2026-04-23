"""Append-only persistence primitives for Data rows.

Four operations, deliberately minimal:

  - :func:`insert_append` — durable write with runtime-maintained ``Version``
    auto-increment. Concurrency-safe via ``pg_advisory_xact_lock`` keyed on
    the natural-key tuple.
  - :func:`insert_idempotent` — ``INSERT ... ON CONFLICT (dedup_hash) DO
    NOTHING``. Returns the number of rows actually inserted (0 when the
    dedup_hash collided, 1 otherwise).
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
from typing import Any

from sqlalchemy import text

from app.data.session import get_session
from app.runtime.data import Data, dedup_fields, key_fields, version_field
from app.runtime.migrator import _table_name


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


async def insert_append(obj: Data) -> int:
    """Append ``obj`` as a new row; auto-assign ``Version`` if declared.

    Holds a per-key ``pg_advisory_xact_lock`` during ``MAX(ver)`` read and
    the subsequent ``INSERT`` so that concurrent writers against the same
    key produce monotonic, gapless versions. The lock key is an MD5 of the
    natural-key tuple, truncated into a 31-bit int (fits pg int4); MD5 is
    not cryptographic here — only a stable bucket. Collisions between
    unrelated keys are benign (they serialize more than strictly necessary,
    never miss a lock).
    """
    cls = type(obj)
    table = _table_name(cls)
    ver_col = version_field(cls)
    keys = key_fields(cls)

    cols_map: dict[str, Any] = {c: getattr(obj, c) for c in cls.model_fields}
    key_tuple = tuple(getattr(obj, k) for k in keys)
    lock_key = int(hashlib.md5(str(key_tuple).encode()).hexdigest()[:15], 16) % (2**31)

    async with get_session() as s:
        await s.execute(
            text("SELECT pg_advisory_xact_lock(:k)"),
            {"k": lock_key},
        )
        if ver_col:
            where = " AND ".join(f"{k} = :{k}" for k in keys)
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

        cols = list(cols_map.keys())
        placeholders = ", ".join(f":{c}" for c in cols)
        sql = (
            f"INSERT INTO {table} ({', '.join(cols)}) "
            f"VALUES ({placeholders})"
        )
        await s.execute(text(sql), cols_map)
    return 1


async def insert_idempotent(obj: Data) -> int:
    """Insert ``obj`` if its dedup_hash has not been seen; return rows inserted.

    Uses ``ON CONFLICT (dedup_hash) DO NOTHING RETURNING id`` so we can
    count the actually-inserted rows regardless of driver-reported
    ``rowcount`` semantics (which asyncpg does not reliably populate for
    ``DO NOTHING``).
    """
    cls = type(obj)
    table = _table_name(cls)

    cols_map: dict[str, Any] = {c: getattr(obj, c) for c in cls.model_fields}
    cols_map["dedup_hash"] = _dedup_hash(obj)

    cols = list(cols_map.keys())
    placeholders = ", ".join(f":{c}" for c in cols)
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT (dedup_hash) DO NOTHING RETURNING 1"
    )
    async with get_session() as s:
        r = await s.execute(text(sql), cols_map)
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
