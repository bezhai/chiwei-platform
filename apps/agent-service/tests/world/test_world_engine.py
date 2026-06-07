"""world engine 节点契约 — pull 范式（world 按自排节奏醒来、批量 pull act）.

world 是世界的推演层，不是导演。pull 范式下它只被**两源唤醒**（保底心跳 / 自排提前
卡点），都走到点 gate；act 不再唤醒 world。任何唤醒源醒来后它**跑一个 agent 工具
循环**：从"上次消费游标之后"批量读这段时间攒下的 act（``list_recent_acts``，一轮
最多 N 条）一并推演，用 update_world 写下世界叙述、对动作推演客观结果、用 notify
把够得着的角色推演出来投客观动静、用 sleep 定下次再看，最后把游标推进到本批末尾。

这些测试 mock ``Agent.run``（模拟模型连续调工具）+ stub 现成 handler，钉死
机制层硬约束，不是 LLM 决策：

  * world_tick 跑通 agent 循环、把 WORLD_TOOLS 交给 Agent；
  * **开头先 renotify_unread**（信箱对账自愈，先于循环、不依赖循环成功）；
  * run 传 ``max_retries=1``（关掉整轮重放 durable 工具）；
  * run 传正确的 session_id（``make_session_id(lane, "world", 今天)``）；
  * run 的 context.features 带 world 本轮 lane + round_id（工具体读它行动）；
  * cold_start：无 WorldState 时也走循环、prompt 里告诉模型这是冷启动；
  * **engine 收口不再写快照**（世界叙述改由 update_world 工具在循环里负责写）；
  * **任何唤醒源**都从游标 pull act 喂 world、读满 N 条告知积压、收口推进游标。

赤尾设计宪法：world 推演谁够得着、产什么动静、世界什么样全由 LLM 在循环里
判断，代码里没有阈值 / 计数器替它决策。10 分钟保底心跳 + sleep 上限只决定
"何时醒 / 别睡死"，N=10 读取上限只是防单轮 context 爆炸的护栏，都不进世界内容决策。
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

    from app.agent import session as session_mod
    from app.agent.neutral import Message

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
    """world 节点读快照、对账信箱、读 act 批次、推进游标都打桩，专测引擎机制（不碰真库）。

    收口不再写快照（世界叙述改由 update_world 工具负责写），所以这里不再 stub
    write_world_state。advance_act_cursor 打桩成记录调用，验证收口推进游标。
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

    async def fake_list_recent_acts(*, lane, cursor_created_at, cursor_act_id, limit):
        # 默认空批次：专测引擎机制的用例不碰真库。需要断言动作内容的用例自己
        # 覆写这个桩（monkeypatch.setattr engine_mod.list_recent_acts）。
        # 返回 list[tuple[ActPerformed, created_at_str]]（pull 范式：游标用 created_at）。
        return []

    cursor_calls: list[dict] = []

    async def fake_advance_act_cursor(*, lane, created_at, act_id):
        cursor_calls.append({"lane": lane, "created_at": created_at, "act_id": act_id})

    monkeypatch.setattr(engine_mod, "read_world_state", fake_read_world_state)
    monkeypatch.setattr(engine_mod, "renotify_unread", fake_renotify_unread)
    monkeypatch.setattr(engine_mod, "list_recent_acts", fake_list_recent_acts)
    monkeypatch.setattr(engine_mod, "advance_act_cursor", fake_advance_act_cursor)

    engine_mod._test_renotify_calls = renotify_calls  # type: ignore[attr-defined]
    engine_mod._test_cursor_calls = cursor_calls  # type: ignore[attr-defined]
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


def _act(lane, act_id, persona_id, description, occurred_at, created_at):
    """构造一条 (ActPerformed, created_at) 元组（list_recent_acts 的新返回形态）。

    pull 范式游标用 created_at（单调落库序），occurred_at 只用于 prompt 显示。所以
    stub 必须给两个时刻：occurred_at（做事时刻、prompt 显示）+ created_at（落库时刻、
    游标推进 / round_id 派生靠它）。
    """
    from app.domain.world_events import ActPerformed

    return (
        ActPerformed(
            lane=lane,
            act_id=act_id,
            persona_id=persona_id,
            description=description,
            occurred_at=occurred_at,
        ),
        created_at,
    )


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
    detail）由 update_world 工具负责，engine 收口只剩 fire_self_wake + advance_act_cursor，
    不再调 write_world_state。
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


