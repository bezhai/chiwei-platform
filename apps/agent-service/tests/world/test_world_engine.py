"""world engine 节点契约 — Task 2（agent 工具循环）.

world 是发动机，被三源唤醒（保底心跳 / 自排提前卡点 / life 回灌的
``IntentRaised``）。被唤醒后它**跑一个 agent 工具循环**：连续调
move_persona / emit_event / sleep 推进世界，平淡时段也主动产 event，直到自己
不再调工具就收口。它不再"填表"返回一个结构化大对象。

这些测试 mock ``Agent.run``（模拟模型连续调工具）+ stub 现成 handler，钉死
机制层硬约束，不是 LLM 决策：

  * world_tick 跑通 agent 循环、把工具交给 Agent；
  * **开头先 renotify_unread**（信箱对账自愈，先于循环、不依赖循环成功）；
  * run 传 ``max_retries=1``（关掉整轮重放 durable 工具）；
  * run 传正确的 session_id（``make_session_id(lane, "world", 今天)``）；
  * run 的 context.features 带 world 本轮 lane + round_id（工具体读它行动）；
  * cold_start：无 WorldState 时也走循环、prompt 里告诉模型这是冷启动；
  * 落一版只含 world_time 的快照（不写 situation —— spec 决策 6）；
  * 给模型的指令是 agent 工具指令、客观投影不碰情绪。

赤尾设计宪法：world 醒后产什么 event、挪谁、睡多久全由 LLM 在循环里判断，
代码里没有阈值 / 计数器替它决策。10 分钟保底心跳 + sleep 上限只决定"何时醒 /
别睡死"，绝不进入世界内容决策。
"""

from __future__ import annotations

from datetime import datetime

import pytest

import app.world.engine as engine_mod
from app.agent.neutral import Message, Role
from app.world.engine import WorldTick, world_tick
from app.world.tools import WORLD_TOOLS


@pytest.fixture(autouse=True)
def _stub_state(monkeypatch):
    """world 节点读快照、写快照、对账信箱都打桩，专测引擎机制（不碰真库）。"""

    async def fake_read_world_state(*, lane):
        from app.world.state import WorldState

        return WorldState(
            lane=lane,
            world_time="2026-06-03T06:30:00+08:00",
            situation="",
        )

    async def fake_read_presence(*, lane, persona_id):
        return {"chinagi": "kitchen", "akao": "akao_room"}.get(persona_id)

    world_writes: list[dict] = []

    async def fake_write_world_state(*, lane, world_time, situation):
        world_writes.append(
            {"lane": lane, "world_time": world_time, "situation": situation}
        )

    renotify_calls: list[str] = []

    async def fake_renotify_unread(*, lane):
        renotify_calls.append(lane)
        return 0

    monkeypatch.setattr(engine_mod, "read_world_state", fake_read_world_state)
    monkeypatch.setattr(engine_mod, "read_presence", fake_read_presence)
    monkeypatch.setattr(engine_mod, "write_world_state", fake_write_world_state)
    monkeypatch.setattr(engine_mod, "renotify_unread", fake_renotify_unread)

    engine_mod._test_world_writes = world_writes  # type: ignore[attr-defined]
    engine_mod._test_renotify_calls = renotify_calls  # type: ignore[attr-defined]
    yield


def _mock_run(monkeypatch, *, order: list[str] | None = None):
    """把 ``Agent.run`` 换成记录调用参数的桩，返回 captured。

    记录传给 run 的 context / max_retries / tools，以及（可选）相对
    renotify 的调用顺序，用来断言 renotify 先于循环。
    """
    captured: dict = {}

    async def fake_run(self, messages, *, prompt_vars=None, context=None, max_retries=2):
        if order is not None:
            order.append("run")
        captured["messages"] = messages
        captured["prompt_vars"] = prompt_vars
        captured["context"] = context
        captured["max_retries"] = max_retries
        captured["tools"] = self._tools
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(engine_mod.Agent, "run", fake_run)
    return captured


@pytest.mark.asyncio
async def test_world_tick_runs_agent_loop_with_world_tools(monkeypatch):
    """world_tick 把 world 工具集交给 Agent 跑循环。"""
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert captured["tools"] == WORLD_TOOLS, "world_tick 应把 WORLD_TOOLS 交给 Agent"


@pytest.mark.asyncio
async def test_world_tick_passes_max_retries_one(monkeypatch):
    """run 必须传 max_retries=1：关掉整轮重放（durable 工具不能被重放）。"""
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert captured["max_retries"] == 1, (
        "world 调 run 必须 max_retries=1，否则瞬时失败会整轮重放已执行的 durable 工具"
    )


