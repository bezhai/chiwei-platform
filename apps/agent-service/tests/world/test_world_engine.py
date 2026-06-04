"""world engine 节点契约 — Task 2.

world 是发动机，被三源唤醒（保底心跳 / 自排提前卡点 / life 回灌的
``IntentRaised``），读自己客观快照，让 LLM 判断此刻客观世界有没有"够格成为
event"的事、谁该感知到，克制地产出，按 event 锚定房间的当前在场集合投递
（客观感官投影、绝不含情绪），最后自排下次醒（≤10 分钟保底心跳内）。

这些测试 mock LLM（``_world_deliberate``），不烧真模型。它们钉死的是机制层
硬约束，不是 LLM 决策：

  * 三源都能唤醒 world、走通同一条推演—投递—自排回路；
  * 自排 delay 永远 ≤ 10 分钟保底心跳（world 不许自己排长闹钟把世界睡死）；
  * event 只投给所锚房间的当前在场者（产生侧在场过滤，信息差命门）；
  * LLM 返回"无事"时 world 克制不投递、仅自排下次醒（大部分心跳不产 event）；
  * 给 LLM 的产 event 指令明确要求客观感官投影、不含情绪（赤尾设计宪法：
    world 绝不碰主观解读）。

赤尾设计宪法：world 醒后"够不够格成 event""谁该感知"全由 LLM 判断，代码里
没有任何阈值 / 计数器 / if 分支替它决策。10 分钟心跳只决定"何时醒"，绝不进入
世界内容的决策。
"""

from __future__ import annotations

import pytest

import app.world.engine as engine_mod
from app.world.engine import (
    WORLD_HEARTBEAT_MS,
    PresenceChange,
    WorldDeliberation,
    WorldEventDraft,
    WorldTick,
    world_tick,
)


@pytest.fixture(autouse=True)
def _stub_state(monkeypatch):
    """world 节点读快照 / 在场、写快照、投递都打桩，专测引擎机制。

    - read_world_state → 一张固定起手快照
    - read_presence    → 固定在场表（chinagi 在 kitchen，akao 在 akao_room）
    - set_presence / write_world_state → 记录不落库
    - deliver_event    → 记录投了什么给谁
    - emit_delayed     → 记录自排了多久
    """
    presence = {"chinagi": "kitchen", "akao": "akao_room"}

    async def fake_read_world_state(*, lane):
        from app.world.state import WorldState

        return WorldState(
            lane=lane,
            world_time="2026-06-03T06:30:00+08:00",
            situation="千凪在厨房做早饭，赤尾、绫奈在各自房间。",
        )

    async def fake_read_presence(*, lane, persona_id):
        return presence.get(persona_id)

    async def fake_in_room(*, lane, room_id):
        return [p for p, r in presence.items() if r == room_id]

    delivered: list[dict] = []

    async def fake_deliver_event(**kwargs):
        delivered.append(kwargs)
        return 1

    self_wakes: list[int] = []

    async def fake_emit_delayed(data, *, delay_ms, durability="durable"):
        self_wakes.append(delay_ms)

    set_calls: list[dict] = []

    async def fake_set_presence(*, lane, persona_id, room_id):
        # 写进同一张在场表，让后续 read_presence / personas_in_room 看到变更
        presence[persona_id] = room_id
        set_calls.append({"lane": lane, "persona_id": persona_id, "room_id": room_id})

    world_writes: list[dict] = []

    async def fake_write_world_state(*, lane, world_time, situation):
        world_writes.append(
            {"lane": lane, "world_time": world_time, "situation": situation}
        )

    monkeypatch.setattr(engine_mod, "read_world_state", fake_read_world_state)
    monkeypatch.setattr(engine_mod, "read_presence", fake_read_presence)
    monkeypatch.setattr(engine_mod, "personas_in_room", fake_in_room)
    monkeypatch.setattr(engine_mod, "set_presence", fake_set_presence)
    monkeypatch.setattr(engine_mod, "write_world_state", fake_write_world_state)
    monkeypatch.setattr(engine_mod, "deliver_event", fake_deliver_event)
    monkeypatch.setattr(engine_mod, "emit_delayed", fake_emit_delayed)

    engine_mod._test_delivered = delivered  # type: ignore[attr-defined]
    engine_mod._test_self_wakes = self_wakes  # type: ignore[attr-defined]
    engine_mod._test_presence = presence  # type: ignore[attr-defined]
    engine_mod._test_set_calls = set_calls  # type: ignore[attr-defined]
    engine_mod._test_world_writes = world_writes  # type: ignore[attr-defined]
    yield