# ---------------------------------------------------------------------------
# pull 范式：任何唤醒源都从游标批量读 act 喂 world
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_any_wake_source_reads_acts_from_cursor(monkeypatch):
    """heartbeat / self 唤醒都从游标 pull act 喂 world（不再只 act 唤醒才读）。"""
    from datetime import timedelta

    from app.world.engine import _CST
    from app.world.state import WorldState

    # self 走到点 gate：需要 snapshot 有已过的 next_wake_at + self 携带匹配 target。
    past = (datetime.now(_CST) - timedelta(seconds=5)).isoformat()

    async def snapshot(*, lane):
        return WorldState(lane=lane, world_time="t", detail="d", next_wake_at=past)

    calls: list[str] = []

    async def fake_list_recent_acts(*, lane, cursor_created_at, cursor_act_id, limit):
        calls.append(lane)
        return []

    monkeypatch.setattr(engine_mod, "read_world_state", snapshot)
    monkeypatch.setattr(engine_mod, "list_recent_acts", fake_list_recent_acts)
    _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))
    await world_tick(WorldTick(lane="coe-t2", reason="self", target_wake_at=past))

    assert calls == ["coe-t2", "coe-t2"], (
        "pull 范式下任何唤醒源都该从游标读 act 批次"
    )


@pytest.mark.asyncio
async def test_pull_passes_acts_to_model(monkeypatch):
    """读到的 act 内容透给模型（在喂入的 prompt context 里）。"""

    async def fake_list_recent_acts(*, lane, cursor_created_at, cursor_act_id, limit):
        return [
            _act(lane, "a1", "chinagi", "我起床去厨房煮咖啡",
                 "2026-06-04T08:00:00+08:00", "2026-06-04T08:00:00+08:00"),
        ]

    monkeypatch.setattr(engine_mod, "list_recent_acts", fake_list_recent_acts)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    assert "煮咖啡" in blob


@pytest.mark.asyncio
async def test_pull_reads_all_acts_in_batch(monkeypatch):
    """一批多条 act 全喂 world（对称 life 读 mailbox）。"""

    async def fake_list_recent_acts(*, lane, cursor_created_at, cursor_act_id, limit):
        return [
            _act(lane, "a1", "ayana", "我出门上学",
                 "2026-06-04T08:00:00+08:00", "2026-06-04T08:00:00+08:00"),
            _act(lane, "a2", "akao", "我去找千凪",
                 "2026-06-04T08:00:20+08:00", "2026-06-04T08:00:20+08:00"),
            _act(lane, "a3", "chinagi", "我起床去厨房煮咖啡",
                 "2026-06-04T08:00:40+08:00", "2026-06-04T08:00:40+08:00"),
        ]

    monkeypatch.setattr(engine_mod, "list_recent_acts", fake_list_recent_acts)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    assert "我出门上学" in blob
    assert "我去找千凪" in blob
    assert "我起床去厨房煮咖啡" in blob


@pytest.mark.asyncio
async def test_pull_query_uses_lane_cursor_and_limit(monkeypatch):
    """pull 按 lane + 复合游标读、传 N 上限（不读无界）。

    冷启动快照（read_world_state stub 无游标）→ cursor 为 None，读全既有。
    """
    calls: list[dict] = []

    async def fake_list_recent_acts(*, lane, cursor_created_at, cursor_act_id, limit):
        calls.append(
            {
                "lane": lane,
                "cursor_created_at": cursor_created_at,
                "cursor_act_id": cursor_act_id,
                "limit": limit,
            }
        )
        return []

    monkeypatch.setattr(engine_mod, "list_recent_acts", fake_list_recent_acts)
    _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert len(calls) == 1
    assert calls[0]["lane"] == "coe-t2"
    # 默认快照无游标 → None（读全既有）
    assert calls[0]["cursor_created_at"] is None
    assert calls[0]["cursor_act_id"] is None
    assert calls[0]["limit"] == engine_mod.WORLD_ACT_PULL_LIMIT
    assert calls[0]["limit"] > 0, "读取上限必须是正整数（有界，防 context 爆炸）"


