"""world 到点 gate + next_wake_at 生命周期 — 阶段 1B Task 1.

核心 bug：world 调 sleep(3600) 想长睡，但 WorldState 没存"下次该醒的时刻"，
600 秒保底心跳照样无条件把它拍醒、长睡意愿从未生效。Task 1 给 world 补上：

  * WorldState 多一个 ``next_wake_at`` 字段（nullable，存现实 aware ISO 时刻）。
  * world 决定 sleep(seconds) 时，目标唤醒时刻 = 现实 now + seconds，在循环收口
    （fire_self_wake）写进 WorldState.next_wake_at；emit 的 self WorldTick 携带这个
    目标时刻（``target_wake_at``），到期时供判 stale。
  * world_tick 真正推演前走「到点 gate」（pull 范式下只剩 self / heartbeat 两源、
    都走 gate；act 已退出唤醒语义）：
      - reason == self / heartbeat：走 gate。到点判定用**现实时间**（now ≥
        next_wake_at）且（对 self）它携带的目标时刻 == WorldState 当前 next_wake_at
        （没被更新覆盖）；不满足判废（log + 不推演 + 不产新 state）。next_wake_at
        为 None（从没排过）时心跳放行（别卡死首轮）。
      - gate 比较一律用现实 aware 时间，不用 world_time（world_time 会因 gate 停滞）。

这些测试 mock ``Agent.run`` + stub 现成 handler，钉死 gate 机制（不是 LLM 决策）。
gate 是"让自排意愿生效"的机制护栏，不替 world 决定推演内容（赤尾宪法）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import fakeredis.aioredis
import pytest

import app.world.engine as engine_mod
import app.world.tools as tools_mod
from app.agent.neutral import Message, Role
from app.world.engine import WorldTick, world_tick
from app.world.state import WorldState


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
    """In-memory session transcript store（防止 gate 放行的轮真去读 PG transcript）。"""
    import json

    from app.agent import session as session_mod
    from app.agent.neutral import Message as _Msg

    store: dict[str, str] = {}

    async def fake_load(session_id: str):
        raw = store.get(session_id)
        if raw is None:
            return []
        return [_Msg.from_replay_dict(d) for d in json.loads(raw)]

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
def _stub_io(monkeypatch):
    """stub 信箱对账 / act 批次读取 / 游标推进（不碰真库）；read_world_state 由每个用例自定。"""

    async def fake_renotify_unread(*, lane):
        return 0

    async def fake_list_recent_acts(*, lane, cursor_created_at, cursor_act_id, limit):
        return []

    async def fake_advance_act_cursor(*, lane, created_at, act_id):
        return None

    monkeypatch.setattr(engine_mod, "renotify_unread", fake_renotify_unread)
    monkeypatch.setattr(engine_mod, "list_recent_acts", fake_list_recent_acts)
    monkeypatch.setattr(engine_mod, "advance_act_cursor", fake_advance_act_cursor)


def _stub_state(monkeypatch, snapshot: WorldState | None):
    async def fake_read(*, lane):
        return snapshot

    monkeypatch.setattr(engine_mod, "read_world_state", fake_read)


def _mock_run(monkeypatch):
    """把 ``Agent.run`` 换成记录是否被调的桩。"""
    captured: dict = {"ran": False, "messages": None}

    async def fake_run(
        self, messages, *, prompt_vars=None, context=None, session_id=None, max_retries=2
    ):
        captured["ran"] = True
        captured["messages"] = messages
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(engine_mod.Agent, "run", fake_run)
    return captured


def _now_cst() -> datetime:
    return datetime.now(engine_mod._CST)


# ---------------------------------------------------------------------------
# WorldState.next_wake_at 字段
# ---------------------------------------------------------------------------


def test_worldstate_has_nullable_next_wake_at():
    """WorldState 多一个 ``next_wake_at`` 字段，nullable（默认 None）。"""
    assert "next_wake_at" in WorldState.model_fields
    snap = WorldState(lane="x", world_time="t", detail="d")
    assert snap.next_wake_at is None, "next_wake_at 默认 None（从没排过）"


# ---------------------------------------------------------------------------
# gate：长 sleep 后的保底心跳被判废、不推演
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_before_next_wake_is_gated_out(monkeypatch):
    """world 排了一个未来才到的 next_wake_at → 保底心跳来时判废，不跑 run。"""
    future = (_now_cst() + timedelta(minutes=30)).isoformat()
    _stub_state(
        monkeypatch,
        WorldState(lane="coe-t2", world_time="t", detail="d", next_wake_at=future),
    )
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert captured["ran"] is False, "未到 next_wake_at 的心跳必须被 gate 判废、不推演"


@pytest.mark.asyncio
async def test_heartbeat_after_next_wake_passes(monkeypatch):
    """next_wake_at 已过 → 心跳到点放行、跑 run。"""
    past = (_now_cst() - timedelta(seconds=5)).isoformat()
    _stub_state(
        monkeypatch,
        WorldState(lane="coe-t2", world_time="t", detail="d", next_wake_at=past),
    )
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert captured["ran"] is True, "next_wake_at 已过的心跳应到点放行"


@pytest.mark.asyncio
async def test_heartbeat_passes_when_next_wake_is_none(monkeypatch):
    """next_wake_at 为 None（从没排过）→ 心跳放行，别卡死首轮。"""
    _stub_state(
        monkeypatch,
        WorldState(lane="coe-t2", world_time="t", detail="d", next_wake_at=None),
    )
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert captured["ran"] is True, "next_wake_at=None 时心跳必须放行（首轮不卡死）"


@pytest.mark.asyncio
async def test_heartbeat_passes_on_cold_start_no_snapshot(monkeypatch):
    """冷启动（无 WorldState 快照）→ 心跳放行（没排过等价 None）。"""
    _stub_state(monkeypatch, None)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert captured["ran"] is True, "冷启动无快照时心跳必须放行"


# ---------------------------------------------------------------------------
# gate：self 唤醒到点 + stale 判定
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_wake_on_time_and_matching_target_passes(monkeypatch):
    """self 唤醒到点（now ≥ next_wake_at）且携带目标 == state 当前值 → 放行。"""
    target = (_now_cst() - timedelta(seconds=1)).isoformat()
    _stub_state(
        monkeypatch,
        WorldState(lane="coe-t2", world_time="t", detail="d", next_wake_at=target),
    )
    captured = _mock_run(monkeypatch)

    await world_tick(
        WorldTick(lane="coe-t2", reason="self", target_wake_at=target)
    )

    assert captured["ran"] is True, "到点且目标匹配的 self 唤醒应放行"


@pytest.mark.asyncio
async def test_self_wake_before_time_is_gated_out(monkeypatch):
    """self 唤醒没到点（now < next_wake_at）→ 判废，不跑。"""
    future = (_now_cst() + timedelta(minutes=10)).isoformat()
    _stub_state(
        monkeypatch,
        WorldState(lane="coe-t2", world_time="t", detail="d", next_wake_at=future),
    )
    captured = _mock_run(monkeypatch)

    await world_tick(
        WorldTick(lane="coe-t2", reason="self", target_wake_at=future)
    )

    assert captured["ran"] is False, "没到点的 self 唤醒必须判废"


@pytest.mark.asyncio
async def test_self_wake_stale_target_is_gated_out(monkeypatch):
    """self 携带的旧目标时刻被 state 当前值覆盖（被新自排 / 外部打断）→ 判废 stale。

    哪怕这条旧 self 唤醒到点了（携带的旧目标已过），只要 state.next_wake_at 已被更新
    成另一个值（说明这条 self 已作废），就必须判废、不误触发推演。
    """
    stale_target = (_now_cst() - timedelta(minutes=5)).isoformat()
    current = (_now_cst() - timedelta(seconds=1)).isoformat()
    _stub_state(
        monkeypatch,
        WorldState(lane="coe-t2", world_time="t", detail="d", next_wake_at=current),
    )
    captured = _mock_run(monkeypatch)

    await world_tick(
        WorldTick(lane="coe-t2", reason="self", target_wake_at=stale_target)
    )

    assert captured["ran"] is False, (
        "self 携带的目标时刻与 state 当前 next_wake_at 不符（被覆盖）必须判废 stale"
    )


# ---------------------------------------------------------------------------
# gate 用现实时间，不用 world_time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_uses_realtime_not_stale_world_time(monkeypatch):
    """gate 比较用现实 aware 时间，不用 world_time（world_time 会因 gate 停滞）。

    构造一个 next_wake_at 已过（现实时间判定该醒），但 world_time 还停在很久以前
    （gate 期间没推演、world_time 不动）的快照。心跳必须按现实时间放行，证明 gate
    没有用 world_time 做比较。
    """
    past_wake = (_now_cst() - timedelta(seconds=2)).isoformat()
    stale_world_time = (_now_cst() - timedelta(hours=3)).isoformat()
    _stub_state(
        monkeypatch,
        WorldState(
            lane="coe-t2",
            world_time=stale_world_time,
            detail="d",
            next_wake_at=past_wake,
        ),
    )
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert captured["ran"] is True, (
        "gate 必须用现实时间判到点（next_wake_at 已过），不被停滞的 world_time 影响"
    )


# ---------------------------------------------------------------------------
# fire_self_wake：写 next_wake_at + self 唤醒携带目标时刻
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_self_wake_writes_next_wake_at_and_carries_target(monkeypatch):
    """fire_self_wake：目标时刻 = 现实 now + delay，写进 WorldState.next_wake_at，
    且 emit 的 self WorldTick 携带这个目标时刻（target_wake_at）。"""
    delayed: list[dict] = []
    set_calls: list[dict] = []

    async def fake_emit_delayed(data, *, delay_ms, durability="durable"):
        delayed.append({"data": data, "delay_ms": delay_ms})

    async def fake_set_next_wake_at(*, lane, next_wake_at):
        set_calls.append({"lane": lane, "next_wake_at": next_wake_at})

    monkeypatch.setattr(tools_mod, "emit_delayed", fake_emit_delayed)
    monkeypatch.setattr(tools_mod, "set_next_wake_at", fake_set_next_wake_at)

    before = _now_cst()
    fired = await tools_mod.fire_self_wake(
        lane="coe-t2", self_wake={"delay_ms": 1800_000}
    )
    after = _now_cst()

    assert fired is True
    assert len(delayed) == 1
    tick = delayed[0]["data"]
    assert tick.reason == "self"
    assert tick.lane == "coe-t2"
    # self 唤醒携带目标时刻
    assert tick.target_wake_at, "self WorldTick 必须携带目标唤醒时刻"
    # 目标时刻 ≈ now + 1800s（落在 [before+1800, after+1800] 区间内）
    target = datetime.fromisoformat(tick.target_wake_at)
    assert before + timedelta(seconds=1800) <= target <= after + timedelta(seconds=1800)
    # 写进 WorldState 的 next_wake_at 与 tick 携带的目标时刻一致
    assert len(set_calls) == 1
    assert set_calls[0]["lane"] == "coe-t2"
    assert set_calls[0]["next_wake_at"] == tick.target_wake_at, (
        "写进 state 的 next_wake_at 必须 == self 唤醒携带的目标时刻（stale 判定靠相等）"
    )


@pytest.mark.asyncio
async def test_fire_self_wake_no_pending_does_not_write_or_emit(monkeypatch):
    """没调 sleep（空待办）→ 不写 next_wake_at、不 emit（靠保底心跳兜底）。"""
    delayed: list = []
    set_calls: list = []

    async def fake_emit_delayed(data, *, delay_ms, durability="durable"):
        delayed.append(data)

    async def fake_set_next_wake_at(*, lane, next_wake_at):
        set_calls.append(next_wake_at)

    monkeypatch.setattr(tools_mod, "emit_delayed", fake_emit_delayed)
    monkeypatch.setattr(tools_mod, "set_next_wake_at", fake_set_next_wake_at)

    fired = await tools_mod.fire_self_wake(lane="coe-t2", self_wake={})

    assert fired is False
    assert delayed == []
    assert set_calls == [], "没自排就不该写 next_wake_at"


# ---------------------------------------------------------------------------
# renotify_unread 在 gate 之前：判废也补敲（机械 IO 兜底不被 gate 挡）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_renotify_runs_even_when_tick_is_gated_out(monkeypatch):
    """world tick 被 gate 判废（如长睡期间的保底心跳）时，renotify_unread 仍补敲一次。

    renotify_unread 是纯机械 IO 兜底（补遗留 / 敲门丢失的未读 event，不经 LLM、不进
    世界内容决策、对已读 persona 无害），不该被到点 gate 挡掉。否则 world 长睡期间每个
    保底心跳都被判废、stranded 信箱就永远没人补敲。所以每个 tick 一进来先 renotify，
    再走 gate；gate 判废仍 early return（但补敲已经做过）。
    """
    future = (_now_cst() + timedelta(minutes=30)).isoformat()
    _stub_state(
        monkeypatch,
        WorldState(lane="coe-t2", world_time="t", detail="d", next_wake_at=future),
    )

    renotify_calls: list[str] = []

    async def tracking_renotify(*, lane):
        renotify_calls.append(lane)
        return 0

    monkeypatch.setattr(engine_mod, "renotify_unread", tracking_renotify)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert captured["ran"] is False, "未到 next_wake_at 的心跳仍必须被 gate 判废、不推演"
    assert renotify_calls == ["coe-t2"], (
        "renotify_unread 是机械 IO 兜底，必须在 gate 判废时也补敲一次（不被 gate 挡）"
    )
