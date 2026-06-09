"""annotation → PG 列类型判断的共享 helper（runtime/schema_types）.

这层判断有两个真实消费方：``migrator`` 建表时按它决定列类型，``persist`` 写入时
按它决定哪些列走 JSONB 编码。两者必须用同一份基于【声明类型】的判断，否则建表侧
和写入侧会分叉。把判断提成 runtime 公共 helper、两边都调它，消除 persist 对
migrator 私有函数的耦合。

这里直接钉死共享 helper 的契约；migrator / persist 各自的端到端测试再覆盖集成。
"""

from __future__ import annotations

import datetime
import typing
from typing import Annotated

import pytest
from pydantic import BaseModel

from app.runtime.schema_types import (
    UnmappablePgType,
    is_jsonb_column,
    pg_type,
    pg_type_for_annotation,
)


class _M(BaseModel):
    s: str
    n: int
    f: float
    b: bool
    raw: bytes
    d: dict
    l: list  # noqa: E741
    when: datetime.datetime
    day: datetime.date
    opt_s: str | None = None
    opt_d: dict | None = None
    bad: Annotated[int, "meta"] = 0


def test_scalar_annotations_map_to_scalar_pg_types():
    assert pg_type_for_annotation(str) == "TEXT"
    assert pg_type_for_annotation(int) == "BIGINT"
    assert pg_type_for_annotation(float) == "DOUBLE PRECISION"
    assert pg_type_for_annotation(bool) == "BOOLEAN"
    assert pg_type_for_annotation(bytes) == "BYTEA"
    assert pg_type_for_annotation(datetime.datetime) == "TIMESTAMPTZ"
    assert pg_type_for_annotation(datetime.date) == "DATE"


def test_dict_and_list_map_to_jsonb():
    assert pg_type_for_annotation(dict) == "JSONB"
    assert pg_type_for_annotation(list) == "JSONB"
    assert pg_type_for_annotation(dict[str, int]) == "JSONB"
    assert pg_type_for_annotation(list[dict]) == "JSONB"


def test_optional_unwraps_to_scalar_pg_type():
    # Both Union spellings must unwrap: typing.Union (origin is typing.Union)
    # and the X | None form (origin is types.UnionType) — the mapper checks
    # both origins, so cover both.
    assert pg_type_for_annotation(typing.Union[str, None]) == "TEXT"  # noqa: UP007
    assert pg_type_for_annotation(str | None) == "TEXT"
    # Optional[dict] still JSONB by declared type, never runtime None.
    assert pg_type_for_annotation(dict | None) == "JSONB"


def test_non_optional_union_rejected_with_union_message():
    # Message text must keep saying "Union" so the migrator wrapper's
    # MigrationError(match="Union") contract holds.
    with pytest.raises(UnmappablePgType, match="Union"):
        pg_type_for_annotation(int | str)


def test_pg_type_reads_field_info_annotation():
    fields = _M.model_fields
    assert pg_type(fields["s"]) == "TEXT"
    assert pg_type(fields["d"]) == "JSONB"
    assert pg_type(fields["when"]) == "TIMESTAMPTZ"
    assert pg_type(fields["opt_d"]) == "JSONB"


def test_is_jsonb_column_picks_only_structured_fields():
    fields = _M.model_fields
    assert is_jsonb_column(fields["d"]) is True
    assert is_jsonb_column(fields["l"]) is True
    assert is_jsonb_column(fields["opt_d"]) is True
    assert is_jsonb_column(fields["s"]) is False
    assert is_jsonb_column(fields["raw"]) is False
    assert is_jsonb_column(fields["when"]) is False
    assert is_jsonb_column(fields["opt_s"]) is False