@pytest.mark.asyncio
async def test_pull_passes_state_cursor_to_query(monkeypatch):
    """world 醒来按 WorldState 里上次消费到的复合游标（created_at）读 act。"""
    from app.world.state import WorldState

    async def snapshot_with_cursor(*, lane):
        return WorldState(
            lane=lane,
            world_time="2026-06-04T08:00:00+08:00",
            detail="d",
            act_cursor_created_at="2026-06-04T07:30:00+08:00",
            act_cursor_act_id="prev-act",
        )

    calls: list[dict] = []

    async def fake_list_recent_acts(*, lane, cursor_created_at, cursor_act_id, limit):
        calls.append({"created": cursor_created_at, "id": cursor_act_id})
        return []

    monkeypatch.setattr(engine_mod, "read_world_state", snapshot_with_cursor)
    monkeypatch.setattr(engine_mod, "list_recent_acts", fake_list_recent_acts)
    _mock_run(monkeypatch)

    # snapshot 无 next_wake_at（None）→ heartbeat gate 放行（首轮不卡死）；用 heartbeat
    # 验证游标传递（self 在 next_wake_at=None 时会被 gate 判废，不适合这条用例）。
    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert calls == [{"created": "2026-06-04T07:30:00+08:00", "id": "prev-act"}], (
        "pull 必须把 WorldState 当前 created_at 游标传给 list_recent_acts"
    )


@pytest.mark.asyncio
async def test_pull_full_batch_tells_model_backlog(monkeypatch):
    """读满 N 条（积压）→ 缘由 / 批次文本告诉 world 还有动作没读完，由她自己排短 sleep。"""

    async def full_batch(*, lane, cursor_created_at, cursor_act_id, limit):
        # engine 传进来的 limit == WORLD_ACT_PULL_LIMIT；返回正好读满 limit 条模拟积压。
        return [
            _act(lane, f"a{i}", "akao", f"动作{i}",
                 f"2026-06-04T08:00:{i:02d}+08:00", f"2026-06-04T08:00:{i:02d}+08:00")
            for i in range(limit)
        ]

    monkeypatch.setattr(engine_mod, "list_recent_acts", full_batch)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    assert ("积压" in blob) or ("没读完" in blob) or ("还有" in blob), (
        "读满 N 条必须在 prompt 里告知 world 有积压（由她排短 sleep 尽快消化）"
    )


@pytest.mark.asyncio
async def test_pull_partial_batch_no_backlog_text(monkeypatch):
    """没读满 N 条 → 不告知积压（只读到的就是全部）。"""

    async def partial(*, lane, cursor_created_at, cursor_act_id, limit):
        return [
            _act(lane, "a1", "akao", "唯一一条",
                 "2026-06-04T08:00:00+08:00", "2026-06-04T08:00:00+08:00"),
        ]

    monkeypatch.setattr(engine_mod, "list_recent_acts", partial)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    assert "积压" not in blob, "没读满 N 条不该告知积压"


# ---------------------------------------------------------------------------
# 收口推进游标：成功才推进、到本批末尾（用 created_at）；空批次不推进
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cursor_advances_to_batch_end_on_success(monkeypatch):
    """推演成功收口 → 把游标推进到本批最后一条 act 的 (created_at, act_id)。

    游标用 created_at（落库时刻），不是 occurred_at（做事时刻）。这里给两个时刻不同，
    钉死游标推进用的是 created_at 那个。
    """

    async def batch(*, lane, cursor_created_at, cursor_act_id, limit):
        return [
            _act(lane, "a1", "akao", "先",
                 "2026-06-04T08:05:00+08:00", "2026-06-04T08:00:00+08:00"),
            _act(lane, "a2", "ayana", "后",
                 "2026-06-04T08:01:00+08:00", "2026-06-04T08:00:30+08:00"),
        ]

    monkeypatch.setattr(engine_mod, "list_recent_acts", batch)
    _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert engine_mod._test_cursor_calls == [
        {"lane": "coe-t2", "created_at": "2026-06-04T08:00:30+08:00", "act_id": "a2"}
    ], "游标必须推进到本批末尾的 created_at（不是 occurred_at）"


@pytest.mark.asyncio
async def test_cursor_not_advanced_on_empty_batch(monkeypatch):
    """空批次（醒来没新 act）→ 不推进游标（没读到东西没什么可推进）。"""
    _mock_run(monkeypatch)  # 默认 list_recent_acts 返回 []

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert engine_mod._test_cursor_calls == [], "空批次不该推进游标"


@pytest.mark.asyncio
async def test_cursor_not_advanced_on_run_failure(monkeypatch):
    """场景②（run 失败）：推演中途失败（run 抛）→ 游标不推进、marker 没写。

    下轮重读起点同 round_id、marker 不在 → 不命中 → 重新 run（正常重试）。
    """

    async def batch(*, lane, cursor_created_at, cursor_act_id, limit):
        return [
            _act(lane, "a1", "akao", "x",
                 "2026-06-04T08:00:00+08:00", "2026-06-04T08:00:00+08:00"),
        ]

    async def boom_run(self, messages, *, prompt_vars=None, context=None,
                       session_id=None, max_retries=2):
        raise RuntimeError("model boom")

    monkeypatch.setattr(engine_mod, "list_recent_acts", batch)
    monkeypatch.setattr(engine_mod.Agent, "run", boom_run)

    with pytest.raises(RuntimeError):
        await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert engine_mod._test_cursor_calls == [], "run 失败时游标绝不推进（下轮重读）"


