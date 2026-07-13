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
  * **任何唤醒源**都从游标 pull act 喂 world、命中防爆栅栏（真有剩余）才告知积压、
    收口推进游标。

赤尾设计宪法：world 推演谁够得着、产什么动静、世界什么样全由 LLM 在循环里
判断，代码里没有阈值 / 计数器替它决策。10 分钟保底心跳 + sleep 上限只决定
"何时醒 / 别睡死"，WORLD_ACT_PULL_LIMIT 是防病态洪峰撑爆单轮 prompt 的防爆栅栏
（正常一次拉完、命中必 warning），都不进世界内容决策。
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

    close_calls: list[dict] = []

    async def fake_record_world_round_close(
        *, lane, advance_cursor_to, materials_ingested_date, roster_ingested_date=None
    ):
        # 正常成功收口走这个统一收口：既推游标（advance_cursor_to 非 None 时）又标记
        # 底料 / 名册已纳入（对应日期非 None 时），一次 append。这里只记录调用，同时
        # 把"推进游标"那部分镜像进 _test_cursor_calls，保持旧用例（断言游标推进）
        # 不动——它们仍能断言"游标推到本批末尾"。
        close_calls.append(
            {
                "lane": lane,
                "advance_cursor_to": advance_cursor_to,
                "materials_ingested_date": materials_ingested_date,
                "roster_ingested_date": roster_ingested_date,
            }
        )
        if advance_cursor_to is not None:
            cursor_calls.append(
                {
                    "lane": lane,
                    "created_at": advance_cursor_to[0],
                    "act_id": advance_cursor_to[1],
                }
            )

    materials_calls: list[dict] = []

    async def fake_find_daily_materials(*, lane, date):
        # 默认今天还没抓到底料（None）：专测引擎机制的用例不碰真库。需要断言外部
        # 底料的用例自己覆写这个桩（monkeypatch.setattr engine_mod.find_daily_materials）。
        materials_calls.append({"lane": lane, "date": date})
        return None

    arc_reads: list[str] = []

    async def fake_read_world_arc(*, lane):
        # 默认世界阶段还是空白（None = 冷启动）：需要断言阶段内容的用例自己覆写这个桩
        # （monkeypatch.setattr engine_mod.read_world_arc）。
        arc_reads.append(lane)
        return None

    outline_reads: list[str] = []

    async def fake_read_world_outline(*, lane):
        # 默认大纲还是空白（None = 冷启动还没记过线）：需要断言大纲内容 / reminder 的用例
        # 自己覆写这个桩（monkeypatch.setattr engine_mod.read_world_outline）。
        outline_reads.append(lane)
        return None

    roster_reads: list[str] = []

    async def fake_list_npc_roster(*, lane):
        # 默认名册为空（[] = 还没 seed）：需要断言名册段的用例自己覆写这个桩
        # （monkeypatch.setattr engine_mod.list_npc_roster）。
        roster_reads.append(lane)
        return []

    seed_calls: list[str] = []

    async def fake_seed_npc_roster(*, lane):
        # 种子名册的生产自动入口（必改 1）每天首醒会调一次 seed（默认快照
        # roster_ingested_date=None → 视作首醒），真打 PG 的 CAS insert。专测引擎机制
        # 的用例不关心 seed 本身，统一打成 no-op，让单测无库也安静。需要断言 seed
        # 行为的用例自己覆写这个桩（monkeypatch.setattr engine_mod.seed_npc_roster）。
        seed_calls.append(lane)
        return 0

    persona_id_calls: list[str] = []

    async def fake_list_all_persona_ids():
        # 默认没有任何 persona（空名单）：专测引擎机制的用例不碰真库、不拼三姐妹段。
        # 需要断言三姐妹此刻段的用例自己覆写这个桩 + find_life_state 桩。
        persona_id_calls.append("called")
        return []

    life_state_reads: list[dict] = []

    async def fake_find_life_state(*, lane, persona_id):
        # 默认读不到任何 LifeState（None）：无库环境下安静降级。需要断言三姐妹此刻
        # 状态内容的用例自己覆写这个桩。
        life_state_reads.append({"lane": lane, "persona_id": persona_id})
        return None

    reflect_calls: list[dict] = []

    async def fake_run_arc_reflection(**kwargs):
        # 默认打桩反思环节（记录调用、不跑真 Agent / 不碰真库）：专测引擎机制的用例
        # 不该触发真实反思（langfuse prompt 拉取 / PG 标记）。需要断言反思行为的用例
        # 自己覆写这个桩（monkeypatch.setattr engine_mod.run_arc_reflection）。
        reflect_calls.append(kwargs)

    # 收口后两步旁路（记成本 + transcript 沉淀折叠）都会真打 PG（record_round_cost
    # 落 thinking token、fold_session 读 session transcript），它们各自 fail-open（只
    # log warning 不抛），所以**不打桩也不会让用例 fail**——但每轮都对真库发一次连接
    # 尝试（无库环境徒增噪声 + 慢）。专测引擎机制的用例不关心成本 / 折叠，统一在这里
    # 打成 no-op，让单测边界干净、无库也安静（codex 建议 2）。需要断言成本 / 折叠行为
    # 的用例自己覆写这两个桩。
    cost_calls: list[dict] = []

    async def fake_record_round_cost(**kwargs):
        cost_calls.append(kwargs)

    fold_calls: list[str] = []

    async def fake_fold_session(session_id, policy):
        fold_calls.append(session_id)

    monkeypatch.setattr(engine_mod, "run_arc_reflection", fake_run_arc_reflection)
    monkeypatch.setattr(engine_mod, "record_round_cost", fake_record_round_cost)
    monkeypatch.setattr(engine_mod, "fold_session", fake_fold_session)
    monkeypatch.setattr(engine_mod, "read_world_state", fake_read_world_state)
    monkeypatch.setattr(engine_mod, "renotify_unread", fake_renotify_unread)
    monkeypatch.setattr(engine_mod, "list_recent_acts", fake_list_recent_acts)
    monkeypatch.setattr(engine_mod, "advance_act_cursor", fake_advance_act_cursor)
    monkeypatch.setattr(
        engine_mod, "record_world_round_close", fake_record_world_round_close
    )
    monkeypatch.setattr(
        engine_mod, "find_daily_materials", fake_find_daily_materials
    )
    monkeypatch.setattr(engine_mod, "read_world_arc", fake_read_world_arc)
    monkeypatch.setattr(engine_mod, "read_world_outline", fake_read_world_outline)
    monkeypatch.setattr(engine_mod, "list_npc_roster", fake_list_npc_roster)
    monkeypatch.setattr(engine_mod, "seed_npc_roster", fake_seed_npc_roster)
    monkeypatch.setattr(
        engine_mod, "list_all_persona_ids", fake_list_all_persona_ids
    )
    monkeypatch.setattr(engine_mod, "find_life_state", fake_find_life_state)

    engine_mod._test_materials_calls = materials_calls  # type: ignore[attr-defined]
    engine_mod._test_arc_reads = arc_reads  # type: ignore[attr-defined]
    engine_mod._test_outline_reads = outline_reads  # type: ignore[attr-defined]
    engine_mod._test_roster_reads = roster_reads  # type: ignore[attr-defined]
    engine_mod._test_reflect_calls = reflect_calls  # type: ignore[attr-defined]
    engine_mod._test_cost_calls = cost_calls  # type: ignore[attr-defined]
    engine_mod._test_fold_calls = fold_calls  # type: ignore[attr-defined]
    engine_mod._test_seed_calls = seed_calls  # type: ignore[attr-defined]

    engine_mod._test_renotify_calls = renotify_calls  # type: ignore[attr-defined]
    engine_mod._test_cursor_calls = cursor_calls  # type: ignore[attr-defined]
    engine_mod._test_close_calls = close_calls  # type: ignore[attr-defined]
    engine_mod._test_persona_id_calls = persona_id_calls  # type: ignore[attr-defined]
    engine_mod._test_life_state_reads = life_state_reads  # type: ignore[attr-defined]
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


def _materials(
    *,
    lane="coe-t2",
    date="2026-06-08",
    briefing="今天广州多云转小雨，气温 22~28℃；《某番》今晚更新第 8 话；是普通工作日。",
    fetched_at="2026-06-08T06:05:00+08:00",
):
    """构造一条 DailyMaterials（world 当天读到的外部底料）。

    DailyMaterials 已简化成只存抓取 agent 组织好的那段 ``briefing`` 中文话 + date / lane
    / fetched_at —— 某源是否拿到 agent 已在 briefing 里说清，不再有每源 ``*_text`` /
    ``*_ok`` 字段。
    """
    from app.fetch.materials import DailyMaterials

    return DailyMaterials(
        lane=lane,
        date=date,
        briefing=briefing,
        fetched_at=fetched_at,
    )


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
# 防爆栅栏（不是节拍器）：world 醒来把游标之后攒下的 act **一次拉完**、把这段
# 时间的账一口气收进「世界流到此刻的样子」、叙述落在现实此刻。
# WORLD_ACT_PULL_LIMIT 只防病态洪峰（life 失控刷 act）撑爆单轮 prompt——正常
# （含整天宕机后的追账）永远不触发；触发必打 warning（截了多少、还剩多少，
# no silent caps），游标只推进到实际消化的最后一条、剩下下轮从游标继续。
# ---------------------------------------------------------------------------


def _act_store(lane: str, n: int):
    """构造 n 条按 (created_at, act_id) 升序的 act 存量（每条间隔 1 分钟）。"""
    from datetime import timedelta

    from app.infra.cst_time import CST

    base = datetime(2026, 6, 4, 8, 0, 0, tzinfo=CST)
    return [
        _act(
            lane,
            f"a{i:04d}",
            "akao",
            f"动作{i}号",
            (base + timedelta(minutes=i)).isoformat(),
            (base + timedelta(minutes=i)).isoformat(),
        )
        for i in range(n)
    ]


def _query_from(store):
    """模拟真实 list_recent_acts 的复合游标 + limit 语义（栅栏探询会二次查询）。"""

    async def fake(*, lane, cursor_created_at, cursor_act_id, limit):
        if cursor_created_at is None or cursor_act_id is None:
            after = store
        else:
            after = [
                (a, c)
                for a, c in store
                if (c, a.act_id) > (cursor_created_at, cursor_act_id)
            ]
        return after[:limit]

    return fake


def test_pull_limit_is_a_fence_not_a_metronome():
    """栅栏量级在 100~300：远超正常产出（三个 life 一天 ~125 条），正常永不触发。"""
    assert 100 <= engine_mod.WORLD_ACT_PULL_LIMIT <= 300, (
        "WORLD_ACT_PULL_LIMIT 是防爆栅栏不是节拍器：量级必须远超正常 act 产出"
        "（三个 life 一天 ~125 条），让正常运行乃至整天宕机后的追账都一轮拉得完"
    )


@pytest.mark.asyncio
async def test_backlog_below_fence_pulled_in_one_round(monkeypatch, caplog):
    """积压 N（>10、<栅栏）条 → 一轮全拉完：全进 prompt、游标推到全量末尾、无积压告知、无 warning。

    这是「节拍器 → 防爆栅栏」的核心行为：旧实现每轮只消化 10 条、按旧 act 的
    时间戳逐轮补叙（coe 实证世界叙事落后现实 ~9 小时）；新语义是没命中栅栏时
    行为 = 全量、一轮把账收完。
    """
    import logging

    store = _act_store("coe-t2", 40)
    monkeypatch.setattr(engine_mod, "list_recent_acts", _query_from(store))
    captured = _mock_run(monkeypatch)

    with caplog.at_level(logging.INFO, logger="app.world.engine"):
        await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    for i in range(40):
        assert f"动作{i}号" in blob, f"积压低于栅栏必须一轮全拉完（缺第 {i} 条）"
    assert "积压" not in blob, "没命中栅栏不该告知积压（行为 = 全量）"

    last_act, last_created = store[-1]
    assert engine_mod._test_cursor_calls == [
        {"lane": "coe-t2", "created_at": last_created, "act_id": last_act.act_id}
    ], "游标应推进到本批（全量）末尾"

    warnings = [
        r
        for r in caplog.records
        if r.name == "app.world.engine" and r.levelno >= logging.WARNING
    ]
    assert warnings == [], "没命中栅栏不该打 warning"


@pytest.mark.asyncio
async def test_fence_hit_warns_and_cursor_stops_at_consumed_end(monkeypatch, caplog):
    """命中栅栏（病态洪峰）→ 拉到上限 + warning（截了多少、还剩多少）+ 积压告知 + 游标只推到消化末尾。"""
    import logging

    fence = engine_mod.WORLD_ACT_PULL_LIMIT
    store = _act_store("coe-t2", fence + 25)
    monkeypatch.setattr(engine_mod, "list_recent_acts", _query_from(store))
    captured = _mock_run(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="app.world.engine"):
        await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    assert f"动作{fence - 1}号" in blob, "栅栏内最后一条必须在 prompt 里"
    assert f"动作{fence}号" not in blob, "超出栅栏的 act 本轮不该进 prompt"
    assert ("积压" in blob) or ("没读完" in blob) or ("还有" in blob), (
        "命中栅栏必须告知 world 还有动作没读完（由她排短 sleep 下轮继续）"
    )

    consumed_act, consumed_created = store[fence - 1]
    assert engine_mod._test_cursor_calls == [
        {
            "lane": "coe-t2",
            "created_at": consumed_created,
            "act_id": consumed_act.act_id,
        }
    ], "命中栅栏游标只推进到实际消化的最后一条（剩下下轮从这继续）"

    fence_warnings = [
        r
        for r in caplog.records
        if r.name == "app.world.engine" and r.levelno >= logging.WARNING
    ]
    assert fence_warnings, "命中栅栏必须打 warning（no silent caps）"
    text = " ".join(r.getMessage() for r in fence_warnings)
    assert str(fence) in text, "warning 必须说明这轮截到多少条（栅栏值）"
    assert "25" in text, "warning 必须说明还剩多少条没消化"


@pytest.mark.asyncio
async def test_backlog_exactly_fence_is_not_a_fence_hit(monkeypatch, caplog):
    """积压正好 == 栅栏值（一轮恰好拉完、没有剩余）→ 不算命中：无 warning、不告知积压。"""
    import logging

    fence = engine_mod.WORLD_ACT_PULL_LIMIT
    store = _act_store("coe-t2", fence)
    monkeypatch.setattr(engine_mod, "list_recent_acts", _query_from(store))
    captured = _mock_run(monkeypatch)

    with caplog.at_level(logging.INFO, logger="app.world.engine"):
        await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    assert "积压" not in blob, "恰好拉完（没有剩余）不算命中栅栏、不该告知积压"
    warnings = [
        r
        for r in caplog.records
        if r.name == "app.world.engine" and r.levelno >= logging.WARNING
    ]
    assert warnings == [], "恰好拉完不该打 warning（行为 = 全量）"