@pytest.mark.asyncio
async def test_world_tick_passes_daily_world_session_id(monkeypatch):
    """run 的 context.session_id = make_session_id(lane, "world", 今天)。"""
    from app.agent.trace import make_session_id

    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    ctx = captured["context"]
    assert ctx is not None and ctx.session_id is not None
    today = datetime.now().strftime("%Y-%m-%d")
    # lane / 角色固定 world / 当天 —— 同一天多次唤醒落同一 session
    assert ctx.session_id.startswith(make_session_id("coe-t2", "world", today)[:18])
    assert "world" in ctx.session_id


@pytest.mark.asyncio
async def test_world_tick_context_carries_lane_and_round(monkeypatch):
    """run 的 context.features 带本轮 lane + round_id（工具体读它行动 + 派生 event_id）。"""
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    feats = captured["context"].features
    assert feats.get("world_lane") == "coe-t2"
    assert feats.get("world_round_id"), "本轮必须带确定性 round_id（event_id 派生靠它）"


@pytest.mark.asyncio
async def test_world_tick_renotifies_before_loop(monkeypatch):
    """信箱对账自愈在 agent 循环之前跑（兜底不依赖循环成功）。"""
    order: list[str] = []

    async def fake_renotify(*, lane):
        order.append("renotify")
        return 0

    monkeypatch.setattr(engine_mod, "renotify_unread", fake_renotify)
    _mock_run(monkeypatch, order=order)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert order == ["renotify", "run"], (
        f"renotify_unread 应在 agent 循环之前跑，实际 {order}"
    )


@pytest.mark.asyncio
async def test_world_tick_renotify_uses_current_lane(monkeypatch):
    """对账按当前 lane 调。"""
    _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert engine_mod._test_renotify_calls == ["coe-t2"]


@pytest.mark.asyncio
async def test_world_tick_writes_world_time_snapshot_no_situation(monkeypatch):
    """落一版快照：world_time 跟现实走、不写 situation（决策 6）。"""
    _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert engine_mod._test_world_writes, "每次唤醒必须落最新 world_time"
    w = engine_mod._test_world_writes[-1]
    # world_time 跟现实走，不是固定起手快照时刻
    assert w["world_time"] != "2026-06-03T06:30:00+08:00"
    # 不让模型写 situation：落空（不是第二事实源）
    assert w["situation"] == ""


@pytest.mark.asyncio
async def test_cold_start_runs_loop_and_tells_model(monkeypatch):
    """冷启动：无 WorldState 时也走循环，prompt 里告诉模型这是冷启动。"""

    async def no_world_state(*, lane):
        return None

    monkeypatch.setattr(engine_mod, "read_world_state", no_world_state)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    # 走了循环（run 被调）且喂给模型的内容提到冷启动 / 首次
    blob = "".join(m.text() for m in captured["messages"])
    assert ("冷启动" in blob) or ("首次" in blob)
    # 冷启动也落一版快照
    assert engine_mod._test_world_writes


@pytest.mark.asyncio
async def test_intent_wake_passes_intent_to_model(monkeypatch):
    """intent 唤醒：意图内容透给模型（在喂入的 prompt context 里）。"""
    captured = _mock_run(monkeypatch)

    await world_tick(
        WorldTick(
            lane="coe-t2",
            reason="intent",
            intent_persona_id="chinagi",
            intent_summary="我想起床去厨房煮咖啡",
        )
    )

    blob = "".join(m.text() for m in captured["messages"])
    assert "煮咖啡" in blob


@pytest.mark.asyncio
async def test_world_tick_seeds_round_scoped_emit_and_wake_state(monkeypatch):
    """run 的 context.features 带 round-scoped 的 emit 计数 + 待办 self-wake 容器。

    每轮新建可变 state（emit 累计安全阀靠它、sleep 覆盖待办 self-wake 靠它），
    工具体跨多次调用读写、engine 收口后读它落安全阀 / 一次 self-wake。
    """
    from app.world.tools import FEATURE_EMIT_COUNT, FEATURE_SELF_WAKE

    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    feats = captured["context"].features
    assert feats.get(FEATURE_EMIT_COUNT) == {"n": 0}, "每轮新建归零的 emit 计数容器"
    assert feats.get(FEATURE_SELF_WAKE) == {}, "每轮新建空的待办 self-wake 容器"