# ---------------------------------------------------------------------------
# round_id：非空批次从游标起点派生（崩溃扩批仍同 round_id）、空批次按 now
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round_id_stable_across_replays_for_same_start_cursor(monkeypatch):
    """同一游标起点重读 → 同 round_id（turn 幂等 + event_id 去重命门）。

    必改 2：批次非空时 round_id 从**游标起点**（cursor_created_at, cursor_act_id）派生
    （不从本批 act 集合、不用 now）。游标起点不变 → round_id 不变（哪怕批集合 / 时刻变）。
    """
    from app.world.state import WorldState

    async def snapshot(*, lane):
        # 固定游标起点（C0）。
        return WorldState(
            lane=lane, world_time="t", detail="d",
            act_cursor_created_at="2026-06-04T08:00:00+08:00",
            act_cursor_act_id="c0",
        )

    async def batch(*, lane, cursor_created_at, cursor_act_id, limit):
        return [
            _act(lane, "a1", "akao", "x",
                 "2026-06-04T08:01:00+08:00", "2026-06-04T08:01:00+08:00"),
            _act(lane, "a2", "ayana", "y",
                 "2026-06-04T08:02:00+08:00", "2026-06-04T08:02:00+08:00"),
        ]

    rounds: list[str] = []

    async def capture_run(self, messages, *, prompt_vars=None, context=None,
                          session_id=None, max_retries=2):
        rounds.append(context.features["world_round_id"])
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(engine_mod, "read_world_state", snapshot)
    monkeypatch.setattr(engine_mod, "list_recent_acts", batch)
    monkeypatch.setattr(engine_mod.Agent, "run", capture_run)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))
    import asyncio

    await asyncio.sleep(0.002)  # 时间变了，但同起点 round_id 应不变
    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert rounds[0] == rounds[1], (
        f"同一游标起点重读应得同一 round_id（与 now / 批集合无关），实际 {rounds}"
    )


@pytest.mark.asyncio
async def test_round_id_stable_when_batch_grows_same_start_cursor(monkeypatch):
    """场景④核心：游标起点不变但批集合变大（崩溃后新增 act）→ round_id 仍不变。

    必改 2 的命门——旧实现 round_id 绑"本批 act 集合"，崩溃后新增 act 让批变大、
    round_id 变 → turn 幂等失效 → 旧 act 被重复推演。改成绑游标起点后批集合变大
    round_id 不变 → marker 仍命中、不重推。
    """
    from app.world.state import WorldState

    async def snapshot(*, lane):
        return WorldState(
            lane=lane, world_time="t", detail="d",
            act_cursor_created_at="2026-06-04T08:00:00+08:00",
            act_cursor_act_id="c0",
        )

    batches = [
        # 第一轮：批到 a5（C5）。
        [
            _act("coe-t2", f"a{i}", "akao", f"动作{i}",
                 f"2026-06-04T08:0{i}:00+08:00", f"2026-06-04T08:0{i}:00+08:00")
            for i in range(1, 6)
        ],
        # 第二轮：崩溃期间新增 a6（C6），批变大到 a6。起点不变。
        [
            _act("coe-t2", f"a{i}", "akao", f"动作{i}",
                 f"2026-06-04T08:0{i}:00+08:00", f"2026-06-04T08:0{i}:00+08:00")
            for i in range(1, 7)
        ],
    ]

    rounds: list[str] = []

    async def capture_run(self, messages, *, prompt_vars=None, context=None,
                          session_id=None, max_retries=2):
        rounds.append(context.features["world_round_id"])
        return Message(role=Role.ASSISTANT, content="")

    async def batch(*, lane, cursor_created_at, cursor_act_id, limit):
        return batches.pop(0)

    monkeypatch.setattr(engine_mod, "read_world_state", snapshot)
    monkeypatch.setattr(engine_mod, "list_recent_acts", batch)
    monkeypatch.setattr(engine_mod.Agent, "run", capture_run)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))
    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert rounds[0] == rounds[1], (
        f"游标起点不变、批集合变大 round_id 也必须不变（绑起点不绑批集合），实际 {rounds}"
    )


