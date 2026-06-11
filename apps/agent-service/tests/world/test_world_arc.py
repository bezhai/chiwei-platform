"""世界阶段快照的持久化契约 — 活的世界（时间常数分层）的慢层.

world 的状态分两层钟、两张表：

  * :class:`WorldState` —— 「此刻」的客观叙述快照，每轮都可能重写，明天就过时。
  * :class:`WorldArc` —— 「世界走到哪个阶段」的自然语言全文快照，只在翻页级转变
    （考完 / 放榜 / 搬家 / 换季）时整篇重写，写进去的话下周读仍然为真。as_latest
    （append-only + 读最新一版），Key 带 lane。

这些都是真实 Postgres 持久化测试（testcontainers）——世界阶段的正确性故事全在
"能不能 append 进去、版本是否递增、能不能按 lane 查回最新一版"，mock pg
等于什么都没测。lane 隔离是命门：coe / ppe 绝不能覆盖 prod 的世界阶段。
"""

from __future__ import annotations

import pytest

from app.world.arc import WorldArc, read_world_arc, write_world_arc
from tests.runtime.conftest import migrate


@pytest.fixture
async def arc_db(test_db):
    await migrate(WorldArc, test_db)
    yield test_db


def test_worldarc_key_carries_lane():
    """WorldArc 的自然键必须含 lane —— 泳道隔离的硬约束（同 WorldState）。"""
    from app.runtime.data import key_fields

    assert "lane" in key_fields(WorldArc)


def test_worldarc_fields_avoid_framework_reserved_columns():
    """字段名不撞框架保留列（id / created_at / updated_at / dedup_hash）。

    翻页时刻所以叫 ``turned_at`` 而不是 ``created_at``——后者是框架的落库时刻，
    语义不同且是保留列。
    """
    reserved = {"id", "created_at", "updated_at", "dedup_hash"}
    assert not reserved & set(WorldArc.model_fields)
    assert "turned_at" in WorldArc.model_fields


@pytest.mark.integration
async def test_write_then_read_world_arc(arc_db):
    """写一版世界阶段 → 读回最新（含全文 narrative + 翻页时刻 turned_at）。"""
    await write_world_arc(
        lane="coe-t2",
        narrative="高考结束了，赤尾进入考后的漫长暑假，在等放榜。",
        turned_at="2026-06-09T18:00:00+08:00",
    )

    arc = await read_world_arc(lane="coe-t2")
    assert arc is not None
    assert "高考结束" in arc.narrative
    assert arc.turned_at == "2026-06-09T18:00:00+08:00"


@pytest.mark.integration
async def test_world_arc_appends_incrementing_versions(arc_db):
    """append-only 版本链：每次写入版本递增，历史保留。"""
    from app.runtime.persist import select_all_versions

    await write_world_arc(
        lane="coe-t2",
        narrative="赤尾在备考，全家围着高考转。",
        turned_at="2026-06-01T08:00:00+08:00",
    )
    await write_world_arc(
        lane="coe-t2",
        narrative="高考结束了，赤尾进入考后的漫长暑假。",
        turned_at="2026-06-09T18:00:00+08:00",
    )

    versions = await select_all_versions(WorldArc, {"lane": "coe-t2"})
    assert [arc.version for arc in versions] == [1, 2], (
        "世界阶段是 append-only 版本链：版本必须逐次递增、旧版保留"
    )


@pytest.mark.integration
async def test_read_world_arc_returns_latest_version(arc_db):
    """as_latest：再写一版 read 拿到的是最新那版（翻页取代旧页，不是排在后面）。"""
    await write_world_arc(
        lane="coe-t2",
        narrative="赤尾在备考。",
        turned_at="2026-06-01T08:00:00+08:00",
    )
    await write_world_arc(
        lane="coe-t2",
        narrative="高考结束了。",
        turned_at="2026-06-09T18:00:00+08:00",
    )

    arc = await read_world_arc(lane="coe-t2")
    assert arc is not None
    assert arc.narrative == "高考结束了。"
    assert arc.turned_at == "2026-06-09T18:00:00+08:00"


@pytest.mark.integration
async def test_read_world_arc_cold_start_returns_none(arc_db):
    """没写过任何世界阶段的 lane 读回 None（冷启动：世界阶段还是空白，prompt 引导补写）。"""
    assert await read_world_arc(lane="coe-never-written") is None


@pytest.mark.integration
async def test_update_arc_tool_persists_to_pg_and_reads_back(arc_db):
    """工具级真链路：真实调用 update_arc 工具 → 真 PG 落库 → read_world_arc 读回同一 narrative。

    test_world_tools.py 对 update_arc mock 了 write_world_arc（只测透传）；这里不 mock
    持久化，走完整链路：工具体从 ambient AgentContext 读 lane（monkeypatch 工具的 lane
    来源 = 造一个带 world_lane 的 context）→ write_world_arc 真 insert_append 进 PG →
    read_world_arc 按同一 lane 读回最新一版。turned_at 由工具体自填现实 CST（带 +08:00）。
    """
    from app.agent.context import AgentContext
    from app.agent.runtime_context import agent_context
    from app.world.tools import update_arc

    narrative = "高考结束了，赤尾进入考后的漫长暑假，在等放榜。"
    ctx = AgentContext(
        features={"world_lane": "coe-t2", "world_round_id": "round-arc-int"}
    )
    with agent_context(ctx):
        await update_arc.invoke({"narrative": narrative})

    arc = await read_world_arc(lane="coe-t2")
    assert arc is not None
    assert arc.narrative == narrative, "工具真调 → 真 PG 落库 → 读回必须是同一 narrative"
    # turned_at 由工具体自填现实 CST（不让模型编）：非空、带 CST 偏移
    assert arc.turned_at
    assert "+08:00" in arc.turned_at
    # lane 隔离：别的 lane 读不到这条世界阶段
    assert await read_world_arc(lane="prod") is None


@pytest.mark.integration
async def test_world_arc_lane_isolation(arc_db):
    """两个 lane 各一条世界阶段，互不可见，coe 绝不覆盖 prod 的世界阶段。"""
    await write_world_arc(
        lane="prod",
        narrative="prod 的世界阶段。",
        turned_at="2026-06-09T18:00:00+08:00",
    )
    await write_world_arc(
        lane="coe-t2",
        narrative="coe 的世界阶段。",
        turned_at="2026-06-09T19:00:00+08:00",
    )

    prod_arc = await read_world_arc(lane="prod")
    coe_arc = await read_world_arc(lane="coe-t2")
    assert prod_arc.narrative == "prod 的世界阶段。"
    assert coe_arc.narrative == "coe 的世界阶段。"
