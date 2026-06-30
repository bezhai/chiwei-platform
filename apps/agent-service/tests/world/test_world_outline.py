"""world 续写的工作记忆「大纲」(WorldOutline) 持久化契约 + update_outline 工具.

大纲是 world 续写自己维护的「活的 spec」——记着世界此刻正在走的几条未完成客观线
（每条「现在走到哪 + 客观上接下来怎么走 + 改写/结束条件」）。结构照 ``WorldArc`` 的
append-only 版本链：``lane`` Key、``narrative`` 整篇重写的全文、``outlined_at`` 时间
标注、``version`` 自增。读侧只读最新一版（as_latest）。

两层测试：
  * Data 层（``@pytest.mark.integration``，真实 Postgres / testcontainers）——大纲的
    正确性故事全在「能不能 append、版本是否递增、能不能按 lane 查回最新一版」，mock
    pg 等于什么都没测。lane 隔离是命门：coe / ppe 绝不能覆盖 prod 的大纲。
  * 工具层（stub 持久化）——``update_outline`` 薄 wrap：从 ambient context 读 lane、
    自填现实 CST 时刻、调 ``write_world_outline`` append 一版、只碰大纲不碰别的状态。
"""

from __future__ import annotations

import pytest

import app.world.tools as tools_mod
from app.agent.context import AgentContext
from app.agent.runtime_context import agent_context
from app.world.outline import (
    WorldOutline,
    read_world_outline,
    write_world_outline,
)
from app.world.tools import FEATURE_SELF_WAKE, WORLD_TOOLS, update_outline
from tests.runtime.conftest import migrate


@pytest.fixture
async def outline_db(test_db):
    await migrate(WorldOutline, test_db)
    yield test_db


# ---------------------------------------------------------------------------
# Data 层契约（结构 / 保留列 / 版本链 / lane 隔离）
# ---------------------------------------------------------------------------


def test_worldoutline_key_carries_lane():
    """WorldOutline 的自然键必须含 lane —— 泳道隔离的硬约束（同 WorldArc / WorldState）。"""
    from app.runtime.data import key_fields

    assert "lane" in key_fields(WorldOutline)


def test_worldoutline_fields_avoid_framework_reserved_columns():
    """字段名不撞框架保留列（id / created_at / updated_at / dedup_hash）。

    梳理大纲的时刻所以叫 ``outlined_at`` 而不是 ``created_at``——后者是框架的落库
    时刻，语义不同且是保留列（语义对齐 WorldArc.turned_at）。
    """
    reserved = {"id", "created_at", "updated_at", "dedup_hash"}
    assert not reserved & set(WorldOutline.model_fields)
    assert "outlined_at" in WorldOutline.model_fields
    # 字段集合就是这四样，不多不少（结构同 WorldArc）
    assert set(WorldOutline.model_fields) == {
        "lane",
        "narrative",
        "outlined_at",
        "version",
    }


@pytest.mark.integration
async def test_write_then_read_world_outline(outline_db):
    """写一版大纲 → 读回最新（含全文 narrative + 梳理时刻 outlined_at）。"""
    await write_world_outline(
        lane="coe-t2",
        narrative="绫奈生病：在医院等检查结果→该出结果、医生给诊断。",
        outlined_at="2026-06-09T18:00:00+08:00",
    )

    outline = await read_world_outline(lane="coe-t2")
    assert outline is not None
    assert "绫奈生病" in outline.narrative
    assert outline.outlined_at == "2026-06-09T18:00:00+08:00"


@pytest.mark.integration
async def test_world_outline_appends_incrementing_versions(outline_db):
    """append-only 版本链：每次写入版本递增，历史保留。"""
    from app.runtime.persist import select_all_versions

    await write_world_outline(
        lane="coe-t2",
        narrative="绫奈生病：刚挂急诊，在候诊。",
        outlined_at="2026-06-01T08:00:00+08:00",
    )
    await write_world_outline(
        lane="coe-t2",
        narrative="绫奈生病：在医院做完检查，等结果。",
        outlined_at="2026-06-01T11:00:00+08:00",
    )

    versions = await select_all_versions(WorldOutline, {"lane": "coe-t2"})
    assert [o.version for o in versions] == [1, 2], (
        "大纲是 append-only 版本链：版本必须逐次递增、旧版保留"
    )


