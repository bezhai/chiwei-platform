"""life 工具循环的工具定义测试 (Task 3 + 阶段 1A act 范式).

life 不再填一张 LifeDecision 表，而是在 ReAct 循环里连续调工具行动。两件工具：

  * ``update_life_state`` —— 更新她此刻在干嘛 / 什么情绪 / 活动类型；可调 0 次或
    多次，多次以最后一次为准（spec 决策 2）。落到 ``save_life_state``。
  * ``act`` —— 她自主做一件影响外部世界的事（自然语言 ``description``，如
    "我去厨房做饭"）。act 是"她做了"、不是申请裁决：直接汇给 world 让它推演
    客观结果。act_id 由本轮 (lane + persona + 读到的 event_ids) 派生 —— 整轮重放
    幂等。落到 ``perform_act`` handler。

工具是 per-round 闭包：``build_life_tools(lane, persona_id, act_id, observed_at)``
把这一轮的绑定（她是谁、哪个泳道、本轮 act_id、观测时刻）capture 进去，模型
只看见业务参数（current_state / response_mood / activity_type / description），看不到
lane / act_id 这些机制绑定。
"""

from __future__ import annotations

import pytest

import app.nodes.life_tools as lt
from app.agent.tooling import Tool


@pytest.fixture
def stub_handlers(monkeypatch):
    """把工具底下的 durable handler 换成可观测 fake。"""
    state: dict = {"saved": [], "acts": [], "delivered": []}

    async def fake_save_life_state(**kwargs):
        state["saved"].append(kwargs)

    async def fake_perform_act(**kwargs):
        state["acts"].append(kwargs)

    async def fake_deliver_event(**kwargs):
        state["delivered"].append(kwargs)
        return 1

    monkeypatch.setattr(lt, "save_life_state", fake_save_life_state)
    monkeypatch.setattr(lt, "perform_act", fake_perform_act)
    monkeypatch.setattr(lt, "deliver_event", fake_deliver_event)
    return state


def _tools_by_name(tools: list[Tool]) -> dict[str, Tool]:
    return {t.name: t for t in tools}


def test_build_life_tools_returns_base_tools():
    """不给 self_wake 时工具集是 update_life_state + act + chat（chat 是 1C 常驻基础工具）。"""
    tools = lt.build_life_tools(
        lane="coe-t3",
        persona_id="akao",
        act_id="a-1",
        observed_at="2026-06-03T12:30:00+00:00",
    )
    by_name = _tools_by_name(tools)
    assert set(by_name) == {"update_life_state", "act", "chat"}
    for t in tools:
        assert isinstance(t, Tool)


def test_act_tool_name_is_act():
    """对模型暴露的 Tool.name / definition.name 都是 "act"（不是函数名 act_tool）。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    act = tools["act"]
    assert act.name == "act"
    assert act.definition.name == "act"


def test_tool_schema_hides_mechanism_bindings():
    """模型只看见业务参数，看不见 lane / persona_id / act_id / observed_at。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    update_props = set(tools["update_life_state"].definition.parameters["properties"])
    assert update_props == {"current_state", "response_mood", "activity_type"}

    act_props = set(tools["act"].definition.parameters["properties"])
    assert act_props == {"description"}


@pytest.mark.asyncio
async def test_update_life_state_tool_calls_handler(stub_handlers):
    """update_life_state 调一次 → save_life_state 收到这一轮的绑定 + 模型给的字段。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    await tools["update_life_state"].invoke(
        {
            "current_state": "起身去厨房",
            "response_mood": "迷糊",
            "activity_type": "move",
        }
    )
    assert stub_handlers["saved"] == [
        {
            "lane": "coe-t3",
            "persona_id": "akao",
            "current_state": "起身去厨房",
            "response_mood": "迷糊",
            "activity_type": "move",
            "observed_at": "2026-06-03T12:30:00+00:00",
        }
    ]


@pytest.mark.asyncio
async def test_update_life_state_multiple_calls_all_recorded(stub_handlers):
    """update 可调多次（每次 append 一版）—— 收口读最新即"最后一次为准"。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    await tools["update_life_state"].invoke(
        {"current_state": "想了想", "response_mood": "平静", "activity_type": "idle"}
    )
    await tools["update_life_state"].invoke(
        {"current_state": "决定去厨房", "response_mood": "期待", "activity_type": "move"}
    )
    assert [s["current_state"] for s in stub_handlers["saved"]] == [
        "想了想",
        "决定去厨房",
    ]


@pytest.mark.asyncio
async def test_act_tool_derives_per_act_id_from_base(stub_handlers):
    """act 落库用从 base act_id 派生的 per-act id（本轮第 1 件）；模型只给 description。

    per-act id = uuid5(base act_id 入参, 本轮序号)：第一件序号 1 → 确定派生值，绑死
    base + 序号 → 同件重投幂等。模型看不见 lane / act_id / 序号，只给 description。
    """
    import uuid

    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="derived-round-id",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    await tools["act"].invoke({"description": "我去厨房煮咖啡"})
    expected_id = str(uuid.uuid5(uuid.NAMESPACE_OID, "derived-round-id:1"))
    assert stub_handlers["acts"] == [
        {
            "lane": "coe-t3",
            "act_id": expected_id,
            "persona_id": "akao",
            "description": "我去厨房煮咖啡",
            "occurred_at": "2026-06-03T12:30:00+00:00",
        }
    ]