@pytest.mark.asyncio
async def test_fence_hit_probe_saturated_warns_lower_bound(monkeypatch, caplog):
    """命中栅栏且探询也读满（剩余 ≥ 栅栏值）→ warning 如实报下界 ">=N"、积压缘由照常告知。

    探询本身有界（最多再读一个栅栏值），剩余超过探询窗口时拿不到精确数，warning
    必须如实报 ">=栅栏值" 下界、不冒充精确值（no silent caps 的另一半：不说谎）。
    与 test_fence_hit_warns_and_cursor_stops_at_consumed_end（剩余 25 条、报精确数）
    互补，钉死 remaining 报数的两个分支。store 给 2×栅栏 + 5 条：首拉满栅栏、探询
    从消化末尾按游标继续读、又读满 → 只能报下界。

    （前身 test_pull_full_batch_tells_model_backlog 把「读满 N 条」当积压语义，且
    mock 无视游标、探询永远满批——它实际测的就是这条「探询饱和」路径，只是顶着
    旧名字与「积压==栅栏不告警」的新边界互相矛盾。改成游标感知后归位到这里。）
    """
    import logging

    fence = engine_mod.WORLD_ACT_PULL_LIMIT
    store = _act_store("coe-t2", fence * 2 + 5)
    monkeypatch.setattr(engine_mod, "list_recent_acts", _query_from(store))
    captured = _mock_run(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="app.world.engine"):
        await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    assert ("积压" in blob) or ("没读完" in blob) or ("还有" in blob), (
        "真有剩余（命中栅栏）必须在 prompt 里告知 world 有积压"
    )

    consumed_act, consumed_created = store[fence - 1]
    assert engine_mod._test_cursor_calls == [
        {
            "lane": "coe-t2",
            "created_at": consumed_created,
            "act_id": consumed_act.act_id,
        }
    ], "探询饱和时游标同样只推进到实际消化的最后一条"

    fence_warnings = [
        r
        for r in caplog.records
        if r.name == "app.world.engine" and r.levelno >= logging.WARNING
    ]
    assert fence_warnings, "命中栅栏必须打 warning（no silent caps）"
    text = " ".join(r.getMessage() for r in fence_warnings)
    assert f">={fence}" in text, (
        "剩余 ≥ 栅栏值（探询窗口拿不到精确数）时 warning 必须如实报下界 >=N、不冒充精确值"
    )


@pytest.mark.asyncio
async def test_act_batch_framing_folds_old_acts_into_present(monkeypatch):
    """act 批框架文案允许把跨了一段时间的旧账一笔收拢、叙述落在现实此刻。

    coe 实证：world 对着带旧时间戳的 act 清单会按旧时间戳逐轮补叙旧戏（detail 在
    傍晚还在补叙早上八点）。一次拉完后单批可能横跨几小时，框架文案必须明示：
    不按各条旧时间戳逐条补叙，把这段时间的账收进世界流到此刻的样子。
    """
    store = _act_store("coe-t2", 3)
    monkeypatch.setattr(engine_mod, "list_recent_acts", _query_from(store))
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    assert ("一笔收" in blob) or ("一口气收" in blob) or ("收拢" in blob), (
        "act 批框架文案必须允许把这段时间的旧账一笔收拢"
    )
    assert ("补叙" in blob) or ("旧时间戳" in blob) or ("回放" in blob), (
        "act 批框架文案必须明示不按各条旧时间戳逐条补叙旧场景"
    )


# ---------------------------------------------------------------------------
# 刀 3 调整：world「当天第一次醒纳入底料一次、进意识流，之后当天不再重喂」
#
# 之前每轮都 find_daily_materials + _materials_section + 拼背景（啰嗦）。改成：
# 今天有底料 **且** WorldState.materials_ingested_date != 今天 → 纳入一次（拼进
# 这轮 user 消息），收口标记 materials_ingested_date=今天；当天后续轮次（已纳入）
# 不再重喂；今天没底料（None）→ 不拼、不读昨天；纳入那轮崩溃 → 不误标记。
# ---------------------------------------------------------------------------


def _snapshot_with(monkeypatch, **kwargs):
    """覆写 read_world_state，返回一个带指定字段的 WorldState（其余字段取默认）。"""
    from app.world.state import WorldState

    base = {"lane": "coe-t2", "world_time": "t", "detail": "d"}
    base.update(kwargs)

    async def fake_read(*, lane):
        return WorldState(**{**base, "lane": lane})

    monkeypatch.setattr(engine_mod, "read_world_state", fake_read)


@pytest.mark.asyncio
async def test_first_wake_today_with_materials_ingests_briefing_and_marks(monkeypatch):
    """① 当天第一次醒、有底料、未纳入 → briefing 进 user 消息，且收口标记 materials_ingested_date=今天。"""
    from app.infra import cst_time

    today = cst_time.now_cst().strftime("%Y-%m-%d")
    # 未纳入过（materials_ingested_date=None，区别于今天）。
    _snapshot_with(monkeypatch, materials_ingested_date=None)

    async def materials(*, lane, date):
        return _materials(
            briefing="今天广州一整天小雨；《某番》今晚更新第 8 话；普通工作日。",
        )

    monkeypatch.setattr(engine_mod, "find_daily_materials", materials)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    assert "小雨" in blob, "当天首醒有底料应把 briefing 纳入 world 的 USER 消息"
    assert "第 8 话" in blob, "当天首醒有底料应把 briefing 纳入 world 的 USER 消息"

    # 收口把 materials_ingested_date 标成今天（并进统一收口的同一次 append）。
    assert engine_mod._test_close_calls, "纳入那轮收口必须走 record_world_round_close"
    last = engine_mod._test_close_calls[-1]
    assert last["materials_ingested_date"] == today, (
        f"纳入那轮收口必须把 materials_ingested_date 标成今天 {today}，实际 {last}"
    )


@pytest.mark.asyncio
async def test_first_wake_queries_materials_for_today_cst(monkeypatch):
    """未纳入时按 (当前 lane, 今天 CST date) 查底料 —— 绝不读昨天。"""
    _snapshot_with(monkeypatch, materials_ingested_date=None)

    calls: list[dict] = []

    async def materials(*, lane, date):
        calls.append({"lane": lane, "date": date})
        return _materials(lane=lane, date=date)

    monkeypatch.setattr(engine_mod, "find_daily_materials", materials)
    _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    from app.infra import cst_time

    today = cst_time.now_cst().strftime("%Y-%m-%d")
    assert calls == [{"lane": "coe-t2", "date": today}], (
        f"未纳入时必须按 (当前 lane, 今天 CST date) 查底料，绝不读昨天，实际 {calls}"
    )


@pytest.mark.asyncio
async def test_second_wake_same_day_does_not_refeed_briefing(monkeypatch):
    """② 同一天第二次醒（已纳入）→ briefing 不再进 user 消息（不重喂）。"""
    from app.infra import cst_time

    today = cst_time.now_cst().strftime("%Y-%m-%d")
    # 已纳入过今天（materials_ingested_date == 今天）。
    _snapshot_with(monkeypatch, materials_ingested_date=today)

    secret_briefing = "今天广州一整天小雨这串只在底料里出现"
    queried: list = []

    async def materials(*, lane, date):
        queried.append(date)
        return _materials(briefing=secret_briefing)

    monkeypatch.setattr(engine_mod, "find_daily_materials", materials)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    assert secret_briefing not in blob, (
        "当天已纳入过底料，第二次醒不该再把 briefing 重喂进 user 消息"
    )
    assert "【今天的外部底料】" not in blob, "已纳入当天不再拼外部底料段（不重喂）"
    # 收口也不再标记（传 None，不重标）。
    assert engine_mod._test_close_calls, "收口仍应走 record_world_round_close"
    assert engine_mod._test_close_calls[-1]["materials_ingested_date"] is None, (
        "已纳入当天收口不再标记 materials_ingested_date（传 None）"
    )


@pytest.mark.asyncio
async def test_new_day_with_new_materials_reingests(monkeypatch):
    """③ 跨到第二天有新底料（materials_ingested_date 是昨天）→ 重新纳入一次。"""
    from app.infra import cst_time

    today = cst_time.now_cst().strftime("%Y-%m-%d")
    yesterday = "2000-01-01"  # 任意 != 今天 的旧日期，模拟昨天纳入过
    _snapshot_with(monkeypatch, materials_ingested_date=yesterday)

    async def materials(*, lane, date):
        return _materials(date=date, briefing="新的一天，今天台风蓝色预警。")

    monkeypatch.setattr(engine_mod, "find_daily_materials", materials)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    assert "台风蓝色预警" in blob, "跨天有新底料应重新纳入一次"
    assert engine_mod._test_close_calls[-1]["materials_ingested_date"] == today, (
        "跨天重新纳入那轮收口应把 materials_ingested_date 标成今天（不是昨天）"
    )


@pytest.mark.asyncio
async def test_no_materials_today_does_not_feed_or_read_yesterday(monkeypatch):
    """④ 今天没底料（None）→ 不拼底料段、不标记、不读昨天（find_daily_materials 只按今天查）。"""
    _snapshot_with(monkeypatch, materials_ingested_date=None)

    calls: list[dict] = []

    async def no_materials(*, lane, date):
        calls.append({"lane": lane, "date": date})
        return None

    monkeypatch.setattr(engine_mod, "find_daily_materials", no_materials)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    # 不拼底料段（None 时不喂、不冒充）。
    assert "【今天的外部底料】" not in blob, "今天没底料时不该拼外部底料段"
    assert "晴" not in blob, "没底料时绝不能凭空出现晴天等具体天气事实（冒充）"
    # 只按今天查（绝不读昨天）。
    from app.infra import cst_time

    today = cst_time.now_cst().strftime("%Y-%m-%d")
    assert calls == [{"lane": "coe-t2", "date": today}], (
        f"find_daily_materials 只按今天查，绝不读昨天，实际 {calls}"
    )
    # 没纳入 → 收口不标记 materials_ingested_date（传 None）。
    assert engine_mod._test_close_calls[-1]["materials_ingested_date"] is None, (
        "今天没底料时收口不该标记 materials_ingested_date"
    )


@pytest.mark.asyncio
async def test_ingest_round_crash_does_not_mark_materials(monkeypatch):
    """⑤ 纳入那轮崩溃（run 抛）→ 收口不执行，materials_ingested_date 不被误标记成已纳入。

    崩溃时统一收口（record_world_round_close）根本不会被调到（run 抛在它之前），所以
    materials_ingested_date 保持上一版（未标记），下轮重醒会重新纳入（底料不丢）。
    """
    _snapshot_with(monkeypatch, materials_ingested_date=None)

    async def materials(*, lane, date):
        return _materials(briefing="今天有底料。")

    async def boom_run(self, messages, *, prompt_vars=None, context=None,
                       session_id=None, max_retries=2):
        raise RuntimeError("model boom during ingest round")

    monkeypatch.setattr(engine_mod, "find_daily_materials", materials)
    monkeypatch.setattr(engine_mod.Agent, "run", boom_run)

    with pytest.raises(RuntimeError):
        await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    # 崩溃时统一收口没被调到 → 没标记（下轮重醒重新纳入，底料不丢）。
    assert engine_mod._test_close_calls == [], (
        "纳入那轮崩溃时收口绝不该执行（materials_ingested_date 不被误标记）"
    )


def test_render_materials_section_uses_briefing():
    """渲染器：把 agent 组织好的 briefing 原样裹一句背景定性喂给 world。

    DailyMaterials 已简化成只存 briefing —— 某源是否拿到 agent 已在 briefing 里说清，
    渲染器不再做每源失败标注，只把 briefing 当公共背景渲染出来。
    """
    from app.world.engine import _materials_section

    m = _materials(briefing="今天的连贯背景简报，天气源今天没拿到。")
    section = _materials_section(m)
    assert "今天的连贯背景简报，天气源今天没拿到。" in section, (
        "主背景应原样用 agent 整理的 briefing"
    )
    assert "公共可得的背景信息" in section, "渲染器应裹一句背景定性"


# ---------------------------------------------------------------------------
# 世界阶段：每轮推演输入带最新阶段；空白给冷启动引导（不硬编任何剧情事实）
# ---------------------------------------------------------------------------


def _arc(*, lane="coe-t2", narrative, turned_at="2026-06-09T18:00:00+08:00"):
    """构造一条 WorldArc（world 读到的最新一版世界阶段）。"""
    from app.world.arc import WorldArc

    return WorldArc(lane=lane, narrative=narrative, turned_at=turned_at)


@pytest.mark.asyncio
async def test_round_feeds_latest_arc_into_messages(monkeypatch):
    """每轮推演输入带【世界阶段】段，内容是最新一版阶段 narrative。"""
    narrative = "三姐妹家已经搬进新小区，妹妹换了新学校，眼下是初夏。"

    async def fake_read_world_arc(*, lane):
        return _arc(lane=lane, narrative=narrative)

    monkeypatch.setattr(engine_mod, "read_world_arc", fake_read_world_arc)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    assert "【世界阶段】" in blob, "每轮推演输入必须带【世界阶段】段"
    assert narrative in blob, "世界阶段段落内容必须是最新一版 narrative"


@pytest.mark.asyncio
async def test_empty_arc_feeds_cold_start_guidance(monkeypatch):
    """世界阶段为 None（还没翻过页）→ 阶段段落如实说明空白；**不**引导续写去调 update_arc。

    翻页（含空白时写第一版）已归反思环节独占——续写工具集里没有 update_arc，引导它
    去调一个不存在的工具只会让循环报错。阶段空白时续写只需知道它还是空白、顺着此刻
    往前推演即可（第一版由反思写）。
    """
    captured = _mock_run(monkeypatch)  # 默认 read_world_arc 桩返回 None

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    assert "【世界阶段】" in blob, "阶段空白时【世界阶段】段也要出现（带空白说明）"
    assert "空白" in blob, "必须明示世界阶段还是空白"
    assert "update_arc" not in blob, (
        "续写输入不得引导 update_arc（翻页归反思独占，续写没有这只手）"
    )


def test_empty_arc_guidance_has_no_hardcoded_plot_facts():
    """冷启动引导文案绝不硬编任何剧情事实（高考 / 具体日期 / 角色名之类）——这是宪法。

    直接测引导渲染器 ``_arc_section(None)``（引导文案的单一来源）：世界走到哪由
    world 自己从底色和此刻读出来，代码里一个剧情字都不许写。
    """
    from app.world.engine import _arc_section

    guidance = _arc_section(None)
    assert "高考" not in guidance, "引导文案不得硬编剧情事实（高考）"
    assert not any(ch.isdigit() for ch in guidance), "引导文案不得硬编具体日期 / 数字事实"
    for name in ("千凪", "赤尾", "绫奈", "chinagi", "akao", "ayana"):
        assert name not in guidance, f"引导文案不得硬编剧情事实（角色 {name!r}）"


@pytest.mark.asyncio
async def test_arc_read_uses_current_lane(monkeypatch):
    """世界阶段按当前 lane 读（泳道隔离命门同 WorldState）。"""
    _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert engine_mod._test_arc_reads == ["coe-t2"], (
        "每轮必须 read_world_arc(lane=当前 lane)，绝不读别的泳道"
    )