@pytest.mark.integration
async def test_read_world_outline_returns_latest_version(outline_db):
    """as_latest：再写一版 read 拿到的是最新那版（整篇重写取代旧版，不是排在后面）。"""
    await write_world_outline(
        lane="coe-t2",
        narrative="绫奈生病：在候诊。",
        outlined_at="2026-06-01T08:00:00+08:00",
    )
    await write_world_outline(
        lane="coe-t2",
        narrative="绫奈生病：诊断出来了，是急性肠胃炎，要住院观察。",
        outlined_at="2026-06-01T15:00:00+08:00",
    )

    outline = await read_world_outline(lane="coe-t2")
    assert outline is not None
    assert outline.narrative == "绫奈生病：诊断出来了，是急性肠胃炎，要住院观察。"
    assert outline.outlined_at == "2026-06-01T15:00:00+08:00"


@pytest.mark.integration
async def test_read_world_outline_cold_start_returns_none(outline_db):
    """没写过任何大纲的 lane 读回 None（冷启动：大纲还是空白，prompt 引导续写补写）。"""
    assert await read_world_outline(lane="coe-never-written") is None


@pytest.mark.integration
async def test_world_outline_lane_isolation(outline_db):
    """两个 lane 各一份大纲，互不可见，coe 绝不覆盖 prod 的大纲。"""
    await write_world_outline(
        lane="prod",
        narrative="prod 的大纲。",
        outlined_at="2026-06-09T18:00:00+08:00",
    )
    await write_world_outline(
        lane="coe-t2",
        narrative="coe 的大纲。",
        outlined_at="2026-06-09T19:00:00+08:00",
    )

    prod_outline = await read_world_outline(lane="prod")
    coe_outline = await read_world_outline(lane="coe-t2")
    assert prod_outline.narrative == "prod 的大纲。"
    assert coe_outline.narrative == "coe 的大纲。"


@pytest.mark.integration
async def test_update_outline_tool_persists_to_pg_and_reads_back(outline_db):
    """工具级真链路：真实调用 update_outline 工具 → 真 PG 落库 → read_world_outline 读回同一 narrative。

    不 mock 持久化，走完整链路：工具体从 ambient AgentContext 读 lane → write_world_outline
    真 insert_append 进 PG → read_world_outline 按同一 lane 读回最新一版。outlined_at 由
    工具体自填现实 CST（带 +08:00、不让模型编）。
    """
    narrative = "期中考：下周三开考→这几天学校在出考场安排，到时候出成绩。"
    ctx = AgentContext(
        features={"world_lane": "coe-t2", "world_round_id": "round-outline-int"}
    )
    with agent_context(ctx):
        await update_outline.invoke({"narrative": narrative})

    outline = await read_world_outline(lane="coe-t2")
    assert outline is not None
    assert outline.narrative == narrative, "工具真调 → 真 PG 落库 → 读回必须是同一 narrative"
    # outlined_at 由工具体自填现实 CST（不让模型编）：非空、带 CST 偏移
    assert outline.outlined_at
    assert "+08:00" in outline.outlined_at
    # lane 隔离：别的 lane 读不到这份大纲
    assert await read_world_outline(lane="prod") is None


# ---------------------------------------------------------------------------
# 工具层契约（stub 持久化，专测 update_outline 薄 wrap 的副作用）
# ---------------------------------------------------------------------------


@pytest.fixture
def world_ctx():
    """world 本轮的 ambient context：lane + 确定性 round_id + 待办 self-wake 容器。"""
    return AgentContext(
        session_id="coe-t2:world:2026-06-16",
        features={
            "world_lane": "coe-t2",
            "world_round_id": "round-outline",
            FEATURE_SELF_WAKE: {},
        },
    )


@pytest.fixture
def stub_outline(monkeypatch):
    """stub 大纲写入 + WorldState 写入 + 信箱投递，专测工具只碰大纲、不碰别的状态。"""
    outline_writes: list[dict] = []

    async def fake_write_world_outline(*, lane, narrative, outlined_at):
        outline_writes.append(
            {"lane": lane, "narrative": narrative, "outlined_at": outlined_at}
        )

    state_writes: list[dict] = []

    async def fake_write_world_state(*, lane, world_time, detail):
        state_writes.append(
            {"lane": lane, "world_time": world_time, "detail": detail}
        )

    delivered: list[dict] = []

    async def fake_deliver_event(**kwargs):
        delivered.append(kwargs)
        return 1

    monkeypatch.setattr(tools_mod, "write_world_outline", fake_write_world_outline)
    monkeypatch.setattr(tools_mod, "write_world_state", fake_write_world_state)
    monkeypatch.setattr(tools_mod, "deliver_event", fake_deliver_event)
    return {
        "outline_writes": outline_writes,
        "state_writes": state_writes,
        "delivered": delivered,
    }