@pytest.mark.asyncio
async def test_round_id_varies_with_time_for_empty_batch(monkeypatch):
    """场景⑤（空批次）：纯 self / heartbeat 推进 → round_id 从 now 派生（每次新 round）。"""
    rounds: list[str] = []

    async def capture_run(self, messages, *, prompt_vars=None, context=None,
                          session_id=None, max_retries=2):
        rounds.append(context.features["world_round_id"])
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(engine_mod.Agent, "run", capture_run)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))
    import asyncio

    await asyncio.sleep(0.002)
    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert rounds[0] != rounds[1], "空批次 round_id 随时刻变（不被误幂等）"


@pytest.mark.asyncio
async def test_round_id_differs_for_different_start_cursor(monkeypatch):
    """不同游标起点 → 不同 round_id（推进过的下一批不会误判成同轮跳过）。"""
    from app.world.state import WorldState

    snapshots = [
        WorldState(lane="coe-t2", world_time="t", detail="d",
                   act_cursor_created_at="2026-06-04T08:00:00+08:00", act_cursor_act_id="c0"),
        WorldState(lane="coe-t2", world_time="t", detail="d",
                   act_cursor_created_at="2026-06-04T08:05:00+08:00", act_cursor_act_id="c5"),
    ]

    async def snapshot(*, lane):
        return snapshots.pop(0)

    async def batch(*, lane, cursor_created_at, cursor_act_id, limit):
        return [
            _act(lane, "ax", "ayana", "y",
                 "2026-06-04T09:00:00+08:00", "2026-06-04T09:00:00+08:00"),
        ]

    rounds: list[str] = []

    async def capture_run(self, messages, *, prompt_vars=None, context=None,
                          session_id=None, max_retries=2):
        rounds.append(context.features["world_round_id"])
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(engine_mod, "read_world_state", snapshot)
    monkeypatch.setattr(engine_mod, "list_recent_acts", batch)
    monkeypatch.setattr(engine_mod.Agent, "run", capture_run)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))
    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert rounds[0] != rounds[1], "不同游标起点应得不同 round_id"


@pytest.mark.asyncio
async def test_cold_start_round_id_stable_across_replays(monkeypatch):
    """场景⑥（冷启动）：游标为 None 时 round_id 从固定 cold seed 派生，崩溃重读同 round_id。

    冷启动游标为 None，不能用 now 派生（否则崩溃重读 round_id 变、turn 幂等失效）。
    用固定 cold seed（lane + ":cold"）→ 冷启动崩溃重读得同 round_id。
    """

    async def batch(*, lane, cursor_created_at, cursor_act_id, limit):
        return [
            _act(lane, "a1", "akao", "x",
                 "2026-06-04T08:00:00+08:00", "2026-06-04T08:00:00+08:00"),
        ]

    rounds: list[str] = []

    async def capture_run(self, messages, *, prompt_vars=None, context=None,
                          session_id=None, max_retries=2):
        rounds.append(context.features["world_round_id"])
        return Message(role=Role.ASSISTANT, content="")

    # 默认 _stub_state 的 read_world_state 返回的 snapshot 无游标（cursor None）= 冷启动游标。
    monkeypatch.setattr(engine_mod, "list_recent_acts", batch)
    monkeypatch.setattr(engine_mod.Agent, "run", capture_run)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))
    import asyncio

    await asyncio.sleep(0.002)  # 时刻变，但冷启动 cold seed 派生 round_id 应不变
    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert rounds[0] == rounds[1], (
        f"冷启动（游标 None）非空批次 round_id 必须从固定 cold seed 派生、重读不变，实际 {rounds}"
    )