@pytest.mark.asyncio
async def test_act_multiple_in_round_all_land_with_unique_ids(stub_handlers):
    """一轮里调 N 次 act，每件都真正落 handler、各自唯一 act_id（不再被 if 守卫吞）。

    P6 修复：角色一轮想做几件就做几件，不再"一轮只生效一件"。per-act id 用
    base act_id + 本轮第 N 件的序号派生（序号是纯机制 seed、只标识第几件，不当
    行为闸），每件 act 各自落库、各自唯一 id —— world 端按 (lane, act_id) 幂等消化，
    N 件 act → N 个不同 id → N 次推演（第二件不再被静默吞）。
    """
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="derived-round-id",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )

    r1 = await tools["act"].invoke({"description": "我去厨房煮咖啡"})
    r2 = await tools["act"].invoke({"description": "顺便给千凪带一杯"})
    r3 = await tools["act"].invoke({"description": "把窗帘拉开"})

    # 三件都真正落到 handler（不再"一轮只生效一件"）
    assert [a["description"] for a in stub_handlers["acts"]] == [
        "我去厨房煮咖啡",
        "顺便给千凪带一杯",
        "把窗帘拉开",
    ]
    # 每件各自唯一 act_id（per-act 派生、不共用 base）
    act_ids = [a["act_id"] for a in stub_handlers["acts"]]
    assert len(set(act_ids)) == 3, f"每件 act 应各自唯一 id，实得 {act_ids}"
    # 每件都正常返回确认（不是某件被吞、不返回拒绝提示）
    for r in (r1, r2, r3):
        assert isinstance(r, str) and r


@pytest.mark.asyncio
async def test_act_per_act_id_stable_under_round_replay(stub_handlers):
    """整轮重投同一批唤醒（同一 base act_id）→ 同一件 act（同序号）得同一 per-act id。

    幂等命门：base act_id 由唤醒源派生、整轮重投稳定不变；per-act id 从 (base act_id,
    本轮第 N 件序号) 纯函数派生 —— 第一轮第 1 件与重投第 1 件得同一 id，world 端
    insert_idempotent 按 (lane, act_id) 去重 → 不重复推演同一动作。
    """
    # 第一轮：同一 base act_id 下做两件
    tools_a = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="same-base-act-id",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    await tools_a["act"].invoke({"description": "我去厨房煮咖啡"})
    await tools_a["act"].invoke({"description": "顺便给千凪带一杯"})
    first_round_ids = [a["act_id"] for a in stub_handlers["acts"]]

    stub_handlers["acts"].clear()

    # 重投：整轮重新构建工具（同一 base act_id）、LLM 重新做同序的两件
    tools_b = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="same-base-act-id",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    await tools_b["act"].invoke({"description": "我去厨房煮咖啡"})
    await tools_b["act"].invoke({"description": "顺便给千凪带一杯"})
    replay_round_ids = [a["act_id"] for a in stub_handlers["acts"]]

    # 同序号的 act 在重投下得同一 per-act id（幂等：world 不重复推演）
    assert replay_round_ids == first_round_ids


@pytest.mark.asyncio
async def test_act_seq_advances_only_after_perform_act_succeeds(monkeypatch):
    """P6 必改：act_seq 只在 perform_act 成功后才推进 —— 失败重试用同一个 per_act_id。

    bug：旧实现 ``act_seq += 1`` 发生在 perform_act **之前**。act_tool 叠 @tool_error
    会吞掉 perform_act 抛的错（把错误 outcome 喂回模型让它重试），所以"perform_act
    写库成功了但返回链路抛错（DB commit 后 ack 丢失 / 网络抖动）"这种场景下：
    模型重试 act → act_seq 又 +1 → 用新序号派生**新的** per_act_id → 同一件 act 落两条、
    world 推演两次。根因是序号绑定了"调用尝试次数"而非"已确认落库的 act slot"。

    修法：用 act_seq+1 算 per_act_id、perform_act **成功后**才把 act_seq 推进到那个值。
    本测模拟：第一次 perform_act 抛错（写成功但 ack 丢 / 或纯写失败），第二次正常落库；
    模型重试同一意图（连调两次 act）。断言两次尝试用**同一个** per_act_id —— 这样
    world 端 (lane, act_id) durable 去重才能把它当同一件、只落一条。
    """
    seen_ids: list[str] = []
    calls = {"n": 0}

    async def flaky_perform_act(**kwargs):
        # 记下每次尝试用的 act_id（不论成败），断言重试用同一个。
        seen_ids.append(kwargs["act_id"])
        calls["n"] += 1
        if calls["n"] == 1:
            # 第一次：模拟写库成功但返回链路抛错（ack 丢 / 网络抖动）。
            raise RuntimeError("ack lost after commit")
        # 第二次（模型重试）：正常落库。

    monkeypatch.setattr(lt, "perform_act", flaky_perform_act)

    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="derived-round-id",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )

    # 第一次尝试：perform_act 抛错 → @tool_error 吞错、返回结构化 outcome（不抛）。
    out1 = await tools["act"].invoke({"description": "我去厨房煮咖啡"})
    assert isinstance(out1, dict) and out1["kind"] == "tool_error"

    # 模型重试同一意图：再调一次 act。
    out2 = await tools["act"].invoke({"description": "我去厨房煮咖啡"})
    assert out2 == "已经做了"

    # 命门断言：两次尝试用的是同一个 per_act_id（失败没消耗序号）。
    assert len(seen_ids) == 2
    assert seen_ids[0] == seen_ids[1], (
        f"失败重试必须用同一个 per_act_id（durable 去重靠它只落一条），"
        f"实得 {seen_ids}"
    )


