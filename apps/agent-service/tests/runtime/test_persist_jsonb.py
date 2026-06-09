"""dict / list 字段 → JSONB 列的持久化往返（地基刀 0 / Task A）.

framework 的两个 durable 写库函数 ``insert_idempotent`` / ``insert_append``
原来把 Data 字段原样绑定给 asyncpg。dict / list 字段要落 JSONB 列时，asyncpg
经 ``text()`` 绑定拿不到列类型、把原生 Python dict / list 当未知类型，直接
``DataError``。这刀让它们能把 dict / list 编码进 JSONB、读回字段（含嵌套）原样
还原。

两条写入路径必须各测一遍——``insert_append`` 多 Version 自增 + 重算 dedup hash，
和 ``insert_idempotent`` 是不同代码路径。

设计约束（来自 spec）：结构化 JSONB 字段不进 Key / DedupKey（payload 是内容不是
身份）；去重键只用标量身份字段。故下面两个测试专用 Data 的 dict / list 字段都是
非 Key 的 payload，自然键 / 版本键全是标量。
"""

from __future__ import annotations

import datetime
from typing import Annotated

import pytest

from app.runtime.data import Data, Key, Version
from app.runtime.persist import (
    _encode_params,
    _jsonb_columns,
    insert_append,
    insert_idempotent,
    select_all_versions,
    select_latest,
)
from tests.runtime.conftest import migrate

# 嵌套 + 空 dict + 空 list + None 的混合负载，钉死「深度相等、不丢结构」。
_NESTED_PAYLOAD: dict = {
    "outer": {"inner": [1, 2, {"k": "v"}], "flag": True},
    "empty_dict": {},
    "empty_list": [],
    "n": None,
    "unicode": "千凪在厨房",
}
_LIST_PAYLOAD: list = [
    {"role": "user", "content": "早"},
    {"role": "assistant", "content": "早安", "meta": {"tone": "soft"}},
    [],
    {},
]


class JIdempotent(Data):
    """测试专用 durable Data：标量自然键 + dict / list / Optional[dict] payload。

    走 ``insert_idempotent`` 路径（无 Version，dedup_hash 基于标量 Key）。
    """

    kid: Annotated[str, Key]
    payload: dict
    items: list
    maybe: dict | None = None


class JAppend(Data):
    """测试专用 durable Data：标量自然键 + Version + dict / list payload。

    走 ``insert_append`` 路径（Version 自增 + dedup_hash 折入版本号）。
    """

    pid: Annotated[str, Key]
    ver: Annotated[int, Version] = 0
    payload: dict
    items: list
    maybe: dict | None = None


class JMixed(Data):
    """标量（含 datetime）+ JSONB 混合字段，验证分流：标量保持原生绑定。"""

    kid: Annotated[str, Key]
    when: datetime.datetime
    note: str
    payload: dict


class JBlobMixed(Data):
    """非 UTF-8 bytes 标量（BYTEA 列）+ dict JSONB 字段混合。

    复现 codex 抓到的必改 #1：旧实现对【整个对象】调 ``model_dump(mode="json")``
    再取 JSONB 字段值——一旦某 Data 同时有 JSONB 字段和非 JSON 安全标量（这里是
    非 UTF-8 bytes），整对象 dump 会先在标量上炸（pydantic json mode 把 bytes 当
    UTF-8 解码失败），违背 spec「只有 JSONB 列走 json 编码、标量保持原生绑定」。
    """

    kid: Annotated[str, Key]
    blob: bytes
    payload: dict


def test_jsonb_columns_picks_only_structured_fields_by_annotation():
    """_jsonb_columns 按声明类型挑出结构化列，标量列（含 datetime）不在内。"""
    assert _jsonb_columns(JMixed) == frozenset({"payload"})
    assert _jsonb_columns(JIdempotent) == frozenset({"payload", "items", "maybe"})


def test_encode_params_leaves_scalar_values_native():
    """只 JSONB 列被 json.dumps 成文本；标量列（datetime / str）保持原生 Python 值。

    钉死设计决策 #2：绝不对全字段无脑 model_dump（那会把 datetime 转成字符串、
    改掉标量列的绑定语义）。
    """
    when = datetime.datetime(2026, 6, 9, 8, 0, 0, tzinfo=datetime.UTC)
    obj = JMixed(kid="k", when=when, note="hi", payload={"a": 1})
    cols_map = {n: getattr(obj, n) for n in JMixed.model_fields}
    out = _encode_params(obj, dict(cols_map), _jsonb_columns(JMixed))

    # 标量：原生值不变（datetime 仍是 datetime，不是字符串）
    assert out["when"] is when
    assert out["note"] == "hi"
    assert out["kid"] == "k"
    # JSONB：被序列化成 json 文本（pydantic dump_json 输出紧凑形式、无空格）
    assert out["payload"] == '{"a":1}'


