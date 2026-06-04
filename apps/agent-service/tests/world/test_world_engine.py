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

import fakeredis.aioredis
import pytest

import app.world.engine as engine_mod
from app.agent.neutral import Message, Role
from app.world.engine import WorldTick, world_tick
from app.world.tools import WORLD_TOOLS


@pytest.fixture(autouse=True)
def _fake_redis(monkeypatch):
    """In-memory redis for world_tick 的串行化锁 + session 续接读写.

    world_tick 现在开头按 actor 拿单飞锁（覆盖全段），并用确定性 session_id
    读 / 写 transcript。这两条都打 redis，给 fakeredis 让引擎单测自包含。同时
    重置 ``get_redis_capability`` 的 singleton（它可能被先前测试用真 redis 填过，
    monkeypatch ``_redis`` 不会影响已建的 singleton）。
    """
    import app.capabilities.redis as cap_mod
    import app.infra.redis as redis_mod

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_mod, "_redis", fake)
    monkeypatch.setattr(cap_mod, "_singleton", None)


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

    async def fake_list_recent_intents(*, lane, since_iso):
        # 默认空批次：专测引擎机制的用例不碰真库。需要断言意图内容的用例自己
        # 覆写这个桩（monkeypatch.setattr engine_mod.list_recent_intents）。
        return []

    monkeypatch.setattr(engine_mod, "read_world_state", fake_read_world_state)
    monkeypatch.setattr(engine_mod, "read_presence", fake_read_presence)
    monkeypatch.setattr(engine_mod, "write_world_state", fake_write_world_state)
    monkeypatch.setattr(engine_mod, "renotify_unread", fake_renotify_unread)
    monkeypatch.setattr(engine_mod, "list_recent_intents", fake_list_recent_intents)

    engine_mod._test_world_writes = world_writes  # type: ignore[attr-defined]
    engine_mod._test_renotify_calls = renotify_calls  # type: ignore[attr-defined]
    yield


def _mock_run(monkeypatch, *, order: list[str] | None = None):
    """把 ``Agent.run`` 换成记录调用参数的桩，返回 captured。

    记录传给 run 的 context / max_retries / tools，以及（可选）相对
    renotify 的调用顺序，用来断言 renotify 先于循环。
    """
    captured: dict = {}

    async def fake_run(
        self, messages, *, prompt_vars=None, context=None, session_id=None, max_retries=2
    ):
        if order is not None:
            order.append("run")
        captured["messages"] = messages
        captured["prompt_vars"] = prompt_vars
        captured["context"] = context
        captured["session_id"] = session_id
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
async def test_world_tick_passes_daily_world_session_id_explicitly(monkeypatch):
    """run 收到**显式** session_id = make_session_id(lane, "world", 今天)。

    续接命门：world_tick 必须把确定性 session_id 显式传给 ``Agent.run(session_id=)``，
    让 task1 的 run 从 Redis 读历史续接（task1 给 run 加的显式 session_id 优先于
    context.session_id）。只靠 context.session_id 只是 langfuse 归类、不触发续接。
    """
    from app.agent.trace import make_session_id

    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    today = datetime.now().strftime("%Y-%m-%d")
    expected = make_session_id("coe-t2", "world", today)
    assert captured["session_id"] == expected, (
        "world_tick 必须把确定性 session_id 显式传给 run（续接靠它，不能只靠 ctx）"
    )
    # context.session_id 仍带同一 id（langfuse 归类一致）
    assert captured["context"].session_id == expected


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
    """intent 唤醒：意图内容透给模型（在喂入的 prompt context 里）。

    内容现在从 PG 读全那一批 intent（list_recent_intents）拼进 prompt，不再靠合并闸
    透进来的单条 payload。这里 stub 查询返回这条意图，断言它出现在喂给模型的 context。
    """
    from app.domain.world_events import IntentRaised

    async def fake_list_recent_intents(*, lane, since_iso):
        return [
            IntentRaised(lane=lane, intent_id="i1", persona_id="chinagi",
                         summary="我想起床去厨房煮咖啡", occurred_at="2026-06-04T08:00:00+08:00"),
        ]

    monkeypatch.setattr(engine_mod, "list_recent_intents", fake_list_recent_intents)
    captured = _mock_run(monkeypatch)

    await world_tick(
        WorldTick(
            lane="coe-t2",
            reason="intent",
            intent_id="i1",
            intent_persona_id="chinagi",
            intent_summary="我想起床去厨房煮咖啡",
        )
    )

    blob = "".join(m.text() for m in captured["messages"])
    assert "煮咖啡" in blob