def _mock_deliberate(monkeypatch, deliberation: WorldDeliberation):
    captured: dict = {}

    async def fake_deliberate(*, lane, snapshot, presence_text, wake_reason):
        captured["lane"] = lane
        captured["snapshot"] = snapshot
        captured["presence_text"] = presence_text
        captured["wake_reason"] = wake_reason
        return deliberation

    monkeypatch.setattr(engine_mod, "_world_deliberate", fake_deliberate)
    return captured


@pytest.mark.asyncio
async def test_heartbeat_wakes_reads_produces_and_self_schedules(monkeypatch):
    """心跳唤醒 → 读快照 → (LLM 产 event) → 投递 → (LLM 想提前看) → 自排下次醒。"""
    _mock_deliberate(
        monkeypatch,
        WorldDeliberation(
            events=[
                WorldEventDraft(
                    room_id="kitchen",
                    summary="飘来煎蛋和咖啡的香味",
                    occurred_at="2026-06-03T06:30:00+08:00",
                )
            ],
            # LLM 想 5 分钟后再看一眼（饭快好了）→ 走自排提前卡点路径
            next_check_seconds=300,
        ),
    )

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    # 投递发生了，且 LLM 给了提前需求 → 自排了下次醒
    assert len(engine_mod._test_delivered) == 1
    assert len(engine_mod._test_self_wakes) == 1


@pytest.mark.asyncio
async def test_self_wake_never_exceeds_heartbeat(monkeypatch):
    """LLM 给的提前卡点超过 10 分钟保底心跳时被夹到 ≤ 心跳 —— world 不许排长闹钟。"""
    # LLM 想 20 分钟后再看（1200s > 600s 心跳），必须被夹到 ≤ 心跳窗口。
    _mock_deliberate(
        monkeypatch, WorldDeliberation(events=[], next_check_seconds=1200)
    )

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert engine_mod._test_self_wakes  # 给了提前需求 → 一定自排了
    for delay in engine_mod._test_self_wakes:
        assert 0 < delay <= WORLD_HEARTBEAT_MS, (
            f"自排 {delay}ms 超过 10 分钟保底心跳 {WORLD_HEARTBEAT_MS}ms"
        )


@pytest.mark.asyncio
async def test_no_self_schedule_when_llm_gives_no_early_check(monkeypatch):
    """必改 1 复现：LLM 不给提前卡点需求 → world_tick **不** emit self。

    旧 bug：每轮 world_tick 末尾无条件 emit_delayed(self, 600s)，与 interval
    600s 保底心跳并存 → 源循环下 self tick 线性累积。修复后：只有 LLM 明确给了
    一个提前需求才自排，没给就靠 interval 兜底，无累积。
    """
    # LLM 没填 next_check_seconds（默认 0 / None）→ 不该有任何自排。
    _mock_deliberate(monkeypatch, WorldDeliberation(events=[]))

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert engine_mod._test_self_wakes == [], (
        "LLM 没给提前卡点需求时 world_tick 不该无条件 emit self（旧 bug：每轮累积一个）"
    )