# ---------------------------------------------------------------------------
# 反思环节（Task 2b）：翻页能力从续写剥离、归独立反思——当日未反思先跑反思、先于
# 续写；当日已反思不跑；续写读世界阶段必须在反思之后现读
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_reflection_when_already_reflected_today(monkeypatch):
    """普通轮（当日反思标记 == 今天）→ 不跑反思。"""
    from app.infra import cst_time

    today = cst_time.now_cst().strftime("%Y-%m-%d")
    _snapshot_with(monkeypatch, arc_reflected_date=today)
    _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert engine_mod._test_reflect_calls == [], "当日已反思过不该再跑反思"


@pytest.mark.asyncio
async def test_reflection_runs_before_deliberation_when_marker_empty(monkeypatch):
    """标记为空（冷启 / 部署后首跑）→ 跑反思，且先于续写推演。"""
    order: list[str] = []

    async def fake_reflection(**kwargs):
        order.append("reflect")

    monkeypatch.setattr(engine_mod, "run_arc_reflection", fake_reflection)
    _mock_run(monkeypatch, order=order)
    # 默认 _stub_state 的 snapshot 无 arc_reflected_date（None != 今天）。

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert order == ["reflect", "run"], (
        f"反思必须先于续写推演（先对表翻页、续写再顺流），实际 {order}"
    )


@pytest.mark.asyncio
async def test_reflection_runs_when_marker_stale(monkeypatch):
    """标记是过去某天（!= 今天）→ 跑反思（跨天重新对表）。"""
    _snapshot_with(monkeypatch, arc_reflected_date="2000-01-01")
    _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert len(engine_mod._test_reflect_calls) == 1, "标记非今日必须跑反思"


@pytest.mark.asyncio
async def test_reflection_receives_round_context(monkeypatch):
    """反思拿到与续写同等的运行契约：lane / round_id / trace session / 快照 / 今日底料。"""
    materials_obj = _materials()

    async def materials(*, lane, date):
        return materials_obj

    monkeypatch.setattr(engine_mod, "find_daily_materials", materials)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert len(engine_mod._test_reflect_calls) == 1
    call = engine_mod._test_reflect_calls[0]
    assert call["lane"] == "coe-t2"
    assert call["round_id"] == captured["context"].features["world_round_id"]
    assert call["trace_session_id"] == captured["session_id"]
    assert call["materials"] is materials_obj, "反思必须拿到引擎读出的今日底料"
    assert call["snapshot"] is not None and call["snapshot"].detail, (
        "反思必须拿到最新 WorldState 快照（detail + world_time 喂对表）"
    )
    assert call["now"], "反思必须拿到现实此刻（对表的现实锚点）"


@pytest.mark.asyncio
async def test_deliberation_reads_arc_fresh_after_reflection(monkeypatch):
    """续写读世界阶段必须在反思之后**现读**（不能用反思前缓存的值）。

    模拟「update_arc 已 durable 落库、反思 Agent 随后失败（fail-open 不抛）」：
    反思桩把世界阶段库里的最新版换成新一页，续写的输入必须读到新阶段、不是旧的。
    """
    current = {"narrative": "旧的世界阶段：这一页还没翻。"}

    async def fake_read_world_arc(*, lane):
        return _arc(lane=lane, narrative=current["narrative"])

    async def fake_reflection(**kwargs):
        # update_arc 已落库（库里最新版变了）；随后反思 Agent 失败也不抛（fail-open）。
        current["narrative"] = "新的世界阶段：页已经翻过去了。"

    monkeypatch.setattr(engine_mod, "read_world_arc", fake_read_world_arc)
    monkeypatch.setattr(engine_mod, "run_arc_reflection", fake_reflection)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    assert "新的世界阶段：页已经翻过去了。" in blob, (
        "续写必须读到反思（update_arc）落库后的新世界阶段——世界阶段要在反思之后现读"
    )
    assert "旧的世界阶段：这一页还没翻。" not in blob, "续写不能用反思前缓存的旧世界阶段"


@pytest.mark.asyncio
async def test_gated_wake_does_not_run_reflection(monkeypatch):
    """被到点 gate 判废的唤醒（早返）→ 不跑反思（反思挂在 gate 之后的真实推演轮里）。"""
    from datetime import timedelta

    from app.world.engine import _CST

    future = (datetime.now(_CST) + timedelta(seconds=300)).isoformat()
    _snapshot_with(monkeypatch, next_wake_at=future)
    _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert engine_mod._test_reflect_calls == [], "gate 判废的唤醒不该跑反思"


@pytest.mark.asyncio
async def test_turn_idempotent_skip_does_not_run_reflection(monkeypatch):
    """turn 幂等命中（崩溃重读同一轮）→ 跳过整轮，也不跑反思（避免重读风暴重复反思）。"""
    from app.agent.session import append_session
    from app.agent.trace import make_session_id
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

    round_id = engine_mod._derive_round_id(
        "coe-t2",
        cursor_created_at="2026-06-04T08:00:00+08:00",
        cursor_act_id="c0",
        has_acts=True,
        now_iso="ignored",
    )
    session_id = make_session_id("coe-t2", "world", datetime.now().strftime("%Y-%m-%d"))
    marker = _round_marker(
        round_id, end_created_at="2026-06-04T08:05:00+08:00", end_act_id="a5"
    )
    await append_session(session_id, [Message(role=Role.USER, content=f"{marker}\n上一轮")])
    _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert engine_mod._test_reflect_calls == [], (
        "turn 幂等跳过的轮不该跑反思（同日反思由下一个真实推演轮重试）"
    )


# ---------------------------------------------------------------------------
# 反思双触发（眼睛闭环）：world 24×7，每天 00:0X 首轮就触发第一班反思——那时眼睛
# 还没跑、当天底料不存在，单一标记会让「当天 briefing 永远不被当天反思消化」。
# 改成两班：第一班照旧（arc_reflected_date != 今天，无底料也凭常识对表）；第二班
# 当日底料存在且 arc_materials_reflected_date != 今天时再跑一次。一次带底料的成功
# 反思同时落两个标记（覆盖两班职责），所以一天最多两次、绝不冗余第三次。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_materials_arrival_triggers_second_reflection_shift(monkeypatch):
    """第二班：第一班已跑过（arc_reflected_date == 今天）、当日底料落地且第二班
    标记不是今天 → 再跑一次反思（把当天 briefing 消化进对表）。"""
    from app.infra import cst_time

    today = cst_time.now_cst().strftime("%Y-%m-%d")
    _snapshot_with(
        monkeypatch,
        arc_reflected_date=today,
        arc_materials_reflected_date=None,
        materials_ingested_date=today,  # 续写已纳入过，与反思的第二班标记无关
    )

    materials_obj = _materials()

    async def materials(*, lane, date):
        return materials_obj

    monkeypatch.setattr(engine_mod, "find_daily_materials", materials)
    _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert len(engine_mod._test_reflect_calls) == 1, (
        "当日底料落地后必须补一班反思（否则当天 briefing 永远不被当天反思消化）"
    )
    assert engine_mod._test_reflect_calls[0]["materials"] is materials_obj, (
        "补班反思必须拿到当天底料"
    )


@pytest.mark.asyncio
async def test_no_second_shift_when_materials_already_reflected_today(monkeypatch):
    """两班都跑过（两个标记都是今天）→ 即便底料还在，也不再跑反思（各班同日至多一次）。"""
    from app.infra import cst_time

    today = cst_time.now_cst().strftime("%Y-%m-%d")
    _snapshot_with(
        monkeypatch,
        arc_reflected_date=today,
        arc_materials_reflected_date=today,
        materials_ingested_date=today,
    )

    async def materials(*, lane, date):
        return _materials()

    monkeypatch.setattr(engine_mod, "find_daily_materials", materials)
    _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert engine_mod._test_reflect_calls == [], (
        "两班标记都是今天时不得再跑反思（一天最多两次）"
    )


@pytest.mark.asyncio
async def test_first_shift_with_materials_runs_single_reflection(monkeypatch):
    """带底料的首班（如午后部署首跑：两标记都空、底料已在）→ 只跑一次反思。

    这一次反思已带底料、覆盖两班职责（run_arc_reflection 成功会同落两个标记），
    engine 在同一轮里绝不能因为两个触发条件同时满足就跑两次。
    """
    _snapshot_with(
        monkeypatch,
        arc_reflected_date=None,
        arc_materials_reflected_date=None,
    )

    async def materials(*, lane, date):
        return _materials()

    monkeypatch.setattr(engine_mod, "find_daily_materials", materials)
    _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert len(engine_mod._test_reflect_calls) == 1, (
        "两班条件同时满足时也只跑一次反思（一次带底料反思覆盖两班职责）"
    )


@pytest.mark.asyncio
async def test_no_second_shift_without_materials(monkeypatch):
    """第一班已跑、今天没底料 → 不跑第二班（第二班只为消化底料而存在）。

    这也是既有语义的回归钉子：单标记时代「arc_reflected_date == 今天就不再反思」
    在没底料的日子必须原样成立。
    """
    from app.infra import cst_time

    today = cst_time.now_cst().strftime("%Y-%m-%d")
    _snapshot_with(
        monkeypatch, arc_reflected_date=today, arc_materials_reflected_date=None
    )
    _mock_run(monkeypatch)  # 默认 find_daily_materials 桩返回 None

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert engine_mod._test_reflect_calls == [], (
        "今天没底料时第二班不触发（无底料班只有每日首轮那一班）"
    )


# ---------------------------------------------------------------------------
# 冷启动反思标记的整条链（真实 PG）+ 占位快照不影响 gate / 冷启动分支
#
# 冷启动时反思先于续写跑：mark_arc_reflected 落标时还没有任何 WorldState 行。
# 若它 no-op，标记丢失、同日每轮重跑反思。修法是插一行最小占位快照承载标记；
# 占位行（detail 空白）不得改变 gate 行为，也不得被当成「已有世界叙述」。
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cold_start_reflection_mark_chain_no_same_day_rerun(test_db, monkeypatch):
    """整条链（真实 PG）：冷启动 → 反思成功落标 → 续写 update_world 写真实首版
    detail 且标记被保留 → 同日下一轮不再反思。

    反思桩用真实 mark_arc_reflected（被测主角），续写桩用真实 write_world_state
    （保留链是被测的另一半）。若冷启动落标 no-op，第一轮后标记为 None、第二轮
    会重跑反思——断言 reflect 只被调一次钉死「成功后同日不再重复跑」。
    """
    from app.world import state as state_mod
    from app.world.state import WorldState
    from tests.runtime.conftest import migrate

    await migrate(WorldState, test_db)

    # 引擎接回真实 state 函数（autouse _stub_state 替掉了它们）。
    monkeypatch.setattr(engine_mod, "read_world_state", state_mod.read_world_state)
    monkeypatch.setattr(engine_mod, "advance_act_cursor", state_mod.advance_act_cursor)
    monkeypatch.setattr(
        engine_mod, "record_world_round_close", state_mod.record_world_round_close
    )

    # 反思桩：模拟反思成功——真实落当日标记（冷启动时这就是被测链路的第一环）。
    reflect_calls: list[str] = []

    async def reflecting(**kwargs):
        reflect_calls.append(kwargs["lane"])
        await state_mod.mark_arc_reflected(
            lane=kwargs["lane"], date=kwargs["now"].strftime("%Y-%m-%d")
        )

    monkeypatch.setattr(engine_mod, "run_arc_reflection", reflecting)

    # 续写桩：模拟循环里 update_world 写真实首版叙述（真实落库、走保留链）。
    async def fake_run(
        self, messages, *, prompt_vars=None, context=None, session_id=None, max_retries=2
    ):
        await state_mod.write_world_state(
            lane="coe-t2",
            world_time="2026-06-10T08:35:00+08:00",
            detail="清晨，世界的第一版叙述。",
        )
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(engine_mod.Agent, "run", fake_run)

    # 第一轮（冷启动）：反思跑了、标记落了、续写写下首版叙述、标记被保留链带上。
    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))
    assert reflect_calls == ["coe-t2"], "冷启动（标记 None != 今天）必须跑反思"
    snap = await state_mod.read_world_state(lane="coe-t2")
    assert snap is not None
    assert snap.detail == "清晨，世界的第一版叙述。"
    today = datetime.now(engine_mod._CST).strftime("%Y-%m-%d")
    assert snap.arc_reflected_date == today, (
        "冷启动反思成功落的标记必须挺过续写写首版叙述（write_world_state 保留链）"
    )

    # 同日第二轮：标记 == 今天 → 不再反思（每日一次真生效）。
    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))
    assert reflect_calls == ["coe-t2"], "同日第二轮不得重跑反思（标记已是今天）"


@pytest.mark.asyncio
async def test_placeholder_snapshot_keeps_cold_start_branch(monkeypatch):
    """占位快照（detail 空白、仅承载反思标记）仍走冷启动分支。

    冷启动反思成功落标后、续写若在写首版叙述前崩溃，下一轮读到的就是这行占位
    快照——它不能被当成「已有世界叙述」喂给模型一段空叙述：prompt 仍要告知首次
    醒来、不标注「这段叙述写于」；且占位行带的当日标记必须挡住重复反思。
    """
    from app.infra import cst_time

    today = cst_time.now_cst().strftime("%Y-%m-%d")
    _snapshot_with(monkeypatch, detail="", world_time="", arc_reflected_date=today)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    assert ("冷启动" in blob) or ("首次" in blob), (
        "占位快照（还没有真实世界叙述）仍是冷启动，prompt 必须告知模型"
    )
    assert "这段叙述写于" not in blob, "占位行无真实写入时刻，不得标注「这段叙述写于」"
    assert engine_mod._test_reflect_calls == [], (
        "占位行带的当日标记必须挡住重复反思（这正是占位行存在的意义）"
    )


@pytest.mark.asyncio
async def test_placeholder_snapshot_gate_behaves_like_cold_start(monkeypatch):
    """占位快照 next_wake_at=None 的 gate 行为与真冷启一致：心跳放行、self 判废。

    心跳放行由上面 test_placeholder_snapshot_keeps_cold_start_branch 证（它真跑了
    一轮）；这里钉 self：占位行从没排过下次醒，self 唤醒没有合法目标可比对、判废。
    """
    _snapshot_with(monkeypatch, detail="", world_time="")
    captured = _mock_run(monkeypatch)

    await world_tick(
        WorldTick(
            lane="coe-t2", reason="self", target_wake_at="2026-06-10T09:00:00+08:00"
        )
    )

    assert "messages" not in captured, (
        "占位快照（next_wake_at=None）下 self 唤醒必须被 gate 判废（与真冷启一致）"
    )


