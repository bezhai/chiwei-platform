"""Data classes, and the names and column types derived from them.

A Data class is an immutable pydantic model of one stored row. Fields are annotated with markers
(``Key`` / ``Version``) to declare their role in persistence. Every subclass with fields is
registered in ``DATA_REGISTRY`` when its module is imported, so the migrator
(:mod:`app.data.migrator`) and the write and read primitives (:mod:`app.data.persist`) can
reflect on the full set of known data types.

What is derived from a class is computed here, once, so every consumer agrees:

* :func:`table_name`: ``data_`` plus the class name in snake_case. The module is not part of
  it: moving a class to another module keeps its table, renaming the class does not.
* :func:`to_snake`: the CamelCase -> snake_case conversion behind table names and durable queue
  names. One implementation keeps those identifiers aligned: for a Data class ``HTTPRequest``
  both reference ``http_request`` instead of drifting to ``h_t_t_p_request`` in one place.
* :func:`pg_type` / :func:`is_jsonb_column`: a field's PostgreSQL column type, judged on the
  *declared* annotation, never on a runtime value. The migrator decides each column's DDL type
  from it and persist decides which columns take the JSONB json-encoding path on write, so the
  table-creation side and the write side cannot drift (e.g. one treating ``Optional[dict]`` as
  JSONB and the other as a scalar).
"""

from __future__ import annotations

import datetime
import re
import types
import typing
from typing import get_args, get_origin

from pydantic import BaseModel, ConfigDict
from pydantic.fields import FieldInfo


class Key:
    """Marker: field is part of the natural key."""


class Version:
    """Marker: append-only version column (runtime-maintained)."""


DATA_REGISTRY: set[type[Data]] = set()


class Data(BaseModel):
    """Immutable payload carried through the graph.

    Every concrete subclass must declare at least one ``Key`` field.
    Subclasses are auto-registered in ``DATA_REGISTRY``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: object) -> None:
        # Runs after pydantic has fully populated ``model_fields`` on
        # ``cls`` (unlike ``__init_subclass__``, which fires before the
        # metaclass assigns fields). A subclass with no fields is treated
        # as an abstract intermediate and skipped.
        super().__pydantic_init_subclass__(**kwargs)
        if not cls.model_fields:
            return
        if not key_fields(cls):
            raise TypeError(
                f"{cls.__name__} must declare at least one Key field"
            )
        DATA_REGISTRY.add(cls)


def key_fields(cls: type[Data]) -> tuple[str, ...]:
    """Return the names of fields annotated with ``Key``."""
    return tuple(
        name
        for name, field in cls.model_fields.items()
        if Key in field.metadata
    )


def version_field(cls: type[Data]) -> str | None:
    """Return the name of the ``Version`` field, if declared."""
    for name, field in cls.model_fields.items():
        if Version in field.metadata:
            return name
    return None


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

_CAMEL_TO_SNAKE_1 = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_TO_SNAKE_2 = re.compile(r"([a-z0-9])([A-Z])")


def to_snake(name: str) -> str:
    """Convert a ``CamelCase`` identifier to ``snake_case``.

    Acronym boundaries are split the way humans read them:

      * ``Ping``           -> ``ping``
      * ``HTTPRequest``    -> ``http_request``
      * ``MyDataV2``       -> ``my_data_v2``
    """
    if not name:
        return name
    s = _CAMEL_TO_SNAKE_1.sub(r"\1_\2", name)
    return _CAMEL_TO_SNAKE_2.sub(r"\1_\2", s).lower()


def table_name(cls: type[Data]) -> str:
    """The table ``cls`` is stored in: ``data_`` plus the class name in snake_case."""
    return f"data_{to_snake(cls.__name__)}"


# ---------------------------------------------------------------------------
# Column types
# ---------------------------------------------------------------------------


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