@pytest.mark.asyncio
async def test_act_seq_advances_per_act_when_perform_act_succeeds(monkeypatch):
    """成功路径下 act_seq 正常推进 —— 同轮连做两件各自唯一 id（修复不破坏多件语义）。

    修了失败重试不消耗序号后，必须保证成功路径仍然每件 act 推进序号、同轮不同件
    得不同 id（否则就退化成"一轮只生效一件"被去重吞）。
    """
    seen_ids: list[str] = []

    async def ok_perform_act(**kwargs):
        seen_ids.append(kwargs["act_id"])

    monkeypatch.setattr(lt, "perform_act", ok_perform_act)

    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="derived-round-id",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    await tools["act"].invoke({"description": "我去厨房煮咖啡"})
    await tools["act"].invoke({"description": "顺便给千凪带一杯"})

    assert len(seen_ids) == 2
    assert seen_ids[0] != seen_ids[1], (
        f"成功路径下同轮两件 act 应各自唯一 id（序号正常推进），实得 {seen_ids}"
    )


@pytest.mark.asyncio
async def test_act_per_act_id_is_uuid_shaped_wire_contract(stub_handlers):
    """per-act id 必须保持 UUID 形（只含 hex + ``-``）—— world 端 marker 解析的硬契约。

    world engine 的 round marker 用 ``|`` 分隔、``rpartition("|")`` 解析回 act_id
    （app/world/engine.py），文档明写"act_id 是 UUID 串（只有 hex + ``-``、不含
    ``|`` ``]``）"。改派生格式不能引入 ``|`` / ``]`` / ``:`` 等字符，否则炸 world
    解析。本测钉死 per-act id 仍是合法 UUID。
    """
    import uuid as _uuid

    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="derived-round-id",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    await tools["act"].invoke({"description": "我去厨房煮咖啡"})
    await tools["act"].invoke({"description": "顺便给千凪带一杯"})

    for a in stub_handlers["acts"]:
        # 能被 UUID 解析回 = 只含 hex + "-"，绝无 | ] : 等会炸 world 解析的字符
        parsed = _uuid.UUID(a["act_id"])
        assert str(parsed) == a["act_id"]


def test_act_description_guides_toward_low_action():
    """act 措辞软引导降频（spec 决策 5 内容判断那层）。

    多数时候她只是经历这一刻（更新状态就够），只有做的事会在自己之外留下痕迹、被
    够得着的人感知到时才 act。这是软内容引导（赤尾宪法：不加 if 强制），所以只能断言指令文本已改、不能
    断言行为。也验证旧"申请 / 裁决"语义已不在文案里——act 是"你做了"不是"你请求"。
    """
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    desc = tools["act"].definition.description
    # 新判据软引导：act 是"会在你之外的世界留下痕迹、被够得着的人感知到"的事
    assert "留下痕迹" in desc
    assert ("经历这一刻" in desc) or ("多数时候" in desc)
    # act 是"你做了"不是"申请 / 等批准"——旧裁决语义不该残留
    assert "裁决" not in desc
    assert "批准" not in desc
    # P6 修复：删掉"一轮只生效一件事"的硬限制措辞（一轮做几件由她自己定）
    assert "一轮只生效一件" not in desc
    assert "只能做一件" not in desc


# ---------------------------------------------------------------------------
# schedule —— life 自排工具（阶段 1B Task 2，照搬 world sleep 的 round-scoped 覆盖）。
# ---------------------------------------------------------------------------


def test_build_life_tools_includes_schedule_when_slot_given():
    """传 self_wake 容器时多一件 schedule（共 update_life_state + act + chat + schedule）。"""
    slot: dict = {}
    tools = lt.build_life_tools(
        lane="coe-t3",
        persona_id="akao",
        act_id="a-1",
        observed_at="2026-06-03T12:30:00+00:00",
        self_wake=slot,
    )
    by_name = _tools_by_name(tools)
    assert set(by_name) == {"update_life_state", "act", "chat", "schedule"}