@pytest.mark.asyncio
async def test_normal_success_writes_marker_and_advances_cursor(monkeypatch):
    """场景①（正常）：起点 C0 读批末尾 C5、run 成功 → 写 marker(round, 终点C5)、游标进到 C5。"""
    from app.agent.session import append_session
    from app.world.engine import _derive_round_id, _round_already_processed
    from app.world.state import WorldState

    async def snapshot(*, lane):
        return WorldState(
            lane=lane, world_time="t", detail="d",
            act_cursor_created_at="2026-06-04T08:00:00+08:00", act_cursor_act_id="c0",
        )

    async def batch(*, lane, cursor_created_at, cursor_act_id, limit):
        return [
            _act(lane, "a5", "akao", "批末尾那条",
                 "2026-06-04T08:05:00+08:00", "2026-06-04T08:05:00+08:00"),
        ]

    captured_msgs: dict = {}

    async def fake_run(self, messages, *, prompt_vars=None, context=None,
                       session_id=None, max_retries=2):
        captured_msgs["msgs"] = list(messages)
        await append_session(session_id, list(messages))
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(engine_mod, "read_world_state", snapshot)
    monkeypatch.setattr(engine_mod, "list_recent_acts", batch)
    monkeypatch.setattr(engine_mod.Agent, "run", fake_run)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    # 游标推进到批末尾 C5。
    assert engine_mod._test_cursor_calls == [
        {"lane": "coe-t2", "created_at": "2026-06-04T08:05:00+08:00", "act_id": "a5"}
    ]
    # marker 写进 transcript，编码了 round_id（从起点 C0 派生）+ 批终点游标 (C5, a5)：
    # 用 _round_already_processed 反查，命中并返回终点游标。
    round_id = _derive_round_id(
        "coe-t2",
        cursor_created_at="2026-06-04T08:00:00+08:00",
        cursor_act_id="c0",
        has_acts=True,
        now_iso="ignored",
    )
    hit = _round_already_processed(captured_msgs["msgs"], round_id)
    assert hit == ("2026-06-04T08:05:00+08:00", "a5"), (
        f"成功收口写的 marker 必须编码批终点游标 (C5, a5)，反查得 {hit}"
    )


@pytest.mark.asyncio
async def test_crash_replay_advances_to_marker_end_and_skips(monkeypatch):
    """场景③（崩溃）：run 成功写了 transcript+marker、但 advance 崩。

    重读起点 C0、round_id=R(C0)、marker 在、终点 C5 → 推进游标到 C5、跳过 run。
    """
    from app.agent.session import append_session
    from app.world.engine import _round_marker
    from app.world.state import WorldState

    async def snapshot(*, lane):
        return WorldState(
            lane=lane, world_time="t", detail="d",
            act_cursor_created_at="2026-06-04T08:00:00+08:00", act_cursor_act_id="c0",
        )

    async def batch(*, lane, cursor_created_at, cursor_act_id, limit):
        return [
            _act(lane, "a5", "akao", "批末尾",
                 "2026-06-04T08:05:00+08:00", "2026-06-04T08:05:00+08:00"),
        ]

    monkeypatch.setattr(engine_mod, "read_world_state", snapshot)
    monkeypatch.setattr(engine_mod, "list_recent_acts", batch)

    # 预置一条 transcript：模拟"上一轮 run 成功写了带终点 C5 的 marker、但 advance 崩了"。
    # 先算出本轮 round_id（从起点 C0 派生），手工写一条带 marker（含终点 C5）的 USER 消息。
    round_id = engine_mod._derive_round_id(
        "coe-t2",
        cursor_created_at="2026-06-04T08:00:00+08:00",
        cursor_act_id="c0",
        has_acts=True,
        now_iso="ignored",
    )
    today = datetime.now().strftime("%Y-%m-%d")
    from app.agent.trace import make_session_id

    session_id = make_session_id("coe-t2", "world", today)
    marker = _round_marker(round_id, end_created_at="2026-06-04T08:05:00+08:00", end_act_id="a5")
    await append_session(session_id, [Message(role=Role.USER, content=f"{marker}\n上一轮内容")])

    run_calls: list = []

    async def fake_run(self, messages, *, prompt_vars=None, context=None,
                       session_id=None, max_retries=2):
        run_calls.append(1)
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(engine_mod.Agent, "run", fake_run)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert run_calls == [], "marker 命中应跳过 run（不重推）"
    assert engine_mod._test_cursor_calls == [
        {"lane": "coe-t2", "created_at": "2026-06-04T08:05:00+08:00", "act_id": "a5"}
    ], "命中时游标推进到 marker 记的终点 C5（不是当前批末尾）"


