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
    state: dict = {
        "saved": [],
        "acts": [],
        "delivered": [],
        "noted": [],
        "edited": [],
        "listed": [],
    }

    async def fake_save_life_state(**kwargs):
        state["saved"].append(kwargs)

    async def fake_perform_act(**kwargs):
        state["acts"].append(kwargs)

    async def fake_deliver_event(**kwargs):
        state["delivered"].append(kwargs)
        return 1

    async def fake_note_entry(**kwargs):
        state["noted"].append(kwargs)

    async def fake_update_entry(**kwargs):
        state["edited"].append(kwargs)

    async def fake_list_notebook_entries(**kwargs):
        state["listed"].append(kwargs)
        # 返回测试预置的本子条目（默认空本子）。
        return state.get("notebook_rows", [])

    monkeypatch.setattr(lt, "save_life_state", fake_save_life_state)
    monkeypatch.setattr(lt, "perform_act", fake_perform_act)
    monkeypatch.setattr(lt, "deliver_event", fake_deliver_event)
    monkeypatch.setattr(lt, "note_entry", fake_note_entry)
    monkeypatch.setattr(lt, "update_entry", fake_update_entry)
    monkeypatch.setattr(lt, "list_notebook_entries", fake_list_notebook_entries)
    return state


def _tools_by_name(tools: list[Tool]) -> dict[str, Tool]:
    return {t.name: t for t in tools}


def test_build_life_tools_returns_base_tools():
    """不给 self_wake 时工具集是 update_life_state + act + chat + look_up_contact + send_message + 本子三件 + look_up + browse_feed（都是常驻基础工具）。"""
    tools = lt.build_life_tools(
        lane="coe-t3",
        persona_id="akao",
        act_id="a-1",
        observed_at="2026-06-03T12:30:00+00:00",
    )
    by_name = _tools_by_name(tools)
    assert set(by_name) == {
        "update_life_state",
        "act",
        "chat",
        "look_up_contact",
        "send_message",
        "note",
        "edit_note",
        "read_notebook",
        "look_up",
        "browse_feed",
    }
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
# bug 2 / 3：领域层校验经活轮工具喂回模型（不用 stub_handlers，走真 update_entry /
# note_entry 的 fail-fast 校验——脏 status / 脏 remind_at 在 DB 之前就抛 ValueError，
# @tool_error 把它兜成结构化 outcome 喂回模型重填，绝不静默写脏 / 不挂提醒）。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_note_invalid_status_returns_tool_error_no_pending():
    """bug 2：edit_note 传拼错的 status（complete）→ @tool_error 兜成 outcome 喂回模型，
    不静默写脏、不挂提醒（校验在 DB 之前 fail-fast，所以无需真 PG）。"""
    reminders: dict = {}
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-13T12:30:00+08:00",
            schedule_reminders=reminders,
        )
    )
    out = await tools["edit_note"].invoke(
        {"entry_id": "n1", "status": "complete"}  # 拼错：合法是 done
    )
    assert isinstance(out, dict) and out["kind"] == "tool_error"
    assert reminders == {}, "校验失败不该留待挂提醒"


@pytest.mark.asyncio
async def test_edit_note_invalid_remind_at_returns_tool_error_no_pending():
    """bug 3：edit_note 传脏 remind_at（解析不出 ISO）→ @tool_error 兜成 outcome 喂回
    模型，不静默写脏、不挂提醒（脏串挂上会被夹成立即错误提醒）。"""
    reminders: dict = {}
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-13T12:30:00+08:00",
            schedule_reminders=reminders,
        )
    )
    out = await tools["edit_note"].invoke(
        {"entry_id": "n1", "remind_at": "下午三点"}  # 脏串
    )
    assert isinstance(out, dict) and out["kind"] == "tool_error"
    assert reminders == {}, "脏 remind_at 校验失败不该留待挂提醒"


@pytest.mark.asyncio
async def test_note_invalid_remind_at_returns_tool_error_no_pending():
    """bug 3（首写）：note 带脏 remind_at → @tool_error 兜成 outcome 喂回模型，不静默
    落库、不挂提醒。"""
    reminders: dict = {}
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-13T12:30:00+08:00",
            schedule_reminders=reminders,
        )
    )
    out = await tools["note"].invoke(
        {"content": "排个日程", "remind_at": "明天早上"}  # 脏串
    )
    assert isinstance(out, dict) and out["kind"] == "tool_error"
    assert reminders == {}, "脏 remind_at 校验失败不该留待挂提醒"


# ---------------------------------------------------------------------------
# 日程到点提醒（备忘录 & 日程 第三块）—— note / edit_note 带 remind_at 时把「待挂的
# 日程提醒」记进 round-scoped 容器，engine 收口 fire_schedule_reminders 每条各 emit
# 一条 ScheduleReminderTick（每条日程各挂各的）。这是日程那条（保留），区别于已删的
# 自设闹钟（next_wake_at / schedule）。
# ---------------------------------------------------------------------------


def _tools_with_reminders(reminders, stub_handlers):
    """造带 schedule_reminders 容器的工具集（日程那条，自设闹钟已删、无 self_wake）。"""
    return _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="base-act-id",
            observed_at="2026-06-13T12:30:00+08:00",
            schedule_reminders=reminders,
        )
    )


@pytest.mark.asyncio
async def test_note_with_remind_at_records_pending_reminder(stub_handlers):
    """note 带 remind_at（排了日程）→ 把待挂提醒记进 round-scoped 容器（按 entry_id）。"""
    import uuid

    reminders: dict = {}
    tools = _tools_with_reminders(reminders, stub_handlers)

    await tools["note"].invoke(
        {"content": "三点陪我妹", "remind_at": "2026-06-13T15:00:00+08:00"}
    )

    # 第一件 note 的 entry_id（next_seq=1，note: 前缀派生）
    entry_id = str(uuid.uuid5(uuid.NAMESPACE_OID, "base-act-id:note:1"))
    assert reminders == {entry_id: "2026-06-13T15:00:00+08:00"}, (
        "排了日程要记一条待挂提醒（entry_id → remind_at）"
    )


@pytest.mark.asyncio
async def test_note_without_remind_at_records_no_reminder(stub_handlers):
    """note 不带时间（纯备忘）→ 不记任何待挂提醒（备忘不到点提醒）。"""
    reminders: dict = {}
    tools = _tools_with_reminders(reminders, stub_handlers)

    await tools["note"].invoke({"content": "想看那部动画"})

    assert reminders == {}, "纯备忘不挂日程提醒"


@pytest.mark.asyncio
async def test_edit_set_remind_at_records_pending_reminder(stub_handlers):
    """edit_note 给条目补 / 改时间 → 记一条该 entry 的待挂提醒（备忘补成日程 / 改期）。"""
    reminders: dict = {}
    tools = _tools_with_reminders(reminders, stub_handlers)

    await tools["edit_note"].invoke(
        {"entry_id": "n-existing", "remind_at": "2026-06-13T16:00:00+08:00"}
    )

    assert reminders == {"n-existing": "2026-06-13T16:00:00+08:00"}, (
        "补 / 改时间要给这条记一条待挂提醒"
    )


@pytest.mark.asyncio
async def test_edit_clear_remind_at_records_no_pending(stub_handlers):
    """edit_note 撤掉时间（日程变回备忘）→ 记成「这条不挂提醒」（None），不留旧提醒。"""
    reminders: dict = {}
    tools = _tools_with_reminders(reminders, stub_handlers)

    await tools["edit_note"].invoke({"entry_id": "n-existing", "remind_at": ""})

    # 容器里这条记成 None（覆盖同轮可能先 set 过的），fire 时不为它挂提醒。
    assert reminders.get("n-existing") is None, "撤时间后这条不该挂新提醒"


@pytest.mark.asyncio
async def test_edit_set_then_clear_same_entry_in_round_last_wins(stub_handlers):
    """同轮先补时间再撤掉同一条 → 容器最后一次为准（None），不挂提醒（覆盖而非追加）。"""
    reminders: dict = {}
    tools = _tools_with_reminders(reminders, stub_handlers)

    await tools["edit_note"].invoke(
        {"entry_id": "n1", "remind_at": "2026-06-13T15:00:00+08:00"}
    )
    await tools["edit_note"].invoke({"entry_id": "n1", "remind_at": ""})

    assert reminders.get("n1") is None, "同轮先 set 再 clear，最后一次为准（不挂）"