def test_schedule_tool_hides_mechanism_only_seconds_exposed():
    """schedule 只对模型暴露 seconds 业务参数，不暴露 lane / persona_id。"""
    slot: dict = {}
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
            self_wake=slot,
        )
    )
    props = set(tools["schedule"].definition.parameters["properties"])
    assert props == {"seconds"}


@pytest.mark.asyncio
async def test_schedule_within_limit_records_pending_self_wake():
    """schedule 合法 → 把待办 self-wake 记进 round-scoped slot（不直接 emit）。"""
    slot: dict = {}
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
            self_wake=slot,
        )
    )
    await tools["schedule"].invoke({"seconds": 1800})
    assert slot["delay_ms"] == 1_800_000


@pytest.mark.asyncio
async def test_schedule_multi_in_round_last_wins_no_accumulate():
    """一轮内多次 schedule 不累积 —— 最后一次为准（唤醒风暴命门，照搬 world sleep）。"""
    slot: dict = {}
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
            self_wake=slot,
        )
    )
    await tools["schedule"].invoke({"seconds": 300})
    await tools["schedule"].invoke({"seconds": 600})
    await tools["schedule"].invoke({"seconds": 900})
    assert slot == {"delay_ms": 900_000}, "只留最后一次（覆盖而非追加）"


@pytest.mark.asyncio
async def test_schedule_at_min_floor_allowed():
    """schedule == 下限 → 合法（边界含下限）。"""
    slot: dict = {}
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00", self_wake=slot,
        )
    )
    await tools["schedule"].invoke({"seconds": lt.LIFE_SCHEDULE_MIN_SECONDS})
    assert slot["delay_ms"] == lt.LIFE_SCHEDULE_MIN_SECONDS * 1000


@pytest.mark.asyncio
async def test_schedule_at_max_ceiling_allowed():
    """schedule == 上限 → 合法（边界含上限，上限放宽到能睡整觉）。"""
    slot: dict = {}
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00", self_wake=slot,
        )
    )
    await tools["schedule"].invoke({"seconds": lt.LIFE_SCHEDULE_MAX_SECONDS})
    assert slot["delay_ms"] == lt.LIFE_SCHEDULE_MAX_SECONDS * 1000


@pytest.mark.asyncio
async def test_schedule_under_floor_errors_no_pending():
    """schedule < 下限 → 返回错误喂回模型重调（不静默夹）、不留待办。"""
    slot: dict = {}
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00", self_wake=slot,
        )
    )
    out = await tools["schedule"].invoke({"seconds": lt.LIFE_SCHEDULE_MIN_SECONDS - 1})
    assert isinstance(out, dict)
    assert out["kind"] == "tool_error"
    assert slot == {}, "超下限不该留待办 self-wake"


@pytest.mark.asyncio
async def test_schedule_over_ceiling_errors_no_pending():
    """schedule > 上限 → 返回错误喂回模型重调（不静默夹）、不留待办。"""
    slot: dict = {}
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00", self_wake=slot,
        )
    )
    out = await tools["schedule"].invoke({"seconds": lt.LIFE_SCHEDULE_MAX_SECONDS + 1})
    assert isinstance(out, dict)
    assert out["kind"] == "tool_error"
    assert slot == {}


def test_schedule_ceiling_allows_full_night_sleep():
    """上限放宽到能睡整觉（≥ 8h）——夜里一觉到天亮（spec 决策 3）。"""
    assert lt.LIFE_SCHEDULE_MAX_SECONDS >= 8 * 3600
    # 下限防排太密，但不至于神经质每分钟一轮
    assert lt.LIFE_SCHEDULE_MIN_SECONDS >= 60


def test_schedule_description_mentions_self_wake():
    """schedule docstring 说清"排过多久再醒来继续过日子"（给模型的语义）。"""
    slot: dict = {}
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00", self_wake=slot,
        )
    )
    desc = tools["schedule"].definition.description
    assert "醒" in desc, "schedule 文案要让模型知道这是排下次醒来"


# ---------------------------------------------------------------------------
# fire_life_self_wake —— 收口 emit + 落 next_wake_at（对称 world fire_self_wake）。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_life_self_wake_writes_next_wake_at_and_carries_target(monkeypatch):
    """fire_life_self_wake：目标时刻 = 现实 now + delay，写进 LifeState.next_wake_at，
    且 emit 的 self LifeWakeTick 携带这个目标时刻（target_wake_at）。"""
    from datetime import datetime, timedelta

    from app.infra import cst_time

    delayed: list[dict] = []
    set_calls: list[dict] = []

    async def fake_emit_delayed(data, *, delay_ms, durability="durable"):
        delayed.append({"data": data, "delay_ms": delay_ms})

    async def fake_set(*, lane, persona_id, next_wake_at):
        set_calls.append(
            {"lane": lane, "persona_id": persona_id, "next_wake_at": next_wake_at}
        )

    monkeypatch.setattr(lt, "emit_delayed", fake_emit_delayed)
    monkeypatch.setattr(lt, "set_life_next_wake_at", fake_set)

    before = cst_time.now_cst()
    fired = await lt.fire_life_self_wake(
        lane="coe-t3", persona_id="akao", self_wake={"delay_ms": 1800_000}
    )
    after = cst_time.now_cst()

    assert fired is True
    assert len(delayed) == 1
    tick = delayed[0]["data"]
    assert tick.reason == "self"
    assert tick.lane == "coe-t3"
    assert tick.persona_id == "akao"
    assert tick.target_wake_at, "self LifeWakeTick 必须携带目标唤醒时刻"
    target = datetime.fromisoformat(tick.target_wake_at)
    assert before + timedelta(seconds=1800) <= target <= after + timedelta(seconds=1800)
    # 写进 state 的 next_wake_at 与 tick 携带目标一致（stale 判定靠相等）
    assert len(set_calls) == 1
    assert set_calls[0]["lane"] == "coe-t3"
    assert set_calls[0]["persona_id"] == "akao"
    assert set_calls[0]["next_wake_at"] == tick.target_wake_at