@pytest.mark.asyncio
async def test_self_schedule_when_llm_asks_to_look_again_soon(monkeypatch):
    """LLM 给了一个 0 < x < 600 的提前需求 → emit self、delay = x 秒、≤ 心跳。

    饭快好了想 5 分钟后再看一眼这类——提前与否由 world LLM 判断（赤尾宪法），
    代码只忠实把它的提前需求落成一条 emit_delayed。
    """
    _mock_deliberate(
        monkeypatch, WorldDeliberation(events=[], next_check_seconds=300)
    )

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert engine_mod._test_self_wakes == [300_000], (
        f"LLM 想 300s 后再看，应 emit 一条 delay=300000ms 的 self，"
        f"实际 {engine_mod._test_self_wakes}"
    )


@pytest.mark.asyncio
async def test_event_only_delivered_to_in_room_personas(monkeypatch):
    """event 只投给所锚房间当前在场者 —— 产生侧在场过滤（信息差命门）。

    event 锚 kitchen，只有 chinagi 在 kitchen；akao 在 akao_room、收不到。
    """
    _mock_deliberate(
        monkeypatch,
        WorldDeliberation(
            events=[
                WorldEventDraft(
                    room_id="kitchen",
                    summary="飘来煎蛋和咖啡的香味",
                    occurred_at="2026-06-03T06:30:00+08:00",
                )
            ],
        ),
    )

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    recipients = {d["persona_id"] for d in engine_mod._test_delivered}
    assert recipients == {"chinagi"}, (
        f"event 锚 kitchen 只该投在场的 chinagi，实际投给 {recipients}"
    )
    # 投出去的 event 带房间锚点，走客观投影 summary
    d = engine_mod._test_delivered[0]
    assert d["room_id"] == "kitchen"
    assert d["summary"] == "飘来煎蛋和咖啡的香味"
    assert d["kind"] == "ambient"
    assert d["source"] == "world"


@pytest.mark.asyncio
async def test_event_in_empty_room_delivered_to_nobody(monkeypatch):
    """锚到没人在的房间 → 不投给任何人（不为不在场的人产 event）。"""
    _mock_deliberate(
        monkeypatch,
        WorldDeliberation(
            events=[
                WorldEventDraft(
                    room_id="balcony",  # 没人在阳台
                    summary="阳台的花被风吹动",
                    occurred_at="2026-06-03T06:30:00+08:00",
                )
            ],
        ),
    )

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert engine_mod._test_delivered == []
    # LLM 没给提前卡点需求 → 不自排，靠 interval 保底心跳兜底（必改 1）
    assert engine_mod._test_self_wakes == []


@pytest.mark.asyncio
async def test_quiet_heartbeat_produces_no_event_and_no_self_schedule(monkeypatch):
    """大部分心跳克制不产 event：LLM 返回无事且不想提前看 → 不投递、不自排。

    旧 bug：每轮无条件自排一个 self（与 interval 保底心跳并存）→ 源循环下线性
    累积。修复后：没产 event 且 LLM 没给提前需求时，world_tick 不 emit 任何
    self，下次醒交给 wiring 里固定 600s 的 interval 保底心跳唯一兜底。
    """
    _mock_deliberate(monkeypatch, WorldDeliberation(events=[]))

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    assert engine_mod._test_delivered == []
    assert engine_mod._test_self_wakes == []


@pytest.mark.asyncio
async def test_intent_feedback_wakes_world_to_adjudicate(monkeypatch):
    """IntentRaised 回灌唤醒 world：收到意图 → 读快照 → 让 LLM 裁决。

    world 被意图唤醒走的是同一条推演回路，wake_reason 透出意图内容供 LLM
    裁决（是否变更世界 / 产 event）。
    """
    captured = _mock_deliberate(
        monkeypatch,
        WorldDeliberation(
            events=[
                WorldEventDraft(
                    room_id="kitchen",
                    summary="开关门的声音",
                    occurred_at="2026-06-03T06:30:00+08:00",
                )
            ],
        ),
    )

    await world_tick(
        WorldTick(
            lane="coe-t2",
            reason="intent",
            intent_persona_id="chinagi",
            intent_summary="我想起床去厨房煮咖啡",
        )
    )

    # 意图被读快照 + 透给 LLM 裁决
    assert captured["snapshot"] is not None
    assert "煮咖啡" in captured["wake_reason"]
    # 裁决产了 event（自排与否由 LLM 的 next_check_seconds 决定，这里不产提前需求）
    assert engine_mod._test_delivered