@pytest.mark.asyncio
async def test_edit_status_only_does_not_touch_reminders(stub_handlers):
    """edit_note 只改状态（划掉 / 标做了）不动时间 → 不记待挂提醒（时间没变）。"""
    reminders: dict = {}
    tools = _tools_with_reminders(reminders, stub_handlers)

    await tools["edit_note"].invoke({"entry_id": "n1", "status": "dropped"})

    assert reminders == {}, "只改状态、没动时间，不该新挂提醒"


def test_build_life_tools_without_reminders_slot_unchanged(stub_handlers):
    """不给 schedule_reminders 容器（旧调用方 / 契约测试）→ 工具集与行为向后兼容。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-13T12:30:00+08:00",
        )
    )
    assert "note" in tools and "edit_note" in tools, "本子工具常驻、不依赖 reminders 容器"


# --- fire_schedule_reminders —— 收口每条各 emit 一条 ScheduleReminderTick ---


@pytest.mark.asyncio
async def test_fire_schedule_reminders_emits_one_tick_per_entry(monkeypatch):
    """收口：容器里每条有 remind_at 的日程各 emit 一条携带 (entry_id, remind_at) 的 tick。"""
    from app.infra import cst_time
    from app.nodes.life_wake import ScheduleReminderTick

    emitted: list[dict] = []

    async def fake_emit_delayed(data, *, delay_ms, durability="durable"):
        emitted.append({"data": data, "delay_ms": delay_ms})

    monkeypatch.setattr(lt, "emit_delayed", fake_emit_delayed)

    future_a = (cst_time.now_cst() + __import__("datetime").timedelta(minutes=30)).isoformat()
    future_b = (cst_time.now_cst() + __import__("datetime").timedelta(hours=2)).isoformat()

    await lt.fire_schedule_reminders(
        lane="coe-t3",
        persona_id="akao",
        schedule_reminders={"n1": future_a, "n2": future_b},
    )

    assert len(emitted) == 2, "两条日程各 emit 一条 tick（每条各挂各的）"
    by_entry = {e["data"].entry_id: e for e in emitted}
    assert set(by_entry) == {"n1", "n2"}
    for entry_id, want in (("n1", future_a), ("n2", future_b)):
        tick = by_entry[entry_id]["data"]
        assert isinstance(tick, ScheduleReminderTick)
        assert tick.lane == "coe-t3"
        assert tick.persona_id == "akao"
        assert tick.remind_at == want, "tick 携带这条日程的 remind_at（gate 据它判 stale）"
        # delay_ms ≈ (remind_at - now)；future_a 约 30min、future_b 约 2h
        assert by_entry[entry_id]["delay_ms"] > 0


@pytest.mark.asyncio
async def test_fire_schedule_reminders_skips_cleared_entries(monkeypatch):
    """容器里值为 None 的条目（撤了时间）不 emit tick（不挂提醒）。"""
    emitted: list = []

    async def fake_emit_delayed(data, *, delay_ms, durability="durable"):
        emitted.append(data)

    monkeypatch.setattr(lt, "emit_delayed", fake_emit_delayed)

    future = (
        __import__("app.infra.cst_time", fromlist=["now_cst"]).now_cst()
        + __import__("datetime").timedelta(minutes=30)
    ).isoformat()
    await lt.fire_schedule_reminders(
        lane="coe-t3", persona_id="akao",
        schedule_reminders={"n1": future, "n2": None},
    )

    entry_ids = {d.entry_id for d in emitted}
    assert entry_ids == {"n1"}, "撤了时间（None）的条目不挂提醒"


@pytest.mark.asyncio
async def test_fire_schedule_reminders_past_time_fires_promptly(monkeypatch):
    """edge 3（排了已过去的时刻）：delay 夹到 0（立即），下一轮就提醒——不炸、不漏。"""
    from app.infra import cst_time

    emitted: list[dict] = []

    async def fake_emit_delayed(data, *, delay_ms, durability="durable"):
        emitted.append({"data": data, "delay_ms": delay_ms})

    monkeypatch.setattr(lt, "emit_delayed", fake_emit_delayed)

    past = (cst_time.now_cst() - __import__("datetime").timedelta(hours=1)).isoformat()
    await lt.fire_schedule_reminders(
        lane="coe-t3", persona_id="akao", schedule_reminders={"n1": past}
    )

    assert len(emitted) == 1
    assert emitted[0]["delay_ms"] == 0, "已过去的时刻 → delay 夹到 0、立即提醒（不负、不炸）"
    assert emitted[0]["data"].remind_at == past, "携带原 remind_at（gate 仍据它对账）"


@pytest.mark.asyncio
async def test_fire_schedule_reminders_unparseable_remind_at_fires_promptly(monkeypatch):
    """remind_at 脏串解析不出 → 不炸，夹到立即提醒（不静默吞这条日程）。"""
    emitted: list[dict] = []

    async def fake_emit_delayed(data, *, delay_ms, durability="durable"):
        emitted.append({"data": data, "delay_ms": delay_ms})

    monkeypatch.setattr(lt, "emit_delayed", fake_emit_delayed)

    await lt.fire_schedule_reminders(
        lane="coe-t3", persona_id="akao", schedule_reminders={"n1": "不是时间的脏串"}
    )

    assert len(emitted) == 1, "脏 remind_at 不该把这条日程静默吞掉"
    assert emitted[0]["delay_ms"] == 0


@pytest.mark.asyncio
async def test_fire_schedule_reminders_empty_no_emit(monkeypatch):
    """空容器（这一轮没排 / 改任何日程）→ 不 emit 任何 tick。"""
    emitted: list = []

    async def fake_emit_delayed(data, *, delay_ms, durability="durable"):
        emitted.append(data)

    monkeypatch.setattr(lt, "emit_delayed", fake_emit_delayed)

    await lt.fire_schedule_reminders(
        lane="coe-t3", persona_id="akao", schedule_reminders={}
    )
    assert emitted == []


@pytest.mark.asyncio
async def test_fire_schedule_reminders_emit_failure_does_not_raise(monkeypatch, caplog):
    """某条 emit 失败 → log warning、不往上炸、不拖垮其余条目（本轮 durable 收口已落定）。"""
    import logging

    emitted: list = []

    async def flaky_emit(data, *, delay_ms, durability="durable"):
        if data.entry_id == "n1":
            raise RuntimeError("broker down")
        emitted.append(data.entry_id)

    monkeypatch.setattr(lt, "emit_delayed", flaky_emit)

    future = (
        __import__("app.infra.cst_time", fromlist=["now_cst"]).now_cst()
        + __import__("datetime").timedelta(minutes=30)
    ).isoformat()

    with caplog.at_level(logging.WARNING):
        # 不该往上炸
        await lt.fire_schedule_reminders(
            lane="coe-t3", persona_id="akao",
            schedule_reminders={"n1": future, "n2": future},
        )

    assert "n2" in emitted, "一条挂失败不该拖垮其余条目"
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "挂失败必须 log warning（不静默吞）"


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
    """工具集多一件 chat（与 update_life_state / act 并列，常驻基础工具）。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
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