@pytest.mark.asyncio
async def test_fire_life_self_wake_emit_failure_logs_warning_not_silent(
    monkeypatch, caplog
):
    """必改 4（可观测）：emit_delayed 抛错时 log warning（带 lane/persona/target），别静默吞。

    life 没保底心跳，emit_delayed 失败会留一个未来 wake state（next_wake_at 已写）但没
    实际唤醒（机械漏投）。完整恢复（watchdog）是 Non-goal，但至少不静默：失败要 log
    warning 带 lane/persona/target。本测：set 已成功写、emit_delayed 抛错 → 有 warning
    log（不静默吞），且不把异常往上炸（已 durable 落地的这一轮不该被漏投拖垮）。
    """
    import logging

    async def fake_set(*, lane, persona_id, next_wake_at):
        return None

    async def boom_emit_delayed(data, *, delay_ms, durability="durable"):
        raise RuntimeError("broker down")

    monkeypatch.setattr(lt, "set_life_next_wake_at", fake_set)
    monkeypatch.setattr(lt, "emit_delayed", boom_emit_delayed)

    with caplog.at_level(logging.WARNING):
        # emit_delayed 抛错不该往上炸（已 durable 落地的这一轮收口不被漏投拖垮）
        await lt.fire_life_self_wake(
            lane="coe-t3", persona_id="akao", self_wake={"delay_ms": 1800_000}
        )

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "emit_delayed 失败必须 log warning（不静默吞）"
    blob = " ".join(r.getMessage() for r in warnings)
    assert "coe-t3" in blob, "warning 要带 lane"
    assert "akao" in blob, "warning 要带 persona"


@pytest.mark.asyncio
async def test_fire_life_self_wake_no_pending_does_not_write_or_emit(monkeypatch):
    """没调 schedule（空待办）→ 不写 next_wake_at、不 emit（靠 world notify 起头兜底）。"""
    delayed: list = []
    set_calls: list = []

    async def fake_emit_delayed(data, *, delay_ms, durability="durable"):
        delayed.append(data)

    async def fake_set(*, lane, persona_id, next_wake_at):
        set_calls.append(next_wake_at)

    monkeypatch.setattr(lt, "emit_delayed", fake_emit_delayed)
    monkeypatch.setattr(lt, "set_life_next_wake_at", fake_set)

    fired = await lt.fire_life_self_wake(lane="coe-t3", persona_id="akao", self_wake={})

    assert fired is False
    assert delayed == []
    assert set_calls == []


@pytest.mark.asyncio
async def test_tool_failure_returns_outcome_not_raise(monkeypatch):
    """单个工具自身抛错被吞掉、喂回模型让它自纠，不炸整轮（spec 决策 3）。"""

    async def boom(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(lt, "save_life_state", boom)

    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    out = await tools["update_life_state"].invoke(
        {"current_state": "x", "response_mood": "y", "activity_type": "z"}
    )
    # @tool_error 把失败变成结构化 outcome dict，不抛
    assert isinstance(out, dict)
    assert out["kind"] == "tool_error"


# ---------------------------------------------------------------------------
# chat —— 角色直连对话工具（1C Task 3）。
#
# 「说话」从 act 里分出来：act 只管"做了一件事"，chat 管"对谁说了一句话"。chat 走
# 双轨：原话直投收件人信箱（speech event、不经 world），同时给 world 一条不含原话的
# 低成本元信息（复用 act 流）。收件人取自固定通讯录（三姐妹互为固定联系人）、由角色
# 自选，不是在场名单、不抠自然语言。
# ---------------------------------------------------------------------------


def test_build_life_tools_includes_chat():
    """工具集多一件 chat（与 update_life_state / act / schedule 并列）。"""
    slot: dict = {}
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
            self_wake=slot,
        )
    )
    assert "chat" in tools
    assert isinstance(tools["chat"], Tool)