@pytest.mark.asyncio
async def test_intent_wake_reads_all_recent_intents_not_just_latest(monkeypatch):
    """codex 致命修复：intent 唤醒从 PG 读这段时间所有 intent 全喂 world，不止合并闸最后一条。

    合并闸 latest-only 只把最后一条 intent 的 payload 透进 WorldTick；前面几条
    （life "想去厨房 / 出门"）对 world 等价丢失、困死能动性。修：world 被 intent
    唤醒时调 list_recent_intents 读这段时间所有 intent，整批拼进喂 world 的 prompt。
    这里 tick 只带最后一条（千凪），但 PG 里还有前两条（绫奈出门 / 赤尾找人）——
    三条都必须出现在喂给模型的 context 里。
    """
    from app.domain.world_events import IntentRaised

    async def fake_list_recent_intents(*, lane, since_iso):
        return [
            IntentRaised(lane=lane, intent_id="i1", persona_id="ayana",
                         summary="我想出门上学", occurred_at="2026-06-04T08:00:00+08:00"),
            IntentRaised(lane=lane, intent_id="i2", persona_id="akao",
                         summary="我想去找千凪", occurred_at="2026-06-04T08:00:20+08:00"),
            IntentRaised(lane=lane, intent_id="i3", persona_id="chinagi",
                         summary="我想起床去厨房煮咖啡", occurred_at="2026-06-04T08:00:40+08:00"),
        ]

    monkeypatch.setattr(engine_mod, "list_recent_intents", fake_list_recent_intents)
    captured = _mock_run(monkeypatch)

    # 闸传进来的只有最后一条（千凪）
    await world_tick(
        WorldTick(
            lane="coe-t2",
            reason="intent",
            intent_id="i3",
            intent_persona_id="chinagi",
            intent_summary="我想起床去厨房煮咖啡",
        )
    )

    blob = "".join(m.text() for m in captured["messages"])
    # 三条 intent 全在喂给模型的 context 里（不止最后一条）
    assert "我想出门上学" in blob, "前面被合并闸丢掉的 intent（绫奈出门）必须从 PG 读回喂 world"
    assert "我想去找千凪" in blob, "前面被合并闸丢掉的 intent（赤尾找人）必须从 PG 读回喂 world"
    assert "我想起床去厨房煮咖啡" in blob, "最后那条 intent 也在"


@pytest.mark.asyncio
async def test_intent_wake_query_since_uses_lane_and_lookback(monkeypatch):
    """intent 唤醒按 lane 读、since 截断点存在（不读无界历史）。

    断言查询用对了 lane，且 since_iso 非空（有时间窗下界，不把所有历史 intent
    全捞出来）。具体 since 取值由实现决定（自上次快照 world_time 或最近 lookback），
    这里只钉死"按 lane 限定 + 有界窗口"两条契约。
    """
    calls: list[dict] = []

    async def fake_list_recent_intents(*, lane, since_iso):
        calls.append({"lane": lane, "since_iso": since_iso})
        return []

    monkeypatch.setattr(engine_mod, "list_recent_intents", fake_list_recent_intents)
    _mock_run(monkeypatch)

    await world_tick(
        WorldTick(
            lane="coe-t2",
            reason="intent",
            intent_id="i1",
            intent_persona_id="chinagi",
            intent_summary="我想去厨房",
        )
    )

    assert len(calls) == 1, "intent 唤醒必须读一次最近 intent 批次"
    assert calls[0]["lane"] == "coe-t2"
    assert calls[0]["since_iso"], "since 截断点必须非空（有界窗口，不读无界历史）"