@pytest.mark.asyncio
async def test_world_instruction_does_not_enumerate_update_arc():
    """续写指令**不再**枚举 update_arc——翻页归反思独占（工具集物理隔离的 prompt 面）。

    续写工具是六个（notify / update_world / update_outline / sense / npc_visit / sleep）。
    世界阶段仍是续写的输入（【世界阶段】段保留），但续写无手碰世界阶段：指令里不能再有
    update_arc 的使用指令，否则模型会去调一个不存在的工具（区别于 task2 加进续写工具集、
    续写自己维护的 update_outline——后者**必须**枚举）。
    """
    instruction = engine_mod.world_loop_instruction()
    assert "update_arc" not in instruction, (
        "续写指令不得枚举 update_arc（翻页已归反思环节独占）"
    )
    assert "五个工具" not in instruction, "续写工具已是六个（含 update_outline），不能再写「五个工具」"
    assert "六个工具" in instruction, (
        "工具清单应明确是六个（update_arc 已移出、含 npc_visit / update_outline）"
    )


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


# ---------------------------------------------------------------------------
# 1C Task 3：world 低成本感知链路（双轨）—— world 凭元信息反映氛围、绝不读对话原话
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_world_sees_conversation_meta_not_original_speech(monkeypatch):
    """② world 低成本感知链路（承重红线）：world 拿到的是对话元信息、绝不含逐句原话。

    chat 走双轨：原话直投收件人信箱（kind=speech、不经 world），同时给 world 一条
    **不含原话**的低成本元信息（复用 act 流）。这条元信息以 ActPerformed 落库、world
    醒来 pull 它，让 world 在客观叙事里反映「有人在交谈」（隔壁第三人感知到「厨房有
    人在交谈」）。命门：world 醒来读到的批次里只有元信息事实、绝无对话逐句原话。

    本测模拟 world pull 到一条"对话发生"的元信息 act（description 是元信息、不带原话）
    + 断言：① world 的输入里**没有**对话逐句原话（红线钉死）；② world 输入里**有**
    「有人在交谈」这类元信息（让它能反映氛围）。
    """
    secret_line = "绫奈姐姐你在做什么好吃的呀这句是绝密原话"

    async def fake_list_recent_acts(*, lane, cursor_created_at, cursor_act_id, limit):
        return [
            # chat 给 world 的低成本元信息：只有"和谁交谈"的事实，绝无逐句原话。
            _act(lane, "c1", "akao", "我和 ayana 说了几句话",
                 "2026-06-04T12:30:00+08:00", "2026-06-04T12:30:00+08:00"),
        ]

    monkeypatch.setattr(engine_mod, "list_recent_acts", fake_list_recent_acts)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    # 承重红线：world 的输入里绝不出现对话逐句原话。
    assert secret_line not in blob, (
        "world 绝不读对话原话——pull 到的批次 / 喂给 world 的 context 里不能有逐句原话"
    )
    # world 凭元信息知道"有人在交谈"（提到交谈对象），能据此反映氛围。
    assert "交谈" in blob or "说了几句" in blob, (
        "world 应从元信息知道有一场对话在发生（反映氛围），即便读不到原话"
    )


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
    # 五工具范式：提到她能用工具行动
    assert ("notify" in instruction) or ("update_world" in instruction)
    assert ("sleep" in instruction) or ("再看" in instruction)


@pytest.mark.asyncio
async def test_world_instruction_enumerates_sense_tool():
    """必改：world_loop_instruction 必须把 1C 新增的 sense（五官）枚举进工具清单。

    Task2 给 world 加了 sense 工具（per-person 投周遭客观切片）。但循环指令若还写
    「三个工具」、只枚举 update_world / notify / sleep，真实模型就不知道有 sense、
    根本不会调 —— Task2 的「world 当五官投周遭」形同虚设。所以指令必须：

      * 明确提到 sense 工具（按名字枚举）；
      * 工具数从「三个」改成「四个」（不能再写「三个工具」）；
      * 引导 world 为单个 recipient 逐角色推演「此刻她在哪、谁在身边、环境怎样」的
        客观周遭切片并 sense 给她，且守信息差（每人只拿到她够得着的那份）。
    """
    instruction = engine_mod.world_loop_instruction()
    assert "sense" in instruction, "循环指令必须枚举 sense 工具（否则模型不会调它）"
    assert "三个工具" not in instruction, "工具早已不止三个，指令不能再写「三个工具」"
    assert "六个工具" in instruction, (
        "工具清单应明确是六个（含 sense / npc_visit / update_outline；update_arc 已归反思环节）"
    )
    # sense 是逐角色（per-person）投周遭客观切片：引导逐角色 + 信息差
    assert ("逐角色" in instruction) or ("每个角色" in instruction) or ("单个" in instruction), (
        "sense 引导应说明是逐角色（per-person）投周遭切片"
    )
    assert ("信息差" in instruction) or ("够得着" in instruction), (
        "sense 引导应守信息差（每人只拿到她够得着的那份）"
    )
    # 周遭切片的口径：此刻她在哪 / 谁在身边 / 环境怎样
    assert ("周遭" in instruction) or ("身边" in instruction), (
        "sense 引导应说明投的是她此刻的周遭客观切片"
    )


@pytest.mark.asyncio
async def test_world_instruction_guides_idle_sense_wake_judgment():
    """life-idle-wake-via-sense Task 1：world_loop_instruction 必须补三处引导缺口——

      ① sense 新增的 ``idle`` 参数说明，用刚起床 / 睡前 / 饭后窝着这类具体场景举例
         （不是抽象的「闲」概念）；
      ② 场景持续静止不变的时候也该规律性地评估要不要再投一次 sense（原指令只在
         「角色刚醒来 / 周遭变了」这类反应式场景引导调用 sense，静止期从没被引导过，
         这正是本次要覆盖的最典型场景——她已经在沙发上待了一阵、什么都没变）；
      ③ 判断准则是对她上次已知状态的时间外推（跟 update_world 推演用的是同一种
         功夫），不能等她先产生一个新的 life act 来"证明"她闲——这是结构性避免
         重现旧自锁（life 不动 → world 判无需唤醒 → life 更不会动）的关键。
    """
    instruction = engine_mod.world_loop_instruction()

    assert "idle" in instruction, "sense 段必须枚举新增的 idle 参数"
    for example in ("刚起床", "睡前", "饭后"):
        assert example in instruction, f"idle 判断引导应举例具体的闲场景 {example!r}"

    assert ("静" in instruction) and (
        "顺手看一眼" in instruction or "规律性" in instruction
    ), "指令必须补上「场景静止不变时也该规律性评估要不要 sense」的引导"

    assert ("时间" in instruction) and ("外推" in instruction), (
        "指令必须给出时间外推的判断准则（对她上次已知状态 + 流逝的真实时间推断）"
    )
    assert ("新动作" in instruction) or ("先做点什么" in instruction), (
        "指令必须明确闲判断不能靠等她先产生一个新动作来证明，否则会重现旧自锁"
    )

    # 红线复核：不能带回 Task 1 收口时删掉的旧「判唤醒」措辞（那是自锁根因的表述，
    # 这次新增的是完全不同的机制——per-recipient 时间外推，不是扫描三姐妹状态停滞）。
    for wake_phrase in ("状态停滞", "太久没更新", "该不该叫醒", "明显该醒"):
        assert wake_phrase not in instruction, (
            f"不该带回旧「判唤醒」措辞 {wake_phrase!r}（新机制是时间外推，不是这套）"
        )


@pytest.mark.asyncio
async def test_world_instruction_idle_excludes_sleep_and_forbids_repeat():
    """T3 code review 必改 1（唤醒风暴风险，复现历史事故）：world 大约每 30 分钟推
    一轮，若只靠"天然闲时刻"这一条标准，某个角色持续处于同一个静止场景（比如她一直
    窝在沙发上没变化、甚至正在睡觉）会导致 world 每轮都重新判"这仍是天然闲时刻"、
    每轮都传 idle=True——这正是当年"world 每轮 sense 把自排睡着的姐妹敲醒、睡不满"
    那次事故的根因。指令必须补两条边界：① 正在安睡不算天然的闲；② 同一个没怎么变
    的静止场景不逐轮机械重复判 True。禁止用数字阈值/计数器堵这个口子，只能是
    prompt 层的引导让 world 自己判断、自己记得。
    """
    instruction = engine_mod.world_loop_instruction()

    # ① 睡眠期间不触发：明确说安睡不算天然的闲。
    assert "安睡不算天然的闲" in instruction, (
        "指令必须明确「正在安睡不算天然的闲」，否则 idle 判断会在她该睡满时把她吵醒"
    )
    assert "notify" in instruction.split("安睡不算天然的闲")[-1].split("\n")[0] or (
        "notify" in instruction
    ), "真有必须惊动睡着的人的理由，应指向 notify 而不是 idle"

    # ② 同一静止场景不逐轮重复触发。
    assert "不逐轮" in instruction and "机械重复判" in instruction, (
        "指令必须明确「同一个没怎么变的静止场景不逐轮机械重复判 idle=True」"
    )
    assert "上一轮" in instruction, (
        "指令必须引导 world 记得自己上一轮是否已经因为同一场景判过 idle=True"
    )

    # 历史事故复核：必须点名"每 30 分钟"和事故根因，让这条引导有具体的失败场景锚定
    # （不是抽象的"别重复"）。
    assert "30 分钟" in instruction
    assert "被动通道" in instruction or "自排睡着的姐妹" in instruction, (
        "指令应锚定历史事故（sense 整体改被动通道的根因），让 world 理解为什么这条"
        "边界不能踩"
    )

    # 代码侧不引入任何数字阈值/计数器：判断权仍完全在 idle 判断的语义引导里，不是
    # 靠一个「超过 N 次 / N 分钟」的机械规则堵口子。
    for forbidden in ("超过 3 次", "超过 2 次", "冷却 30 分钟", "最多 1 次"):
        assert forbidden not in instruction, (
            f"不该引入数字阈值/计数器 {forbidden!r}——闲不闲、该不该重复由 world 自己判断"
        )


@pytest.mark.asyncio
async def test_world_instruction_idle_is_sole_exception_to_no_wake_judgment_rule():
    """T3 code review 必改 3：新加的 idle 判断本质是一种"该不该叫醒"的判断，跟既有的
    "你不替任何角色判断该不该醒"护栏字面冲突——模型读到前后矛盾的指令会不知道该信
    哪句。指令必须明确承认 idle 判断是这条规则唯一的例外，而不是让读的人自己去猜。
    """
    instruction = engine_mod.world_loop_instruction()

    assert "你不替任何角色判断该不该醒" in instruction, "既有护栏措辞应保留（没有被误删）"
    assert "这条规则只有一个例外" in instruction, (
        "必须明确承认 sense 的 idle 判断是「不替任何角色判断该不该醒」这条规则的例外，"
        "否则前后矛盾"
    )
    # 例外的落点紧跟在护栏措辞之后，读的人不用去猜。
    guard_idx = instruction.index("你不替任何角色判断该不该醒")
    exception_idx = instruction.index("这条规则只有一个例外")
    assert 0 <= exception_idx - guard_idx < 200, (
        "例外说明应紧跟在「不替任何角色判断该不该醒」护栏之后，不能离得太远让人读不到"
    )


