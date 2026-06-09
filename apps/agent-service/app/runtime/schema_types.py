"""Shared annotation → PostgreSQL column-type judgement.

Two real consumers depend on this judgement and must agree by construction:

  * the :mod:`~app.runtime.migrator` decides each column's DDL type from it,
    and
  * :mod:`~app.runtime.persist` decides which columns take the JSONB
    json-encoding path on write from it.

Keeping the mapping in one place stops the table-creation side and the
write side from drifting (e.g. one treating ``Optional[dict]`` as JSONB and
the other as a scalar). The judgement is made on the *declared* annotation,
never on a runtime value.
"""

from __future__ import annotations

import datetime
import types
import typing
from typing import get_args, get_origin

from pydantic.fields import FieldInfo


class UnmappablePgType(Exception):
    """Raised when an annotation cannot map to a single PG column type.

    The only case today is a non-Optional ``Union`` (genuinely ambiguous —
    no single column type fits). The migrator translates this into its own
    ``MigrationError`` so callers building DDL see a migration-shaped error.
    """


_PY_TO_PG: dict[type, str] = {
    str: "TEXT",
    int: "BIGINT",
    float: "DOUBLE PRECISION",
    bool: "BOOLEAN",
    bytes: "BYTEA",
    dict: "JSONB",
    list: "JSONB",
}


def pg_type_for_annotation(t: object) -> str:
    """Map a Python annotation to a PostgreSQL column type.

    Unwraps ``Optional[X]`` (``Union[X, None]``) by recursing on ``X``; a
    non-Optional ``Union`` cannot be mapped to a single column type and
    raises :class:`UnmappablePgType`. Unknown generic origins preserve their
    structure as ``JSONB``; bare unknown types fall back to ``TEXT``.
    """
    origin = get_origin(t)
    if origin is typing.Union or origin is types.UnionType:
        args = [a for a in get_args(t) if a is not type(None)]
        if len(args) == 1:
            return pg_type_for_annotation(args[0])
        raise UnmappablePgType(
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


def pg_type(fi: FieldInfo) -> str:
    """Map a pydantic ``FieldInfo`` annotation to a PostgreSQL column type.

    Pydantic v2 already strips ``Annotated`` metadata before exposing the
    bare runtime type on ``FieldInfo.annotation``. ``Optional[X]`` is
    unwrapped so nullable fields get the correct scalar column type;
    non-Optional unions are rejected.
    """
    return pg_type_for_annotation(fi.annotation)


def is_jsonb_column(fi: FieldInfo) -> bool:
    """Whether the field's declared annotation maps to a JSONB column."""
    return pg_type(fi) == "JSONB"
