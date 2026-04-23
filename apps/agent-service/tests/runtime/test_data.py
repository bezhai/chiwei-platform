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


# ---------------------------------------------------------------------------
# Meta.dedup_column 与 DedupKey / Version 标记冲突检测
# ---------------------------------------------------------------------------


def test_dedup_column_rejects_extra_dedup_key():
    """Meta.dedup_column 采用外部唯一约束，额外的 DedupKey 标记会让去重意图
    产生歧义（runtime 会悄悄绕过 dedup_hash 分支）——必须在建类时报错。"""
    import pytest

    with pytest.raises(TypeError) as exc_info:

        class BadDedup(Data):
            mid: Annotated[str, Key]
            gen: Annotated[int, DedupKey] = 0

            class Meta:
                existing_table = "ext"
                dedup_column = "mid"

    msg = str(exc_info.value)
    assert "BadDedup" in msg
    assert "gen" in msg
    assert "dedup_column" in msg


def test_dedup_column_rejects_version_marker():
    """Meta.dedup_column 下 runtime 不再管版本；再保留 Version 语义冲突。"""
    import pytest

    with pytest.raises(TypeError) as exc_info:

        class BadVersion(Data):
            mid: Annotated[str, Key]
            ver: Annotated[int, Version] = 0

            class Meta:
                existing_table = "ext"
                dedup_column = "mid"

    msg = str(exc_info.value)
    assert "BadVersion" in msg
    assert "ver" in msg
    assert "dedup_column" in msg


def test_dedup_column_alone_is_fine():
    """无额外标记时，dedup_column 单独使用必须能正常创建（Message 的场景）。"""

    class OkDedupColumn(Data):
        mid: Annotated[str, Key]
        payload: str

        class Meta:
            existing_table = "ext"
            dedup_column = "mid"

    assert key_fields(OkDedupColumn) == ("mid",)


def test_dedup_column_allows_redundant_dedup_key_on_key_field():
    """Key 字段同时带 DedupKey 是冗余但无害——所有 Key 自动是 dedup 字段。
    只有"超出 Key 的 DedupKey"才与 dedup_column 冲突。"""

    class OkRedundant(Data):
        mid: Annotated[str, Key, DedupKey]
        payload: str

        class Meta:
            existing_table = "ext"
            dedup_column = "mid"

    assert key_fields(OkRedundant) == ("mid",)
