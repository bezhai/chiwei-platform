from typing import Annotated

from app.data.schema import (
    Data,
    Key,
    Version,
    key_fields,
    version_field,
)


class Sample(Data):
    pid: Annotated[str, Key]
    ver: Annotated[int, Version] = 0
    text: str


def test_key_fields():
    assert key_fields(Sample) == ("pid",)


def test_version_field_detected():
    assert version_field(Sample) == "ver"


def test_registry_populated():
    from app.data.schema import DATA_REGISTRY

    assert Sample in DATA_REGISTRY


def test_data_without_key_rejected():
    import pytest

    with pytest.raises(TypeError, match="must declare at least one Key"):

        class Bad(Data):
            text: str  # no Key