@pytest.mark.asyncio
async def test_world_time_follows_reality_not_llm(monkeypatch):
    """world_time 每次唤醒取现实当前时间，不依赖 LLM 填 next_world_time。

    spec key decision: 世界时钟跟现实走、不快进。LLM 不填 next_world_time 时，
    world_time 也必须前进到现实当前时刻（旧 bug：LLM 不填就永远停在冷启动时刻）。
    """
    _mock_deliberate(monkeypatch, WorldDeliberation(events=[]))  # LLM 不填任何世界状态

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    # 即便 LLM 没填 next_world_time，world 也落了一版带"现实当前时间"的快照
    assert engine_mod._test_world_writes, "每次唤醒必须落最新 world_time"
    written_times = [w["world_time"] for w in engine_mod._test_world_writes]
    # 写进去的 world_time 不是固定的起手快照时刻（06:30），而是跟现实走的新时刻
    assert all(t != "2026-06-03T06:30:00+08:00" for t in written_times), (
        f"world_time 没跟现实走，仍停在起手快照时刻：{written_times}"
    )


@pytest.mark.asyncio
async def test_rhythm_driven_presence_change_moves_persona(monkeypatch):
    """节律驱动在场变更：world 推演表达"挪人" → set_presence 真被调用、在场变了。

    到点该上学 / 放学 / 吃饭时，world 把相关 persona 挪到对应房间。这条断言
    deliberation 的 presence_changes 被应用：set_presence 真被调用、在场表更新。
    """
    _mock_deliberate(
        monkeypatch,
        WorldDeliberation(
            presence_changes=[
                PresenceChange(persona_id="akao", room_id="kitchen"),
            ],
            events=[
                WorldEventDraft(
                    room_id="kitchen",
                    summary="脚步声从走廊靠近厨房",
                    occurred_at="2026-06-03T06:30:00+08:00",
                )
            ],
        ),
    )

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    # set_presence 真被调用，把 akao 挪进了 kitchen
    assert {"lane": "coe-t2", "persona_id": "akao", "room_id": "kitchen"} in (
        engine_mod._test_set_calls
    )
    # 在场真的变了
    assert engine_mod._test_presence["akao"] == "kitchen"


@pytest.mark.asyncio
async def test_intent_adjudication_changes_presence(monkeypatch):
    """意图裁决驱动在场变更：life 说"我想去厨房"，world 裁准 → set_presence。

    reason==intent 时，world LLM 判断意图合理就改在场并产对应 event。这条断言
    意图路径下 presence_changes 同样被应用。
    """
    _mock_deliberate(
        monkeypatch,
        WorldDeliberation(
            presence_changes=[
                PresenceChange(persona_id="akao", room_id="kitchen"),
            ],
            events=[
                WorldEventDraft(
                    room_id="kitchen",
                    summary="赤尾走进了厨房",
                    occurred_at="2026-06-03T06:30:00+08:00",
                )
            ],
        ),
    )

    await world_tick(
        WorldTick(
            lane="coe-t2",
            reason="intent",
            intent_persona_id="akao",
            intent_summary="我想去厨房煮咖啡",
        )
    )

    assert {"lane": "coe-t2", "persona_id": "akao", "room_id": "kitchen"} in (
        engine_mod._test_set_calls
    )
    assert engine_mod._test_presence["akao"] == "kitchen"