def test_act_description_steers_online_to_real_hands():
    """act 文案点破：上网看东西不走 act —— 用 act 假装上网是自己编的（假的），引向 look_up / browse_feed 两只真手。

    coe 实测根因：life_wake prompt 的「能用几件事」清单没把两只手算进去 + act 是万能
    「做事」，她想上网时抓 act 凑一个假的、不切真工具。act 文案这一刀对称「说话不走
    act」补「上网不走 act」：点破编的是假的、把想查 / 想刷引向 look_up / browse_feed。
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
    # 把「想查个答案 / 想刷刷」引向两只真手（不让她用万能 act 凑一个假的）
    assert "look_up" in desc, "act 文案应把想查答案引向 look_up"
    assert "browse_feed" in desc, "act 文案应把想刷刷引向 browse_feed"
    # 点破：用 act 假装上网 = 自己编 = 假的
    assert ("编" in desc) or ("假" in desc), "act 文案要点破用 act 假装上网是自己编的"


def lt_speech_kind() -> str:
    """speech event 的 kind 常量（让测试与实现共用同一来源，不硬编码字面量）。"""
    return lt.EVENT_KIND_SPEECH


# ---------------------------------------------------------------------------
# look_up —— 「带问题查」的手（life web access Task 1）。
#
# 她生活里冒出具体目的时，自己想好一个具体问题（query 必填）去网上查、当场拿到真
# 答案，基于真数据反应。底层走现有 search_web —— search_web 返回的就是带源的文本
# （每条 [i] 标题 / 出处链接 / 关键摘录），look_up 原样把它送进她当轮上下文，**不**
# 用第二次 LLM 把结果消化成一段话（否则她的反应就不挂在真东西上、又「一眼假」）。
# 拿不到结果时如实说没查到，绝不编一个顶上。
# ---------------------------------------------------------------------------


class _FakeSearchWeb:
    """假的 search_web Tool：记录被调入参、返回预置文本（模拟 search_web 的带源输出）。"""

    def __init__(self, return_value: str):
        self.return_value = return_value
        self.calls: list[dict] = []

    async def invoke(self, arguments: dict) -> str:
        self.calls.append(arguments)
        return self.return_value


# search_web 真实输出形如：每条 "[i] 标题\n    链接\n    摘录"，多条以空行分隔。
_SEARCH_WEB_SOURCED_RESULT = (
    "[1] 广州明天天气预报 - 中国天气网\n"
    "    https://weather.example.com/guangzhou\n"
    "    广州明日多云转小雨，最高28度，建议带伞。\n\n"
    "[2] 周末广州天气 - 某气象站\n"
    "    https://qx.example.com/gz\n"
    "    明日午后有阵雨概率60%。"
)


def test_build_life_tools_includes_look_up():
    """工具集常驻 look_up（「带问题查」的手），与 chat / 本子三件并列、不依赖 self_wake。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    assert "look_up" in tools
    assert isinstance(tools["look_up"], Tool)


def test_look_up_schema_hides_mechanism_only_query_exposed():
    """模型只看见 query 业务参数，看不见 lane / persona_id / act_id；query 必填。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    params = tools["look_up"].definition.parameters
    assert set(params["properties"]) == {"query"}
    # query 必填 —— 「查」是带着具体问题去（决策 2），不像「刷」那样无 query。
    assert params.get("required") == ["query"]


@pytest.mark.asyncio
async def test_look_up_passes_query_to_search_web(monkeypatch):
    """带 query 调 look_up → 走到 search_web，且把她想好的问题原样传进去。"""
    fake = _FakeSearchWeb(_SEARCH_WEB_SOURCED_RESULT)
    monkeypatch.setattr(lt, "search_web", fake)

    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    await tools["look_up"].invoke({"query": "广州明天会下雨吗"})

    assert len(fake.calls) == 1, "look_up 必须走到 search_web"
    assert fake.calls[0].get("query") == "广州明天会下雨吗", (
        "她自己想好的问题要原样传给 search_web"
    )


@pytest.mark.asyncio
async def test_look_up_returns_sources_into_context(monkeypatch):
    """返回带源（标题 / 出处链接 / 摘录）进她当轮上下文 —— 不被消化成一段话（承重红线）。"""
    fake = _FakeSearchWeb(_SEARCH_WEB_SOURCED_RESULT)
    monkeypatch.setattr(lt, "search_web", fake)

    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    out = await tools["look_up"].invoke({"query": "广州明天会下雨吗"})

    assert isinstance(out, str)
    # search_web 的带源原文必须原样在返回里：标题、出处链接、关键摘录都在。
    assert "广州明天天气预报 - 中国天气网" in out, "标题要进上下文"
    assert "https://weather.example.com/guangzhou" in out, "出处链接要进上下文"
    assert "多云转小雨" in out, "关键摘录要进上下文"
    # 不是只剩一句消化后的话 —— 第二条来源也得在（没被压成单段总结）。
    assert "https://qx.example.com/gz" in out, "多条来源都要带进上下文、不被压成一句"


@pytest.mark.asyncio
async def test_look_up_no_results_says_so_no_fabrication(monkeypatch):
    """search_web 没查到（返回未配置 / 无结果文案）→ 如实说没查到，不编一个顶上。"""
    fake = _FakeSearchWeb("搜索服务未配置或未搜索到结果")
    monkeypatch.setattr(lt, "search_web", fake)

    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    out = await tools["look_up"].invoke({"query": "某个查不到的冷门问题"})

    assert isinstance(out, str)
    assert "没查到" in out, "拿不到结果要如实说没查到（兜底），不编内容"


@pytest.mark.asyncio
async def test_look_up_tool_failure_returns_outcome_not_raise(monkeypatch):
    """search_web 抛错 → @tool_error 兜成结构化 outcome 喂回模型，不炸整轮（spec 决策 3）。"""

    class _BoomSearch:
        async def invoke(self, arguments):
            raise RuntimeError("search backend down")

    monkeypatch.setattr(lt, "search_web", _BoomSearch())

    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    out = await tools["look_up"].invoke({"query": "广州明天会下雨吗"})
    assert isinstance(out, dict)
    assert out["kind"] == "tool_error"


def test_look_up_description_guides_question_driven_lookup():
    """look_up 文案引导「带着具体问题去查、拿真答案」，不是漫无目的浏览（区别于刷手机）。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    desc = tools["look_up"].definition.description
    assert "查" in desc, "文案要让模型知道这是去查东西"
    # 引导她带着自己想好的具体问题（不是无 query 的浏览）
    assert "问题" in desc or "想知道" in desc


def test_look_up_description_contrasts_with_fake_act():
    """look_up 文案补反向对比：别用 act 假装「我查了下」（那是自己编的假货）—— 真想知道用这只真手。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    desc = tools["look_up"].definition.description
    # 反向对比到 act：别用 act 假装查（避免被万能 act 惯性吸走）
    assert "act" in desc, "look_up 文案要反向对比：别用 act 假装查"
    assert ("编" in desc) or ("假" in desc), "look_up 文案要点破用 act 假装查是自己编的"


# ---------------------------------------------------------------------------
# browse_feed —— 「刷手机」的手（life web access Task 2）。
#
# 她没具体目的、就想随便逛逛时用：带一个从自己当下状态/心情里自然涌出的「方向」
# （自然语言、可以很泛），工具拿这个方向去 search_web 捞一**批**（多条、像一条 feed）
# 带源真内容回来供她浏览，翻到感兴趣的才停。与 look_up 的真区别：look_up 带一个具体
# 问题求一个答案、返回聚焦；browse_feed 带一个泛方向逛一圈、返回一批供她挑选。
#
# 违宪红线（必守）：工具内不写死兴趣标签 / 规则、不替她猜该看啥、不另起 agent；她看
# 啥全由她自己带进来的方向决定。边界（Non-goal）：刷的是她兴趣圈的东西、不是时政社会
# 要闻（那归 world）——但只靠 docstring 引导，绝不用规则 / 黑名单 / 关键词过滤拦
# （过滤就是工具内替她决策、违宪）。
# ---------------------------------------------------------------------------


# search_web 真实输出形如「一批」带源条目：每条 "[i] 标题\n    链接\n    摘录"，
# 多条以空行分隔（像一条 feed 的多条）。
_SEARCH_WEB_FEED_RESULT = (
    "[1] 本季新番补全！这几部口碑爆了 - 某番剧站\n"
    "    https://anime.example.com/season\n"
    "    本季多部新番更新，讨论度最高的是……\n\n"
    "[2] 大家都在聊的搞笑名场面合集 - 某社区\n"
    "    https://bbs.example.com/funny\n"
    "    最近刷屏的几个梗，评论区笑疯了。\n\n"
    "[3] 那部你惦记的番更新了吗 - 某资讯号\n"
    "    https://news.example.com/update\n"
    "    第8话已更，本周讨论热度回升。"
)


def test_build_life_tools_includes_browse_feed():
    """工具集常驻 browse_feed（「刷手机」的手），与 look_up / chat / 本子三件并列、不依赖 self_wake。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    assert "browse_feed" in tools
    assert isinstance(tools["browse_feed"], Tool)


def test_browse_feed_and_look_up_are_two_distinct_hands_both_in_toolset():
    """browse_feed 和 look_up 是两只**不同**的手、都在工具集里（语义不同、不是马甲）。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    # 两只手都在
    assert "look_up" in tools and "browse_feed" in tools
    # 它们是不同的工具对象、不同的 name
    assert tools["look_up"].name != tools["browse_feed"].name
    # 业务参数也不同：look_up 带「问题」、browse_feed 带「方向」（参数名不同 = 用法不同）
    look_up_props = set(tools["look_up"].definition.parameters["properties"])
    feed_props = set(tools["browse_feed"].definition.parameters["properties"])
    assert look_up_props == {"query"}
    assert feed_props == {"direction"}
    assert look_up_props != feed_props


def test_browse_feed_schema_hides_mechanism_only_direction_exposed():
    """模型只看见 direction 业务参数，看不见 lane / persona_id / act_id / num 等机制。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    params = tools["browse_feed"].definition.parameters
    assert set(params["properties"]) == {"direction"}