@pytest.mark.asyncio
async def test_intent_wake_window_anchored_to_intent_not_world_time(monkeypatch):
    """codex 关键修复：since 窗口锚定触发 intent 的 occurred_at，不被 world_time 漂移挤掉。

    codex 场景：heartbeat / self / 并发 world 轮次推进了 world_time，但它们不读 intent。
    随后 intent wake 若用 world_time（更晚的快照）当 since 下界，会把这条**未被消费的**
    intent 排除出窗口 → 又静默丢掉，正是本次要修的 bug 复发。

    这里让快照 world_time 比 intent occurred_at **更晚**（模拟 heartbeat 已推进时钟），
    断言 since_iso 仍 ≤ intent 的 occurred_at（窗口覆盖这条未消费 intent）。
    """
    from datetime import timedelta

    from app.world.engine import _CST
    from app.world.state import WorldState

    now = datetime.now(_CST)
    intent_occurred = (now - timedelta(seconds=40)).isoformat()  # intent 40s 前起的
    # 快照 world_time 比 intent 还新（heartbeat 在 intent 之后推进了时钟，但没读 intent）
    snapshot_world_time = (now - timedelta(seconds=10)).isoformat()

    async def fresh_snapshot(*, lane):
        return WorldState(lane=lane, world_time=snapshot_world_time, situation="")

    captured_since: list[str] = []

    async def fake_list_recent_intents(*, lane, since_iso):
        captured_since.append(since_iso)
        return []

    monkeypatch.setattr(engine_mod, "read_world_state", fresh_snapshot)
    monkeypatch.setattr(engine_mod, "list_recent_intents", fake_list_recent_intents)
    _mock_run(monkeypatch)

    await world_tick(
        WorldTick(
            lane="coe-t2",
            reason="intent",
            intent_id="i-late",
            intent_persona_id="chinagi",
            intent_summary="我想去厨房",
            intent_occurred_at=intent_occurred,
        )
    )

    assert len(captured_since) == 1
    since_dt = datetime.fromisoformat(captured_since[0])
    intent_dt = datetime.fromisoformat(intent_occurred)
    assert since_dt <= intent_dt, (
        f"since 窗口必须覆盖触发 intent 的 occurred_at（{intent_occurred}），"
        f"不能被更晚的 world_time 快照挤掉，实际 since={captured_since[0]}"
    )


def test_intent_since_cutoff_anchors_on_intent_occurred_at():
    """回看窗口下界 = 触发 intent 的 occurred_at - lookback（锚 intent，不锚 now/world_time）。

    occurred_at 缺失 / naive 时退回 now - lookback 兜底（不抛、仍读近窗 intent）。
    """
    from datetime import timedelta

    from app.world.engine import (
        _CST,
        WORLD_INTENT_LOOKBACK_SECONDS,
        WorldTick,
        _intent_since_cutoff,
    )

    now = datetime(2026, 6, 4, 8, 0, 0, tzinfo=_CST)
    occurred = (now - timedelta(seconds=40)).isoformat()

    tick = WorldTick(
        lane="x", reason="intent", intent_id="i1",
        intent_persona_id="akao", intent_summary="s",
        intent_occurred_at=occurred,
    )
    cutoff = datetime.fromisoformat(_intent_since_cutoff(tick, now))
    assert cutoff == datetime.fromisoformat(occurred) - timedelta(
        seconds=WORLD_INTENT_LOOKBACK_SECONDS
    ), "下界 = 触发 intent occurred_at - lookback"

    # occurred_at 缺失：退回 now - lookback 兜底
    tick_empty = WorldTick(
        lane="x", reason="intent", intent_id="i2",
        intent_persona_id="akao", intent_summary="s",
    )
    cutoff_fallback = datetime.fromisoformat(_intent_since_cutoff(tick_empty, now))
    assert cutoff_fallback == now - timedelta(seconds=WORLD_INTENT_LOOKBACK_SECONDS), (
        "occurred_at 缺失退回 now - lookback 兜底"
    )