@pytest.mark.asyncio
async def test_crash_grow_replay_advances_to_marker_end_not_new_batch_end(monkeypatch):
    """场景④（崩溃+扩批）：marker(R(C0),C5) 已写、advance 崩、期间新增 act C6。

    重读起点 C0、批变成到 C6、但 round_id 仍=R(C0)（起点派生）→ marker 命中、终点 C5
    → 游标推进到 **C5**（不是 C6）→ 跳过；下轮起点 C5 正常推 C6（不重不漏不重推）。
    """
    from app.agent.session import append_session
    from app.world.engine import _round_marker
    from app.world.state import WorldState

    async def snapshot(*, lane):
        return WorldState(
            lane=lane, world_time="t", detail="d",
            act_cursor_created_at="2026-06-04T08:00:00+08:00", act_cursor_act_id="c0",
        )

    # 批扩到 C6（新增 a6），但起点仍 C0。
    async def batch(*, lane, cursor_created_at, cursor_act_id, limit):
        return [
            _act(lane, "a5", "akao", "C5",
                 "2026-06-04T08:05:00+08:00", "2026-06-04T08:05:00+08:00"),
            _act(lane, "a6", "ayana", "C6（崩溃期间新增）",
                 "2026-06-04T08:06:00+08:00", "2026-06-04T08:06:00+08:00"),
        ]

    monkeypatch.setattr(engine_mod, "read_world_state", snapshot)
    monkeypatch.setattr(engine_mod, "list_recent_acts", batch)

    round_id = engine_mod._derive_round_id(
        "coe-t2",
        cursor_created_at="2026-06-04T08:00:00+08:00",
        cursor_act_id="c0",
        has_acts=True,
        now_iso="ignored",
    )
    today = datetime.now().strftime("%Y-%m-%d")
    from app.agent.trace import make_session_id

    session_id = make_session_id("coe-t2", "world", today)
    # marker 记的终点是 C5（崩溃前那批末尾），不是现在扩出的 C6。
    marker = _round_marker(round_id, end_created_at="2026-06-04T08:05:00+08:00", end_act_id="a5")
    await append_session(session_id, [Message(role=Role.USER, content=f"{marker}\n上一轮")])

    run_calls: list = []

    async def fake_run(self, messages, *, prompt_vars=None, context=None,
                       session_id=None, max_retries=2):
        run_calls.append(1)
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(engine_mod.Agent, "run", fake_run)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert run_calls == [], "崩溃+扩批：marker 命中应跳过 run（不重推旧 act）"
    assert engine_mod._test_cursor_calls == [
        {"lane": "coe-t2", "created_at": "2026-06-04T08:05:00+08:00", "act_id": "a5"}
    ], "命中时游标推进到 marker 记的终点 C5（不是扩出的新批末尾 C6）"


@pytest.mark.asyncio
async def test_marker_encodes_and_parses_end_cursor():
    """marker 编码 round_id + 批终点游标 (created_at, act_id)，能原样解析回。"""
    from app.world.engine import _round_already_processed, _round_marker

    marker = _round_marker(
        "round123", end_created_at="2026-06-04T08:05:00+08:00", end_act_id="a5"
    )
    msg = Message(role=Role.USER, content=f"{marker}\nsome content")

    hit = _round_already_processed([msg], "round123")
    assert hit == ("2026-06-04T08:05:00+08:00", "a5"), (
        f"_round_already_processed 命中应返回终点游标，实际 {hit}"
    )
    miss = _round_already_processed([msg], "other-round")
    assert miss is None, "round_id 不匹配应返回 None"


