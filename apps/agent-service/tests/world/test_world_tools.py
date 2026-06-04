"""world 工具（move_persona / emit_event / sleep）契约 — Task 2.

world 不再"填表"，而是在一个 agent 循环里连续调工具行动。这些工具是对现成
handler（set_presence / personas_in_room+deliver_event / emit_delayed）的薄 wrap，
工具体从 ambient ``AgentContext`` 读 world 本轮的 lane + round_id（不在工具签名里
让模型填——lane / round 是机制层的，不是世界内容决策）。

这些测试 stub 现成 handler（不碰真库），钉死工具机制层硬约束：
  * move_persona → set_presence（按 ctx 的 lane）；
  * emit_event → 取所锚房间在场者、对每人 deliver_event（产生侧在场过滤）；
  * emit_event 的 event_id **基于 lane+room+summary+round_id 派生**，同输入同 id
    （整轮重放幂等命门）、不同 summary 不同 id；
  * sleep ≤ 1h 合法 → emit_delayed 一条 self WorldTick（delay=秒*1000）；
  * sleep > 1h → 返回错误喂回模型（不静默夹）、不 emit_delayed。
"""

from __future__ import annotations

import pytest

import app.world.tools as tools_mod
from app.agent.context import AgentContext
from app.agent.runtime_context import agent_context
from app.world.tools import (
    FEATURE_EMIT_COUNT,
    FEATURE_SELF_WAKE,
    WORLD_EMIT_SOFT_CAP,
    WORLD_SLEEP_MAX_SECONDS,
    derive_event_id,
    emit_event,
    move_persona,
    sleep,
)


def _round_features() -> dict:
    """world_tick 每轮新建的 round-scoped 可变状态（emit 计数 + 待办 self-wake）。

    engine 在跑循环前把这两块 round-scoped 可变 state 塞进 features，工具体
    跨多次调用读写它（emit 累计计数、sleep 覆盖待办 self-wake），engine 在循环
    收口后读它落安全阀 / 一次 self-wake。
    """
    return {
        "world_lane": "coe-t2",
        "world_round_id": "round-abc",
        FEATURE_EMIT_COUNT: {"n": 0},
        FEATURE_SELF_WAKE: {},
    }


@pytest.fixture
def _ctx():
    """world 本轮的 ambient context：lane + 确定性 round_id + round-scoped 状态。"""
    return AgentContext(features=_round_features())


@pytest.fixture(autouse=True)
def _stub_handlers(monkeypatch):
    """stub 现成 handler，专测工具薄 wrap 的副作用，不碰真库。"""
    presence = {"chinagi": "kitchen", "akao": "akao_room"}

    set_calls: list[dict] = []

    async def fake_set_presence(*, lane, persona_id, room_id):
        presence[persona_id] = room_id
        set_calls.append({"lane": lane, "persona_id": persona_id, "room_id": room_id})

    async def fake_in_room(*, lane, room_id):
        return [p for p, r in presence.items() if r == room_id]

    delivered: list[dict] = []

    async def fake_deliver_event(**kwargs):
        delivered.append(kwargs)
        return 1

    monkeypatch.setattr(tools_mod, "set_presence", fake_set_presence)
    monkeypatch.setattr(tools_mod, "personas_in_room", fake_in_room)
    monkeypatch.setattr(tools_mod, "deliver_event", fake_deliver_event)

    tools_mod._test_presence = presence  # type: ignore[attr-defined]
    tools_mod._test_set_calls = set_calls  # type: ignore[attr-defined]
    tools_mod._test_delivered = delivered  # type: ignore[attr-defined]
    # sleep 不再直接 emit_delayed（它把待办 self-wake 记进 round state、由 engine
    # 收口后 emit），这个空列表只用来断言"tool 层没有任何直接 self-wake 发生"。
    tools_mod._test_self_wakes = []  # type: ignore[attr-defined]
    yield


@pytest.mark.asyncio
async def test_move_persona_sets_presence_by_ctx_lane(_ctx):
    """move_persona → set_presence，lane 从 ctx 取（不让模型填 lane）。"""
    with agent_context(_ctx):
        await move_persona.invoke({"persona_id": "akao", "room_id": "kitchen"})

    assert {"lane": "coe-t2", "persona_id": "akao", "room_id": "kitchen"} in (
        tools_mod._test_set_calls
    )
    assert tools_mod._test_presence["akao"] == "kitchen"