@pytest.mark.asyncio
async def test_world_instruction_enumerates_npc_visit_tool():
    """必改：world_loop_instruction 必须把 NPC 层的 npc_visit 枚举进工具清单。

    npc_visit 让 world 以具名 NPC 身份投一件指向某姐妹的 event（同学约她、同事找她
    那种）。但循环指令若不枚举它，真实模型就不知道有这个工具、根本不会调 —— NPC 层
    「world 推具名 NPC 来撞姐妹」形同虚设。所以指令必须：

      * 明确枚举 npc_visit 工具（按名字）+ 它的两个内容参数 what_npc_says / world_fact；
      * 钉死 world_fact 是客观可感那一面、**绝不写情绪 / 绝不写 what_npc_says 的私密
        原话**（与 sense / notify 的客观投影口吻一致）；
      * 守「引导而非强制」的口吻：谁来 / 来不来 / 来干嘛由 world 按世界此刻自然推演、
        不定时不机械，安静时不硬造 NPC 来访；
      * 提醒 npc_visit 已把 world_fact 写进世界叙述，随后 update_world 要延续别覆盖丢；
      * 说明名册之外的临时路人也可以用它来一下。
    """
    instruction = engine_mod.world_loop_instruction()
    assert "npc_visit" in instruction, "循环指令必须枚举 npc_visit 工具（否则模型不会调它）"
    assert "what_npc_says" in instruction, "npc_visit 段应枚举 what_npc_says 参数"
    assert "world_fact" in instruction, "npc_visit 段应枚举 world_fact 参数"
    # world_fact 客观、绝不写情绪、绝不写私密原话
    assert ("情绪" in instruction), "world_fact 守则应钉死绝不写情绪"
    # 引导而非强制：自然推演、不定时不机械、安静别硬造
    assert ("自然" in instruction) or ("推演" in instruction), (
        "npc_visit 守则应是「由 world 自然推演」的引导口吻"
    )
    assert ("硬造" in instruction), "npc_visit 守则应说安静时别硬造 NPC 来访"
    # 路人也能用它来一下（名册只是固定的几个人）
    assert ("路人" in instruction), "npc_visit 段应说明名册之外的临时路人也可以用它"


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

    npc_visit 段同理：它讲「让一个固定 NPC 来找某个姐妹」这个工具的用法，``sister``
    参数指向哪个姐妹用 persona_id，但**具体哪个 id 对应谁由 system prompt 一处承载**
    —— 续写指令里不得硬编 akao / chinagi / ayana 这些 id 或三姐妹中文名（否则又成
    两处真相）。
    """
    from app.world.engine import _sisters_section, _world_loop_messages

    messages = _world_loop_messages(
        detail="清晨厨房有了动静。",
        detail_written_at=None,  # 必传：None 是"冷启动无上一版"的显式语义
        now_iso="2026-06-05T09:00:00+08:00",
        wake_reason="例行看一眼世界。",
        round_id="r1",
        arc_narrative=None,  # 必传：None 是"还没翻过页"的显式语义，不允许靠默认值
        outline_narrative=None,  # 必传：None 是"还没有大纲"的显式语义，不允许靠默认值
        sisters_text=_sisters_section([]),  # 必传；空名单不引入任何角色坐标
    )
    blob = "".join(m.text() for m in messages)
    assert "作息节律" not in blob, "USER 层不该再拼作息节律段（世界设定归 system 一处）"
    for setting in ("千凪", "赤尾", "绫奈", "chinagi", "akao", "ayana"):
        assert setting not in blob, (
            f"USER 层不该出现世界设定里的客观坐标 {setting!r}（归 system 一处）"
        )


# ---------------------------------------------------------------------------
# spec 决策 5b：续写输入的「上一版叙述」段带写入时刻标注——让续写知道手里这帧画面
# 是什么时候画下的，对着现实此刻一步跨过去而不是逐分钟回放
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prev_detail_section_carries_written_at(monkeypatch):
    """「上一版叙述」段带写入时刻（快照的 world_time）标注。"""
    _snapshot_with(
        monkeypatch,
        detail="深夜，屋里只剩冰箱的低鸣。",
        world_time="2026-06-09T23:40:00+08:00",
    )
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    assert "写于" in blob, "上一版叙述段必须带「写于 X」的写入时刻标注（spec 决策 5b）"
    assert "2026-06-09T23:40:00+08:00" in blob, (
        "写入时刻必须是快照的 world_time（这帧画面画下的时刻）"
    )


@pytest.mark.asyncio
async def test_cold_start_prev_detail_has_no_written_at(monkeypatch):
    """冷启动（无上一版叙述）→ 不标「写于」（占位文本没有写入时刻可标）。"""

    async def no_world_state(*, lane):
        return None

    monkeypatch.setattr(engine_mod, "read_world_state", no_world_state)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    assert "写于" not in blob, "冷启动占位文本不该带「写于」标注（没有真实写入时刻）"


@pytest.mark.asyncio
async def test_world_instruction_has_no_world_setting():
    """world_loop_instruction 是 USER 层行动指令，不该描述世界设定 / 谁是谁 / 作息。

    世界长什么样 / 家庭布局 / 三姐妹是谁 / 几点干嘛这类设定性内容归 system prompt
    一处；USER 层指令只讲四个工具（notify/update_world/sense/sleep）的说明 + 本轮
    怎么做（update_arc 已归反思环节、不在续写指令里）。
    """
    instruction = engine_mod.world_loop_instruction()
    # 不该出现三姐妹的名字 / 年龄 / 作息坐标这类世界设定内容
    for setting in ("千凪", "赤尾", "绫奈", "chinagi", "akao", "ayana"):
        assert setting not in instruction, (
            f"USER 层指令不该描述世界设定（谁是谁）：{setting!r}"
        )
    assert "作息" not in instruction, "USER 层指令不该描述作息（归 system 一处）"
    # 仍是五工具行动指令（含 1C 新增的 sense 五官 + NPC 层的 npc_visit；update_arc 归反思）
    assert ("notify" in instruction) and ("update_world" in instruction)
    assert "update_arc" not in instruction
    assert "sense" in instruction
    assert "npc_visit" in instruction
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


# ---------------------------------------------------------------------------
# NPC 名册（NPC 层第一刀）：world 当天第一次醒把「世界的固定人物」名册纳入一次、
# 进意识流，之后当天不再重喂（照 DailyMaterials 套路，但用独立的 roster 游标）。
#
# 今天名册非空 **且** WorldState.roster_ingested_date != 今天 → 纳入一次（拼进这轮
# user 消息，按 relates_to 归到对应姐妹），收口标记 roster_ingested_date=今天；当天
# 后续轮次（已纳入）不再重喂；名册为空（还没 seed）→ 不拼这段、不标记；纳入那轮崩溃
# → 不误标记。
# ---------------------------------------------------------------------------


def _npc(*, lane="coe-t2", npc_name, relates_to, sketch, version=1):
    """构造一条 NPCRoster（world list 出来的名册一行）。"""
    from app.world.npc_roster import NPCRoster

    return NPCRoster(
        lane=lane,
        npc_name=npc_name,
        relates_to=relates_to,
        sketch=sketch,
        version=version,
    )


def _seed_roster(lane="coe-t2"):
    """构造一份小名册（覆盖三姐妹各至少一个，验证按 relates_to 归类）。"""
    return [
        _npc(lane=lane, npc_name="林小满", relates_to="ayana", sketch="同桌兼死党。"),
        _npc(lane=lane, npc_name="陈鹿", relates_to="akao", sketch="高中闺蜜。"),
        _npc(lane=lane, npc_name="许念", relates_to="chinagi", sketch="同部门同事。"),
    ]


@pytest.mark.asyncio
async def test_first_wake_today_with_roster_ingests_and_marks(monkeypatch):
    """① 当天第一次醒、名册非空、未纳入 → 名册速写进 user 消息，且收口标记 roster_ingested_date=今天。"""
    from app.infra import cst_time

    today = cst_time.now_cst().strftime("%Y-%m-%d")
    _snapshot_with(monkeypatch, roster_ingested_date=None)

    async def roster(*, lane):
        return _seed_roster(lane)

    monkeypatch.setattr(engine_mod, "list_npc_roster", roster)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    assert "同桌兼死党。" in blob, "当天首醒名册非空应把速写纳入 world 的 USER 消息"
    assert "高中闺蜜。" in blob
    assert "同部门同事。" in blob

    # 收口把 roster_ingested_date 标成今天（并进统一收口的同一次 append）。
    assert engine_mod._test_close_calls, "纳入那轮收口必须走 record_world_round_close"
    last = engine_mod._test_close_calls[-1]
    assert last["roster_ingested_date"] == today, (
        f"纳入那轮收口必须把 roster_ingested_date 标成今天 {today}，实际 {last}"
    )


@pytest.mark.asyncio
async def test_roster_section_groups_by_relates_to(monkeypatch):
    """名册段按 relates_to 归到对应姐妹（同一姐妹的 NPC 归在她名下）。"""
    _snapshot_with(monkeypatch, roster_ingested_date=None)

    async def roster(*, lane):
        return [
            _npc(lane=lane, npc_name="林小满", relates_to="ayana", sketch="绫奈的同桌。"),
            _npc(lane=lane, npc_name="顾舟", relates_to="ayana", sketch="绫奈的班长。"),
            _npc(lane=lane, npc_name="许念", relates_to="chinagi", sketch="千凪的同事。"),
        ]

    monkeypatch.setattr(engine_mod, "list_npc_roster", roster)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    # 名字 + 速写都出现（归类是 by relates_to，呈现里至少要能看到这些人）。
    for name in ("林小满", "顾舟", "许念"):
        assert name in blob, f"名册段必须含 NPC 名字 {name}"
    # 归类锚点：三姐妹的名字/标识出现在名册段（按 relates_to 分组的小标题）。
    assert ("绫奈" in blob) or ("ayana" in blob), "名册段应按所属姐妹归类（绫奈/ayana）"
    assert ("千凪" in blob) or ("chinagi" in blob), "名册段应按所属姐妹归类（千凪/chinagi）"


@pytest.mark.asyncio
async def test_first_wake_queries_roster_for_current_lane(monkeypatch):
    """未纳入时按当前 lane list 名册（泳道隔离）。"""
    _snapshot_with(monkeypatch, roster_ingested_date=None)

    calls: list[str] = []

    async def roster(*, lane):
        calls.append(lane)
        return _seed_roster(lane)

    monkeypatch.setattr(engine_mod, "list_npc_roster", roster)
    _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert calls == ["coe-t2"], f"未纳入时必须按当前 lane list 名册，实际 {calls}"


@pytest.mark.asyncio
async def test_second_wake_same_day_does_not_refeed_roster(monkeypatch):
    """② 同一天第二次醒（已纳入）→ 名册不再进 user 消息（不重喂）。"""
    from app.infra import cst_time

    today = cst_time.now_cst().strftime("%Y-%m-%d")
    _snapshot_with(monkeypatch, roster_ingested_date=today)

    secret_sketch = "这串只在名册速写里出现的死党标记"

    async def roster(*, lane):
        return [
            _npc(lane=lane, npc_name="林小满", relates_to="ayana", sketch=secret_sketch),
        ]

    monkeypatch.setattr(engine_mod, "list_npc_roster", roster)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    assert secret_sketch not in blob, "当天已纳入过名册，第二次醒不该再把速写重喂"
    # 不能用「【世界的固定人物】」这个段标题判定——它也出现在 npc_visit 工具指令里
    # （讲「名册里的那些人」）。改判**纳入段的引导句**（_roster_section 渲染的那句、
    # 只在喂入的名册段里出现、不在指令里）：没拼名册段它就不在 blob。
    assert "下面是这个世界里有名有姓的固定人物" not in blob, (
        "已纳入当天不再拼名册段（不重喂）"
    )
    # 收口不再标记（传 None，不重标）。
    assert engine_mod._test_close_calls, "收口仍应走 record_world_round_close"
    assert engine_mod._test_close_calls[-1]["roster_ingested_date"] is None, (
        "已纳入当天收口不再标记 roster_ingested_date（传 None）"
    )


@pytest.mark.asyncio
async def test_new_day_reingests_roster(monkeypatch):
    """③ 跨到第二天（roster_ingested_date 是昨天）→ 重新纳入一次。"""
    from app.infra import cst_time

    today = cst_time.now_cst().strftime("%Y-%m-%d")
    _snapshot_with(monkeypatch, roster_ingested_date="2000-01-01")

    async def roster(*, lane):
        return _seed_roster(lane)

    monkeypatch.setattr(engine_mod, "list_npc_roster", roster)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    assert "同桌兼死党。" in blob, "跨天应重新纳入名册一次"
    assert engine_mod._test_close_calls[-1]["roster_ingested_date"] == today, (
        "跨天重新纳入那轮收口应把 roster_ingested_date 标成今天（不是昨天）"
    )


@pytest.mark.asyncio
async def test_empty_roster_does_not_feed_or_mark(monkeypatch):
    """④ 名册为空（还没 seed）→ 不拼名册段、不标记。"""
    _snapshot_with(monkeypatch, roster_ingested_date=None)

    async def empty_roster(*, lane):
        return []

    monkeypatch.setattr(engine_mod, "list_npc_roster", empty_roster)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    # 同上：判纳入段的引导句（只在喂入的名册段里、不在 npc_visit 指令里），不判段标题。
    assert "下面是这个世界里有名有姓的固定人物" not in blob, "名册为空时不该拼名册段"
    assert engine_mod._test_close_calls[-1]["roster_ingested_date"] is None, (
        "名册为空时收口不该标记 roster_ingested_date"
    )


@pytest.mark.asyncio
async def test_ingest_round_crash_does_not_mark_roster(monkeypatch):
    """⑤ 纳入那轮崩溃（run 抛）→ 收口不执行，roster_ingested_date 不被误标记成已纳入。"""
    _snapshot_with(monkeypatch, roster_ingested_date=None)

    async def roster(*, lane):
        return _seed_roster(lane)

    async def boom_run(self, messages, *, prompt_vars=None, context=None,
                       session_id=None, max_retries=2):
        raise RuntimeError("model boom during roster ingest round")

    monkeypatch.setattr(engine_mod, "list_npc_roster", roster)
    monkeypatch.setattr(engine_mod.Agent, "run", boom_run)

    with pytest.raises(RuntimeError):
        await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert engine_mod._test_close_calls == [], (
        "纳入那轮崩溃时收口绝不该执行（roster_ingested_date 不被误标记）"
    )


@pytest.mark.asyncio
async def test_roster_independent_of_materials_ingest(monkeypatch):
    """名册与底料独立：今天没底料但名册非空 → 仍纳入名册（不被「无底料」连累）。"""
    from app.infra import cst_time

    today = cst_time.now_cst().strftime("%Y-%m-%d")
    _snapshot_with(monkeypatch, roster_ingested_date=None, materials_ingested_date=None)

    async def roster(*, lane):
        return _seed_roster(lane)

    async def no_materials(*, lane, date):
        return None

    monkeypatch.setattr(engine_mod, "list_npc_roster", roster)
    monkeypatch.setattr(engine_mod, "find_daily_materials", no_materials)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    assert "同桌兼死党。" in blob, "今天没底料不该影响名册纳入（两件独立的事）"
    assert "【今天的外部底料】" not in blob, "今天没底料不拼底料段"
    last = engine_mod._test_close_calls[-1]
    assert last["roster_ingested_date"] == today, "名册纳入照常标记"
    assert last["materials_ingested_date"] is None, "没底料不标记底料"


@pytest.mark.asyncio
async def test_first_wake_today_seeds_roster_before_listing(monkeypatch):
    """必改 1：当天第一次醒，纳入名册**之前**先 ensure seed 一次（生产自动入口）。

    seed_npc_roster 原本只在测试里被手动调用、没有任何生产入口——表会一直空、首醒
    list 永远得空名册、NPC 永不出场。这里给它接的自动入口是「world 当天第一次醒、
    list 名册之前 ensure seed」（CAS 幂等、链非空不覆盖）。本测试钉：① 首醒那轮 seed
    被调（按当前 lane）；② seed 在 list 之前发生（先灌再读，否则读到空表）。
    """
    _snapshot_with(monkeypatch, roster_ingested_date=None)

    order: list[str] = []
    seed_calls: list[str] = []

    async def fake_seed(*, lane):
        seed_calls.append(lane)
        order.append("seed")
        return 7

    async def fake_list(*, lane):
        order.append("list")
        return _seed_roster(lane)

    monkeypatch.setattr(engine_mod, "seed_npc_roster", fake_seed)
    monkeypatch.setattr(engine_mod, "list_npc_roster", fake_list)
    _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert seed_calls == ["coe-t2"], (
        f"当天首醒必须按当前 lane ensure seed 一次，实际 {seed_calls}"
    )
    assert order.index("seed") < order.index("list"), (
        "seed 必须在 list 之前（先灌名册再读，否则首醒读到空表、NPC 永不出场）"
    )


@pytest.mark.asyncio
async def test_first_wake_empty_lane_seeds_then_npcs_show_up(monkeypatch):
    """必改 1 端到端意图：首醒前 seed 让原本空的名册有了人、当轮就纳入推演。

    用真实 seed_npc_roster（不打桩），但 list 在 seed 后返回种子名册——证「seed 接进
    首醒分支」让首醒读得到名册、拼进 user 消息。这是「seed 没接入生产路径」这条致命
    缺陷修复后的正向证据：不再依赖测试手动 seed。
    """
    _snapshot_with(monkeypatch, roster_ingested_date=None)

    seeded: dict = {"done": False}

    async def fake_seed(*, lane):
        seeded["done"] = True
        return 7

    async def fake_list(*, lane):
        # seed 跑过后名册才有人（模拟 CAS seed 把 7 个种子灌进空表）。
        return _seed_roster(lane) if seeded["done"] else []

    monkeypatch.setattr(engine_mod, "seed_npc_roster", fake_seed)
    monkeypatch.setattr(engine_mod, "list_npc_roster", fake_list)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    assert "下面是这个世界里有名有姓的固定人物" in blob, (
        "首醒 seed 后名册有人、当轮就该纳入推演（修复前永远空、NPC 不出场）"
    )
    assert "同桌兼死党。" in blob


@pytest.mark.asyncio
async def test_second_wake_same_day_does_not_reseed_roster(monkeypatch):
    """必改 1：当天已纳入过（第二次醒）→ 不再 seed（seed 不进每轮热路径）。

    seed 只该在「首醒纳入名册」那个分支跑一次，不能每轮都调（哪怕 CAS 幂等也别白打
    一次 DB）。当天已纳入（roster_ingested_date == 今天）时既不纳入名册、也不 seed。
    """
    from app.infra import cst_time

    today = cst_time.now_cst().strftime("%Y-%m-%d")
    _snapshot_with(monkeypatch, roster_ingested_date=today)

    seed_calls: list[str] = []

    async def fake_seed(*, lane):
        seed_calls.append(lane)
        return 0

    monkeypatch.setattr(engine_mod, "seed_npc_roster", fake_seed)
    _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert seed_calls == [], (
        f"当天已纳入过名册（第二次醒）不该再 seed（不进每轮热路径），实际 {seed_calls}"
    )


def test_render_roster_section_groups_by_sister():
    """渲染器：把名册按 relates_to 归到对应姐妹、拼成「世界的固定人物」一段。"""
    from app.world.engine import _roster_section

    roster = [
        _npc(npc_name="林小满", relates_to="ayana", sketch="绫奈的同桌。"),
        _npc(npc_name="顾舟", relates_to="ayana", sketch="绫奈的班长。"),
        _npc(npc_name="陈鹿", relates_to="akao", sketch="赤尾的闺蜜。"),
    ]
    section = _roster_section(roster)
    # 名字 + 速写都在
    for s in ("林小满", "绫奈的同桌。", "顾舟", "陈鹿", "赤尾的闺蜜。"):
        assert s in section, f"名册段必须含 {s}"
    # 同一姐妹的 NPC 归在一起（绫奈名下两人在赤尾名下那人之前/之后成块出现）。
    # 至少验证按姐妹分组（出现姐妹的归类锚点）。
    assert ("绫奈" in section) or ("ayana" in section)
    assert ("赤尾" in section) or ("akao" in section)


# ---------------------------------------------------------------------------
# 三姐妹此刻各自的样子（纯客观叙述对齐，Task 1 收口后）：
#
# world 是纯客观世界推演者。它读三姐妹的 LifeState **只为一个用途**——让自己的客观
# 叙述跟她们此刻所处对得上（她在上课就别说她在街上）。所以 USER 消息里那段「她们此刻
# 的样子」只拼每个人的**当前状态**（她此刻在哪、在干嘛），摆在能和【现实此刻】对照的
# 位置；读不到状态的角色如实写「还没有状态记录」、不漏拼不报错。
#
# Task 1 收口删掉的是「判唤醒」那一支：world 不再读 ``next_wake_at``（她想几点醒）/
# 状态新旧（observed_at 停了多久）去判断「谁很久没动、该醒了」再 notify 叫醒——这是
# 自锁源头（life 一静止就不产 act，world 看世界没动静就判「没必要叫」，越静越不叫）。
# 收口后 world 是否产事件只取决于客观世界进程（时间到了下课就会发生），跟 life 活不
# 活跃无关；life 全静止时 world 仍按客观节奏持续产出事件。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_world_produces_events_when_all_life_static(monkeypatch):
    """Task 1 核心：life 全静止（状态老旧、next_wake_at 已过期）→ world 仍正常推演一轮。

    构造三姐妹都很久没动（observed_at 是几小时前）、且都「想醒的点」早已过去
    （next_wake_at 已过期）的死局——这正是旧自锁会卡住的场景。收口后 world 不再
    用这些做唤醒决策，所以照常跑完一轮 agent 循环（产出客观事件），不被 life 的
    静止拽进自锁。
    """
    from datetime import timedelta

    from app.world.engine import _CST

    long_ago = (datetime.now(_CST) - timedelta(hours=4)).isoformat()
    expired_wake = (datetime.now(_CST) - timedelta(hours=2)).isoformat()

    async def personas():
        return ["akao", "chinagi", "ayana"]

    async def find_state(*, lane, persona_id):
        # 三个人都很久没更新、且想醒的点都早过了（旧自锁的死局场景）。
        return _life_state(
            persona_id=persona_id,
            current_state="还睡着，趴在被子里",
            observed_at=long_ago,
            next_wake_at=expired_wake,
        )

    monkeypatch.setattr(engine_mod, "list_all_persona_ids", personas)
    monkeypatch.setattr(engine_mod, "find_life_state", find_state)
    captured = _mock_run(monkeypatch)

    # 不卡死、正常跑完一轮（life 全静止不影响 world 是否推演）。
    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    # world 照常把客观推演 context 喂进 agent 循环、产出这一轮。
    assert "messages" in captured, "life 全静止时 world 仍应跑一轮 agent 循环（不自锁）"
    assert captured["tools"] == WORLD_TOOLS


@pytest.mark.asyncio
async def test_world_still_reads_life_state_for_narration_alignment(monkeypatch):
    """删的是「判唤醒」、留的是「客观叙述对齐」：world 仍读 life 状态、把当前状态喂进 context。

    这条钉死「客观叙述对齐」这个用途没被误删——world 仍要知道每个姐妹此刻在哪 / 在
    干嘛，才能让客观叙述跟她对得上（她在上课就别说她在街上）。
    """

    async def personas():
        return ["chinagi"]

    reads: list[dict] = []

    async def find_state(*, lane, persona_id):
        reads.append({"lane": lane, "persona_id": persona_id})
        return _life_state(
            persona_id="chinagi",
            current_state="在厨房煮咖啡",
            observed_at="2026-06-15T07:00:00+08:00",
        )

    monkeypatch.setattr(engine_mod, "list_all_persona_ids", personas)
    monkeypatch.setattr(engine_mod, "find_life_state", find_state)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    # 仍然读了 life 状态（客观叙述对齐的用途保留）。
    assert reads == [{"lane": "coe-t2", "persona_id": "chinagi"}], (
        "world 仍应读 life 状态用于客观叙述对齐（这个用途不能被误删）"
    )
    # 当前状态喂进了 context（让客观叙述对得上她此刻所处）。
    blob = "".join(m.text() for m in captured["messages"])
    assert "在厨房煮咖啡" in blob, "当前状态应喂进 context（客观叙述对齐）"


@pytest.mark.asyncio
async def test_sisters_section_drops_next_wake_at(monkeypatch):
    """Task 1 收口：三姐妹此刻段**不再**拼「她想几点醒」（next_wake_at 是判唤醒输入、删）。

    next_wake_at 是 world 旧的唤醒决策输入（拿她想醒的点和现实比、判该不该叫）。
    收口后 world 不再判唤醒，这个字段不该再出现在喂给 world 的客观叙述对齐段里。
    """

    async def personas():
        return ["akao"]

    async def find_state(*, lane, persona_id):
        return _life_state(
            persona_id="akao",
            current_state="还睡着",
            observed_at="2026-06-15T03:10:00+08:00",
            # 一个独一无二的时刻串，只要它出现在 prompt 里就说明 next_wake_at 被拼了。
            next_wake_at="2026-06-15T06:34:17+08:00",
        )

    monkeypatch.setattr(engine_mod, "list_all_persona_ids", personas)
    monkeypatch.setattr(engine_mod, "find_life_state", find_state)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    assert "06:34:17" not in blob, (
        "三姐妹此刻段不该再拼 next_wake_at（她想几点醒）——这是被删的判唤醒输入"
    )
    assert "想" not in blob.split("【三姐妹此刻各自的样子】")[-1].split("\n\n")[0], (
        "三姐妹此刻段不该出现「她想几点醒」这类唤醒意愿表述"
    )


def _life_state(
    *,
    lane="coe-t2",
    persona_id,
    current_state,
    observed_at,
    next_wake_at=None,
    response_mood="平静",
    activity_type="rest",
):
    """构造一条 LifeState（world 读到的某姐妹此刻主观快照）。"""
    from app.domain.life_state import LifeState

    return LifeState(
        lane=lane,
        persona_id=persona_id,
        current_state=current_state,
        response_mood=response_mood,
        activity_type=activity_type,
        observed_at=observed_at,
        next_wake_at=next_wake_at,
    )


@pytest.mark.asyncio
async def test_sisters_section_has_each_persona_current_state(monkeypatch):
    """每轮 USER 消息拼出的三姐妹此刻段含每个角色的**当前状态**（客观叙述对齐的依据）。

    Task 1 收口后只拼当前状态（她此刻在哪 / 在干嘛）——它是客观叙述对齐的依据；
    不再拼 next_wake_at（她想几点醒），那是被删的判唤醒输入。
    """

    async def personas():
        return ["akao", "chinagi", "ayana"]

    states = {
        "akao": _life_state(
            persona_id="akao",
            current_state="还睡着，趴在被子里",
            observed_at="2026-06-15T03:10:00+08:00",
            next_wake_at="2026-06-15T06:30:00+08:00",
        ),
        "chinagi": _life_state(
            persona_id="chinagi",
            current_state="在厨房煮咖啡",
            observed_at="2026-06-15T07:00:00+08:00",
            next_wake_at="2026-06-15T12:00:00+08:00",
        ),
        "ayana": _life_state(
            persona_id="ayana",
            current_state="在书桌前写作业",
            observed_at="2026-06-15T07:20:00+08:00",
            next_wake_at=None,
        ),
    }

    async def find_state(*, lane, persona_id):
        return states.get(persona_id)

    monkeypatch.setattr(engine_mod, "list_all_persona_ids", personas)
    monkeypatch.setattr(engine_mod, "find_life_state", find_state)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    # 段标题在
    assert "【三姐妹此刻各自的样子】" in blob, "USER 消息必须拼出三姐妹此刻段"
    # 每个角色的当前状态都在（客观叙述对齐的依据）
    assert "还睡着，趴在被子里" in blob
    assert "在厨房煮咖啡" in blob
    assert "在书桌前写作业" in blob
    # 不再拼 next_wake_at（她想几点醒）——判唤醒输入已删。
    assert "06:30:00 CST" not in blob, "Task 1 收口后不该再拼 akao 想几点醒（next_wake_at）"
    assert "12:00:00 CST" not in blob, "Task 1 收口后不该再拼 chinagi 想几点醒（next_wake_at）"


@pytest.mark.asyncio
async def test_sister_without_life_state_degrades_gracefully(monkeypatch):
    """某角色 LifeState 为 None（还没活过一轮）→ 如实写「还没有状态记录」、不漏拼不报错。"""

    async def personas():
        return ["akao", "chinagi", "ayana"]

    async def find_state(*, lane, persona_id):
        # 只有 chinagi 有状态，akao / ayana 读不到（None）。
        if persona_id == "chinagi":
            return _life_state(
                persona_id="chinagi",
                current_state="在厨房煮咖啡",
                observed_at="2026-06-15T07:00:00+08:00",
            )
        return None

    monkeypatch.setattr(engine_mod, "list_all_persona_ids", personas)
    monkeypatch.setattr(engine_mod, "find_life_state", find_state)
    captured = _mock_run(monkeypatch)

    # 不报错跑通整轮
    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    assert "【三姐妹此刻各自的样子】" in blob
    # 有状态的照常拼
    assert "在厨房煮咖啡" in blob
    # 无状态的如实降级（不漏拼这两个 persona）
    assert blob.count("还没有状态记录") == 2, (
        "akao / ayana 读不到状态都应如实写「还没有状态记录」，不漏拼"
    )
    assert "akao" in blob and "ayana" in blob, "无状态角色仍要出现在段里（不漏拼）"


@pytest.mark.asyncio
async def test_sisters_section_uses_tick_lane(monkeypatch):
    """读 LifeState 用本轮 tick 的 lane（不是进程默认 lane）。"""

    async def personas():
        return ["akao"]

    reads: list[dict] = []

    async def find_state(*, lane, persona_id):
        reads.append({"lane": lane, "persona_id": persona_id})
        return None

    monkeypatch.setattr(engine_mod, "list_all_persona_ids", personas)
    monkeypatch.setattr(engine_mod, "find_life_state", find_state)
    _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert reads == [{"lane": "coe-t2", "persona_id": "akao"}], (
        f"读 LifeState 必须用本轮 tick 的 lane（coe-t2），实际 {reads}"
    )


@pytest.mark.asyncio
async def test_sisters_section_placed_near_now(monkeypatch):
    """三姐妹此刻段拼在能和【现实此刻】直接比对的位置（紧随其后）。"""

    async def personas():
        return ["akao"]

    async def find_state(*, lane, persona_id):
        return _life_state(
            persona_id="akao",
            current_state="还睡着",
            observed_at="2026-06-15T03:10:00+08:00",
        )

    monkeypatch.setattr(engine_mod, "list_all_persona_ids", personas)
    monkeypatch.setattr(engine_mod, "find_life_state", find_state)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    # 锚在结构化 section 的实际内容上（标题字样在指令文案里也会出现，用内容定位才稳）：
    # 【现实此刻】行带的真实时刻锚 now、三姐妹段带的状态文字锚 sisters。两者都只在
    # 结构化 section 里出现一次。
    now_idx = blob.rindex("【现实此刻】")  # 结构化那行（指令里那处在更前）
    sisters_idx = blob.index("还睡着")  # 三姐妹段渲染出的状态内容
    assert sisters_idx > now_idx, "三姐妹此刻段应在【现实此刻】之后（能直接比对现在几点）"


def test_render_sisters_section_only_current_state():
    """渲染器单测：只把每个角色的**当前状态**拼进段（客观叙述对齐），None 降级。

    Task 1 收口后渲染器只承担「客观叙述对齐」——拼当前状态（她此刻在哪 / 在干嘛）；
    不再拼 next_wake_at（她想几点醒）/ 状态新旧那些判唤醒输入。
    """
    from app.world.engine import _sisters_section

    states = [
        (
            "akao",
            _life_state(
                persona_id="akao",
                current_state="还睡着",
                observed_at="2026-06-15T03:10:00+08:00",
                next_wake_at="2026-06-15T06:30:00+08:00",
            ),
        ),
        ("ayana", None),
    ]
    section = _sisters_section(states)
    assert "akao" in section and "ayana" in section
    assert "还睡着" in section, "渲染器应带当前状态（客观叙述对齐的依据）"
    assert "还没有状态记录" in section, "None 状态应如实降级"
    # 不再拼 next_wake_at（她想几点醒）
    assert "06:30:00 CST" not in section, "渲染器不该再拼 next_wake_at（判唤醒输入已删）"


def test_render_sisters_section_does_not_carry_wake_decision_inputs():
    """渲染器：不带任何「她想几点醒 / 状态停滞太久该叫」的判唤醒输入与引导（已删）。

    断言针对旧的判唤醒**短语 / 字段**（不是裸字 "醒"——客观叙述对齐段措辞里可能出现
    "她在上课就别说她在街上" 这类否定式说明里的字，那是 load-bearing 的、不能误伤）。
    """
    from app.world.engine import _sisters_section

    section = _sisters_section(
        [
            (
                "akao",
                _life_state(
                    persona_id="akao",
                    current_state="在写作业",
                    observed_at="2026-06-15T07:20:00+08:00",
                    next_wake_at="2026-06-15T22:00:00+08:00",
                ),
            )
        ]
    )
    assert "在写作业" in section, "当前状态仍要拼（客观叙述对齐）"
    assert "22:00:00 CST" not in section, "不该拼 next_wake_at（她想几点醒）"
    # 段里不该出现旧的判唤醒引导短语（不是裸字）。
    for wake_phrase in (
        "她想几点醒",
        "想醒的点",
        "停滞",
        "不对劲",
        "该有的动静",
        "把她那边",
    ):
        assert wake_phrase not in section, (
            f"客观叙述对齐段不该出现判唤醒引导 {wake_phrase!r}（判唤醒已删）"
        )


def test_render_sisters_section_carries_observed_at_for_idle_extrapolation():
    """T3 code review 必改 3：sense 新加的 idle 判断要求对"她上次已知状态"做时间外推，
    但 Task 1 收口后 ``_sisters_section`` 只拼 ``current_state``、没有任何时间戳，
    world 根本没有数据可以算"流逝了多久"，只能瞎猜。

    这里窄范围地加回 ``observed_at``（不是 ``next_wake_at``——那个"她自己排的下次
    想醒的时间"概念已经整个删除、不该复活），专门给 idle 判断补一个可外推的时间锚。
    """
    from app.infra import cst_time
    from app.world.engine import _sisters_section

    observed_at = "2026-06-15T03:10:00+08:00"
    section = _sisters_section(
        [
            (
                "akao",
                _life_state(
                    persona_id="akao",
                    current_state="还睡着",
                    observed_at=observed_at,
                ),
            )
        ]
    )
    assert cst_time.to_cst_full(observed_at) in section, (
        "渲染器必须把 observed_at（格式化成完整 CST 口径）拼进段里，否则 idle 判断"
        f"没有时间戳可以外推。实际渲染：{section!r}"
    )


def test_render_sisters_section_acknowledges_idle_exception_not_blanket_ban():
    """T3 code review 必改 3：旧渲染文案是"不是让你判断该不该叫醒谁"的一刀切说法，
    跟 sense 新加的 idle 判断（本质就是一种"该不该叫醒"判断）正面冲突。渲染器必须
    改成明确承认这一个划定清楚的例外，而不是让模型自己去猜哪句更权威。
    """
    from app.world.engine import _sisters_section

    section = _sisters_section(
        [
            (
                "akao",
                _life_state(
                    persona_id="akao",
                    current_state="还睡着",
                    observed_at="2026-06-15T03:10:00+08:00",
                ),
            )
        ]
    )
    assert "idle 判断" in section, "渲染器必须点名 observed_at 服务于 sense 的 idle 判断"
    assert "这一个例外" in section, (
        "渲染器必须明确承认 idle 判断是这条「不判该不该醒」规则的例外，不是一刀切禁止"
    )


def test_world_instruction_has_no_wake_guidance():
    """Task 1 收口：world_loop_instruction 不再有「判唤醒」软引导。

    旧引导让 world「顺手看一眼三姐妹、谁状态停滞太久 / 不对劲就 notify 把她唤回」——
    这是自锁源头（life 静止 → world 判没必要叫 → 越静越不叫）。收口后 world 是纯客观
    推演者，绝不读角色状态做唤醒决策，所以这段引导整段删除。
    """
    instruction = engine_mod.world_loop_instruction()
    # 不再有判唤醒的**正向引导短语**：把她唤回 / 状态停滞太久该叫 / 临近到点排短 sleep
    # 接她 这类一律不该出现（针对旧引导的实际措辞，不是裸字——"不替任何角色判断该不该
    # 醒" 这类否定式 load-bearing 说明里出现的字不算违规）。
    for wake_phrase in (
        "唤回这个世界",
        "把她自然唤回",
        "想醒的点",
        "状态停滞",
        "太久没更新",
        "把她那边此刻该出现的客观动静想出来",
        "把她那边此刻该有的动静想出来",
        "早点回来接着看她那边",
        "明显该醒了的人同样该被你看见",
    ):
        assert wake_phrase not in instruction, (
            f"world 指令不该再有判唤醒引导 {wake_phrase!r}（Task 1 收口删除）"
        )
    # notify 仍在（它的客观投递语义保留，只是不再为「叫醒谁」服务）。
    assert "notify" in instruction, "notify 工具说明仍应在（客观投递语义保留）"


def test_world_instruction_advances_world_by_real_time():
    """world 指令引导「客观时间独立推进世界」——别把 life 静止当成世界冻结。

    coe 实证：world 推演基于「上一版世界叙述 + 三姐妹此刻状态」，三姐妹 life 状态停
    在某一刻（都「在吃晚饭」）后，45 分钟后 world 还在叙述「她们仍在餐桌吃饭」——它把
    life 的静止当成了世界的冻结点（晚饭本该吃完、有人起身收拾、天该更黑，这些客观进程
    没推演出来）。设计意图：world 应基于真实流逝的时间主动推动客观世界前进，跟 life
    动没动无关。这条钉死指令在 prompt 层有这层引导：

      * 真实时间在流逝、上一版是过去某一刻的快照、现在世界客观上已经不一样——推演这段
        时间世界自然推进成了什么样，别照搬复述上一版；
      * 三姐妹的状态是她**上次被观测到**的过去快照、不是此刻一定还那样——让客观时间把
        场景往前带，而不是把她冻在那一刻复述；
      * 区分「硬造戏剧性事件」（不要）vs「客观时间推进的自然变化」（要：一顿饭会吃完、
        天会黑、人会从这个场景挪到下一个）——安静 ≠ 冻结。
    """
    instruction = engine_mod.world_loop_instruction()

    # ① 真实时间独立推进世界、上一版是过去快照、别照搬复述。
    assert ("时间" in instruction) and ("流逝" in instruction or "过去" in instruction), (
        "指令必须点出真实时间在流逝 / 上一版是过去某一刻的"
    )
    assert ("照搬" in instruction) or ("复述" in instruction), (
        "指令必须提醒别照搬复述上一版世界叙述"
    )
    # ② 三姐妹状态是上次被观测到的过去快照、不是此刻一定还那样。
    assert "快照" in instruction, (
        "指令必须说明三姐妹此刻的样子是上次被观测到的过去快照"
    )
    assert ("把场景往前带" in instruction) or ("场景往前" in instruction), (
        "指令必须引导让客观时间把场景往前带、而不是把人冻在那一刻"
    )
    # ③ 区分硬造戏剧 vs 客观时间推进的自然变化（安静 ≠ 冻结）。
    assert "冻结" in instruction, (
        "指令必须区分『安静』和『冻结』（安静不等于把世界冻在那一刻）"
    )
    assert ("吃完" in instruction) or ("结束" in instruction) or ("天会黑" in instruction), (
        "指令必须举例客观时间推进的自然变化（一顿饭会吃完 / 天会黑 / 人会挪到下一个场景）"
    )

    # 红线复核：这条新引导绝不能重新引入「判唤醒」语义（Task 1 已收口）。
    for wake_phrase in (
        "状态停滞",
        "太久没更新",
        "该不该叫醒",
        "明显该醒",
    ):
        assert wake_phrase not in instruction, (
            f"推进世界 ≠ 判唤醒：指令不该出现 {wake_phrase!r}（绝不重新引入判唤醒）"
        )


def test_world_instruction_does_not_author_sisters_autonomous_action():
    """第二轮调优 A：world 推进世界**不替三姐妹编她们自己的自主行动**。

    coe 实证：world 推进世界时写了「赤尾从餐桌边起身到厨房收拢碗筷」——但她（life）
    还没醒、没决定起身。world 是客观推演者，不替角色决定她做什么。已对齐的边界：

      * world 推的是客观环境随时间的进程（饭凉了、天黑了）+ 外部 / NPC 的客观动静；
      * world **可以**反映三姐妹**已经做了**的 act 的客观结果（她去厨房 → 厨房有动静），
        这是反映既成事实；
      * world **绝不预先替她编她还没做的行动**（她还在吃饭 / 还没醒，就不能写「她起身
        收拾了」）——那是她 life 醒来后自己决定的。

    这条钉死指令在 prompt 层明确这层「反映已做 ✓ / 预编没做 ✗」的区分。
    """
    instruction = engine_mod.world_loop_instruction()

    # ① 明确「不替三姐妹决定她们自己做什么」（她起不起身 / 去不去做某事是她自己的事）。
    assert ("不替" in instruction), "指令必须明确 world 不替角色决定她做什么"
    assert ("她自己" in instruction) or ("自己决定" in instruction) or (
        "自己的事" in instruction
    ), "指令必须说明她做不做某事是她自己（life 醒来后）决定的"

    # ② 反映已做的 act ✓ —— 把她已经做了的事的客观结果体现进世界（既成事实）。
    assert ("已经做" in instruction) or ("已做" in instruction) or (
        "既成事实" in instruction
    ), "指令必须允许 world 反映三姐妹已经做了的 act 的客观结果（既成事实）"

    # ③ 预编没做的行动 ✗ —— 绝不预先替她编她还没做 / 还没醒时的自主行动。
    assert ("还没做" in instruction) or ("没做的行动" in instruction) or (
        "预编" in instruction
    ) or ("预先" in instruction), (
        "指令必须钉死 world 绝不预先替角色编她还没做的自主行动"
    )

    # 红线复核：这条边界绝不能重新引入「判唤醒」语义（Task 1 已收口）。
    for wake_phrase in ("状态停滞", "太久没更新", "该不该叫醒", "明显该醒"):
        assert wake_phrase not in instruction, (
            f"职责边界 ≠ 判唤醒：指令不该出现 {wake_phrase!r}（绝不重新引入判唤醒）"
        )


def test_world_instruction_notifies_objective_changes_from_advance():
    """第二轮调优 B：真实时间推进让世界冒出在场的人够得着的客观动静，就要 notify 投出去。

    coe 实证：world 推进了世界（出了碗盘声、水流声），却没 notify 投给在场的人，life
    仍没被卷起来；它甚至推进完世界后还自相矛盾地写「没有出现可见的新变化」。已对齐的
    边界：notify 是把「世界推进」转成「life 被卷」的唯一通道——真实时间推进让世界冒出
    在场的人够得着的客观动静（环境到了新节点：饭凉了、天黑该开灯了；或外部 / NPC 动静：
    家人喊吃饭、电话响、玄关有动静），就要 notify 投出去、标客观作用域，让在场的人感知到。
    不能推进了世界却判「没有新变化」而不发。

    这条钉死指令在 prompt 层强化 notify：推进出的客观动静要投出去、别推进了却判没变化。
    """
    instruction = engine_mod.world_loop_instruction()

    # ① notify 是把「世界推进」转成「life 被卷 / 感知」的唯一通道。
    assert "notify" in instruction, "指令必须枚举 notify 工具"
    assert ("唯一" in instruction), (
        "指令必须点出 notify 是世界推进传到角色那里的唯一通道"
    )

    # ② 推进冒出的客观动静（环境到了新节点 / 外部 / NPC 动静）就要 notify 投出去。
    assert ("推进" in instruction) and ("动静" in instruction), (
        "指令必须引导：推进世界冒出的客观动静要 notify 投出去"
    )

    # ③ 别推进了世界却判「没有新变化」而不发（这是 coe 实证的自相矛盾）。
    assert ("没有新变化" in instruction) or ("没有变化" in instruction) or (
        "没新变化" in instruction
    ), "指令必须钉死：别推进了世界却判「没有新变化」而不发"

    # 红线复核：强化 notify ≠ 硬造戏剧（自然冒出的动静才投，不为卷人硬造）。
    assert "硬造" in instruction, (
        "强化 notify 必须同时守住「别为卷人硬造动静」（自然冒出才投）"
    )
    # 红线复核：绝不重新引入「判唤醒」语义（notify 投客观动静、不挑谁该醒）。
    for wake_phrase in ("状态停滞", "太久没更新", "该不该叫醒", "明显该醒"):
        assert wake_phrase not in instruction, (
            f"强化 notify ≠ 判唤醒：指令不该出现 {wake_phrase!r}（绝不重新引入判唤醒）"
        )


# ---------------------------------------------------------------------------
# 硬超时：整轮推演挂死 → wait_for 掐死、走 fail-open（堵 world 唯一的永久睡死口）
#
# world_tick 被 time source loop 同步 await：没有硬超时时，一轮 LLM（或冷启路径里
# 任何一步）挂死会让 world_tick 永不返回 → source loop 永等 → world 永久睡死（真机
# coe 清库冷启零 emit 就是它）。对照组 day_review / persona_review 早有 wait_for，所以
# 同样被同步 await 也从不永久死。这两条测试把同样的硬超时模式补到 world_tick。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_world_tick_hard_timeout_fails_open(monkeypatch, caplog):
    """整轮推演挂死（run 不返回）→ 硬超时掐死：不向上抛、error 留痕、绝不真等。"""
    import asyncio

    async def hanging_run(self, messages, **kwargs):
        await asyncio.sleep(5)  # 远超（被调小的）超时
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(engine_mod.Agent, "run", hanging_run)
    monkeypatch.setattr(engine_mod, "WORLD_TICK_TIMEOUT_SECONDS", 0.05)

    with caplog.at_level("ERROR"):
        await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))  # 不抛、不真等 5s

    assert any(r.levelname == "ERROR" for r in caplog.records), (
        "超时的一轮必须 error 留痕，不能静默吞"
    )


def test_world_tick_timeout_below_single_flight_ttl():
    """硬超时必须 < 单飞锁 TTL（600s）：锁 TTL 到期后下一拍能进，挂死的旧轮必须先被
    掐死、释放锁，否则两轮并发写同一 world transcript（确定性 session_id 读改写竞态）。"""
    assert (
        engine_mod.WORLD_TICK_TIMEOUT_SECONDS
        < engine_mod.WORLD_TICK_LOCK_TTL_SECONDS
    )


# ---------------------------------------------------------------------------
# task2：续写自维护大纲 + 沿线推进世界（spec 决策 1/3/4 + 事实优先级时序）
#   * 大纲软 reminder 纯函数（_outline_reminder_text）：白天 + 大纲旧 → 提醒；
#     大纲新 / 夜里 → 空（只控 context 注入、绝不验「提醒后大纲必须被改」）。
#   * _world_loop_messages：大纲段插在【世界阶段】之后、上一版叙述之前；reminder 段
#     插在这批动作之前。
#   * _run_world_round：每轮读大纲当朝向注入、按大纲时间 + 现实此刻算 reminder。
#   * world_loop_instruction：枚举 update_outline、写入大纲纪律（硬不变量 / 事实优先级）。
# ---------------------------------------------------------------------------


def test_outline_reminder_text_stale_in_daytime_nudges():
    """大纲很久没更新 + 当前是白天（world 活跃时段）→ 注入一句软提醒（spec 决策 3）。"""
    from app.world.engine import OUTLINE_STALE_HOURS, _outline_reminder_text

    outlined = "2026-06-10T08:00:00+08:00"
    # 白天 14:00，距 08:00 已 6 小时 > 阈值（OUTLINE_STALE_HOURS 初值 4）。
    now = "2026-06-10T14:00:00+08:00"
    assert 6 > OUTLINE_STALE_HOURS, "本用例前提：6h 跨过 stale 阈值"
    text = _outline_reminder_text(outlined, now)
    assert text != "", "白天 + 大纲旧 → 应注入软提醒"
    assert "update_outline" in text, "提醒应点名 update_outline（引导 world 回看大纲）"
    # 软引导口径（spec 决策 3 命门）：world 读完可以不改、改不改自决，绝不强制。
    assert ("自己判断" in text) or ("不改" in text), (
        "提醒必须是软引导（world 自决、可以不改），不能写成强制改"
    )


def test_outline_reminder_text_fresh_no_nudge():
    """大纲刚更新过（不旧）→ 不注入提醒。"""
    from app.world.engine import _outline_reminder_text

    outlined = "2026-06-10T13:30:00+08:00"
    now = "2026-06-10T14:00:00+08:00"  # 才半小时，远未到 stale 阈值
    assert _outline_reminder_text(outlined, now) == "", "大纲新 → 不该注入提醒"


def test_outline_reminder_text_night_no_nudge():
    """深夜（world 非活跃时段）即便大纲旧也不打扰 → 不注入提醒。"""
    from app.world.engine import _outline_reminder_text

    outlined = "2026-06-10T20:00:00+08:00"
    now = "2026-06-11T02:00:00+08:00"  # 凌晨 2 点（深夜），距 20:00 已 6h（够旧）
    assert _outline_reminder_text(outlined, now) == "", "夜里即便大纲旧也不打扰"


def test_outline_reminder_text_no_outline_no_nudge():
    """还没有大纲（outlined_at=None）→ 无从判旧，不注入提醒。"""
    from app.world.engine import _outline_reminder_text

    assert _outline_reminder_text(None, "2026-06-10T14:00:00+08:00") == "", (
        "还没有大纲时不该催（没有可回看的大纲）"
    )


# --- _outline_reminder_text 边界用例（codex T3 建议 2）：白天窗口两端 / 阈值边界 /
#     解析失败 / 跨时区，把"差一点点就翻车"的边界行为逐条钉死 ---


def test_outline_reminder_hour_4_is_daytime_nudges():
    """白天窗口下边界 hour=4（含）→ 大纲旧时算白天、给提醒。"""
    from app.world.engine import _outline_reminder_text

    outlined = "2026-06-09T20:00:00+08:00"  # 距 8h、够旧
    now = "2026-06-10T04:00:00+08:00"  # hour=4，[4,23) 的下边界（含）
    assert _outline_reminder_text(outlined, now) != "", (
        "hour=4 是白天窗口下边界（含），大纲旧应提醒"
    )


def test_outline_reminder_hour_23_is_night_no_nudge():
    """白天窗口上边界 hour=23（不含）→ 算夜里、不提醒。"""
    from app.world.engine import _outline_reminder_text

    outlined = "2026-06-10T15:00:00+08:00"  # 距 8h、够旧
    now = "2026-06-10T23:00:00+08:00"  # hour=23，[4,23) 的上边界（不含）
    assert _outline_reminder_text(outlined, now) == "", (
        "hour=23 落在白天窗口 [4,23) 之外，算夜里、不提醒"
    )


def test_outline_reminder_elapsed_exactly_threshold_is_stale():
    """elapsed 正好等于阈值（4.0h）→ 按 `< 阈值才算新` 的实现算「旧」→ 提醒（边界钉死）。"""
    from app.world.engine import OUTLINE_STALE_HOURS, _outline_reminder_text

    assert OUTLINE_STALE_HOURS == 4.0, "本用例前提：阈值初值 4.0h"
    outlined = "2026-06-10T10:00:00+08:00"
    now = "2026-06-10T14:00:00+08:00"  # 正好 4h、白天
    assert _outline_reminder_text(outlined, now) != "", (
        "elapsed 正好等于阈值算「旧」（实现是 `< 阈值` 才算「新」），把这个边界行为钉死——"
        "若改成 `<= 阈值` 算新，正好 4h 会被判新、漏提醒"
    )


def test_outline_reminder_unparseable_outlined_at_no_nudge():
    """outlined_at 解析失败（脏串）→ 无从判旧、保守不催。"""
    from app.world.engine import _outline_reminder_text

    assert _outline_reminder_text("garbage-not-a-time", "2026-06-10T14:00:00+08:00") == "", (
        "outlined_at 解析失败时保守不催（不拿解析不出的时刻乱算时间差）"
    )


def test_outline_reminder_handles_mixed_timezone_offsets():
    """outlined_at 与 world_time 带不同 tz offset → 时间差按真实时刻算、hour 按 CST 取。

    构造：world_time 用 UTC offset，CST 看是白天但 UTC 看是夜里——若实现取 hour 时忘了
    astimezone(CST)、直接 .hour，会按 UTC 2 点误判夜里、漏提醒。
    """
    from app.world.engine import _outline_reminder_text

    outlined = "2026-06-10T20:00:00+00:00"  # = CST 2026-06-11 04:00
    now = "2026-06-11T02:00:00+00:00"  # = CST 2026-06-11 10:00（白天）；真实差 6h（够旧）
    assert _outline_reminder_text(outlined, now) != "", (
        "跨时区：时间差按真实时刻算（6h 够旧）、hour 按 CST 取（10 白天）→ 应提醒；"
        "忘了 astimezone(CST) 取 hour 会按 UTC 2 点判夜里、漏提醒"
    )


def test_world_loop_messages_inject_outline_after_arc_before_detail():
    """传了 outline_narrative → 大纲段出现，且在【世界阶段】之后、上一版叙述之前。"""
    from app.world.engine import _sisters_section, _world_loop_messages

    # 锚点用各段**独有内容 / 独有段标题**，避开 world_loop_instruction 正文里出现的
    # 相似措辞（指令里也提「上一版世界叙述」「大纲」，纯按这些词 index 会落到指令上）。
    arc_text = "眼下初夏，一家四口刚搬进新小区。"
    narrative = "线A：邻居家在装修，这两天白天会有电钻声，预计周末完工。"
    messages = _world_loop_messages(
        detail="清晨厨房有了动静。",
        detail_written_at="2026-06-10T08:00:00+08:00",
        now_iso="2026-06-10T14:00:00+08:00",
        wake_reason="例行看一眼世界。",
        round_id="r1",
        arc_narrative=arc_text,
        sisters_text=_sisters_section([]),
        outline_narrative=narrative,
    )
    blob = "".join(m.text() for m in messages)
    assert narrative in blob, "传了 outline_narrative 时大纲全文必须进续写 context"
    i_arc = blob.index(arc_text)  # 世界阶段段的独有内容
    i_outline_header = blob.index("【你的大纲")  # 大纲段独有段标题（指令里不出现）
    i_outline = blob.index(narrative)
    i_detail_header = blob.index("【你记得的上一版世界叙述】")  # 带【】的段标题独有
    assert i_arc < i_outline_header <= i_outline < i_detail_header, (
        "大纲段必须插在【世界阶段】之后、上一版世界叙述段之前"
    )


def test_world_loop_messages_empty_outline_guides_recording():
    """大纲为 None（冷启动还没记过线）→ 大纲段如实说明空白、引导用 update_outline 起头记。"""
    from app.world.engine import _outline_section

    guidance = _outline_section(None)
    assert "空白" in guidance, "大纲空白时要如实说明还没记过线"
    assert "update_outline" in guidance, (
        "大纲由续写自维护，空白时应引导 world 用 update_outline 起头记（区别于 arc 的「不归你动手」）"
    )
    # 引导文案绝不硬编剧情事实（宪法）。
    assert not any(ch.isdigit() for ch in guidance), "大纲空白引导不得硬编数字事实"
    for name in ("千凪", "赤尾", "绫奈", "chinagi", "akao", "ayana"):
        assert name not in guidance, f"大纲空白引导不得硬编角色 {name!r}"


def test_world_loop_messages_inject_reminder_before_acts():
    """传了 reminder_text → 软提醒段出现，且插在这批动作之前。"""
    from app.world.engine import _sisters_section, _world_loop_messages

    reminder = "<<<OUTLINE-REMINDER-SENTINEL>>>"
    acts = "- akao：在厨房煮咖啡。"
    messages = _world_loop_messages(
        detail="清晨。",
        detail_written_at="2026-06-10T08:00:00+08:00",
        now_iso="2026-06-10T14:00:00+08:00",
        wake_reason="例行看一眼世界。",
        round_id="r1",
        arc_narrative=None,
        sisters_text=_sisters_section([]),
        outline_narrative=None,
        reminder_text=reminder,
        act_batch_text=acts,
    )
    blob = "".join(m.text() for m in messages)
    assert reminder in blob, "传了 reminder_text 时软提醒段必须进续写 context"
    assert blob.index(reminder) < blob.index(acts), "reminder 段必须插在这批动作之前"


def test_world_loop_messages_omit_reminder_section_when_empty():
    """reminder_text 为空（默认）→ 不插 reminder 段（条件注入，不凭空出现）。"""
    from app.world.engine import _sisters_section, _world_loop_messages

    sentinel = "<<<OUTLINE-REMINDER-SENTINEL>>>"
    common = {
        "detail": "清晨。",
        "detail_written_at": None,
        "now_iso": "2026-06-10T14:00:00+08:00",
        "wake_reason": "例行看一眼世界。",
        "round_id": "r1",
        "arc_narrative": None,
        "sisters_text": _sisters_section([]),
        "outline_narrative": None,
    }
    with_reminder = "".join(
        m.text() for m in _world_loop_messages(**common, reminder_text=sentinel)
    )
    without = "".join(m.text() for m in _world_loop_messages(**common))
    assert sentinel in with_reminder, "传了 reminder_text 时应注入"
    assert sentinel not in without, "没传 reminder_text 时不该凭空出现 reminder 段"


@pytest.mark.asyncio
async def test_round_feeds_outline_into_messages(monkeypatch):
    """每轮推演输入带大纲段，内容是最新一版大纲 narrative（_run_world_round 读它当朝向）。"""
    from app.world.outline import WorldOutline

    narrative = "线A：等的快递今天会到、需要有人在家签收；线B：阳台的花该浇水了。"

    async def fake_read_world_outline(*, lane):
        return WorldOutline(
            lane=lane, narrative=narrative, outlined_at="2026-06-03T06:00:00+08:00"
        )

    monkeypatch.setattr(engine_mod, "read_world_outline", fake_read_world_outline)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    assert narrative in blob, "每轮推演输入必须带最新一版大纲 narrative"


@pytest.mark.asyncio
async def test_outline_read_uses_current_lane(monkeypatch):
    """大纲按当前 lane 读（泳道隔离命门同 WorldState / WorldArc）。"""
    reads: list[str] = []

    async def fake_read_world_outline(*, lane):
        reads.append(lane)
        return None

    monkeypatch.setattr(engine_mod, "read_world_outline", fake_read_world_outline)
    _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert reads == ["coe-t2"], "每轮必须 read_world_outline(lane=当前 lane)，绝不读别的泳道"


@pytest.mark.asyncio
async def test_run_world_round_feeds_computed_reminder_into_messages(monkeypatch):
    """接线（codex T3 建议 1，最关键）：_run_world_round 把 _outline_reminder_text 算出的
    reminder 真传进喂 agent 的 messages。

    纯函数（_outline_reminder_text）+ 注入（_world_loop_messages 收到 reminder_text 会插段）
    各自已测，但「前者的输出真的流到了后者」这段**接线**之前没测。这里用 spy 替
    _outline_reminder_text（返回哨兵 + 录入参），脱开 wall-clock 的「白天」判定、确定性地钉死：
      ① _run_world_round 用 outline.outlined_at + now_iso 调它；
      ② 它的返回值流进了 messages。
    回归验证：把 _run_world_round 里传给 _world_loop_messages 的 reminder_text=reminder_text
    改成 reminder_text="" → 哨兵不再出现 → 本测试 fail。
    """
    from app.infra import cst_time
    from app.world.outline import WorldOutline

    sentinel = "<<<WIRED-REMINDER-SENTINEL>>>"
    outlined_at = "2026-06-03T06:00:00+08:00"
    calls: list[tuple[str | None, str]] = []

    async def fake_read_world_outline(*, lane):
        return WorldOutline(
            lane=lane, narrative="线A：绫奈在医院等检查结果。", outlined_at=outlined_at
        )

    def spy_reminder(outlined, world_time_iso):
        calls.append((outlined, world_time_iso))
        return sentinel

    monkeypatch.setattr(engine_mod, "read_world_outline", fake_read_world_outline)
    monkeypatch.setattr(engine_mod, "_outline_reminder_text", spy_reminder)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    assert sentinel in blob, (
        "_run_world_round 必须把 _outline_reminder_text 的返回值传进 messages"
        "（接线断了 / 传了空串 → 哨兵不出现）"
    )
    assert len(calls) == 1, "_run_world_round 每轮应调一次 _outline_reminder_text"
    got_outlined, got_now = calls[0]
    assert got_outlined == outlined_at, (
        "必须用 outline.outlined_at 调 reminder（读错字段会让白天 / 旧判定失真）"
    )
    # now_iso 是 _run_world_round 现算的现实 CST，无法预测精确值，验它是可解析的 CST 时刻。
    assert cst_time.parse(got_now) is not None, (
        "必须把现实此刻 now_iso（可解析的 CST 时刻）传给 reminder"
    )


def test_world_instruction_enumerates_update_outline_tool():
    """world_loop_instruction 必须枚举 update_outline + 写入大纲纪律（spec task2 part C）。

    update_outline 是 task1 加进 WORLD_TOOLS 的第六件工具。续写指令若不枚举它、不写
    大纲纪律，真实模型就不知道有大纲这份工作记忆、不会沿线推进、不会入账未完成线——
    绫奈急诊那条线又会被漏掉。所以指令必须按 spec 加入这些点（措辞留给 coe 精修，这里
    只钉死结构性内容在场）。
    """
    instruction = engine_mod.world_loop_instruction()
    # 工具枚举：第六件工具 update_outline 必须按名出现，工具数从五改成六。
    assert "update_outline" in instruction, "指令必须枚举 update_outline（否则模型不会调它）"
    assert "六个工具" in instruction, "工具清单已是六件（含 update_outline）"
    assert "五个工具" not in instruction, "工具数已改成六、不能再写「五个工具」"
    # 大纲是 world 自己的工作记忆 / 活的 spec。
    assert "大纲" in instruction, "指令必须说明 world 有一份自己的大纲"
    assert ("工作记忆" in instruction) or ("spec" in instruction), (
        "大纲应被点明是 world 的工作记忆（像活的 spec）"
    )
    # 硬不变量（决策 4 命门）：未完成线本轮出结果或 update_outline 入账，不蒸发。
    assert ("入账" in instruction) and ("蒸发" in instruction), (
        "必须写硬不变量：未完成线要么出结果、要么入账进大纲，绝不蒸发"
    )
    # 事实优先级：act 最硬 / 此刻推进 > 大纲预期。
    assert "act" in instruction, "事实优先级里必须提 act（life 已做出的角色主张、最硬）"
    assert ("预期" in instruction) or ("让步" in instruction), (
        "必须写「此刻推进 > 大纲预期」（大纲那条线的接下来怎么走只是预期、冲突时让步现实）"
    )
    # 从大纲的线上克制地长出客观事件、不即兴硬造。
    assert "克制" in instruction, "必须写「从大纲的线上克制地长出客观事件」"
    # reminder 是软提醒：读完可以不改（决策 3 命门、与 _outline_reminder_text 呼应）。
    assert "提醒" in instruction, "必须说明会有「大纲该回看了」的软提醒"