@pytest.mark.asyncio
async def test_browse_feed_passes_direction_and_asks_for_a_batch(monkeypatch):
    """带 direction 调 browse_feed → 走到 search_web，方向原样作 query 传入、且要一批（num > 默认聚焦）。"""
    fake = _FakeSearchWeb(_SEARCH_WEB_FEED_RESULT)
    monkeypatch.setattr(lt, "search_web", fake)

    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    await tools["browse_feed"].invoke({"direction": "想看点搞笑的，还有那部番更没更"})

    assert len(fake.calls) == 1, "browse_feed 必须走到 search_web"
    call = fake.calls[0]
    # 她涌出的方向原样作检索方向传给 search_web（工具不替她改写、不猜兴趣）
    assert call.get("query") == "想看点搞笑的，还有那部番更没更"
    # 刷手机是「逛一圈看有啥」—— 要一批（num 比 look_up 聚焦的默认多），像一条 feed
    assert call.get("num", 0) > 5, "刷手机要捞一批（num > 默认聚焦的 5），像一条 feed"


@pytest.mark.asyncio
async def test_browse_feed_returns_batch_of_sources_into_context(monkeypatch):
    """返回**一批**带源（标题 / 出处链接 / 摘录）进她当轮上下文 —— 不被压成一段话（承重红线）。"""
    fake = _FakeSearchWeb(_SEARCH_WEB_FEED_RESULT)
    monkeypatch.setattr(lt, "search_web", fake)

    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    out = await tools["browse_feed"].invoke({"direction": "刷刷新番和搞笑的"})

    assert isinstance(out, str)
    # 一批里每一条的标题 / 出处链接 / 摘录都原样在返回里（没被压成单段总结）。
    assert "本季新番补全" in out, "第1条标题要进上下文"
    assert "https://anime.example.com/season" in out, "第1条出处要进上下文"
    assert "搞笑名场面合集" in out, "第2条标题要进上下文"
    assert "https://bbs.example.com/funny" in out, "第2条出处要进上下文"
    assert "https://news.example.com/update" in out, "第3条出处要进上下文"
    # 多条来源都在 = 是「一批」、不是被消化成一句（与 look_up 聚焦不同）。
    assert out.count("https://") >= 3, "刷手机返回的是一批带源内容、不被压成一段"


@pytest.mark.asyncio
async def test_browse_feed_nothing_new_says_so_no_fabrication(monkeypatch):
    """search_web 没刷到（返回未配置 / 无结果文案）→ 如实说没刷到新鲜的，不编一批顶上。"""
    fake = _FakeSearchWeb("搜索服务未配置或未搜索到结果")
    monkeypatch.setattr(lt, "search_web", fake)

    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    out = await tools["browse_feed"].invoke({"direction": "随便看看"})

    assert isinstance(out, str)
    assert "没" in out, "没刷到要如实说（兜底），不编一批内容顶上"


@pytest.mark.asyncio
async def test_browse_feed_tool_failure_returns_outcome_not_raise(monkeypatch):
    """search_web 抛错 → @tool_error 兜成结构化 outcome 喂回模型，不炸整轮（spec 决策 3）。"""

    class _BoomSearch:
        async def invoke(self, arguments):
            raise RuntimeError("search backend down")

    monkeypatch.setattr(lt, "search_web", _BoomSearch())

    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    out = await tools["browse_feed"].invoke({"direction": "随便逛逛"})
    assert isinstance(out, dict)
    assert out["kind"] == "tool_error"


def test_browse_feed_description_guides_aimless_browsing_distinct_from_look_up():
    """browse_feed 文案引导「漫无目的逛一圈、带泛方向看一批」，并明确「有具体问题求答案走 look_up」。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    desc = tools["browse_feed"].definition.description
    # 漫无目的逛 / 看有啥新鲜的（不是带问题求答案）
    assert ("随便" in desc) or ("逛" in desc) or ("刷" in desc)
    assert "方向" in desc, "文案要让她知道带的是一个方向（不是必填检索词）"
    # 明确「有具体问题求答案那是另一只手（look_up）、不走这里」
    assert "look_up" in desc, "文案要把「有具体问题求答案」引向 look_up（两只手分清）"


def test_browse_feed_description_contrasts_with_fake_act():
    """browse_feed 文案补反向对比：别用 act 假装「我刷了刷手机」（那是自己编的）—— 真想刷用这只真手。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    desc = tools["browse_feed"].definition.description
    # 反向对比到 act：别用 act 假装刷（避免被万能 act 惯性吸走）
    assert "act" in desc, "browse_feed 文案要反向对比：别用 act 假装刷"
    assert ("编" in desc) or ("假" in desc), "browse_feed 文案要点破用 act 假装刷是自己编的"


def test_browse_feed_does_not_hardcode_interest_rules():
    """违宪红线：工具的**可执行代码**里不写死兴趣标签枚举 / 黑名单关键词过滤（看啥由她带的方向决定）。

    源码层面钉死：browse_feed 的可执行代码（剔除 docstring + 注释后）里不出现硬编的
    兴趣枚举（anime / career / gaokao 之类）当作分类规则，也不出现「时政 / 社会 / 灾害」
    这类用于过滤拦截 result 的黑名单关键词——边界只靠 docstring 软引导，不靠规则拦
    （过滤就是工具内替她决策、违宪）。剔注释 / docstring 是因为引导文字（"不写死兴趣
    标签""不刷时政"）出现在说明里是允许的、甚至是该有的，违宪的是把它写成可执行逻辑。
    """
    import inspect

    src = inspect.getsource(lt.build_life_tools)
    # 截出 browse_feed 函数体那一段（到下一个 @tool_error 装饰的工具为止），只查它内部。
    start = src.index("async def browse_feed")
    rest = src[start:]
    nxt = rest.find("@tool_error", 1)
    body = rest if nxt == -1 else rest[:nxt]

    # 只看可执行代码：去掉 docstring（三引号块）和 # 行注释——引导 / 说明文字允许出现，
    # 违宪的是把兴趣规则 / 过滤写成可执行逻辑。
    import re as _re

    code = _re.sub(r'""".*?"""', "", body, flags=_re.DOTALL)
    code_lines = []
    for line in code.splitlines():
        stripped = line.split("#", 1)[0]
        if stripped.strip():
            code_lines.append(stripped)
    code_only = "\n".join(code_lines)

    # 可执行代码里不写死兴趣标签枚举（这些 token 只可能作为硬编分类规则出现）
    for banned in ("anime", "career", "gaokao", "时政", "灾害", "黑名单"):
        assert banned not in code_only, (
            f"browse_feed 可执行代码不该写死兴趣规则 / 过滤关键词：{banned!r}"
        )
    # 不对 search_web 结果做任何过滤 / 改写（原样进上下文）：可执行代码里不该出现
    # 把 result 切片 / 替换 / 按关键词筛的操作。
    assert "filter(" not in code_only, "browse_feed 不该过滤 search_web 结果"
    assert ".replace(" not in code_only, "browse_feed 不该改写 search_web 结果"


# ---------------------------------------------------------------------------
# 本子工具 —— 备忘录 & 日程 app 第一块（她对本子能用的 note / edit_note /
# read_notebook 三件）。常驻基础工具，与 update / act / chat 并列（不依赖 self_wake）。
# ---------------------------------------------------------------------------

from app.domain.notebook import (  # noqa: E402
    STATUS_DONE,
    STATUS_DROPPED,
    NotebookEntry,
)


def test_build_life_tools_includes_notebook_tools():
    """工具集常驻三件本子工具：note / edit_note / read_notebook。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    assert {"note", "edit_note", "read_notebook"} <= set(tools)


def test_notebook_tools_hide_mechanism_bindings():
    """本子工具只对模型暴露业务参数，不暴露 lane / persona_id / entry_id 派生口径。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    note_props = set(tools["note"].definition.parameters["properties"])
    assert note_props == {"content", "remind_at"}

    edit_props = set(tools["edit_note"].definition.parameters["properties"])
    assert edit_props == {"entry_id", "content", "remind_at", "status"}

    read_props = set(tools["read_notebook"].definition.parameters["properties"])
    assert read_props == {"include_all"}


