"""认主人:从 common_user.is_owner 读 owner id 集合,fail-closed,进程内缓存。

新契约(砍掉三姐妹 / persona 视角):
  - ``get_relation(common_user_id)`` 命中 owner 集合 → ``"owner"``,否则 ``None``。
  - owner 集合来自 DB(``is_owner=true``),首次调用 lazy load 一次、成功才缓存。
  - fail-closed:空 id、查询异常(含"列不存在") → ``None``;load 失败返回空集合
    且不污染缓存(下次可重试),绝不回退显示名。
"""

from __future__ import annotations

import pytest

from app.memory import identity_registry as ir
from app.memory.identity_registry import get_relation

OWNER = "00000000-0000-0000-0000-0000000owner"
OWNER2 = "00000000-0000-0000-0000-000000owner2"
STRANGER = "00000000-0000-0000-0000-000stranger"


@pytest.fixture(autouse=True)
def _reset_cache():
    """每个用例前清掉进程内缓存,避免互相污染。"""
    ir._OWNER_IDS = None
    yield
    ir._OWNER_IDS = None


def _patch_loader(monkeypatch, owners: set[str]):
    """打桩 DB load,注入 fake owner 集合,顺带计数 load 次数。"""
    calls = {"n": 0}

    async def fake_load() -> set[str]:
        calls["n"] += 1
        return set(owners)

    monkeypatch.setattr(ir, "_load_owner_ids", fake_load)
    return calls


async def test_owner_hit_returns_owner(monkeypatch):
    _patch_loader(monkeypatch, {OWNER, OWNER2})
    assert await get_relation(OWNER) == "owner"
    assert await get_relation(OWNER2) == "owner"


async def test_non_owner_returns_none(monkeypatch):
    _patch_loader(monkeypatch, {OWNER})
    assert await get_relation(STRANGER) is None


async def test_none_id_returns_none(monkeypatch):
    _patch_loader(monkeypatch, {OWNER})
    assert await get_relation(None) is None


async def test_empty_id_returns_none(monkeypatch):
    _patch_loader(monkeypatch, {OWNER})
    assert await get_relation("") is None


async def test_cache_loads_only_once(monkeypatch):
    calls = _patch_loader(monkeypatch, {OWNER})
    await get_relation(OWNER)
    await get_relation(OWNER)
    await get_relation(STRANGER)
    assert calls["n"] == 1, "owner 集合应只 load 一次(进程内缓存)"


async def test_db_exception_is_fail_closed(monkeypatch):
    async def boom() -> set[str]:
        raise RuntimeError("column is_owner does not exist")

    monkeypatch.setattr(ir, "_load_owner_ids", boom)
    assert await get_relation(OWNER) is None


async def test_failed_load_does_not_poison_cache(monkeypatch):
    """load 失败返回空 / 不缓存:下次可重试,成功后才认主人。"""
    state = {"fail": True}

    async def maybe_boom() -> set[str]:
        if state["fail"]:
            raise RuntimeError("transient")
        return {OWNER}

    monkeypatch.setattr(ir, "_load_owner_ids", maybe_boom)
    assert await get_relation(OWNER) is None  # 第一次失败 → fail-closed
    state["fail"] = False
    assert await get_relation(OWNER) == "owner"  # 缓存没被污染,重试成功认得出


async def test_unloaded_default_is_fail_closed(monkeypatch):
    """没注入任何 owner(空集合) → 谁都不是主人(fail-closed 安全默认)。"""
    _patch_loader(monkeypatch, set())
    assert await get_relation(OWNER) is None


async def test_empty_load_is_not_cached(monkeypatch):
    """load 成功但为空 → 不写缓存,下次仍重试(修复 4)。

    is_owner 列已加但人工 UPDATE 打标晚于首次请求时,空集合若被永久缓存,主人到
    进程重启前一直认不出。空集合应像异常一样不污染缓存、下次可重试。
    """
    calls = _patch_loader(monkeypatch, set())
    assert await get_relation(OWNER) is None  # 首次 load 到空集合
    assert await get_relation(OWNER) is None  # 第二次应再 load(空集合没被缓存)
    assert calls["n"] == 2, "空集合不该被缓存,第二次调用应再 load 一次"


async def test_empty_load_then_marked_owner_recognized(monkeypatch):
    """空 load 不缓存 → 打标后下次 load 到非空就认得出(不必等进程重启)。"""
    state = {"owners": set()}
    calls = {"n": 0}

    async def fake_load() -> set[str]:
        calls["n"] += 1
        return set(state["owners"])

    monkeypatch.setattr(ir, "_load_owner_ids", fake_load)

    assert await get_relation(OWNER) is None  # 还没打标,空集合,不缓存
    state["owners"] = {OWNER}  # 人工 UPDATE 打标
    assert await get_relation(OWNER) == "owner"  # 重试 load 到非空,认得出
    assert calls["n"] == 2


async def test_non_empty_load_is_cached(monkeypatch):
    """load 到非空集合 → 写缓存,只 load 一次(修复 4 的另一半)。"""
    calls = _patch_loader(monkeypatch, {OWNER})
    assert await get_relation(OWNER) == "owner"
    assert await get_relation(OWNER) == "owner"
    assert calls["n"] == 1, "非空集合 load 一次后应缓存,不再重复 load"


async def test_load_owner_ids_delegates_to_typed_query(monkeypatch):
    """业务 loader 只保留 cache/fail-closed 语义，DB 读取交给 query 层。"""
    calls = {"n": 0}

    async def fake_find_owner_ids() -> set[str]:
        calls["n"] += 1
        return {OWNER, OWNER2}

    monkeypatch.setattr(ir, "find_owner_common_user_ids", fake_find_owner_ids)

    assert await ir._load_owner_ids() == {OWNER, OWNER2}
    assert calls["n"] == 1