@pytest.mark.integration
async def test_insert_idempotent_roundtrips_jsonb_fields(test_db):
    """insert_idempotent：dict / list / 嵌套 / 空容器 / None 写库再读回深度相等。"""
    await migrate(JIdempotent, test_db)

    n = await insert_idempotent(
        JIdempotent(
            kid="k1",
            payload=_NESTED_PAYLOAD,
            items=_LIST_PAYLOAD,
            maybe={"a": 1, "b": [2, 3]},
        )
    )
    assert n == 1

    got = await select_latest(JIdempotent, {"kid": "k1"})
    assert got is not None
    assert got.payload == _NESTED_PAYLOAD
    assert got.items == _LIST_PAYLOAD
    assert got.maybe == {"a": 1, "b": [2, 3]}


@pytest.mark.integration
async def test_insert_idempotent_roundtrips_optional_dict_none(test_db):
    """Optional[dict]=None 走 JSONB 列也能往返（按声明类型分流、不看 runtime None）。"""
    await migrate(JIdempotent, test_db)

    await insert_idempotent(
        JIdempotent(kid="k2", payload={}, items=[], maybe=None)
    )

    got = await select_latest(JIdempotent, {"kid": "k2"})
    assert got is not None
    assert got.payload == {}
    assert got.items == []
    assert got.maybe is None


@pytest.mark.integration
async def test_insert_append_roundtrips_jsonb_fields(test_db):
    """insert_append：Version 自增 + dict / list 字段写库再读回深度相等。"""
    await migrate(JAppend, test_db)

    await insert_append(
        JAppend(
            pid="p1",
            payload=_NESTED_PAYLOAD,
            items=_LIST_PAYLOAD,
            maybe={"x": [1, {"y": 2}]},
        )
    )
    await insert_append(
        JAppend(
            pid="p1",
            payload={"second": {"round": [9]}},
            items=[{"v": 2}],
            maybe=None,
        )
    )

    rows = await select_all_versions(JAppend, {"pid": "p1"})
    assert [r.ver for r in rows] == [1, 2]
    # 第一版：嵌套 / 空容器全保真
    assert rows[0].payload == _NESTED_PAYLOAD
    assert rows[0].items == _LIST_PAYLOAD
    assert rows[0].maybe == {"x": [1, {"y": 2}]}
    # 第二版：换了结构、maybe=None 也能往返
    assert rows[1].payload == {"second": {"round": [9]}}
    assert rows[1].items == [{"v": 2}]
    assert rows[1].maybe is None

    latest = await select_latest(JAppend, {"pid": "p1"})
    assert latest is not None
    assert latest.ver == 2
    assert latest.payload == {"second": {"round": [9]}}


def test_encode_params_unserializable_jsonb_names_data_and_field():
    """JSONB 字段含不可 JSON 序列化值时，错误必须点名是哪个 Data / 字段（fail-fast）。

    复现 codex 抓到的必改 #2：旧实现在字段级 try/except 之外对整对象调
    ``model_dump(mode="json")``，所以含 ``object()`` 的 JSONB 字段抛的是裸
    ``PydanticSerializationError``、不带 ``Data类名.字段名``，绕过 spec
    要求的 fail-fast 契约。修复后错误文本必须含 ``JIdempotent.payload``。
    """
    obj = JIdempotent(kid="k", payload={"x": object()}, items=[])
    with pytest.raises(Exception) as exc_info:  # noqa: PT011
        _encode_params(
            obj,
            {n: getattr(obj, n) for n in JIdempotent.model_fields},
            _jsonb_columns(JIdempotent),
        )
    assert "JIdempotent.payload" in str(exc_info.value)


def test_encode_params_does_not_dump_non_json_safe_scalar():
    """非 UTF-8 bytes 标量 + dict JSONB 字段混合：标量不进 json 编码、不被误伤。

    复现必改 #1：旧实现整对象 ``model_dump(mode="json")`` 会在非 UTF-8 bytes
    标量上先炸（pydantic json mode 用 UTF-8 解码 bytes）。修复后只对 JSONB
    字段单独序列化，标量 bytes 保持原生 Python 值绑定。
    """
    blob = b"\xff\xfe\x00\x01"  # 非 UTF-8、含 NUL，json mode 无法解码
    obj = JBlobMixed(kid="k", blob=blob, payload={"a": 1})
    out = _encode_params(
        obj,
        {n: getattr(obj, n) for n in JBlobMixed.model_fields},
        _jsonb_columns(JBlobMixed),
    )
    # 标量 bytes：原生值不变，未被 json 编码
    assert out["blob"] is blob
    # JSONB：被序列化成 json 文本（pydantic dump_json 紧凑形式、无空格）
    assert out["payload"] == '{"a":1}'


@pytest.mark.integration
async def test_insert_idempotent_roundtrips_bytea_and_jsonb_mixed(test_db):
    """非 UTF-8 bytes 标量（BYTEA）+ dict JSONB 混合写库再读回，两类字段各自保真。"""
    await migrate(JBlobMixed, test_db)

    blob = b"\xff\xfe\x00\x01\x80"
    await insert_idempotent(
        JBlobMixed(kid="bk", blob=blob, payload={"nested": {"k": [1, 2]}})
    )

    got = await select_latest(JBlobMixed, {"kid": "bk"})
    assert got is not None
    assert got.blob == blob
    assert got.payload == {"nested": {"k": [1, 2]}}