def test_chat_tool_schema_hides_mechanism_only_recipient_and_content():
    """模型只看见 recipient + content 业务参数，看不见 lane / persona_id / act_id。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    props = set(tools["chat"].definition.parameters["properties"])
    assert props == {"recipient", "content"}


@pytest.mark.asyncio
async def test_chat_delivers_original_speech_to_recipient_inbox(stub_handlers):
    """① 直投链路：chat(收件人, 原话) → 原话作为 speech event 直投收件人信箱。

    原话（content）原样进收件人信箱的 summary，kind=speech、source=说话者 persona_id；
    收件人是工具参数给的（akao 对 ayana 说），不是 world 路由的。
    """
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    await tools["chat"].invoke(
        {"recipient": "ayana", "content": "绫奈姐姐你在做什么好吃的呀"}
    )

    assert len(stub_handlers["delivered"]) == 1
    d = stub_handlers["delivered"][0]
    assert d["lane"] == "coe-t3"
    assert d["persona_id"] == "ayana", "原话直投收件人信箱（不是说话者自己）"
    assert d["summary"] == "绫奈姐姐你在做什么好吃的呀", "原话原样进收件人信箱"
    assert d["kind"] == lt_speech_kind(), "speech 是独立 kind、不混进 ambient/surroundings"
    assert d["source"] == "akao", "source 是说话者 persona_id（不是 world）"
    assert d["occurred_at"] == "2026-06-03T12:30:00+00:00"


@pytest.mark.asyncio
async def test_chat_gives_world_meta_without_original_speech(stub_handlers):
    """② world 低成本感知链路：chat 给 world 的是不含原话的元信息（承重红线）。

    world 凭这条元信息反映「有人在交谈」的氛围，但绝不读对话原话。复用 act 流
    （perform_act）把元信息送给 world —— 关键断言：perform_act 的 description 里
    **不含**对话原话逐句内容，只有"和谁交谈"这类事实。
    """
    secret_line = "绫奈姐姐你在做什么好吃的呀这句是绝密原话"
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    await tools["chat"].invoke({"recipient": "ayana", "content": secret_line})

    # world 凭 act 流感知到"有一场对话在发生"——必须恰好一条元信息进 world。
    assert len(stub_handlers["acts"]) == 1, "chat 要给 world 一条元信息（复用 act 流）"
    meta = stub_handlers["acts"][0]
    assert meta["lane"] == "coe-t3"
    assert meta["persona_id"] == "akao", "元信息记在说话者名下"
    # 承重红线：world 拿到的 description 绝不含对话原话逐句内容。
    assert secret_line not in meta["description"], (
        "world 绝不读对话原话——给 world 的元信息里不能出现逐句原话内容"
    )
    # 元信息仍是"有交谈"的事实（提到了交谈对象 ayana，让 world 能反映氛围）。
    assert "ayana" in meta["description"], "元信息要让 world 知道和谁在交谈（反映氛围）"


@pytest.mark.asyncio
async def test_chat_does_not_route_through_world_recipient_chosen_by_caller(stub_handlers):
    """收件人是工具参数自选、原话直投，world 不参与对话路由（决策 2）。

    给"不在身边"的人 chat 也照样直投其信箱（当面与发消息统一、不分支判在不在场）。
    系统不判在场、不建在场名单，只把角色自选的 recipient 落投递。
    """
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    # chinagi 此刻"在学校"（不在身边）—— 不分支判在场，照样直投。
    await tools["chat"].invoke({"recipient": "chinagi", "content": "姐姐放学早点回"})

    assert len(stub_handlers["delivered"]) == 1
    assert stub_handlers["delivered"][0]["persona_id"] == "chinagi", (
        "不在身边的人也直投其信箱（异步送达），不分支判在不在场"
    )


@pytest.mark.asyncio
async def test_chat_multiple_in_round_independent_idempotency_keys(stub_handlers):
    """一轮多次 chat 各有独立幂等键（直投 event_id 与元信息 act_id 都各自唯一）。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="derived-round-id",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    await tools["chat"].invoke({"recipient": "ayana", "content": "你在干嘛"})
    await tools["chat"].invoke({"recipient": "chinagi", "content": "放学没"})

    # 两条直投各自唯一 event_id
    event_ids = [d["event_id"] for d in stub_handlers["delivered"]]
    assert len(set(event_ids)) == 2, f"一轮两次 chat 直投应各唯一 event_id，实得 {event_ids}"
    # 两条 world 元信息各自唯一 act_id
    act_ids = [a["act_id"] for a in stub_handlers["acts"]]
    assert len(set(act_ids)) == 2, f"一轮两次 chat 元信息应各唯一 act_id，实得 {act_ids}"


