"""ThinkingTokensSpent 持久化契约 —— 观测刀：每轮 world/life 思考的 token 落 durable PG。

为什么这张表存在（已实证）：langfuse 在系统性丢 durable 工具的 trace（实测 akao 在
PG ``data_act_performed`` 有 45 条 act、langfuse 名下 0 条 ``tool.act`` trace），基于
langfuse 的成本统计严重失真。PG 是可靠真相、langfuse 是 best-effort 会丢。所以把"一
轮思考用了多少 token"如实落 durable PG，按 actor 可聚合、不依赖会丢的 langfuse。

集成测试（真实 Postgres）：整个正确性故事是 insert_idempotent 落一行、按自然键去重、
lane / actor 隔离、读回各 token 维度 —— mock pg 等于什么都没测。
"""

from __future__ import annotations

import pytest

from app.domain.thinking_cost import (
    ThinkingTokensSpent,
    record_round_cost,
    record_thinking_tokens,
)
from app.runtime.data import key_fields
from app.runtime.persist import select_all_versions
from tests.runtime.conftest import migrate

# collect_usage 累计 dict 的形态（input/output/total/cache_read_input_tokens/calls）。
_USAGE = {
    "input": 120,
    "output": 40,
    "total": 160,
    "cache_read_input_tokens": 30,
    "calls": 3,
}


def test_thinking_tokens_key_carries_lane_actor_round():
    """自然键 = (lane, actor, round_id)：泳道 + 角色 + 本轮三重隔离 / 去重。"""
    keys = key_fields(ThinkingTokensSpent)
    assert "lane" in keys
    assert "actor" in keys
    assert "round_id" in keys


def test_thinking_tokens_fields_dont_clash_with_reserved():
    """三步检查①：字段名不撞 framework 保留列（id / created_at / updated_at / dedup_hash）。"""
    reserved = {"id", "created_at", "updated_at", "dedup_hash"}
    assert reserved.isdisjoint(set(ThinkingTokensSpent.model_fields))


@pytest.fixture
async def thinking_cost_db(test_db):
    """Build the ThinkingTokensSpent table on the test db."""
    await migrate(ThinkingTokensSpent, test_db)
    yield test_db