@pytest.mark.asyncio
async def test_emit_event_delivers_to_in_room_personas_only(_ctx):
    """emit_event 取所锚房间在场者、对每人 deliver_event（产生侧在场过滤）。"""
    with agent_context(_ctx):
        await emit_event.invoke(
            {"room_id": "kitchen", "summary": "飘来煎蛋和咖啡的香味"}
        )

    recipients = {d["persona_id"] for d in tools_mod._test_delivered}
    assert recipients == {"chinagi"}  # 只有 chinagi 在 kitchen
    d = tools_mod._test_delivered[0]
    assert d["room_id"] == "kitchen"
    assert d["summary"] == "飘来煎蛋和咖啡的香味"
    assert d["kind"] == "ambient"
    assert d["source"] == "world"
    assert d["lane"] == "coe-t2"


@pytest.mark.asyncio
async def test_emit_event_empty_room_delivers_to_nobody(_ctx):
    """锚到没人在的房间 → 不投给任何人（不为不在场的人产 event）。"""
    with agent_context(_ctx):
        await emit_event.invoke(
            {"room_id": "balcony", "summary": "阳台的花被风吹动"}
        )
    assert tools_mod._test_delivered == []


@pytest.mark.asyncio
async def test_emit_event_id_is_idempotent_per_round(_ctx):
    """同一 (lane, room, summary, round_id) 派生同一 event_id —— 整轮重放幂等命门。

    所有收件人共享这一条 event 的同一 id；同输入再调一次仍是同一 id。
    """
    with agent_context(_ctx):
        await emit_event.invoke(
            {"room_id": "kitchen", "summary": "飘来煎蛋和咖啡的香味"}
        )
    first_ids = {d["event_id"] for d in tools_mod._test_delivered}
    assert len(first_ids) == 1  # 一条 event 一个 id（投给多人也是同一条）

    # 同输入重放（整轮重放 / 模型重复调）→ 同一 event_id（deliver_event 幂等去重）
    tools_mod._test_delivered.clear()
    with agent_context(_ctx):
        await emit_event.invoke(
            {"room_id": "kitchen", "summary": "飘来煎蛋和咖啡的香味"}
        )
    second_ids = {d["event_id"] for d in tools_mod._test_delivered}
    assert second_ids == first_ids, "同输入重放应派生同一 event_id（幂等）"


@pytest.mark.asyncio
async def test_emit_event_id_differs_per_summary(_ctx):
    """不同 summary → 不同 event_id（不同的事是不同的 event）。"""
    id_a = derive_event_id(lane="coe-t2", room_id="kitchen", summary="A", round_id="r")
    id_b = derive_event_id(lane="coe-t2", room_id="kitchen", summary="B", round_id="r")
    assert id_a != id_b


@pytest.mark.asyncio
async def test_sleep_within_limit_records_pending_self_wake(_ctx):
    """sleep ≤ 1h 合法 → 把待办 self-wake 记进 round-scoped state（不直接 emit）。

    sleep 不再每次都 emit_delayed 一条 self WorldTick（那会多轮累积唤醒风暴）；
    它只把"下次几时醒"记进本轮 round state，engine 在循环收口后才 emit 一条。
    """
    with agent_context(_ctx):
        await sleep.invoke({"seconds": 1800})  # 30 分钟，合法

    # sleep 不直接 emit_delayed
    assert tools_mod._test_self_wakes == []
    # 待办 self-wake 记进 round state，delay = 秒*1000
    assert _ctx.features[FEATURE_SELF_WAKE]["delay_ms"] == 1_800_000


@pytest.mark.asyncio
async def test_sleep_at_limit_is_allowed(_ctx):
    """sleep == 1h 上限 → 合法（边界含上限），记进 round state。"""
    with agent_context(_ctx):
        await sleep.invoke({"seconds": WORLD_SLEEP_MAX_SECONDS})
    assert (
        _ctx.features[FEATURE_SELF_WAKE]["delay_ms"]
        == WORLD_SLEEP_MAX_SECONDS * 1000
    )


@pytest.mark.asyncio
async def test_multi_sleep_in_round_does_not_accumulate_last_wins(_ctx):
    """一轮内多次 sleep 不累积 self-wake —— 最后一次为准（决策 4 唤醒风暴命门）。

    本分支之前修过"每轮无条件累积 self tick"，sleep 同源：多轮 / 一轮内多次
    sleep 若各 emit 一条 delayed WorldTick 会叠加未来 self-wake → 唤醒风暴。
    现在一轮最多留一条待办 self-wake，重复 sleep 覆盖而非追加。
    """
    with agent_context(_ctx):
        await sleep.invoke({"seconds": 300})
        await sleep.invoke({"seconds": 600})
        await sleep.invoke({"seconds": 900})

    # 不直接 emit；round state 里只剩最后一次的待办（覆盖，不累积）
    assert tools_mod._test_self_wakes == []
    assert _ctx.features[FEATURE_SELF_WAKE]["delay_ms"] == 900_000