@pytest.mark.asyncio
async def test_non_intent_wake_does_not_read_intents(monkeypatch):
    """heartbeat / self 唤醒不读 intent 批次（只有 intent 唤醒才需要呈现意图）。"""
    calls: list = []

    async def fake_list_recent_intents(*, lane, since_iso):
        calls.append(lane)
        return []

    monkeypatch.setattr(engine_mod, "list_recent_intents", fake_list_recent_intents)
    _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))
    await world_tick(WorldTick(lane="coe-t2", reason="self"))

    assert calls == [], "非 intent 唤醒不该读 intent 批次"


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
    async def fake_run(
        self, messages, *, prompt_vars=None, context=None, session_id=None, max_retries=2
    ):
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

    async def capture_run(
        self, messages, *, prompt_vars=None, context=None, session_id=None, max_retries=2
    ):
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

    async def capture_run(
        self, messages, *, prompt_vars=None, context=None, session_id=None, max_retries=2
    ):
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
    """intent_to_world_tick 把 IntentRaised.intent_id 透传进 IntentWorldTick（闸 + round_id 派生靠它）。

    现在 intent 不直接打 WorldTick，而是经一个 transient IntentWorldTick 走 60s
    合并闸（debounce）再翻成 WorldTick。intent_id 必须透传，闸合并 / round_id 派生
    / durable 重投幂等都靠它。
    """
    from app.domain.world_events import IntentRaised
    from app.world.engine import IntentWorldTick

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

    assert isinstance(emitted[0], IntentWorldTick)
    assert emitted[0].intent_id == "intent-xyz"
    assert emitted[0].lane == "coe-t2"
    assert emitted[0].intent_persona_id == "akao"
    assert emitted[0].intent_summary == "我想去厨房"
    # occurred_at 透传：闸后 world 读 PG 窗口锚定它（不锚 world_time），跨重投稳定不变。
    assert emitted[0].intent_occurred_at == "2026-06-03T12:30:00Z"


@pytest.mark.asyncio
async def test_world_instruction_demands_objective_projection():
    """给模型的指令是 agent 工具指令、客观投影不碰情绪（赤尾宪法）。"""
    instruction = engine_mod.world_loop_instruction()
    assert "客观" in instruction
    assert ("情绪" in instruction) or ("主观" in instruction) or ("解读" in instruction)
    # agent 工具指令：提到她能用工具行动
    assert ("emit_event" in instruction) or ("产" in instruction)
    assert ("sleep" in instruction) or ("再看" in instruction)


@pytest.mark.asyncio
async def test_world_instruction_defaults_to_quiet_not_overproduce():
    """循环指令改成"默认安静流动 / 符合世界就只 sleep 不广播"（降频软引导）。

    上一轮调出 82/min 的根是指令写着"宁可多产 / 别睡太久 / 连续行动"。改成引导
    她大部分时刻安静流动：收到反馈 / 意图后，若那件事符合世界、不需要客观纠偏，
    就只 sleep 不广播；只在真发生值得感知的客观变化时才 emit。这是软内容引导
    （赤尾宪法：不加 if 分支强制），所以只断言指令文本已改、不断言行为。
    """
    instruction = engine_mod.world_loop_instruction()
    # 旧的"宁可多产 / 别睡太久"催产措辞必须移除（那是 82/min 的根）
    assert "宁可多产" not in instruction
    assert "别睡太久" not in instruction
    # 新引导：安静流动 + 符合世界就只 sleep 不广播 + 只在客观变化才 emit
    assert ("安静" in instruction) or ("平静" in instruction) or ("流动" in instruction)
    assert ("不广播" in instruction) or ("只 sleep" in instruction) or ("不 emit" in instruction)
    assert ("符合" in instruction) or ("纠偏" in instruction)


@pytest.mark.asyncio
async def test_world_tick_stamps_round_marker_in_stimulus(monkeypatch):
    """喂给循环的 user 消息里带本轮 round_id 标记（turn 幂等查重靠它）。

    重投幂等命门：同一 durable intent 重投得同一 round_id，把 round_id 印进
    stimulus，写回 transcript 后下次重投能从历史里查到这个标记 → 跳过、不重复
    追加同一轮、不重复 emit。
    """
    captured = _mock_run(monkeypatch)

    tick = WorldTick(
        lane="coe-t2",
        reason="intent",
        intent_id="intent-marker-1",
        intent_persona_id="akao",
        intent_summary="我想去厨房",
    )
    await world_tick(tick)

    now_iso = datetime.now(engine_mod._CST).isoformat()
    round_id = engine_mod._derive_round_id(tick, now_iso)
    blob = "".join(m.text() for m in captured["messages"])
    assert round_id in blob, "stimulus 必须带本轮 round_id 标记（幂等查重靠它）"