@pytest.mark.asyncio
async def test_note_memo_records_handler_call(stub_handlers):
    """记一条没时间的 → note_entry 收到本轮绑定 + content，remind_at 为 None。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="base-id",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    out = await tools["note"].invoke({"content": "想看那部动画"})
    assert isinstance(out, str) and out
    assert len(stub_handlers["noted"]) == 1
    call = stub_handlers["noted"][0]
    assert call["lane"] == "coe-t3"
    assert call["persona_id"] == "akao"
    assert call["content"] == "想看那部动画"
    assert call["remind_at"] is None
    assert call["noted_at"] == "2026-06-03T12:30:00+00:00"


@pytest.mark.asyncio
async def test_note_schedule_carries_remind_at(stub_handlers):
    """排一条带时间的 → remind_at 透传给 note_entry（备忘 vs 日程只差这个）。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="base-id",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    await tools["note"].invoke(
        {"content": "三点陪我妹", "remind_at": "2026-06-03T15:00:00+00:00"}
    )
    call = stub_handlers["noted"][0]
    assert call["remind_at"] == "2026-06-03T15:00:00+00:00"


@pytest.mark.asyncio
async def test_note_returns_entry_id(stub_handlers):
    """记一条的出参带这条的 id（以后改 / 划得指到它）。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="base-id",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    out = await tools["note"].invoke({"content": "买猫粮"})
    entry_id = stub_handlers["noted"][0]["entry_id"]
    # id 必须出现在返回给模型的确认里（spec：出参 = id + 一句确认）
    assert entry_id in out


@pytest.mark.asyncio
async def test_note_entry_id_derived_from_base_id(stub_handlers):
    """entry_id 从 base act_id 派生（本轮第 N 件），不让模型生成 —— 同 act 幂等口径。"""
    import uuid

    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="round-base",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    await tools["note"].invoke({"content": "第一件"})
    await tools["note"].invoke({"content": "第二件"})
    ids = [c["entry_id"] for c in stub_handlers["noted"]]
    # 各自唯一（不同序号）
    assert ids[0] != ids[1]
    # 派生口径钉死：uuid5(NAMESPACE_OID, "round-base:note:N")
    assert ids[0] == str(uuid.uuid5(uuid.NAMESPACE_OID, "round-base:note:1"))
    assert ids[1] == str(uuid.uuid5(uuid.NAMESPACE_OID, "round-base:note:2"))


@pytest.mark.asyncio
async def test_note_entry_id_is_uuid_shaped(stub_handlers):
    """派生 entry_id 为合法 UUID 形（与 act id 同口径，不引入怪字符）。"""
    import uuid as _uuid

    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="round-base",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    await tools["note"].invoke({"content": "x"})
    eid = stub_handlers["noted"][0]["entry_id"]
    assert str(_uuid.UUID(eid)) == eid


@pytest.mark.asyncio
async def test_note_id_stable_under_round_replay(stub_handlers):
    """整轮重投同一 base id → 同序号 note 得同一 entry_id（durable 去重靠它只落一条）。"""
    tools_a = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="same-base",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    await tools_a["note"].invoke({"content": "第一件"})
    await tools_a["note"].invoke({"content": "第二件"})
    first_ids = [c["entry_id"] for c in stub_handlers["noted"]]

    stub_handlers["noted"].clear()

    tools_b = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="same-base",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    await tools_b["note"].invoke({"content": "第一件"})
    await tools_b["note"].invoke({"content": "第二件"})
    replay_ids = [c["entry_id"] for c in stub_handlers["noted"]]

    assert replay_ids == first_ids


@pytest.mark.asyncio
async def test_note_seq_advances_only_after_success(monkeypatch):
    """note 序号只在 note_entry 成功后才推进 —— 失败重试用同一 entry_id（对称 act_seq P6）。

    note_entry 写库成功但返回链路抛错（ack 丢）时，@tool_error 吞错让模型重试；序号
    若在成功前 +1，重试会用新序号派生新 id → 同一件记两条。修法：成功后才推进。
    """
    seen: list[str] = []
    calls = {"n": 0}

    async def flaky_note(**kwargs):
        seen.append(kwargs["entry_id"])
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("ack lost after commit")

    monkeypatch.setattr(lt, "note_entry", flaky_note)

    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="base-id",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    out1 = await tools["note"].invoke({"content": "买猫粮"})
    assert isinstance(out1, dict) and out1["kind"] == "tool_error"
    out2 = await tools["note"].invoke({"content": "买猫粮"})
    assert isinstance(out2, str)

    assert len(seen) == 2
    assert seen[0] == seen[1], f"失败重试必须用同一 entry_id，实得 {seen}"


@pytest.mark.asyncio
async def test_edit_note_passes_through_fields(stub_handlers):
    """改 / 划一条 → entry_id + 改的字段透传给 update_entry。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="base-id",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    out = await tools["edit_note"].invoke(
        {"entry_id": "e-1", "content": "改后的内容", "status": STATUS_DONE}
    )
    assert isinstance(out, str) and out
    assert len(stub_handlers["edited"]) == 1
    call = stub_handlers["edited"][0]
    assert call["lane"] == "coe-t3"
    assert call["persona_id"] == "akao"
    assert call["entry_id"] == "e-1"
    assert call["content"] == "改后的内容"
    assert call["status"] == STATUS_DONE


@pytest.mark.asyncio
async def test_edit_note_clear_remind_at_signaled_explicitly(stub_handlers):
    """撤时间：模型给 remind_at 空串 → update_entry 收到 clear_remind_at=True。

    remind_at=None 在工具入参里是「没传、别动」；模型要表达「把时间撤了」用显式空串，
    工具翻成 clear_remind_at=True 透给底层（None 无法区分「没传」与「撤」）。
    """
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="base-id",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    await tools["edit_note"].invoke({"entry_id": "e-1", "remind_at": ""})
    call = stub_handlers["edited"][0]
    assert call.get("clear_remind_at") is True