@pytest.mark.asyncio
async def test_empty_batch_marker_has_no_end_cursor_hit_no_advance(monkeypatch):
    """空批次 marker 命中 → 无终点游标、不推进（空批次本就不推进游标）。"""
    from app.world.engine import _round_already_processed, _round_marker

    marker = _round_marker("emptyround", end_created_at=None, end_act_id=None)
    msg = Message(role=Role.USER, content=f"{marker}\nempty round")

    hit = _round_already_processed([msg], "emptyround")
    assert hit is None, "空批次 marker 命中应返回 None（无终点游标可推进）"


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
    """循环里（模拟模型调 sleep）记下的待办 self-wake，engine 收口后只 emit 一条。

    这条用 heartbeat 唤醒（snapshot.next_wake_at=None → 心跳放行 gate），专测
    「循环收口只 emit 一条 self-wake」。fire_self_wake 现在还会写 next_wake_at，
    stub 掉避免碰真库。
    """
    import app.world.tools as tools_mod
    from app.world.tools import FEATURE_SELF_WAKE

    delayed: list[dict] = []

    async def fake_emit_delayed(data, *, delay_ms, durability="durable"):
        delayed.append({"data": data, "delay_ms": delay_ms})

    async def fake_set_next_wake_at(*, lane, next_wake_at):
        return None

    monkeypatch.setattr(tools_mod, "emit_delayed", fake_emit_delayed)
    monkeypatch.setattr(tools_mod, "set_next_wake_at", fake_set_next_wake_at)

    async def fake_run(
        self, messages, *, prompt_vars=None, context=None, session_id=None, max_retries=2
    ):
        context.features[FEATURE_SELF_WAKE]["delay_ms"] = 300_000
        context.features[FEATURE_SELF_WAKE]["delay_ms"] = 600_000
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(engine_mod.Agent, "run", fake_run)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert len(delayed) == 1, "一轮最多 emit 一条 self WorldTick（最后一次 sleep 为准）"
    assert delayed[0]["delay_ms"] == 600_000
    tick = delayed[0]["data"]
    assert tick.lane == "coe-t2"
    assert tick.reason == "self"
    assert tick.target_wake_at, "emit 的 self WorldTick 必须携带目标唤醒时刻（stale 判定靠它）"


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

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    assert engine_mod._ROUND_MARKER_PREFIX in blob, (
        "stimulus 必须带本轮 round_id 标记（幂等查重靠它）"
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


# ---------------------------------------------------------------------------
# 观测刀：每轮 world 思考的 token 落 durable PG（不依赖会丢的 langfuse）。
#
# world_tick 用 collect_usage() 把 Agent.run 包住、run 完拿累计 token 调
# record_round_cost 落库，actor = "world"。fake_run 在 collector 作用域内
# _accumulate_usage 模拟 adapter 记 token，断言收口确实把累计 token 落了 PG。
# ---------------------------------------------------------------------------


def _mock_run_with_usage(monkeypatch, usage):
    """fake Agent.run，在当前 collect_usage 作用域内累加一笔 usage。"""

    async def fake_run(
        self, messages, *, prompt_vars=None, context=None, session_id=None,
        max_retries=2,
    ):
        from app.agent.trace import _accumulate_usage

        _accumulate_usage(usage)
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(engine_mod.Agent, "run", fake_run)


@pytest.fixture
def world_cost_recorded(monkeypatch):
    """打桩 engine.record_round_cost，记录落库调用 kwargs（含 usage）。"""
    calls: list[dict] = []

    async def fake_record(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(engine_mod, "record_round_cost", fake_record)
    return calls


@pytest.mark.asyncio
async def test_world_round_records_token_cost_with_world_actor(
    monkeypatch, world_cost_recorded
):
    """一轮 world 收口把本轮累计 token 落 PG，actor = "world"、带 collect_usage 累计。"""
    _mock_run_with_usage(
        monkeypatch,
        {"input": 500, "output": 80, "total": 580, "cache_read_input_tokens": 100},
    )

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert len(world_cost_recorded) == 1, "应落且只落一条本轮成本记录"
    rec = world_cost_recorded[0]
    assert rec["lane"] == "coe-t2"
    assert rec["actor"] == "world", "world 的 actor 必须是 'world'"
    # collect_usage 累计的本轮 token 原样传给 record_round_cost
    assert rec["usage"]["input"] == 500
    assert rec["usage"]["output"] == 80
    assert rec["usage"]["total"] == 580
    assert rec["usage"]["cache_read_input_tokens"] == 100
    assert rec["usage"]["calls"] == 1
    assert rec["round_id"], "必须带 round_id（与 turn 幂等同源）"
    assert rec["observed_at"], "必须带观测时刻"


@pytest.mark.asyncio
async def test_world_cost_record_failure_does_not_fail_round(monkeypatch):
    """落成本失败必须 best-effort 吞掉，不把一轮真实推演搞成失败（游标照常推进）。

    打桩真实 ``thinking_cost.record_thinking_tokens`` 抛错（走 record_round_cost 里真正
    的 swallow 路径），而非打桩 engine 的 record_round_cost —— 这样测的是真实吞错语义。
    """
    import app.domain.thinking_cost as tc

    # 一批非空 act，让收口推进游标可被观测。
    batch = [_act("coe-t2", "a1", "akao", "去厨房", "2026-06-03T06:00:00+08:00", "c1")]

    async def fake_batch(*, lane, cursor_created_at, cursor_act_id, limit):
        return batch

    monkeypatch.setattr(engine_mod, "list_recent_acts", fake_batch)
    _mock_run_with_usage(
        monkeypatch, {"input": 1, "output": 1, "total": 2}
    )

    async def boom_record(**kwargs):
        raise RuntimeError("PG down recording cost")

    monkeypatch.setattr(tc, "record_thinking_tokens", boom_record)

    # 不该抛——成本观测是旁路。
    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    # 游标照常推进到本批末尾（成本失败不影响真实推演收口）。
    assert engine_mod._test_cursor_calls == [
        {"lane": "coe-t2", "created_at": "c1", "act_id": "a1"}
    ], "记成本失败不该阻断游标推进"
