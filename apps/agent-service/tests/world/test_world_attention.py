"""关注（WorldAttention）版本链 + update_attention 工具契约 — world 的眼睛的落点.

关注 = world 经反思环节留给眼睛的「想看哪」。长弧写「世界走到哪」、关注写「眼睛
该看哪」，语义不混放、各落各的版本链。WorldAttention 照 WorldArc 模板：lane Key +
narrative + written_at + Version，append-only、读最新一版、整篇重写——写「当前仍
想看的」，看完的被新版取代、不是追加成清单。**清空也是一版**：append-only 链没有
删除态，反思判断当下没有要看的，就重写一版说明「没有特别要看的」取代旧关注，
否则旧关注会被眼睛永远读下去。

持久化用真实 Postgres（testcontainers）——版本链的正确性故事全在"能不能 append
进去、版本是否递增、能不能按 lane 读回最新一版"，mock pg 等于什么都没测。

工具 update_attention 与 update_arc 完全同契约：签名只留 narrative（语义参数）、
lane 从 ambient context 读、written_at 由工具体自填现实 CST（客观时间不让模型编）、
**不包 @tool_error**（durable 写失败必须炸掉整次反思、由 fail-open 接住同日重试）、
只进 WORLD_REFLECT_TOOLS（续写无手碰关注——姿态物理隔离）。
"""

from __future__ import annotations

import pytest

from app.world.attention import (
    WorldAttention,
    read_world_attention,
    write_world_attention,
)
from tests.runtime.conftest import migrate


@pytest.fixture
async def attention_db(test_db):
    await migrate(WorldAttention, test_db)
    yield test_db


# ---------------------------------------------------------------------------
# Data 骨架（泳道隔离 + 不撞框架保留列）
# ---------------------------------------------------------------------------


def test_worldattention_key_carries_lane():
    """WorldAttention 的自然键必须含 lane —— 泳道隔离的硬约束（同 WorldArc）。"""
    from app.runtime.data import key_fields

    assert "lane" in key_fields(WorldAttention)


def test_worldattention_fields_avoid_framework_reserved_columns():
    """字段名不撞框架保留列（id / created_at / updated_at / dedup_hash）。

    写下时刻所以叫 ``written_at`` 而不是 ``created_at``——后者是框架的落库时刻，
    语义不同且是保留列（同 WorldArc 的 turned_at 教训）。
    """
    reserved = {"id", "created_at", "updated_at", "dedup_hash"}
    assert not reserved & set(WorldAttention.model_fields)
    assert "written_at" in WorldAttention.model_fields


# ---------------------------------------------------------------------------
# update_attention 工具契约（与 update_arc 完全同款）
# ---------------------------------------------------------------------------


def _reflect_ctx():
    """反思环节的 ambient context：lane + round_id 走 features，不进工具签名。"""
    from app.agent.context import AgentContext

    return AgentContext(
        features={"world_lane": "coe-t1", "world_round_id": "round-attn"}
    )


def test_update_attention_only_in_reflect_tools_not_world_tools():
    """update_attention 归反思环节独占：在 WORLD_REFLECT_TOOLS、不在 WORLD_TOOLS。

    关注的写入方 = 反思（单一写入方，闭环每环职责唯一）：续写碰它会把拮抗姿态混
    回去、眼睛只如实报告不决定看什么。靠工具集物理隔离钉死，不靠嘱咐。
    """
    from app.world.tools import (
        WORLD_REFLECT_TOOLS,
        WORLD_TOOLS,
        update_arc,
        update_attention,
    )

    assert update_attention not in WORLD_TOOLS, "续写工具集不得含 update_attention"
    assert WORLD_REFLECT_TOOLS == [update_arc, update_attention], (
        "反思工具集 = 翻页 + 关注两件"
    )


@pytest.mark.asyncio
async def test_update_attention_passes_narrative_with_self_filled_time(monkeypatch):
    """update_attention 透传 narrative，written_at 由工具体自填现实 CST。

    与 update_arc 同契约：lane 从 ambient context 读、时间不让模型编。
    """
    import app.world.tools as tools_mod
    from app.agent.runtime_context import agent_context
    from app.world.tools import update_attention

    writes: list[dict] = []

    async def fake_write(*, lane, narrative, written_at):
        writes.append({"lane": lane, "narrative": narrative, "written_at": written_at})

    monkeypatch.setattr(tools_mod, "write_world_attention", fake_write)

    with agent_context(_reflect_ctx()):
        await update_attention.invoke({"narrative": "想看看这周末天气会不会放晴。"})

    assert len(writes) == 1
    w = writes[0]
    assert w["lane"] == "coe-t1"
    assert w["narrative"] == "想看看这周末天气会不会放晴。"
    # written_at 由工具体自填现实 CST（不让模型编）：非空、带 CST 偏移
    assert w["written_at"]
    assert "+08:00" in w["written_at"]


@pytest.mark.asyncio
async def test_update_attention_write_failure_propagates(monkeypatch):
    """write_world_attention 抛错必须穿透 update_attention 向上炸（不包 @tool_error）。

    与 update_arc 完全同理：durable 写失败若被包成 tool result 字符串喂回模型，
    Agent.run 正常返回 → 反思误判成功 → 假成功落当日标记 → 同日重试被吃掉。
    让异常照实穿透炸掉整次反思，由反思的 fail-open 接住：不落标记、同日重试。
    """
    import app.world.tools as tools_mod
    from app.agent.runtime_context import agent_context
    from app.world.tools import update_attention

    async def boom_write(*, lane, narrative, written_at):
        raise RuntimeError("pg down during attention write")

    monkeypatch.setattr(tools_mod, "write_world_attention", boom_write)

    with agent_context(_reflect_ctx()):
        with pytest.raises(RuntimeError, match="pg down during attention write"):
            await update_attention.invoke({"narrative": "想看的东西写不进去了。"})


