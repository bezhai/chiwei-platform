from typing import Annotated

from app.runtime.data import (
    AdminOnly,
    Data,
    DedupKey,
    Key,
    Version,
    dedup_fields,
    is_admin_only,
    key_fields,
    version_field,
)


class Sample(Data):
    pid: Annotated[str, Key]
    ver: Annotated[int, Version] = 0
    gen: Annotated[int, DedupKey] = 0
    text: str


def test_key_fields():
    assert key_fields(Sample) == ("pid",)


def test_dedup_fields_defaults_to_key_plus_extra():
    assert dedup_fields(Sample) == ("pid", "gen")


def test_version_field_detected():
    assert version_field(Sample) == "ver"


def test_is_admin_only_false_by_default():
    assert is_admin_only(Sample) is False


class Cfg(Data, AdminOnly):
    cid: Annotated[str, Key]
    v: dict


def test_admin_only_detected():
    assert is_admin_only(Cfg) is True


def test_registry_populated():
    from app.runtime.data import DATA_REGISTRY

    assert Sample in DATA_REGISTRY
    assert Cfg in DATA_REGISTRY


def test_data_without_key_rejected():
    import pytest

    with pytest.raises(TypeError, match="must declare at least one Key"):

        class Bad(Data):
            text: str  # no Key