@pytest.mark.asyncio
async def test_intent_replay_skips_already_processed_round(monkeypatch):
    """同一 durable intent 重投：第二次从 session 历史查到本轮标记 → 跳过，不再 run。

    第一次 run 把带 round_id 标记的本轮写进 transcript；第二次重投（同 intent_id
    → 同 round_id）world_tick 先 load_session 查到该标记，直接收口跳过：不再 run、
    不再 emit、不重复追加 transcript（决策 7 turn 幂等）。
    """
    run_calls: list = []

    async def fake_run(
        self, messages, *, prompt_vars=None, context=None, session_id=None, max_retries=2
    ):
        run_calls.append(session_id)
        # 模拟 task1 的 run：把带 round 标记的本轮写回 session（真 Redis）
        from app.agent.session import append_session

        await append_session(session_id, list(messages))
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(engine_mod.Agent, "run", fake_run)

    tick = WorldTick(
        lane="coe-t2",
        reason="intent",
        intent_id="intent-replay-1",
        intent_persona_id="akao",
        intent_summary="我想去厨房",
    )
    await world_tick(tick)
    await world_tick(tick)  # durable 重投同一条

    assert len(run_calls) == 1, (
        f"同一 intent 重投不该再跑一轮（turn 幂等），实际 run {len(run_calls)} 次"
    )


@pytest.mark.asyncio
async def test_world_tick_serializes_under_actor_lock(monkeypatch):
    """world_tick 按 actor 拿单飞锁、覆盖全段：并发第二源拿不到锁不能并行进。

    确定性 session_id 把三源打到同一个 Redis transcript key，并发会互相覆盖。
    所以 world 按 actor（lane）串行化，锁覆盖「读历史→run→写回」整段。这里让第一
    轮 run 阻塞住、并发起第二轮，断言第二轮在第一轮持锁期间进不去 run。
    """
    import asyncio

    in_run = asyncio.Event()
    release = asyncio.Event()
    concurrent_runs = {"max": 0, "now": 0}

    async def slow_run(
        self, messages, *, prompt_vars=None, context=None, session_id=None, max_retries=2
    ):
        concurrent_runs["now"] += 1
        concurrent_runs["max"] = max(concurrent_runs["max"], concurrent_runs["now"])
        in_run.set()
        await release.wait()
        concurrent_runs["now"] -= 1
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(engine_mod.Agent, "run", slow_run)

    # 第一轮 heartbeat 进 run 后阻塞住
    first = asyncio.create_task(world_tick(WorldTick(lane="coe-t2", reason="heartbeat")))
    await in_run.wait()
    # 持锁期间起第二轮 self（同 lane）—— 拿不到锁，应被串行化处理（不并行进 run）
    second = asyncio.create_task(world_tick(WorldTick(lane="coe-t2", reason="self")))
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.gather(first, second)

    assert concurrent_runs["max"] == 1, (
        "两源并发不能同时进 run（锁必须覆盖全段、串行化）"
    )


@pytest.mark.asyncio
async def test_heartbeat_lock_conflict_is_swallowed(monkeypatch):
    """heartbeat 撞锁：吞掉（log+return），不抛——它是 10min 保底冗余，丢一次无害。"""
    from app.runtime.single_flight import single_flight

    _mock_run(monkeypatch)

    # 先把 world lane 的锁占住，模拟另一轮在跑
    async with single_flight("world:coe-t2", ttl=60):
        # heartbeat 撞锁不该抛（吞掉）
        await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))


@pytest.mark.asyncio
async def test_self_lock_conflict_is_swallowed(monkeypatch):
    """self 撞锁：吞掉（log+return），不抛——自排是冗余，正在跑的那轮会自己再排。"""
    from app.runtime.single_flight import single_flight

    _mock_run(monkeypatch)

    async with single_flight("world:coe-t2", ttl=60):
        await world_tick(WorldTick(lane="coe-t2", reason="self"))


@pytest.mark.asyncio
async def test_intent_lock_conflict_raises_for_reschedule(monkeypatch):
    """intent 撞锁：抛 SingleFlightConflict 让上游（debounce 闸）重排——绝不丢意图。"""
    from app.runtime.single_flight import SingleFlightConflict, single_flight

    _mock_run(monkeypatch)

    async with single_flight("world:coe-t2", ttl=60):
        with pytest.raises(SingleFlightConflict):
            await world_tick(
                WorldTick(
                    lane="coe-t2",
                    reason="intent",
                    intent_id="i-conflict",
                    intent_persona_id="akao",
                    intent_summary="我想去厨房",
                )
            )