@pytest.mark.asyncio
async def test_chat_stable_under_round_replay(stub_handlers):
    """整轮重投同一批 chat（同 base act_id）→ 同序 chat 得同一直投 event_id + 元信息 act_id。

    幂等命门：base act_id 整轮重投稳定，per-chat 键从 (base, chat 序号) 纯函数派生 ——
    重投同序 chat 得同一 event_id / act_id，deliver_event / perform_act 按自然键去重，
    不重复投递、不重复让 world 推演。
    """
    tools_a = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="same-base",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    await tools_a["chat"].invoke({"recipient": "ayana", "content": "你在干嘛"})
    await tools_a["chat"].invoke({"recipient": "chinagi", "content": "放学没"})
    first_event_ids = [d["event_id"] for d in stub_handlers["delivered"]]
    first_act_ids = [a["act_id"] for a in stub_handlers["acts"]]

    stub_handlers["delivered"].clear()
    stub_handlers["acts"].clear()

    tools_b = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="same-base",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    await tools_b["chat"].invoke({"recipient": "ayana", "content": "你在干嘛"})
    await tools_b["chat"].invoke({"recipient": "chinagi", "content": "放学没"})
    replay_event_ids = [d["event_id"] for d in stub_handlers["delivered"]]
    replay_act_ids = [a["act_id"] for a in stub_handlers["acts"]]

    assert replay_event_ids == first_event_ids, "重投同序 chat 直投 event_id 必须稳定（幂等）"
    assert replay_act_ids == first_act_ids, "重投同序 chat 元信息 act_id 必须稳定（幂等）"


@pytest.mark.asyncio
async def test_chat_seq_advances_only_after_delivery_succeeds(monkeypatch):
    """幂等命门：chat 键只在落库成功后才推进 —— 失败重试用同一对键（对称 act_seq）。

    deliver_event 第一次抛错（写成功但 ack 丢 / 或纯写失败）、模型重试同一意图：两次
    必须用同一个直投 event_id + 同一个元信息 act_id，下游 durable 去重才能只落一条。
    """
    seen_event_ids: list[str] = []
    seen_act_ids: list[str] = []
    calls = {"n": 0}

    async def flaky_deliver_event(**kwargs):
        seen_event_ids.append(kwargs["event_id"])
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("ack lost after commit")
        return 1

    async def ok_perform_act(**kwargs):
        seen_act_ids.append(kwargs["act_id"])

    monkeypatch.setattr(lt, "deliver_event", flaky_deliver_event)
    monkeypatch.setattr(lt, "perform_act", ok_perform_act)

    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="derived-round-id",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )

    out1 = await tools["chat"].invoke({"recipient": "ayana", "content": "你在干嘛"})
    assert isinstance(out1, dict) and out1["kind"] == "tool_error"

    out2 = await tools["chat"].invoke({"recipient": "ayana", "content": "你在干嘛"})
    assert isinstance(out2, str) and out2

    assert len(seen_event_ids) == 2
    assert seen_event_ids[0] == seen_event_ids[1], (
        f"失败重试必须用同一个直投 event_id（去重靠它只落一条），实得 {seen_event_ids}"
    )


@pytest.mark.asyncio
async def test_chat_second_track_failure_retry_dedups_speech_adds_meta(monkeypatch):
    """第二轨（world meta）失败重试：speech 按 event_id 去重不重复投、meta 补上（codex 建议 2）。

    chat 是双轨：第一轨 deliver_event 直投收件人 speech，第二轨 perform_act 给 world
    一条不含原话的 meta。原有测试只覆盖第一轨失败重试。这里补第二轨场景：

      * 第一次调：第一轨 speech 投递成功，第二轨 perform_act 抛错 → @tool_error 吞掉、
        把错误 outcome 喂回模型。
      * 模型重试：因为 chat_seq 只在两轨都成功后才推进，重试用同一对幂等键 ——
          - 第一轨 speech 用同一 event_id 再投一次，deliver_event 按 (lane, persona,
            event_id) 自然键去重，**不重复落第二条**；
          - 第二轨 perform_act 这次成功，meta 补上。
      * 最终：speech 各落一条（去重）、meta 各落一条（补上），weak-consistency 收敛。
    """
    delivered: list[dict] = []
    acts: list[dict] = []
    perform_calls = {"n": 0}

    async def dedup_deliver_event(**kwargs):
        # 模拟 deliver_event 的自然键去重：同 (lane, persona, event_id) 只落一条。
        key = (kwargs["lane"], kwargs["persona_id"], kwargs["event_id"])
        if key not in {(d["lane"], d["persona_id"], d["event_id"]) for d in delivered}:
            delivered.append(kwargs)
        return 1

    async def flaky_perform_act(**kwargs):
        perform_calls["n"] += 1
        if perform_calls["n"] == 1:
            raise RuntimeError("world meta 落库瞬时失败")
        acts.append(kwargs)

    monkeypatch.setattr(lt, "deliver_event", dedup_deliver_event)
    monkeypatch.setattr(lt, "perform_act", flaky_perform_act)

    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="derived-round-id",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )

    # 第一次：speech 投成功、meta 落库抛错 → @tool_error 吞、返回错误 outcome。
    out1 = await tools["chat"].invoke({"recipient": "ayana", "content": "你在干嘛"})
    assert isinstance(out1, dict) and out1["kind"] == "tool_error", (
        "第二轨失败应被 @tool_error 吞成错误 outcome 喂回模型"
    )

    # 模型重试同一意图（chat_seq 未推进 → 同一对幂等键）。
    out2 = await tools["chat"].invoke({"recipient": "ayana", "content": "你在干嘛"})
    assert isinstance(out2, str) and out2, "重试成功应返回正常确认文本"

    # speech 第一轨调了两次（首次成功 + 重试再调），但按 event_id 去重只落一条。
    assert len(delivered) == 1, (
        f"speech 按 event_id 去重应只落一条（first-landed-wins），实得 {len(delivered)}"
    )
    assert delivered[0]["summary"] == "你在干嘛"
    # meta 第二轨首次失败、重试补上，最终恰好一条。
    assert len(acts) == 1, f"world meta 重试后应补上、恰好一条，实得 {len(acts)}"
    assert "ayana" in acts[0]["description"], "meta 是不含原话的「和谁交谈」事实"
    assert "你在干嘛" not in acts[0]["description"], "meta 绝不含对话原话（承重红线）"