async def test_update_outline_writes_narrative_with_self_filled_outlined_at(
    world_ctx, stub_outline
):
    """update_outline 落 narrative durable（write_world_outline），outlined_at 由工具体自填现实 CST。

    与 update_world 对 world_time / update_arc 对 turned_at 的处理同族对称：梳理大纲的
    时刻是客观时间、不让模型编，由工具体按现实当前 CST 自填。
    """
    with agent_context(world_ctx):
        await update_outline.invoke(
            {
                "narrative": "绫奈生病：在医院等检查结果→该出诊断；期中考：下周三开考。"
            }
        )

    assert len(stub_outline["outline_writes"]) == 1
    w = stub_outline["outline_writes"][0]
    assert w["lane"] == "coe-t2"
    assert (
        w["narrative"]
        == "绫奈生病：在医院等检查结果→该出诊断；期中考：下周三开考。"
    )
    # outlined_at 由工具体自填现实 CST（不让模型编）：非空、带 CST 偏移
    assert w["outlined_at"]
    assert "+08:00" in w["outlined_at"]


async def test_update_outline_outlined_at_is_not_modeled(
    world_ctx, stub_outline, monkeypatch
):
    """outlined_at 取现实当前 CST（cst_time.now_cst_iso），客观时间不让模型给。"""
    monkeypatch.setattr(
        tools_mod.cst_time, "now_cst_iso", lambda: "2026-06-10T09:00:00+08:00"
    )
    with agent_context(world_ctx):
        await update_outline.invoke({"narrative": "新起一条线：季节转秋。"})

    assert (
        stub_outline["outline_writes"][0]["outlined_at"]
        == "2026-06-10T09:00:00+08:00"
    )


async def test_update_outline_only_writes_outline_not_state_or_mailbox(
    world_ctx, stub_outline
):
    """update_outline 只写大纲：不碰 WorldState 快照、不投递任何信箱（与既有工具互不干扰）。"""
    with agent_context(world_ctx):
        await update_outline.invoke({"narrative": "大纲改了一版。"})

    assert stub_outline["state_writes"] == [], "update_outline 不该写 WorldState 快照"
    assert stub_outline["delivered"] == [], "update_outline 不该投递任何信箱 event"
    assert len(stub_outline["outline_writes"]) == 1


def test_update_outline_in_world_tools():
    """update_outline 挂进续写工具集（WORLD_TOOLS 含它），与 update_world / sense / sleep 并列。

    大纲是续写自己的工作记忆——写和用是同一个脑子，所以归续写工具集（不学 update_arc
    那样进反思独占的 WORLD_REFLECT_TOOLS）。
    """
    assert update_outline in WORLD_TOOLS


def test_update_outline_not_in_reflect_tools():
    """update_outline 不在反思工具集——它是续写的工作记忆，不归 reflection 独占。"""
    from app.world.tools import WORLD_REFLECT_TOOLS

    assert update_outline not in WORLD_REFLECT_TOOLS


def test_update_outline_docstring_pins_content_contract():
    """update_outline 的 docstring（喂给 LLM 的工具说明）必须钉死大纲的内容契约。

    大纲、detail、arc、life 都是自然语言，不在工具说明里钉住边界会互相污染。必须含：
    ① 写什么（未完成的客观线 + 现在走到哪 + 客观接下来怎么走 + 改写/结束条件）；
    ② 三条边界（不放现场描写=detail、不放主观感受=life、不放跨周月底色=arc）；
    ③ 整篇重写语义（取代而非追加、不写历史流水账）。
    """
    doc = update_outline.definition.description
    # ① 写什么
    assert "客观线" in doc
    assert "走到哪" in doc
    assert "接下来" in doc
    assert "结束" in doc and "改写" in doc
    # ② 三条边界
    assert "现场" in doc, "必须钉死「不写现场描写」（detail 的边界）"
    assert "主观" in doc, "必须钉死「不写主观感受」（life 的边界）"
    assert "跨周月" in doc, "必须钉死「不写跨周月底色」（arc 的边界）"
    # ③ 整篇重写、不是追加、不写流水账
    assert "重写" in doc
    assert "流水账" in doc
