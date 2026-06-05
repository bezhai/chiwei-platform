"""world engine 节点契约 — 阶段 1A（world 推演者）.

world 是世界的推演层，不是导演。它被三源唤醒（保底心跳 / 自排提前卡点 / life
回灌的 ``ActPerformed``）。被唤醒后它**跑一个 agent 工具循环**：用 update_world
写下世界此刻的叙述、对收到的角色动作推演客观结果、用 notify 把够得着的角色推演
出来投客观动静、用 sleep 定下次再看。它不替角色决定她想做什么、不批准 / 拒绝
她的动作。

这些测试 mock ``Agent.run``（模拟模型连续调工具）+ stub 现成 handler，钉死
机制层硬约束，不是 LLM 决策：

  * world_tick 跑通 agent 循环、把 WORLD_TOOLS 交给 Agent；
  * **开头先 renotify_unread**（信箱对账自愈，先于循环、不依赖循环成功）；
  * run 传 ``max_retries=1``（关掉整轮重放 durable 工具）；
  * run 传正确的 session_id（``make_session_id(lane, "world", 今天)``）；
  * run 的 context.features 带 world 本轮 lane + round_id（工具体读它行动）；
  * cold_start：无 WorldState 时也走循环、prompt 里告诉模型这是冷启动；
  * **engine 收口不再写快照**（世界叙述改由 update_world 工具在循环里负责写）；
  * act 唤醒：从 PG 读全那一批 act 喂 world、reason="act"。

赤尾设计宪法：world 推演谁够得着、产什么动静、世界什么样全由 LLM 在循环里
判断，代码里没有阈值 / 计数器替它决策。10 分钟保底心跳 + sleep 上限只决定
"何时醒 / 别睡死"，绝不进入世界内容决策。
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
    """In-memory redis for world_tick 的串行化锁."""
    import app.capabilities.redis as cap_mod
    import app.infra.redis as redis_mod

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_mod, "_redis", fake)
    monkeypatch.setattr(cap_mod, "_singleton", None)


@pytest.fixture(autouse=True)
def _inmem_session(monkeypatch):
    """In-memory session transcript store for the engine unit tests."""
    import json

    from app.agent.neutral import Message
    from app.agent import session as session_mod

    store: dict[str, str] = {}

    async def fake_load(session_id: str):
        raw = store.get(session_id)
        if raw is None:
            return []
        return [Message.from_replay_dict(d) for d in json.loads(raw)]

    async def fake_append(session_id: str, new_messages):
        if not new_messages:
            return
        existing = await fake_load(session_id)
        combined = session_mod._cap_transcript(existing + new_messages, session_id)
        store[session_id] = json.dumps(
            [m.to_replay_dict() for m in combined], ensure_ascii=False
        )

    monkeypatch.setattr(engine_mod, "load_session", fake_load)
    monkeypatch.setattr(session_mod, "append_session", fake_append)


@pytest.fixture(autouse=True)
def _stub_state(monkeypatch):
    """world 节点读快照、对账信箱、读 act 批次都打桩，专测引擎机制（不碰真库）。

    收口不再写快照（世界叙述改由 update_world 工具负责写），所以这里不再 stub
    write_world_state。
    """

    async def fake_read_world_state(*, lane):
        from app.world.state import WorldState

        return WorldState(
            lane=lane,
            world_time="2026-06-03T06:30:00+08:00",
            detail="清晨厨房有了动静，千凪在烧水。",
        )

    renotify_calls: list[str] = []

    async def fake_renotify_unread(*, lane):
        renotify_calls.append(lane)
        return 0

    async def fake_list_recent_acts(*, lane, since_iso):
        # 默认空批次：专测引擎机制的用例不碰真库。需要断言动作内容的用例自己
        # 覆写这个桩（monkeypatch.setattr engine_mod.list_recent_acts）。
        return []

    monkeypatch.setattr(engine_mod, "read_world_state", fake_read_world_state)
    monkeypatch.setattr(engine_mod, "renotify_unread", fake_renotify_unread)
    monkeypatch.setattr(engine_mod, "list_recent_acts", fake_list_recent_acts)

    engine_mod._test_renotify_calls = renotify_calls  # type: ignore[attr-defined]
    yield


def _mock_run(monkeypatch, *, order: list[str] | None = None):
    """把 ``Agent.run`` 换成记录调用参数的桩，返回 captured。"""
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
    """run 收到**显式** session_id = make_session_id(lane, "world", 今天)。"""
    from app.agent.trace import make_session_id

    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    today = datetime.now().strftime("%Y-%m-%d")
    expected = make_session_id("coe-t2", "world", today)
    assert captured["session_id"] == expected, (
        "world_tick 必须把确定性 session_id 显式传给 run（续接靠它，不能只靠 ctx）"
    )
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
async def test_world_tick_does_not_write_snapshot_in_engine(monkeypatch):
    """engine 收口不再写快照（世界叙述改由 update_world 工具在循环里写 —— 1A）。

    旧范式 engine 收口落一版只含 world_time 的快照；新范式世界叙述（world_time +
    detail）由 update_world 工具负责，engine 收口只剩 fire_self_wake，不再调
    write_world_state。
    """
    writes: list = []

    async def boom_write(*, lane, world_time, detail):
        writes.append((lane, world_time, detail))

    monkeypatch.setattr(engine_mod, "write_world_state", boom_write, raising=False)
    _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert writes == [], "engine 收口不该写快照（叙述由 update_world 工具写）"


@pytest.mark.asyncio
async def test_cold_start_runs_loop_and_tells_model(monkeypatch):
    """冷启动：无 WorldState 时也走循环，prompt 里告诉模型这是冷启动 / 首次。"""

    async def no_world_state(*, lane):
        return None

    monkeypatch.setattr(engine_mod, "read_world_state", no_world_state)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    assert ("冷启动" in blob) or ("首次" in blob)


@pytest.mark.asyncio
async def test_act_wake_passes_act_to_model(monkeypatch):
    """act 唤醒：动作内容透给模型（在喂入的 prompt context 里）。

    内容从 PG 读全那一批 act（list_recent_acts）拼进 prompt，不再靠合并闸透进来
    的单条 payload。
    """
    from app.domain.world_events import ActPerformed

    async def fake_list_recent_acts(*, lane, since_iso):
        return [
            ActPerformed(lane=lane, act_id="a1", persona_id="chinagi",
                         description="我起床去厨房煮咖啡", occurred_at="2026-06-04T08:00:00+08:00"),
        ]

    monkeypatch.setattr(engine_mod, "list_recent_acts", fake_list_recent_acts)
    captured = _mock_run(monkeypatch)

    await world_tick(
        WorldTick(
            lane="coe-t2",
            reason="act",
            act_id="a1",
            act_persona_id="chinagi",
            act_description="我起床去厨房煮咖啡",
        )
    )

    blob = "".join(m.text() for m in captured["messages"])
    assert "煮咖啡" in blob


@pytest.mark.asyncio
async def test_act_wake_reads_all_recent_acts_not_just_latest(monkeypatch):
    """act 唤醒从 PG 读这段时间所有 act 全喂 world，不止合并闸最后一条。

    合并闸 latest-only 只把最后一条 act 的 payload 透进 WorldTick；前面几条对
    world 等价丢失。修：world 被 act 唤醒时调 list_recent_acts 读全那一批。
    这里 tick 只带最后一条（千凪），但 PG 里还有前两条——三条都必须出现在喂给
    模型的 context 里。
    """
    from app.domain.world_events import ActPerformed

    async def fake_list_recent_acts(*, lane, since_iso):
        return [
            ActPerformed(lane=lane, act_id="a1", persona_id="ayana",
                         description="我出门上学", occurred_at="2026-06-04T08:00:00+08:00"),
            ActPerformed(lane=lane, act_id="a2", persona_id="akao",
                         description="我去找千凪", occurred_at="2026-06-04T08:00:20+08:00"),
            ActPerformed(lane=lane, act_id="a3", persona_id="chinagi",
                         description="我起床去厨房煮咖啡", occurred_at="2026-06-04T08:00:40+08:00"),
        ]

    monkeypatch.setattr(engine_mod, "list_recent_acts", fake_list_recent_acts)
    captured = _mock_run(monkeypatch)

    await world_tick(
        WorldTick(
            lane="coe-t2",
            reason="act",
            act_id="a3",
            act_persona_id="chinagi",
            act_description="我起床去厨房煮咖啡",
        )
    )

    blob = "".join(m.text() for m in captured["messages"])
    assert "我出门上学" in blob, "前面被合并闸丢掉的 act（绫奈出门）必须从 PG 读回喂 world"
    assert "我去找千凪" in blob, "前面被合并闸丢掉的 act（赤尾找人）必须从 PG 读回喂 world"
    assert "我起床去厨房煮咖啡" in blob, "最后那条 act 也在"


@pytest.mark.asyncio
async def test_act_wake_query_since_uses_lane_and_lookback(monkeypatch):
    """act 唤醒按 lane 读、since 截断点存在（不读无界历史）。"""
    calls: list[dict] = []

    async def fake_list_recent_acts(*, lane, since_iso):
        calls.append({"lane": lane, "since_iso": since_iso})
        return []

    monkeypatch.setattr(engine_mod, "list_recent_acts", fake_list_recent_acts)
    _mock_run(monkeypatch)

    await world_tick(
        WorldTick(
            lane="coe-t2",
            reason="act",
            act_id="a1",
            act_persona_id="chinagi",
            act_description="我去厨房",
        )
    )

    assert len(calls) == 1, "act 唤醒必须读一次最近 act 批次"
    assert calls[0]["lane"] == "coe-t2"
    assert calls[0]["since_iso"], "since 截断点必须非空（有界窗口，不读无界历史）"


@pytest.mark.asyncio
async def test_act_wake_window_anchored_to_act_not_world_time(monkeypatch):
    """since 窗口锚定触发 act 的 occurred_at，不被 world_time 漂移挤掉。

    heartbeat / self / 并发 world 轮次推进了 world_time，但它们不读 act。随后 act
    wake 若用 world_time（更晚的快照）当 since 下界，会把这条未被消费的 act 排除出
    窗口 → 又静默丢掉。这里让快照 world_time 比 act occurred_at 更晚（模拟 heartbeat
    已推进时钟），断言 since_iso 仍 ≤ act 的 occurred_at（窗口覆盖这条未消费 act）。
    """
    from datetime import timedelta

    from app.world.engine import _CST
    from app.world.state import WorldState

    now = datetime.now(_CST)
    act_occurred = (now - timedelta(seconds=40)).isoformat()
    snapshot_world_time = (now - timedelta(seconds=10)).isoformat()

    async def fresh_snapshot(*, lane):
        return WorldState(lane=lane, world_time=snapshot_world_time, detail="")

    captured_since: list[str] = []

    async def fake_list_recent_acts(*, lane, since_iso):
        captured_since.append(since_iso)
        return []

    monkeypatch.setattr(engine_mod, "read_world_state", fresh_snapshot)
    monkeypatch.setattr(engine_mod, "list_recent_acts", fake_list_recent_acts)
    _mock_run(monkeypatch)

    await world_tick(
        WorldTick(
            lane="coe-t2",
            reason="act",
            act_id="a-late",
            act_persona_id="chinagi",
            act_description="我去厨房",
            act_occurred_at=act_occurred,
        )
    )

    assert len(captured_since) == 1
    since_dt = datetime.fromisoformat(captured_since[0])
    act_dt = datetime.fromisoformat(act_occurred)
    assert since_dt <= act_dt, (
        f"since 窗口必须覆盖触发 act 的 occurred_at（{act_occurred}），"
        f"不能被更晚的 world_time 快照挤掉，实际 since={captured_since[0]}"
    )


def test_act_since_cutoff_anchors_on_act_occurred_at():
    """回看窗口下界 = 触发 act 的 occurred_at - lookback（锚 act，不锚 now/world_time）。

    occurred_at 缺失 / naive 时退回 now - lookback 兜底（不抛、仍读近窗 act）。
    """
    from datetime import timedelta

    from app.world.engine import (
        _CST,
        WORLD_ACT_LOOKBACK_SECONDS,
        WorldTick,
        _act_since_cutoff,
    )

    now = datetime(2026, 6, 4, 8, 0, 0, tzinfo=_CST)
    occurred = (now - timedelta(seconds=40)).isoformat()

    tick = WorldTick(
        lane="x", reason="act", act_id="a1",
        act_persona_id="akao", act_description="s",
        act_occurred_at=occurred,
    )
    cutoff = datetime.fromisoformat(_act_since_cutoff(tick, now))
    assert cutoff == datetime.fromisoformat(occurred) - timedelta(
        seconds=WORLD_ACT_LOOKBACK_SECONDS
    ), "下界 = 触发 act occurred_at - lookback"

    tick_empty = WorldTick(
        lane="x", reason="act", act_id="a2",
        act_persona_id="akao", act_description="s",
    )
    cutoff_fallback = datetime.fromisoformat(_act_since_cutoff(tick_empty, now))
    assert cutoff_fallback == now - timedelta(seconds=WORLD_ACT_LOOKBACK_SECONDS), (
        "occurred_at 缺失退回 now - lookback 兜底"
    )


def test_act_batch_text_shows_occurred_at_in_cst():
    """act 批次清单里的 occurred_at 显示转 CST（兜历史 UTC act）。

    life 历史 act 可能写 UTC（``...12:30:00+00:00``），world 把它喂给模型时
    必须显示成 CST（20:30）。文案讲"做了什么"、用 description 字段。
    """
    from app.domain.world_events import ActPerformed
    from app.world.engine import _act_batch_text

    acts = [
        ActPerformed(
            lane="x", act_id="a1", persona_id="akao",
            description="去厨房做饭", occurred_at="2026-06-04T12:30:00+00:00",  # UTC
        ),
        ActPerformed(
            lane="x", act_id="a2", persona_id="ayana",
            description="出门上学", occurred_at="2026-06-04T20:30:00+08:00",  # CST
        ),
    ]
    text = _act_batch_text(acts)
    assert "20:30" in text, "UTC act 时刻该显示成 CST"
    assert "CST" in text, "显示要让模型看得出是 CST"
    assert "去厨房做饭" in text and "出门上学" in text


@pytest.mark.asyncio
async def test_non_act_wake_does_not_read_acts(monkeypatch):
    """heartbeat / self 唤醒不读 act 批次（只有 act 唤醒才需要呈现动作）。"""
    calls: list = []

    async def fake_list_recent_acts(*, lane, since_iso):
        calls.append(lane)
        return []

    monkeypatch.setattr(engine_mod, "list_recent_acts", fake_list_recent_acts)
    _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))
    await world_tick(WorldTick(lane="coe-t2", reason="self"))

    assert calls == [], "非 act 唤醒不该读 act 批次"


@pytest.mark.asyncio
async def test_world_tick_seeds_round_scoped_self_wake_state(monkeypatch):
    """run 的 context.features 带 round-scoped 的待办 self-wake 容器。

    新范式不再有 emit 计数安全阀（recursion_limit 已是失控兜底）；每轮新建空的
    待办 self-wake 容器（sleep 覆盖待办靠它），engine 收口后读它 emit 一次。
    """
    from app.world.tools import FEATURE_SELF_WAKE

    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    feats = captured["context"].features
    assert feats.get(FEATURE_SELF_WAKE) == {}, "每轮新建空的待办 self-wake 容器"


@pytest.mark.asyncio
async def test_world_tick_emits_one_self_wake_from_round_state(monkeypatch):
    """循环里（模拟模型调 sleep）记下的待办 self-wake，engine 收口后只 emit 一条。"""
    import app.world.tools as tools_mod
    from app.world.tools import FEATURE_SELF_WAKE

    delayed: list[dict] = []

    async def fake_emit_delayed(data, *, delay_ms, durability="durable"):
        delayed.append({"data": data, "delay_ms": delay_ms})

    monkeypatch.setattr(tools_mod, "emit_delayed", fake_emit_delayed)

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
async def test_act_wake_round_id_stable_across_replays(monkeypatch):
    """同一 ActPerformed（同 act_id）重投 → 同 round_id（外层重放幂等命门）。"""
    rounds: list[str] = []

    async def capture_run(
        self, messages, *, prompt_vars=None, context=None, session_id=None, max_retries=2
    ):
        rounds.append(context.features["world_round_id"])
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(engine_mod.Agent, "run", capture_run)

    tick = WorldTick(
        lane="coe-t2",
        reason="act",
        act_id="act-stable-1",
        act_persona_id="chinagi",
        act_description="我去厨房煮咖啡",
    )
    await world_tick(tick)
    await world_tick(tick)  # durable 重投同一条

    assert rounds[0] == rounds[1], (
        f"同一 act_id 重投应得同一 round_id，实际 {rounds}"
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

    assert rounds[0] != rounds[1]


@pytest.mark.asyncio
async def test_act_tick_carries_act_id(monkeypatch):
    """act_to_world_tick 把 ActPerformed.act_id 透传进 ActWorldTick（闸 + round_id 派生靠它）。

    act 不直接打 WorldTick，而是经一个 transient ActWorldTick 走 60s 合并闸
    （debounce）再翻成 WorldTick。act_id 必须透传，闸合并 / round_id 派生 /
    durable 重投幂等都靠它。
    """
    from app.domain.world_events import ActPerformed
    from app.world.engine import ActWorldTick

    emitted: list = []

    async def fake_emit(data):
        emitted.append(data)

    monkeypatch.setattr(engine_mod, "emit", fake_emit)

    await engine_mod.act_to_world_tick(
        ActPerformed(
            lane="coe-t2",
            act_id="act-xyz",
            persona_id="akao",
            description="我去厨房",
            occurred_at="2026-06-03T12:30:00Z",
        )
    )

    assert isinstance(emitted[0], ActWorldTick)
    assert emitted[0].act_id == "act-xyz"
    assert emitted[0].lane == "coe-t2"
    assert emitted[0].act_persona_id == "akao"
    assert emitted[0].act_description == "我去厨房"
    assert emitted[0].act_occurred_at == "2026-06-03T12:30:00Z"


@pytest.mark.asyncio
async def test_world_instruction_demands_objective_projection():
    """给模型的指令是 agent 工具指令、客观投影不碰情绪（赤尾宪法）。"""
    instruction = engine_mod.world_loop_instruction()
    assert "客观" in instruction
    assert ("情绪" in instruction) or ("主观" in instruction) or ("解读" in instruction)
    # 三工具范式：提到她能用工具行动
    assert ("notify" in instruction) or ("update_world" in instruction)
    assert ("sleep" in instruction) or ("再看" in instruction)


@pytest.mark.asyncio
async def test_world_instruction_is_deducer_not_director():
    """循环指令是推演者范式：world 推演谁够得着、不替角色决定 / 不批准动作。"""
    instruction = engine_mod.world_loop_instruction()
    # 推演范式：world 推演世界此刻什么样 + 谁够得着
    assert ("推演" in instruction)
    # 不替角色决定 / 不批准（钉死宪法①②）
    assert ("不替" in instruction) or ("不批准" in instruction) or ("不拒绝" in instruction)
    # 安静流动软引导（没值得感知的客观变化就只 update_world + sleep，不 notify）
    assert ("安静" in instruction) or ("平静" in instruction) or ("流动" in instruction)
    # 不再有旧导演词
    assert "move_persona" not in instruction
    assert "裁决" not in instruction


@pytest.mark.asyncio
async def test_world_loop_messages_carry_no_household_rhythm():
    """USER 层不再拼作息节律 —— 世界设定（含三姐妹作息）只由 system prompt 一处承载。

    world 的 system prompt（langfuse 上的 ``world_deliberate``）已补全完整世界观
    （广州小区一家四口、空间布局、三姐妹客观生活坐标）。代码 USER 层若再拼
    ``household_rhythm()`` 就是重复的两处真相。撤掉后喂给循环的 user 消息里既不该有
    「作息节律」这个段标题，也不该出现节律里写死的客观坐标（千凪早起 / 赤尾睡到
    下午 / 绫奈上学）。
    """
    from app.world.engine import _world_loop_messages

    messages = _world_loop_messages(
        detail="清晨厨房有了动静。",
        now_iso="2026-06-05T09:00:00+08:00",
        wake_reason="例行看一眼世界。",
        round_id="r1",
    )
    blob = "".join(m.text() for m in messages)
    assert "作息节律" not in blob, "USER 层不该再拼作息节律段（世界设定归 system 一处）"
    for setting in ("千凪", "赤尾", "绫奈", "chinagi", "akao", "ayana"):
        assert setting not in blob, (
            f"USER 层不该出现世界设定里的客观坐标 {setting!r}（归 system 一处）"
        )


@pytest.mark.asyncio
async def test_world_instruction_has_no_world_setting():
    """world_loop_instruction 是 USER 层行动指令，不该描述世界设定 / 谁是谁 / 作息。

    世界长什么样 / 家庭布局 / 三姐妹是谁 / 几点干嘛这类设定性内容归 system prompt
    一处；USER 层指令只讲三个工具（notify/update_world/sleep）的说明 + 本轮怎么做。
    """
    instruction = engine_mod.world_loop_instruction()
    # 不该出现三姐妹的名字 / 年龄 / 作息坐标这类世界设定内容
    for setting in ("千凪", "赤尾", "绫奈", "chinagi", "akao", "ayana"):
        assert setting not in instruction, (
            f"USER 层指令不该描述世界设定（谁是谁）：{setting!r}"
        )
    assert "作息" not in instruction, "USER 层指令不该描述作息（归 system 一处）"
    # 仍是三工具行动指令
    assert ("notify" in instruction) and ("update_world" in instruction)
    assert "sleep" in instruction


@pytest.mark.asyncio
async def test_world_tick_stamps_round_marker_in_stimulus(monkeypatch):
    """喂给循环的 user 消息里带本轮 round_id 标记（turn 幂等查重靠它）。"""
    captured = _mock_run(monkeypatch)

    tick = WorldTick(
        lane="coe-t2",
        reason="act",
        act_id="act-marker-1",
        act_persona_id="akao",
        act_description="我去厨房",
    )
    await world_tick(tick)

    now_iso = datetime.now(engine_mod._CST).isoformat()
    round_id = engine_mod._derive_round_id(tick, now_iso)
    blob = "".join(m.text() for m in captured["messages"])
    assert round_id in blob, "stimulus 必须带本轮 round_id 标记（幂等查重靠它）"


@pytest.mark.asyncio
async def test_act_replay_skips_already_processed_round(monkeypatch):
    """同一 durable act 重投：第二次从 session 历史查到本轮标记 → 跳过，不再 run。"""
    run_calls: list = []

    async def fake_run(
        self, messages, *, prompt_vars=None, context=None, session_id=None, max_retries=2
    ):
        run_calls.append(session_id)
        from app.agent.session import append_session

        await append_session(session_id, list(messages))
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(engine_mod.Agent, "run", fake_run)

    tick = WorldTick(
        lane="coe-t2",
        reason="act",
        act_id="act-replay-1",
        act_persona_id="akao",
        act_description="我去厨房",
    )
    await world_tick(tick)
    await world_tick(tick)  # durable 重投同一条

    assert len(run_calls) == 1, (
        f"同一 act 重投不该再跑一轮（turn 幂等），实际 run {len(run_calls)} 次"
    )


@pytest.mark.asyncio
async def test_world_tick_serializes_under_actor_lock(monkeypatch):
    """world_tick 按 actor 拿单飞锁、覆盖全段：并发第二源拿不到锁不能并行进。"""
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

    first = asyncio.create_task(world_tick(WorldTick(lane="coe-t2", reason="heartbeat")))
    await in_run.wait()
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

    async with single_flight("world:coe-t2", ttl=60):
        await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))


@pytest.mark.asyncio
async def test_self_lock_conflict_is_swallowed(monkeypatch):
    """self 撞锁：吞掉（log+return），不抛——自排是冗余，正在跑的那轮会自己再排。"""
    from app.runtime.single_flight import single_flight

    _mock_run(monkeypatch)

    async with single_flight("world:coe-t2", ttl=60):
        await world_tick(WorldTick(lane="coe-t2", reason="self"))


@pytest.mark.asyncio
async def test_act_lock_conflict_raises_for_reschedule(monkeypatch):
    """act 撞锁：抛 SingleFlightConflict 让上游（debounce 闸）重排——绝不丢动作。"""
    from app.runtime.single_flight import SingleFlightConflict, single_flight

    _mock_run(monkeypatch)

    async with single_flight("world:coe-t2", ttl=60):
        with pytest.raises(SingleFlightConflict):
            await world_tick(
                WorldTick(
                    lane="coe-t2",
                    reason="act",
                    act_id="a-conflict",
                    act_persona_id="akao",
                    act_description="我去厨房",
                )
            )