@pytest.mark.asyncio
async def test_world_tick_emits_one_self_wake_from_round_state(monkeypatch):
    """循环里（模拟模型调 sleep）记下的待办 self-wake，engine 收口后只 emit 一条。

    firing 走工具域的 ``tools.emit_delayed``（fire_self_wake 收口），patch 那里。
    """
    import app.world.tools as tools_mod
    from app.world.tools import FEATURE_SELF_WAKE

    delayed: list[dict] = []

    async def fake_emit_delayed(data, *, delay_ms, durability="durable"):
        delayed.append({"data": data, "delay_ms": delay_ms})

    monkeypatch.setattr(tools_mod, "emit_delayed", fake_emit_delayed)

    # 模拟模型在循环里调了两次 sleep（最后一次为准），写进 round state。
    async def fake_run(self, messages, *, prompt_vars=None, context=None, max_retries=2):
        context.features[FEATURE_SELF_WAKE]["delay_ms"] = 300_000
        context.features[FEATURE_SELF_WAKE]["delay_ms"] = 600_000
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(engine_mod.Agent, "run", fake_run)

    await world_tick(WorldTick(lane="coe-t2", reason="self"))

    assert len(delayed) == 1, "一轮最多 emit 一条 self WorldTick（最后一次 sleep 为准）"
    assert delayed[0]["delay_ms"] == 600_000
    tick = delayed[0]["data"]
    assert tick.lane == "coe-t2"
    assert tick.reason == "self"


@pytest.mark.asyncio
async def test_world_tick_no_sleep_emits_no_self_wake(monkeypatch):
    """循环没调 sleep（无待办）→ engine 不 emit self-wake（靠保底心跳补）。"""
    import app.world.tools as tools_mod

    delayed: list[dict] = []

    async def fake_emit_delayed(data, *, delay_ms, durability="durable"):
        delayed.append({"data": data, "delay_ms": delay_ms})

    monkeypatch.setattr(tools_mod, "emit_delayed", fake_emit_delayed)
    _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert delayed == [], "没调 sleep 就不该 emit self-wake"


@pytest.mark.asyncio
async def test_intent_wake_round_id_stable_across_replays(monkeypatch):
    """同一 IntentRaised（同 intent_id）重投 → 同 round_id（外层重放幂等命门）。

    round_id 不能从 now_iso 派生：world_tick 半途失败被 durable 重投时新 now_iso
    会让同一条 event 落新 id、deliver_event 去重失效。intent 唤醒用 intent 的
    稳定标识（intent_id）派生 round_id，两次重投得同一 round_id → 同 event_id。
    """
    rounds: list[str] = []

    async def capture_run(self, messages, *, prompt_vars=None, context=None, max_retries=2):
        rounds.append(context.features["world_round_id"])
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(engine_mod.Agent, "run", capture_run)

    tick = WorldTick(
        lane="coe-t2",
        reason="intent",
        intent_id="intent-stable-1",
        intent_persona_id="chinagi",
        intent_summary="我想去厨房煮咖啡",
    )
    await world_tick(tick)
    await world_tick(tick)  # durable 重投同一条

    assert rounds[0] == rounds[1], (
        f"同一 intent_id 重投应得同一 round_id，实际 {rounds}"
    )


@pytest.mark.asyncio
async def test_heartbeat_round_id_varies_with_time(monkeypatch):
    """heartbeat/self 唤醒不会 durable 重投 → round_id 从时刻派生即可（随时间变）。"""
    rounds: list[str] = []

    async def capture_run(self, messages, *, prompt_vars=None, context=None, max_retries=2):
        rounds.append(context.features["world_round_id"])
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(engine_mod.Agent, "run", capture_run)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))
    import asyncio

    await asyncio.sleep(0.001)
    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    # 不同时刻两次心跳 round_id 不同（不会重投，无需稳定）
    assert rounds[0] != rounds[1]


@pytest.mark.asyncio
async def test_intent_tick_carries_intent_id(monkeypatch):
    """intent_to_world_tick 把 IntentRaised.intent_id 透传进 WorldTick（round_id 派生靠它）。"""
    from app.domain.world_events import IntentRaised

    emitted: list = []

    async def fake_emit(data):
        emitted.append(data)

    monkeypatch.setattr(engine_mod, "emit", fake_emit)

    await engine_mod.intent_to_world_tick(
        IntentRaised(
            lane="coe-t2",
            intent_id="intent-xyz",
            persona_id="akao",
            summary="我想去厨房",
            occurred_at="2026-06-03T12:30:00Z",
        )
    )

    assert emitted[0].intent_id == "intent-xyz"


@pytest.mark.asyncio
async def test_world_instruction_demands_objective_projection():
    """给模型的指令是 agent 工具指令、客观投影不碰情绪（赤尾宪法）。"""
    instruction = engine_mod.world_loop_instruction()
    assert "客观" in instruction
    assert ("情绪" in instruction) or ("主观" in instruction) or ("解读" in instruction)
    # agent 工具指令：提到她能用工具行动
    assert ("emit_event" in instruction) or ("产" in instruction)
    assert ("sleep" in instruction) or ("再看" in instruction)