def test_update_attention_docstring_pins_rewrite_and_clear_semantics():
    """update_attention 的 docstring（喂给反思 agent 的工具说明）必须钉死关注语义。

    必须含：① 整篇重写当前仍想看的（不是追加成清单）；② 看完的被取代；③ 清空版
    语义（当下没有要看的就写一版说明没有——append-only 链没有删除态，不写这一版
    旧关注会被眼睛永远读下去）；④ 与长弧的分界（长弧写走到哪、关注写想看哪）。
    """
    from app.world.tools import update_attention

    doc = update_attention.definition.description
    # ① 整篇重写、不是追加
    assert "重写" in doc
    assert "追加" in doc
    # ② 看完的被取代
    assert "取代" in doc
    # ③ 清空版：没有要看的也要写一版说明
    assert "没有" in doc and "清空" in doc
    # ④ 与长弧分界
    assert "长弧" in doc
    assert "想看" in doc


# ---------------------------------------------------------------------------
# 真 PG 端到端（版本链 + 泳道隔离 + 工具真链路）
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_write_then_read_world_attention(attention_db):
    """写一版关注 → 读回最新（含全文 narrative + 写下时刻 written_at）。"""
    await write_world_attention(
        lane="coe-t1",
        narrative="想看看这周末天气会不会放晴，户外的安排定不定得下来。",
        written_at="2026-01-10T23:30:00+08:00",
    )

    attention = await read_world_attention(lane="coe-t1")
    assert attention is not None
    assert "放晴" in attention.narrative
    assert attention.written_at == "2026-01-10T23:30:00+08:00"


@pytest.mark.integration
async def test_world_attention_appends_incrementing_versions(attention_db):
    """append-only 版本链：每次写入版本递增，历史保留。"""
    from app.runtime.persist import select_all_versions

    await write_world_attention(
        lane="coe-t1",
        narrative="想看看这周末天气会不会放晴。",
        written_at="2026-01-10T23:30:00+08:00",
    )
    await write_world_attention(
        lane="coe-t1",
        narrative="天气看过了；现在想知道那家新店什么时候开张。",
        written_at="2026-01-11T23:30:00+08:00",
    )

    versions = await select_all_versions(WorldAttention, {"lane": "coe-t1"})
    assert [a.version for a in versions] == [1, 2], (
        "关注是 append-only 版本链：版本必须逐次递增、旧版保留"
    )


@pytest.mark.integration
async def test_read_world_attention_returns_latest_clear_version(attention_db):
    """清空也是一版：写一版「没有特别要看的」取代旧关注，读侧只认它。

    append-only 链没有删除态——反思不写这一版，旧关注会被眼睛永远读下去。
    """
    await write_world_attention(
        lane="coe-t1",
        narrative="想看看这周末天气会不会放晴。",
        written_at="2026-01-10T23:30:00+08:00",
    )
    await write_world_attention(
        lane="coe-t1",
        narrative="当下没有特别要看的。",
        written_at="2026-01-11T23:30:00+08:00",
    )

    attention = await read_world_attention(lane="coe-t1")
    assert attention is not None
    assert attention.narrative == "当下没有特别要看的。"
    assert attention.written_at == "2026-01-11T23:30:00+08:00"


@pytest.mark.integration
async def test_read_world_attention_cold_start_returns_none(attention_db):
    """没写过任何关注的 lane 读回 None（冷启动：眼睛只做本能扫视）。"""
    assert await read_world_attention(lane="coe-never-written") is None


@pytest.mark.integration
async def test_world_attention_lane_isolation(attention_db):
    """两个 lane 各一版关注，互不可见，coe 绝不覆盖 prod 的关注。"""
    await write_world_attention(
        lane="prod",
        narrative="prod 的关注。",
        written_at="2026-01-10T23:30:00+08:00",
    )
    await write_world_attention(
        lane="coe-t1",
        narrative="coe 的关注。",
        written_at="2026-01-10T23:40:00+08:00",
    )

    prod_attention = await read_world_attention(lane="prod")
    coe_attention = await read_world_attention(lane="coe-t1")
    assert prod_attention.narrative == "prod 的关注。"
    assert coe_attention.narrative == "coe 的关注。"


@pytest.mark.integration
async def test_update_attention_tool_persists_to_pg_and_reads_back(attention_db):
    """工具级真链路：真调 update_attention → 真 PG 落库 → read_world_attention 读回。

    不 mock 持久化，走完整链路：工具体从 ambient AgentContext 读 lane →
    write_world_attention 真 insert_append 进 PG → 按同一 lane 读回最新一版。
    written_at 由工具体自填现实 CST（带 +08:00）。
    """
    from app.agent.runtime_context import agent_context
    from app.world.tools import update_attention

    narrative = "想看看这周末天气会不会放晴，户外的安排定不定得下来。"
    with agent_context(_reflect_ctx()):
        await update_attention.invoke({"narrative": narrative})

    attention = await read_world_attention(lane="coe-t1")
    assert attention is not None
    assert attention.narrative == narrative, (
        "工具真调 → 真 PG 落库 → 读回必须是同一 narrative"
    )
    # written_at 由工具体自填现实 CST（不让模型编）：非空、带 CST 偏移
    assert attention.written_at
    assert "+08:00" in attention.written_at
    # lane 隔离：别的 lane 读不到这版关注
    assert await read_world_attention(lane="prod") is None