@pytest.mark.integration
async def test_record_then_read_back_all_token_dims(thinking_cost_db):
    """record 一轮 → 读回 input/output/total/cached/model_calls + actor/observed_at。"""
    await record_thinking_tokens(
        lane="coe-t",
        actor="akao",
        round_id="r1",
        input_tokens=120,
        output_tokens=40,
        total_tokens=160,
        cached_tokens=30,
        model_calls=3,
        observed_at="2026-06-07T12:30:00+08:00",
    )

    rows = await select_all_versions(
        ThinkingTokensSpent, {"lane": "coe-t", "actor": "akao", "round_id": "r1"}
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.input_tokens == 120
    assert row.output_tokens == 40
    assert row.total_tokens == 160
    assert row.cached_tokens == 30
    assert row.model_calls == 3
    assert row.actor == "akao"
    assert row.observed_at == "2026-06-07T12:30:00+08:00"


@pytest.mark.integration
async def test_record_is_idempotent_on_natural_key(thinking_cost_db):
    """同一 (lane, actor, round_id) 重投（整轮重试 / 重投）只落一行（不重复计成本）。"""
    for _ in range(2):
        await record_thinking_tokens(
            lane="coe-t",
            actor="world",
            round_id="round-dup",
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            cached_tokens=0,
            model_calls=1,
            observed_at="2026-06-07T08:00:00+08:00",
        )

    rows = await select_all_versions(
        ThinkingTokensSpent,
        {"lane": "coe-t", "actor": "world", "round_id": "round-dup"},
    )
    assert len(rows) == 1, "重投同一轮不能重复计成本（insert_idempotent 去重）"


@pytest.mark.integration
async def test_lane_isolation_on_thinking_tokens(thinking_cost_db):
    """lane 隔离命门：prod 与 coe 各自的成本记录绝不互相覆盖 / 互读。"""
    await record_thinking_tokens(
        lane="prod", actor="world", round_id="r",
        input_tokens=100, output_tokens=10, total_tokens=110,
        cached_tokens=0, model_calls=1, observed_at="2026-06-07T08:00:00+08:00",
    )
    await record_thinking_tokens(
        lane="coe-t", actor="world", round_id="r",
        input_tokens=1, output_tokens=1, total_tokens=2,
        cached_tokens=0, model_calls=1, observed_at="2026-06-07T08:00:00+08:00",
    )

    prod = await select_all_versions(
        ThinkingTokensSpent, {"lane": "prod", "actor": "world", "round_id": "r"}
    )
    coe = await select_all_versions(
        ThinkingTokensSpent, {"lane": "coe-t", "actor": "world", "round_id": "r"}
    )
    assert prod[0].total_tokens == 110
    assert coe[0].total_tokens == 2


@pytest.mark.integration
async def test_actor_isolation_on_thinking_tokens(thinking_cost_db):
    """actor 隔离：world 与某 persona 的成本各成一行，可按 actor 聚合（这刀的目的）。"""
    await record_thinking_tokens(
        lane="coe-t", actor="world", round_id="r",
        input_tokens=200, output_tokens=20, total_tokens=220,
        cached_tokens=0, model_calls=2, observed_at="2026-06-07T08:00:00+08:00",
    )
    await record_thinking_tokens(
        lane="coe-t", actor="akao", round_id="r",
        input_tokens=50, output_tokens=10, total_tokens=60,
        cached_tokens=0, model_calls=1, observed_at="2026-06-07T08:00:00+08:00",
    )

    world = await select_all_versions(
        ThinkingTokensSpent, {"lane": "coe-t", "actor": "world", "round_id": "r"}
    )
    akao = await select_all_versions(
        ThinkingTokensSpent, {"lane": "coe-t", "actor": "akao", "round_id": "r"}
    )
    assert world[0].total_tokens == 220
    assert akao[0].total_tokens == 60


# ---------------------------------------------------------------------------
# record_round_cost —— world/life 收口共用的 best-effort 入口：把 collect_usage 的
# 累计 dict 映射成 record_thinking_tokens 各维度落库；失败吞掉不抛（成本观测是旁路）。
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_record_round_cost_maps_usage_dict_to_token_dims(thinking_cost_db):
    """record_round_cost 把 collect_usage 累计 dict 映射成各 token 维度落库。"""
    await record_round_cost(
        lane="coe-t",
        actor="akao",
        round_id="r1",
        usage=_USAGE,
        observed_at="2026-06-07T12:30:00+08:00",
    )

    rows = await select_all_versions(
        ThinkingTokensSpent, {"lane": "coe-t", "actor": "akao", "round_id": "r1"}
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.input_tokens == 120
    assert row.output_tokens == 40
    assert row.total_tokens == 160
    assert row.cached_tokens == 30, "cache_read_input_tokens → cached_tokens"
    assert row.model_calls == 3, "calls → model_calls"


async def test_record_round_cost_swallows_insert_failure(monkeypatch):
    """PG insert 抛错时 record_round_cost 吞掉不抛（best-effort：旁路持久化失败绝不拖垮一轮思考）。

    钉的真实行为：``record_thinking_tokens``（PG insert 入口）抛 DB 错时，
    ``record_round_cost`` 只 log、不抛——成本观测是旁路。这是 fail-fast 改造后
    **只有 insert 留在 try 里**的那条吞错路径。
    """
    import app.domain.thinking_cost as tc

    async def boom(**kwargs):
        raise RuntimeError("PG down")

    monkeypatch.setattr(tc, "record_thinking_tokens", boom)

    # 不该抛 —— 这正是 best-effort 语义（无需真库，纯吞错路径）。
    await record_round_cost(
        lane="coe-t",
        actor="world",
        round_id="r1",
        usage=_USAGE,
        observed_at="2026-06-07T12:30:00+08:00",
    )


@pytest.mark.parametrize(
    "missing", ["input", "output", "total", "cache_read_input_tokens", "calls"]
)
async def test_record_round_cost_raises_on_missing_usage_key(monkeypatch, missing):
    """usage dict 缺键 → record_round_cost 抛 KeyError（契约错误必须暴露、绝不被当落库失败吞掉）。

    钉的真实行为：usage 形态漂移 / 缺键是**契约错误**，跟旁路 PG insert 失败是两码事。
    fail-fast 改造把 usage 字典读取移出 try——缺键直接抛 KeyError，让契约错误炸出来，
    而不是被 best-effort except 静默吞成"落库失败"（成本永远记不上却没人知道）。
    monkeypatch insert 成功，确保抛的是缺键 KeyError 而非 insert 路径的错。
    """
    import app.domain.thinking_cost as tc

    async def noop_insert(**kwargs):
        return None

    monkeypatch.setattr(tc, "record_thinking_tokens", noop_insert)

    broken_usage = {k: v for k, v in _USAGE.items() if k != missing}

    with pytest.raises(KeyError):
        await record_round_cost(
            lane="coe-t",
            actor="world",
            round_id="r1",
            usage=broken_usage,
            observed_at="2026-06-07T12:30:00+08:00",
        )
