"""Data base class and field markers for the runtime layer.

Data subclasses are immutable pydantic models that carry domain payloads
through the graph. Fields are annotated with markers (Key / DedupKey /
Version) to declare their role in persistence; classes may mix in
AdminOnly to declare that business code may not produce instances.

Every subclass is registered in ``DATA_REGISTRY`` so downstream components
(migrator, persist, query) can reflect on the full set of known data types.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Key:
    """Marker: field is part of the natural key."""


class DedupKey:
    """Marker: field joins the dedup hash (Key ∪ DedupKey)."""


class Version:
    """Marker: append-only version column (runtime-maintained)."""


class AdminOnly:
    """Class-level mixin: business code may not produce this Data."""


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


def dedup_fields(cls: type[Data]) -> tuple[str, ...]:
    """Return the names of fields that form the dedup hash.

    Defaults to ``key_fields`` when no ``DedupKey`` is declared; otherwise
    returns ``key_fields`` followed by the extra ``DedupKey`` fields.
    """
    keys = key_fields(cls)
    extras = tuple(
        name
        for name, field in cls.model_fields.items()
        if DedupKey in field.metadata and name not in keys
    )
    return keys + extras if extras else keys


def version_field(cls: type[Data]) -> str | None:
    """Return the name of the ``Version`` field, if declared."""
    for name, field in cls.model_fields.items():
        if Version in field.metadata:
            return name
    return None


def is_admin_only(cls: type[Data]) -> bool:
    """Return True when the class mixes in ``AdminOnly``."""
    return issubclass(cls, AdminOnly)