@pytest.mark.asyncio
async def test_sleep_over_limit_returns_error_no_pending_wake(_ctx):
    """sleep > 1h → 返回错误喂回模型让它重调（不静默夹）、不留待办 self-wake。"""
    with agent_context(_ctx):
        result = await sleep.invoke({"seconds": WORLD_SLEEP_MAX_SECONDS + 1})

    # @tool_error 把错误包成 ToolOutcomeError dict 喂回模型（kind=tool_error）
    assert isinstance(result, dict)
    assert result.get("kind") == "tool_error"
    # 绝不静默夹到上限：没有任何自排发生（既不 emit、也不留待办）
    assert tools_mod._test_self_wakes == []
    assert _ctx.features[FEATURE_SELF_WAKE] == {}


@pytest.mark.asyncio
async def test_emit_soft_cap_logs_and_closes_off_further_emits(_ctx, caplog):
    """emit 到本轮 soft cap → logger.warning（不静默）+ 收口拒绝后续 emit（不静默继续）。

    决策 4 安全阀：正常够不着，触顶要 log + 收口（拒投并把提示喂回模型让它停），
    绝不静默继续投。
    """
    import logging

    # 把在场塞满，确保每次 emit 都真投递（计数才会涨）。
    tools_mod._test_presence["chinagi"] = "kitchen"

    # 先把计数顶到 cap-1，再连续 emit 越过 cap。
    with agent_context(_ctx), caplog.at_level(logging.WARNING):
        for i in range(WORLD_EMIT_SOFT_CAP):
            r = await emit_event.invoke(
                {"room_id": "kitchen", "summary": f"动静{i}"}
            )
            assert not (isinstance(r, dict) and r.get("kind")), "cap 之内不该被拒"
        # 第 cap+1 条：触顶被收口
        capped = await emit_event.invoke(
            {"room_id": "kitchen", "summary": "越界的动静"}
        )

    delivered_summaries = {d["summary"] for d in tools_mod._test_delivered}
    # 越界那条没被投递（收口拒投，不静默继续）
    assert "越界的动静" not in delivered_summaries
    # 触顶 log 不静默
    assert any("cap" in rec.message.lower() or "上限" in rec.message for rec in caplog.records)
    # 返回提示喂回模型（字符串提示或结构化拒绝都行，关键是不是正常成功确认）
    assert isinstance(capped, str)
    assert "已在" not in capped  # 不是正常 "已在 X 产生动静" 的成功文案


@pytest.mark.asyncio
async def test_emit_one_recipient_failure_does_not_strand_others(_ctx, caplog):
    """emit 对在场者逐个独立投递：中途一人失败不影响其他人 + log 失败的 persona。"""
    import logging

    # 三人都在 kitchen
    tools_mod._test_presence["chinagi"] = "kitchen"
    tools_mod._test_presence["akao"] = "kitchen"
    tools_mod._test_presence["ayana"] = "kitchen"

    delivered: list[dict] = []

    async def flaky_deliver(**kwargs):
        if kwargs["persona_id"] == "akao":
            raise RuntimeError("akao 信箱暂时挂了")
        delivered.append(kwargs)
        return 1

    import app.world.tools as tm

    orig = tm.deliver_event
    tm.deliver_event = flaky_deliver  # type: ignore[assignment]
    try:
        with agent_context(_ctx), caplog.at_level(logging.WARNING):
            result = await emit_event.invoke(
                {"room_id": "kitchen", "summary": "厨房水声"}
            )
    finally:
        tm.deliver_event = orig  # type: ignore[assignment]

    got = {d["persona_id"] for d in delivered}
    # akao 失败，但 chinagi / ayana 仍收到（一人失败不炸整条 emit）
    assert "chinagi" in got and "ayana" in got
    assert "akao" not in got
    # 失败的 persona 被 log
    assert any("akao" in rec.message for rec in caplog.records)
    # 整条 emit 不抛、不被 @tool_error 包成错误
    assert not (isinstance(result, dict) and result.get("kind"))