@pytest.mark.asyncio
async def test_chat_unknown_recipient_errors_no_delivery(stub_handlers):
    """收件人不在固定通讯录 → 报错喂回模型重调（机制护栏），不投递、不给 world 元信息。

    通讯录是稳定身份 id 集合（三姐妹互为固定联系人）。这不是"判在不在场"，是"这个
    身份 id 存不存在"的机制校验（对称 schedule 超限报错喂回）。
    """
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    out = await tools["chat"].invoke({"recipient": "陌生人", "content": "你好"})
    assert isinstance(out, dict)
    assert out["kind"] == "tool_error"
    assert stub_handlers["delivered"] == [], "未知收件人不投递"
    assert stub_handlers["acts"] == [], "未知收件人不给 world 元信息"


@pytest.mark.asyncio
async def test_chat_to_self_errors_no_delivery(stub_handlers):
    """收件人 == 说话者自己 → 报错喂回模型重调，不投递、不给 world 元信息（codex 建议 1）。

    SISTERS_CONTACTS 含说话者自己，原实现允许「akao 对 akao 说话」、还会给 world 生成
    「我和 akao 说了几句话」这种自言自语的怪 meta。改成对称「不在通讯录」的报错：
    recipient == persona_id 时报错喂回模型让它重选收件人，既不投 speech、也不给 world
    meta（不污染 world 客观叙事）。
    """
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    out = await tools["chat"].invoke({"recipient": "akao", "content": "我在自言自语"})
    assert isinstance(out, dict)
    assert out["kind"] == "tool_error", "对自己说话应报错喂回模型重调"
    assert stub_handlers["delivered"] == [], "对自己说话不投 speech"
    assert stub_handlers["acts"] == [], "对自己说话不给 world meta（不污染客观叙事）"


@pytest.mark.asyncio
async def test_chat_meta_act_id_is_uuid_shaped_wire_contract(stub_handlers):
    """chat 的 world 元信息 act_id 必须保持 UUID 形 —— world marker 解析的硬契约。

    元信息复用 act 流落 ActPerformed，world 醒来 pull 它、把 act_id 编进 round marker
    （``|`` 分隔、``rpartition("|")`` 解析回，见 app/world/engine.py）。所以 chat 的
    meta act_id 不能引入 ``|`` / ``]`` / ``:`` 等字符，否则炸 world 解析。speech 直投
    event_id 同理保 UUID 形（虽不进 world marker，但保持一致稳健）。
    """
    import uuid as _uuid

    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="derived-round-id",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    await tools["chat"].invoke({"recipient": "ayana", "content": "你在干嘛"})
    await tools["chat"].invoke({"recipient": "chinagi", "content": "放学没"})

    for a in stub_handlers["acts"]:
        parsed = _uuid.UUID(a["act_id"])
        assert str(parsed) == a["act_id"], f"meta act_id 必须是合法 UUID 形：{a['act_id']}"
    for d in stub_handlers["delivered"]:
        parsed = _uuid.UUID(d["event_id"])
        assert str(parsed) == d["event_id"], f"speech event_id 应是合法 UUID 形：{d['event_id']}"


def test_chat_description_guides_self_chosen_recipient_and_async_ok():
    """chat 文案：读懂周遭后自选收件人说话、当面和发消息都用它、对方可能收不到是正常的。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    desc = tools["chat"].definition.description
    # 收件人由角色自选（读懂周遭后决定对谁说）
    assert "对谁" in desc or "收件人" in desc or "联系人" in desc
    # 当面与发消息统一
    assert "当面" in desc and "发消息" in desc
    # 对方可能不在身边、收不到是正常的（信息差天然）
    assert ("不在身边" in desc) or ("不在" in desc)


def test_act_description_no_longer_carries_speech():
    """act 文案分出说话后：act 只管"做了一件事"，说话引导去 chat（不再让 act 承载说话）。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    desc = tools["act"].definition.description
    # 说话改走 chat —— act 文案应引导"说话用 chat"
    assert "chat" in desc, "act 文案应把说话引导到 chat 工具"


def lt_speech_kind() -> str:
    """speech event 的 kind 常量（让测试与实现共用同一来源，不硬编码字面量）。"""
    return lt.EVENT_KIND_SPEECH