@pytest.mark.asyncio
async def test_presence_changes_apply_before_delivery(monkeypatch):
    """在场变更先于投递：把人挪进房间后，那条锚到该房间的 event 投得到她。

    顺序命门：先 set_presence 再按房间在场集合投递，否则刚挪进来的人收不到她
    本该感知的那条 event。akao 原本在 akao_room，被挪进 kitchen 后，锚 kitchen
    的 event 必须投到她。
    """
    _mock_deliberate(
        monkeypatch,
        WorldDeliberation(
            presence_changes=[
                PresenceChange(persona_id="akao", room_id="kitchen"),
            ],
            events=[
                WorldEventDraft(
                    room_id="kitchen",
                    summary="灶台上的咖啡正在滴漏",
                    occurred_at="2026-06-03T06:30:00+08:00",
                )
            ],
        ),
    )

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    recipients = {d["persona_id"] for d in engine_mod._test_delivered}
    # chinagi 本就在 kitchen，akao 被挪进 kitchen —— 两人都该收到
    assert recipients == {"chinagi", "akao"}, (
        f"挪进 kitchen 的 akao 没收到锚 kitchen 的 event，实际收到的：{recipients}"
    )


@pytest.mark.asyncio
async def test_cold_start_places_sisters_then_deliberates(monkeypatch):
    """冷启动：无 WorldState 时，让 LLM 按现实时间+节律判断三姐妹此刻在哪、set。

    不硬编逐时刻死表 —— 冷启动走同一条推演回路，wake_reason 告诉 LLM 这是首次
    醒来、还没人被放置，LLM 通过 presence_changes 表达三姐妹此刻该在的房间。
    """
    # 冷启动：read_world_state 返回 None
    async def no_world_state(*, lane):
        return None

    monkeypatch.setattr(engine_mod, "read_world_state", no_world_state)
    # 起始在场清空：模拟世界第一次醒、谁都还没被放置
    engine_mod._test_presence.clear()

    captured = _mock_deliberate(
        monkeypatch,
        WorldDeliberation(
            presence_changes=[
                PresenceChange(persona_id="chinagi", room_id="kitchen"),
                PresenceChange(persona_id="akao", room_id="akao_room"),
                PresenceChange(persona_id="ayana", room_id="ayana_room"),
            ],
            events=[],
        ),
    )

    await world_tick(WorldTick(lane="coe-t2", reason="heartbeat"))

    # 冷启动也走 LLM 推演（喂了节律 + 现实时间），不是硬编死表
    assert captured["snapshot"] is not None
    assert "冷启动" in captured["wake_reason"] or "首次" in captured["wake_reason"]
    # 三姐妹都被 set 到各自此刻该在的房间
    placed = {c["persona_id"]: c["room_id"] for c in engine_mod._test_set_calls}
    assert placed == {
        "chinagi": "kitchen",
        "akao": "akao_room",
        "ayana": "ayana_room",
    }


@pytest.mark.asyncio
async def test_deliberation_prompt_demands_objective_projection(monkeypatch):
    """给 LLM 的产 event 指令明确要求客观感官投影、不含情绪。

    赤尾设计宪法：world 只做"客观事实 → 各位置客观可感形态"，绝不碰情绪 /
    解读。这一条在 prompt 组装层面断言：world 投喂 LLM 的指令里写明客观投影、
    禁止情绪解读。
    """
    instruction = engine_mod.world_deliberation_instruction()
    assert "客观" in instruction
    # 明确禁止情绪 / 主观解读
    assert ("情绪" in instruction) or ("主观" in instruction) or ("解读" in instruction)


@pytest.mark.asyncio
async def test_deliberation_prompt_explains_presence_change(monkeypatch):
    """给 LLM 的指令告诉它能/该表达在场变更、到点按节律挪人。

    Gap 1 的 prompt 层钉子：world 推演要懂"它可以挪人"（presence_changes），
    且 event 锚定房间与在场变更复用同一套 room_id（看得到当前在场分布文本）。
    """
    instruction = engine_mod.world_deliberation_instruction()
    assert "在场" in instruction  # 告诉 LLM 它能表达在场变更
    # 引导它按节律到点挪人（上学 / 放学 / 吃饭这类客观边界）
    assert ("节律" in instruction) or ("作息" in instruction) or ("房间" in instruction)