@pytest.mark.asyncio
async def test_edit_note_set_remind_at(stub_handlers):
    """改期 / 补时间：给 remind_at 时刻 → 透传，且不当成撤时间。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="base-id",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    await tools["edit_note"].invoke(
        {"entry_id": "e-1", "remind_at": "2026-06-03T18:00:00+00:00"}
    )
    call = stub_handlers["edited"][0]
    assert call["remind_at"] == "2026-06-03T18:00:00+00:00"
    assert not call.get("clear_remind_at")


@pytest.mark.asyncio
async def test_read_notebook_default_active_only(stub_handlers):
    """翻本子默认只看还活着的（active_only=True）。"""
    stub_handlers["notebook_rows"] = [
        NotebookEntry(
            lane="coe-t3", persona_id="akao", entry_id="e-1",
            content="还惦记的", remind_at=None, noted_at="2026-06-03T10:00:00+00:00",
        )
    ]
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="base-id",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    out = await tools["read_notebook"].invoke({})
    assert stub_handlers["listed"][0]["active_only"] is True
    # 出参带每条的 id / 内容（spec：列表每条带 id、内容、时间、状态）
    assert "e-1" in out
    assert "还惦记的" in out


@pytest.mark.asyncio
async def test_read_notebook_include_all(stub_handlers):
    """include_all=True → 看全部（含做过 / 划掉的），active_only=False 透给底层。"""
    stub_handlers["notebook_rows"] = [
        NotebookEntry(
            lane="coe-t3", persona_id="akao", entry_id="e-2",
            content="做过的", status=STATUS_DONE,
            remind_at=None, noted_at="2026-06-03T10:00:00+00:00",
        ),
        NotebookEntry(
            lane="coe-t3", persona_id="akao", entry_id="e-3",
            content="划掉的", status=STATUS_DROPPED,
            remind_at=None, noted_at="2026-06-03T10:00:00+00:00",
        ),
    ]
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="base-id",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    out = await tools["read_notebook"].invoke({"include_all": True})
    assert stub_handlers["listed"][0]["active_only"] is False
    assert "做过的" in out and "划掉的" in out


@pytest.mark.asyncio
async def test_read_notebook_empty(stub_handlers):
    """空本子 → 返回一句「本子是空的」之类的确认，不报错。"""
    stub_handlers["notebook_rows"] = []
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="base-id",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    out = await tools["read_notebook"].invoke({})
    assert isinstance(out, str) and out


# ---------------------------------------------------------------------------
# look_up / browse_feed × 真实 search_web 契约（集成测试，建议②）。
#
# 上面的单测都 monkeypatch 了 lt.search_web，拦不住 life↔search_web 之间的**真实契约
# 漂移**：search_web 的失败 / 空文案变了、或 look_up / browse_feed 漏认某种失败信号，
# 单测全绿但线上把失败当内容顶上去（违 spec 决策 4：拿不到真东西就如实说没查到、绝不
# 编一个顶上）。这一组用**真实的 search_web Tool**（不 patch lt.search_web），只 patch
# 它底层依赖的 capability（app/agent/tools/search.py 里的 _web_search_capability /
# _rerank_capability），让 search_web 真实跑出它自己的失败 / 空文案，再断言 look_up /
# browse_feed 把它如实收成「没查到 / 没刷到」、不当内容。将来 search_web 改了失败表达
# 这组会挂、提醒同步 life 侧识别。
# ---------------------------------------------------------------------------

import app.agent.tools.search as search_mod  # noqa: E402


@pytest.mark.asyncio
async def test_look_up_treats_real_search_web_failure_as_not_found(monkeypatch):
    """必改① 回归守护：底层 capability 抛异常 → 真实 search_web 返回「网页搜索失败: ...」
    → look_up 必须把它如实收成「没查到」、绝不当内容包装成「查到这些」。

    走真实 search_web Tool（不 patch lt.search_web），只 patch 它底层的
    _web_search_capability 抛异常 —— search_web 内部 catch 后真实返回前缀固定、后缀
    变长的 ``f"网页搜索失败: {exc}"``。旧实现只精确匹配两条空文案 + 非 str，漏了这条
    变长失败串 → 会被当真内容顶上去（红）。
    """

    async def boom_capability(*args, **kwargs):
        raise RuntimeError("search backend exploded")

    # 只 patch 底层 capability —— search_web 本身是真的（不动 lt.search_web）。
    monkeypatch.setattr(search_mod, "_web_search_capability", boom_capability)

    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    out = await tools["look_up"].invoke({"query": "广州明天会下雨吗"})

    assert isinstance(out, str)
    # 真实失败串必须被识别成「没查到」，绝不当内容包装。
    assert "没查到" in out, "网页搜索失败串要如实收成『没查到』（必改①）"
    assert "查到这些" not in out, "失败串绝不能被包装成『查到这些』顶上去"
    # 底层异常文本不该原样泄进她的上下文（那就是把技术失败当内容了）。
    assert "网页搜索失败" not in out
    assert "search backend exploded" not in out


@pytest.mark.asyncio
async def test_browse_feed_treats_real_search_web_failure_as_not_found(monkeypatch):
    """必改① 回归守护（browse_feed 侧）：底层 capability 抛异常 → 真实 search_web 返回
    「网页搜索失败: ...」→ browse_feed 必须如实收成「没刷到」、不当内容编一批顶上。"""

    async def boom_capability(*args, **kwargs):
        raise RuntimeError("search backend exploded")

    monkeypatch.setattr(search_mod, "_web_search_capability", boom_capability)

    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    out = await tools["browse_feed"].invoke({"direction": "随便逛逛"})

    assert isinstance(out, str)
    assert "没刷到" in out, "网页搜索失败串要如实收成『没刷到』（必改①）"
    assert "刷到这些" not in out, "失败串绝不能被包装成『刷到这些』顶上去"
    assert "网页搜索失败" not in out
    assert "search backend exploded" not in out


@pytest.mark.asyncio
async def test_look_up_treats_real_search_web_empty_as_not_found(monkeypatch):
    """底层返回空（[]）→ 真实 search_web 返回「搜索服务未配置或未搜索到结果」→
    look_up 把它识别成「没查到」（守护现有空文案契约）。"""

    async def empty_capability(*args, **kwargs):
        return []

    monkeypatch.setattr(search_mod, "_web_search_capability", empty_capability)

    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    out = await tools["look_up"].invoke({"query": "某个查不到的冷门问题"})

    assert isinstance(out, str)
    assert "没查到" in out, "空结果要如实收成『没查到』"
    assert "查到这些" not in out


@pytest.mark.asyncio
async def test_browse_feed_treats_real_search_web_empty_as_not_found(monkeypatch):
    """底层返回空（[]）→ 真实 search_web 返回「搜索服务未配置或未搜索到结果」→
    browse_feed 把它识别成「没刷到」（守护现有空文案契约）。"""

    async def empty_capability(*args, **kwargs):
        return []

    monkeypatch.setattr(search_mod, "_web_search_capability", empty_capability)

    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    out = await tools["browse_feed"].invoke({"direction": "随便看看"})

    assert isinstance(out, str)
    assert "没刷到" in out, "空结果要如实收成『没刷到』"
    assert "刷到这些" not in out


# ===========================================================================
# 隔手机发消息（life proactive messaging, task 3）。
#
# 「当面说话」(chat) 与「隔手机发消息」(send_message) 是两个**模态**、两只手 ——
# 模态由 life 自己选（用哪只手），代码不替她判该当面还是手机（spec 决策 4）。
#
#   * look_up_contact —— 报名字模糊查候选（uid + 简介）给她挑，只返回不取第一个
#     （spec 决策 3：选谁是她的决定）。
#   * send_message —— 按她选的 uid 隔空发：
#       - 姐妹（MailboxTarget）：走信箱、但用**手机消息 kind**（EVENT_KIND_MESSAGE），
#         和当面 speech（EVENT_KIND_SPEECH）在收件人侧可区分（spec 决策 5）。
#       - 真人（LarkP2PTarget）：emit 出站 ChatResponseSegment（is_proactive=True、
#         真实 chat_id，不靠伪来源 id）。
#       - 不可投递（UndeliverableRecipient）：作为 tool error 喂回 life（spec 决策 6）。
# ===========================================================================


@pytest.fixture
def stub_directory(monkeypatch):
    """把目录解析层（search_recipients / resolve_delivery）和出站 emit 换成可观测 fake。"""
    from app.domain.recipient_directory import (
        LarkP2PTarget,
        MailboxTarget,
        RecipientCandidate,
        UndeliverableRecipient,
    )

    state: dict = {
        "search_calls": [],
        "resolve_calls": [],
        "emitted": [],
        # 测试按需预置：search_recipients 返回的候选 / resolve_delivery 的解析结果。
        "candidates": [],
        "resolve_result": None,
        "resolve_raises": None,
    }

    async def fake_search_recipients(query):
        state["search_calls"].append(query)
        return state["candidates"]

    async def fake_resolve_delivery(uid):
        state["resolve_calls"].append(uid)
        if state["resolve_raises"] is not None:
            raise state["resolve_raises"]
        return state["resolve_result"]

    async def fake_emit(data):
        state["emitted"].append(data)

    monkeypatch.setattr(lt, "search_recipients", fake_search_recipients)
    monkeypatch.setattr(lt, "resolve_delivery", fake_resolve_delivery)
    monkeypatch.setattr(lt, "emit", fake_emit)

    state["_RecipientCandidate"] = RecipientCandidate
    state["_MailboxTarget"] = MailboxTarget
    state["_LarkP2PTarget"] = LarkP2PTarget
    state["_UndeliverableRecipient"] = UndeliverableRecipient
    return state


# --- 新 event kind：手机/隔空消息 ≠ 当面 speech ----------------------------


def test_message_kind_is_distinct_from_speech():
    """手机消息 kind（EVENT_KIND_MESSAGE）必须 ≠ 当面 speech kind（收件人侧可区分，决策 5）。"""
    from app.domain.world_events import EVENT_KIND_MESSAGE, EVENT_KIND_SPEECH

    assert EVENT_KIND_MESSAGE != EVENT_KIND_SPEECH


def test_message_kind_is_not_passive_wakes_recipient():
    """手机消息是发给对方让 ta 看到的 → 不是被动 kind（投递要敲门唤醒收件人，对称 speech）。"""
    from app.domain.world_events import (
        EVENT_KIND_MESSAGE,
        EVENT_KIND_SPEECH,
        PASSIVE_EVENT_KINDS,
    )

    assert EVENT_KIND_MESSAGE not in PASSIVE_EVENT_KINDS, (
        "手机消息发给对方让 ta 看到，必须唤醒收件人（与 speech 一样不被动）"
    )
    # speech 现状也非被动（守护：两者唤醒语义一致）。
    assert EVENT_KIND_SPEECH not in PASSIVE_EVENT_KINDS


# --- look_up_contact：查名字拿候选（只返回、不替她选） ----------------------


def test_build_life_tools_includes_contact_and_send_tools():
    """工具集常驻 look_up_contact（查名字）+ send_message（隔空发），与 chat 并列。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    assert "look_up_contact" in tools
    assert "send_message" in tools
    assert isinstance(tools["look_up_contact"], Tool)
    assert isinstance(tools["send_message"], Tool)


