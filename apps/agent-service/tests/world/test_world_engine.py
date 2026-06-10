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

    close_calls: list[dict] = []

    async def fake_record_world_round_close(
        *, lane, advance_cursor_to, materials_ingested_date
    ):
        # 正常成功收口走这个统一收口：既推游标（advance_cursor_to 非 None 时）又标记
        # 底料已纳入（materials_ingested_date 非 None 时），一次 append。这里只记录调用，
        # 同时把"推进游标"那部分镜像进 _test_cursor_calls，保持旧用例（断言游标推进）
        # 不动——它们仍能断言"游标推到本批末尾"。
        close_calls.append(
            {
                "lane": lane,
                "advance_cursor_to": advance_cursor_to,
                "materials_ingested_date": materials_ingested_date,
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
        # 默认长弧还是空白（None = 冷启动）：需要断言长弧内容的用例自己覆写这个桩
        # （monkeypatch.setattr engine_mod.read_world_arc）。
        arc_reads.append(lane)
        return None

    reflect_calls: list[dict] = []

    async def fake_run_arc_reflection(**kwargs):
        # 默认打桩反思环节（记录调用、不跑真 Agent / 不碰真库）：专测引擎机制的用例
        # 不该触发真实反思（langfuse prompt 拉取 / PG 标记）。需要断言反思行为的用例
        # 自己覆写这个桩（monkeypatch.setattr engine_mod.run_arc_reflection）。
        reflect_calls.append(kwargs)

    monkeypatch.setattr(engine_mod, "run_arc_reflection", fake_run_arc_reflection)
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

    engine_mod._test_materials_calls = materials_calls  # type: ignore[attr-defined]
    engine_mod._test_arc_reads = arc_reads  # type: ignore[attr-defined]
    engine_mod._test_reflect_calls = reflect_calls  # type: ignore[attr-defined]

    engine_mod._test_renotify_calls = renotify_calls  # type: ignore[attr-defined]
    engine_mod._test_cursor_calls = cursor_calls  # type: ignore[attr-defined]
    engine_mod._test_close_calls = close_calls  # type: ignore[attr-defined]
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
# 世界长弧：每轮推演输入带最新长弧；空弧给冷启动引导（不硬编任何剧情事实）
# ---------------------------------------------------------------------------


def _arc(*, lane="coe-t2", narrative, turned_at="2026-06-09T18:00:00+08:00"):
    """构造一条 WorldArc（world 读到的最新一版世界长弧）。"""
    from app.world.arc import WorldArc

    return WorldArc(lane=lane, narrative=narrative, turned_at=turned_at)


@pytest.mark.asyncio
async def test_round_feeds_latest_arc_into_messages(monkeypatch):
    """每轮推演输入带【世界的长弧】段，内容是最新一版长弧 narrative。"""
    narrative = "三姐妹家已经搬进新小区，妹妹换了新学校，眼下是初夏。"

    async def fake_read_world_arc(*, lane):
        return _arc(lane=lane, narrative=narrative)

    monkeypatch.setattr(engine_mod, "read_world_arc", fake_read_world_arc)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    assert "【世界的长弧】" in blob, "每轮推演输入必须带【世界的长弧】段"
    assert narrative in blob, "长弧段内容必须是最新一版 narrative"


@pytest.mark.asyncio
async def test_empty_arc_feeds_cold_start_guidance(monkeypatch):
    """长弧为 None（还没翻过页）→ 长弧段如实说明空白；**不**引导续写去调 update_arc。

    翻页（含空弧写第一版）已归反思环节独占——续写工具集里没有 update_arc，引导它
    去调一个不存在的工具只会让循环报错。空弧时续写只需知道长弧还是空白、顺着此刻
    往前推演即可（第一版由反思写）。
    """
    captured = _mock_run(monkeypatch)  # 默认 read_world_arc 桩返回 None

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    assert "【世界的长弧】" in blob, "空弧时长弧段也要出现（带空白说明）"
    assert "空白" in blob, "必须明示长弧还是空白"
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
    """长弧按当前 lane 读（泳道隔离命门同 WorldState）。"""
    _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert engine_mod._test_arc_reads == ["coe-t2"], (
        "每轮必须 read_world_arc(lane=当前 lane)，绝不读别的泳道"
    )


# ---------------------------------------------------------------------------
# 反思环节（Task 2b）：翻页能力从续写剥离、归独立反思——当日未反思先跑反思、先于
# 续写；当日已反思不跑；续写读长弧必须在反思之后现读
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
    """续写读长弧必须在反思之后**现读**（不能用反思前缓存的值）。

    模拟「update_arc 已 durable 落库、反思 Agent 随后失败（fail-open 不抛）」：
    反思桩把长弧库里的最新版换成新一页，续写的输入必须读到新长弧、不是旧的。
    """
    current = {"narrative": "旧长弧：这一页还没翻。"}

    async def fake_read_world_arc(*, lane):
        return _arc(lane=lane, narrative=current["narrative"])

    async def fake_reflection(**kwargs):
        # update_arc 已落库（库里最新版变了）；随后反思 Agent 失败也不抛（fail-open）。
        current["narrative"] = "新长弧：页已经翻过去了。"

    monkeypatch.setattr(engine_mod, "read_world_arc", fake_read_world_arc)
    monkeypatch.setattr(engine_mod, "run_arc_reflection", fake_reflection)
    captured = _mock_run(monkeypatch)

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    blob = "".join(m.text() for m in captured["messages"])
    assert "新长弧：页已经翻过去了。" in blob, (
        "续写必须读到反思（update_arc）落库后的新长弧——长弧要在反思之后现读"
    )
    assert "旧长弧：这一页还没翻。" not in blob, "续写不能用反思前缓存的旧长弧"


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

    续写工具回到四个（notify / update_world / sense / sleep）。长弧仍是续写的输入
    （【世界的长弧】段保留），但续写无手碰长弧：指令里不能再有 update_arc 的使用
    指令，否则模型会去调一个不存在的工具。
    """
    instruction = engine_mod.world_loop_instruction()
    assert "update_arc" not in instruction, (
        "续写指令不得枚举 update_arc（翻页已归反思环节独占）"
    )
    assert "五个工具" not in instruction, "续写工具已回到四个，指令不能再写「五个工具」"
    assert "四个工具" in instruction, "工具清单应明确是四个（update_arc 已移出）"


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
    assert "四个工具" in instruction, (
        "工具清单应明确是四个（含 sense；update_arc 已归反思环节）"
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
        detail_written_at=None,  # 必传：None 是"冷启动无上一版"的显式语义
        now_iso="2026-06-05T09:00:00+08:00",
        wake_reason="例行看一眼世界。",
        round_id="r1",
        arc_narrative=None,  # 必传：None 是"还没翻过页"的显式语义，不允许靠默认值
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
    # 仍是四工具行动指令（含 1C 新增的 sense 五官；update_arc 归反思）
    assert ("notify" in instruction) and ("update_world" in instruction)
    assert "update_arc" not in instruction
    assert "sense" in instruction
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