def test_send_tools_schema_hides_mechanism():
    """模型只看见业务参数：look_up_contact(query)、send_message(uid, content)。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    assert set(tools["look_up_contact"].definition.parameters["properties"]) == {"query"}
    assert set(tools["send_message"].definition.parameters["properties"]) == {
        "uid",
        "content",
    }


@pytest.mark.asyncio
async def test_look_up_contact_returns_all_candidates_with_uid_and_intro(stub_directory):
    """报名字 → search_recipients → 候选（uid + 简介）原样列给她，不排序不取第一个（决策 3）。"""
    RC = stub_directory["_RecipientCandidate"]
    stub_directory["candidates"] = [
        RC(uid="persona:akao", display_name="赤尾", intro="赤尾（你的姐妹）：高三的妹妹"),
        RC(uid="user:u-1", display_name="赤尾", intro="赤尾（真人）"),
    ]
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="chinagi", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    out = await tools["look_up_contact"].invoke({"query": "赤尾"})

    assert stub_directory["search_calls"] == ["赤尾"]
    assert isinstance(out, str)
    # 两个重名候选都列出来交给她挑（不替她取第一个）。
    assert "persona:akao" in out and "user:u-1" in out
    assert "你的姐妹" in out and "真人" in out


@pytest.mark.asyncio
async def test_look_up_contact_no_candidates_says_so(stub_directory):
    """查不到任何候选 → 如实说没找到（喂回 life 让她换名字 / 算了），不静默给空。"""
    stub_directory["candidates"] = []
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    out = await tools["look_up_contact"].invoke({"query": "查无此人"})
    assert isinstance(out, str)
    assert "没找到" in out or "查不到" in out


# --- send_message → 姐妹：手机消息进信箱（新 kind） ------------------------


@pytest.mark.asyncio
async def test_send_message_to_sister_delivers_with_message_kind(
    stub_handlers, stub_directory
):
    """uid 解析成 MailboxTarget（姐妹）→ 走信箱投递、kind=EVENT_KIND_MESSAGE（手机消息）。

    关键区分（决策 5）：和当面 chat 的 speech kind 不同 —— 收件人侧能看出这是「手机
    发来的消息」而非「当面说的话」。source 是发送者 persona_id。
    """
    from app.domain.world_events import EVENT_KIND_MESSAGE

    stub_directory["resolve_result"] = stub_directory["_MailboxTarget"](
        persona_id="ayana"
    )
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    out = await tools["send_message"].invoke(
        {"uid": "persona:ayana", "content": "姐姐我到学校了"}
    )

    assert isinstance(out, str) and out
    assert stub_directory["resolve_calls"] == ["persona:ayana"]
    assert len(stub_handlers["delivered"]) == 1
    d = stub_handlers["delivered"][0]
    assert d["lane"] == "coe-t3"
    assert d["persona_id"] == "ayana", "手机消息直投收件人姐妹信箱"
    assert d["summary"] == "姐姐我到学校了"
    assert d["kind"] == EVENT_KIND_MESSAGE, "手机消息用新 kind，和当面 speech 区分"
    assert d["kind"] != lt.EVENT_KIND_SPEECH
    assert d["source"] == "akao", "source 是发送者 persona_id"
    assert d["occurred_at"] == "2026-06-03T12:30:00+00:00"
    # 姐妹手机消息不走出站队列（那是真人飞书私聊的事）。
    assert stub_directory["emitted"] == []


@pytest.mark.asyncio
async def test_send_message_to_sister_does_not_emit_outbound(
    stub_handlers, stub_directory
):
    """姐妹手机消息只进信箱、不 emit ChatResponseSegment（出站只给真人飞书）。"""
    stub_directory["resolve_result"] = stub_directory["_MailboxTarget"](
        persona_id="chinagi"
    )
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    await tools["send_message"].invoke({"uid": "persona:chinagi", "content": "在吗"})
    assert stub_directory["emitted"] == []
    assert len(stub_handlers["delivered"]) == 1


# --- send_message → 真人：emit 出站段（is_proactive、真实 chat_id、不靠伪 id） ---


@pytest.mark.asyncio
async def test_send_message_to_person_emits_outbound_segment(
    stub_handlers, stub_directory
):
    """uid 解析成 LarkP2PTarget（真人）→ emit ChatResponseSegment 到 chat_response 出站。

    出站段（task 4 接力送达飞书）字段：is_proactive=True、is_p2p=True、
    chat_id=真实 common_conversation_id、bot_name、channel、persona_id=发送者、
    content=内容、lane 显式带。绝不投信箱（真人没有 life 信箱）。
    """
    from app.domain.chat_dataflow import ChatResponseSegment

    stub_directory["resolve_result"] = stub_directory["_LarkP2PTarget"](
        common_conversation_id="cc-direct-1",
        bot_name="chiwei",
        user_id="u-real-1",
        channel="lark",
    )
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    out = await tools["send_message"].invoke(
        {"uid": "user:u-real-1", "content": "在忙吗，方便聊两句吗"}
    )

    assert isinstance(out, str) and out
    assert stub_directory["resolve_calls"] == ["user:u-real-1"]
    # 真人不进信箱（真人没有 life 信箱）。
    assert stub_handlers["delivered"] == []
    # 恰好 emit 一条出站段。
    assert len(stub_directory["emitted"]) == 1
    seg = stub_directory["emitted"][0]
    assert isinstance(seg, ChatResponseSegment)
    assert seg.is_proactive is True, "主动发：is_proactive 标识复用 worker 出站分支"
    assert seg.is_p2p is True, "真人投递走飞书私聊"
    assert seg.chat_id == "cc-direct-1", "用真实 p2p 会话 id 当 chat_id（worker 反查渠道）"
    assert seg.bot_name == "chiwei", "用 resolver 给的发送 bot"
    assert seg.channel == "lark"
    assert seg.user_id == "u-real-1"
    assert seg.persona_id == "akao", "persona_id 是发送者（worker 据它选 bot 身份兜底）"
    assert seg.content == "在忙吗，方便聊两句吗"
    assert seg.lane == "coe-t3", "lane 必须显式带（sink 不注入 header lane）"
    assert seg.is_last is True, "主动发是一段完整消息（worker 据 is_last 收口）"


@pytest.mark.asyncio
async def test_send_message_to_person_return_is_honest_async_not_guaranteed(
    stub_handlers, stub_directory
):
    """真人分支诚实返回（codex 必改 2，bezhai 决策）：给真人发是异步的（emit 到 MQ →
    另一进程 worker 发飞书），emit 成功只是「入队成功」、不等于「送达成功」。返回措辞
    不能说「发到了 / 对方收到了」，要说「已发出（异步送达、不保证送到）」这类，让 life
    知道是发出去了、最终送达异步、可能失败。

    **本刀不做异步失败回流**——只把入队当入队、诚实说出去。同步可知的失败
    （UndeliverableRecipient）继续 fail-loud 喂回不变（见 undeliverable 测试）。
    """
    stub_directory["resolve_result"] = stub_directory["_LarkP2PTarget"](
        common_conversation_id="cc-direct-1",
        bot_name="chiwei",
        user_id="u-real-1",
        channel="lark",
    )
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    out = await tools["send_message"].invoke(
        {"uid": "user:u-real-1", "content": "在忙吗"}
    )

    assert isinstance(out, str) and out
    # 入队成功才返回（emit 一条出站段），但措辞诚实「已发出、异步、不保证送达」。
    assert len(stub_directory["emitted"]) == 1
    assert "已发出" in out, f"真人分支应说「已发出」（入队成功），实得 {out!r}"
    assert ("不保证" in out) or ("不一定" in out), (
        f"真人分支要点明最终送达不保证（异步可能失败），实得 {out!r}"
    )


@pytest.mark.asyncio
async def test_send_message_to_sister_return_unchanged_synchronous_delivered(
    stub_handlers, stub_directory
):
    """姐妹分支（信箱、同步落库）返回保持原样、不染上「异步不保证」措辞（codex 必改 2）。

    姐妹走信箱是同步落库的：deliver_event 成功就是真送到她信箱了，和真人异步 emit 不同。
    所以姐妹分支返回的是确定送达语义，**不能**说「不保证送达」。承重断言：姐妹返回 ≠
    真人那条「不保证」措辞。
    """
    stub_directory["resolve_result"] = stub_directory["_MailboxTarget"](
        persona_id="ayana"
    )
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    out = await tools["send_message"].invoke(
        {"uid": "persona:ayana", "content": "姐姐我到学校了"}
    )

    assert isinstance(out, str) and out
    assert len(stub_handlers["delivered"]) == 1, "姐妹同步落库（deliver_event）"
    assert "不保证" not in out and "不一定" not in out, (
        f"姐妹是同步落库、确定送达，不该染上「不保证送达」措辞，实得 {out!r}"
    )


@pytest.mark.asyncio
async def test_send_message_to_person_does_not_carry_fake_source_id(
    stub_handlers, stub_directory
):
    """主动发没有来源消息 → 出站段绝不带伪造的来源 message_id / root_id（codex 红线）。

    worker 旧路径靠 message_id 反查 LarkMessage 渠道地址；主动发没有来源消息，若塞一个
    伪 message_id 会让 worker 反查炸。这里保证 message_id / root_id 不是伪造的来源 id ——
    要么为空、要么是不指向任何 LarkMessage 的本地派生键。承重断言：它**不等于**任何真实
    来源消息 id，task 4 的 worker 主动发分支据此走「不反查来源、直接用 chat_id」的路径。
    """
    stub_directory["resolve_result"] = stub_directory["_LarkP2PTarget"](
        common_conversation_id="cc-direct-1",
        bot_name="chiwei",
        user_id="u-real-1",
        channel="lark",
    )
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    await tools["send_message"].invoke({"uid": "user:u-real-1", "content": "你好"})
    seg = stub_directory["emitted"][0]
    # 主动发没有来源消息：root_id 必须为空（不伪造一条「被回复的消息」）。
    assert not seg.root_id, "主动发没有来源消息，root_id 不能伪造"
    # message_id 不能是指向某条真实来源 LarkMessage 的 id。它要么空、要么是带主动发
    # 命名空间前缀的本地派生键（明显不是渠道来源消息 id），让 worker 能识别「这是主动发、
    # 别反查来源」。
    assert (not seg.message_id) or seg.message_id.startswith("proactive:"), (
        f"message_id 不能是伪造的来源消息 id，实得 {seg.message_id!r}"
    )


@pytest.mark.asyncio
async def test_send_message_to_person_idempotent_segment_key_stable_under_replay(
    stub_directory,
):
    """整轮重投同序 send_message（同 base act_id）→ 出站段 (message_id, part_index) 稳定。

    出站段是 transient（不落 agent-service 表），但 message_id 仍是 Key —— 主动发的
    段键从 (base act_id, send 序号) 纯函数派生，重投同序得同一键，便于下游按键去重 /
    对齐（对称 chat 的 per-chat 键）。
    """
    stub_directory["resolve_result"] = stub_directory["_LarkP2PTarget"](
        common_conversation_id="cc-direct-1",
        bot_name="chiwei",
        user_id="u-real-1",
        channel="lark",
    )

    def _run_round():
        return _tools_by_name(
            lt.build_life_tools(
                lane="coe-t3", persona_id="akao", act_id="same-base",
                observed_at="2026-06-03T12:30:00+00:00",
            )
        )

    tools_a = _run_round()
    await tools_a["send_message"].invoke({"uid": "user:u-real-1", "content": "你好"})
    first_key = (
        stub_directory["emitted"][0].message_id,
        stub_directory["emitted"][0].part_index,
    )

    stub_directory["emitted"].clear()
    tools_b = _run_round()
    await tools_b["send_message"].invoke({"uid": "user:u-real-1", "content": "你好"})
    replay_key = (
        stub_directory["emitted"][0].message_id,
        stub_directory["emitted"][0].part_index,
    )
    assert replay_key == first_key, "重投同序主动发段键必须稳定（幂等）"


# --- send_message → 不可投递：tool error 喂回 life ------------------------


@pytest.mark.asyncio
async def test_send_message_undeliverable_uid_becomes_tool_error(
    stub_handlers, stub_directory
):
    """uid 不可投递（resolver 抛 UndeliverableRecipient）→ tool error 喂回 life（决策 6）。

    不静默降级、不代她另找目标 —— 把「发不了 + 原因」作为工具错误反馈，让她自己处置
    （换个人 / 重试 / 算了）。原因文本（str(exc)）要带进 tool error message。
    """
    reason = "user:u-x 没有可投递的飞书私聊会话，发不了——这边只能发已经私聊过的人。"
    stub_directory["resolve_raises"] = stub_directory["_UndeliverableRecipient"](reason)
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    out = await tools["send_message"].invoke({"uid": "user:u-x", "content": "在吗"})

    assert isinstance(out, dict), "不可投递应被 @tool_error 收成结构化 outcome 喂回模型"
    assert out["kind"] == "tool_error"
    assert reason in out["message"], "原因文本要带给 life（她自己处置）"
    # 既没投信箱、也没 emit 出站（不静默降级、不另找目标）。
    assert stub_handlers["delivered"] == []
    assert stub_directory["emitted"] == []


@pytest.mark.asyncio
async def test_send_message_unknown_target_type_is_tool_error(
    stub_handlers, stub_directory
):
    """resolver 返回的不是 Mailbox/LarkP2P（理论不该发生）→ 报错喂回，不静默吞。"""
    stub_directory["resolve_result"] = object()  # 非已知投递目标类型
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3", persona_id="akao", act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    out = await tools["send_message"].invoke({"uid": "weird:1", "content": "hi"})
    assert isinstance(out, dict) and out["kind"] == "tool_error"
    assert stub_handlers["delivered"] == []
    assert stub_directory["emitted"] == []


# ---------------------------------------------------------------------------
# Task 2（纯客观事件驱动范式）：删 life 自设闹钟整条。
#
# 闹钟 = 空的时间点（next_wake_at / schedule / fire_life_self_wake），目的只是
# 「维持她运转」，设错 / 丢失就睡死——存活不该压在她自己手上。life 退成纯事件反应者：
# 只被事件激活、跑完不排下次。她的主动计划走日程（notebook + 到点提醒），那条保留。
#
# 这里钉死「自设闹钟已删」：build_life_tools 不再接 self_wake、绝不产 schedule 工具；
# fire_life_self_wake 不存在。日程那条（note / edit_note / fire_schedule_reminders /
# schedule_reminders 容器）在上面 / 下面的 section 仍全绿，证明没误删日程。
# ---------------------------------------------------------------------------


def test_build_life_tools_never_produces_schedule_tool():
    """删自设闹钟：工具集里绝不再有 schedule（不管以前怎么传都不该有）。"""
    tools = lt.build_life_tools(
        lane="coe-t3",
        persona_id="akao",
        act_id="a-1",
        observed_at="2026-06-03T12:30:00+00:00",
    )
    by_name = _tools_by_name(tools)
    assert "schedule" not in by_name, "自设闹钟已删：绝不再产 schedule 工具"


def test_build_life_tools_rejects_self_wake_kwarg():
    """删自设闹钟：build_life_tools 不再接 self_wake 参数（self-wake 容器整条拆掉）。"""
    import inspect

    sig = inspect.signature(lt.build_life_tools)
    assert "self_wake" not in sig.parameters, (
        "self-wake 容器整条拆掉：build_life_tools 不再有 self_wake 参数"
    )


def test_fire_life_self_wake_is_gone():
    """删自设闹钟：fire_life_self_wake 收口函数不复存在（写 next_wake_at 那条腿拆掉）。"""
    assert not hasattr(lt, "fire_life_self_wake"), (
        "fire_life_self_wake（写 next_wake_at 的闹钟收口）必须删掉"
    )


def test_set_life_next_wake_at_no_longer_imported_in_life_tools():
    """删自设闹钟：life_tools 不再 import / 引用 set_life_next_wake_at（没有写入方）。"""
    assert not hasattr(lt, "set_life_next_wake_at"), (
        "set_life_next_wake_at 不再有调用方，life_tools 不该再引用它"
    )
