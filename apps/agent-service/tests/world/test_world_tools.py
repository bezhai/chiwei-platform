"""world 工具（notify / update_world / sleep）契约 — 阶段 1A（world 推演者）.

新范式下 world 是世界推演者，不是导演。它的三个工具：

  * :func:`update_world` —— 写一段自然语言、记"世界此刻什么样"。world_time 由
    工具体自填（现实当前 CST，客观时间不让模型编），detail 是模型给的叙述，
    一起 append 一版 durable 快照。
  * :func:`notify` —— world 推演出"这条客观动静此刻谁够得着"，把 observation
    投给 recipients（persona_id 列表）。对每个 recipient 调 deliver_event 投进
    其信箱（kind=ambient、source="world"、无房间锚点）。event_id 从
    (lane, observation, round_id) 确定性派生（整轮重放幂等命门）：同一 observation
    同一轮同一 id；不同 observation 不同 id。同一 observation 投多个 recipient 用
    同一 event_id（persona 不同自然键不同，不冲突）。
  * :func:`sleep` —— 1A 完全不动：定下次多久再看一眼世界（60～3600s），把待办
    self-wake 记进 round state（覆盖而非追加），engine 收口后 emit 一条。

这些测试 stub 现成 handler（不碰真库），钉死工具机制层硬约束。
"""

from __future__ import annotations

import pytest

import app.world.tools as tools_mod
from app.agent.context import AgentContext
from app.agent.runtime_context import agent_context
from app.world.tools import (
    FEATURE_SELF_WAKE,
    WORLD_SLEEP_MAX_SECONDS,
    WORLD_SLEEP_MIN_SECONDS,
    derive_event_id,
    derive_surroundings_event_id,
    notify,
    sense,
    sleep,
    update_arc,
    update_world,
)


def _round_features() -> dict:
    """world_tick 每轮新建的 round-scoped 可变状态（lane + round_id + 待办 self-wake）。

    新范式下 notify 不再有 emit 计数安全阀（recursion_limit 已是失控兜底），所以
    round state 只剩 lane / round_id / 待办 self-wake。
    """
    return {
        "world_lane": "coe-t2",
        "world_round_id": "round-abc",
        FEATURE_SELF_WAKE: {},
    }


@pytest.fixture
def _ctx():
    """world 本轮的 ambient context：lane + 确定性 round_id + 待办 self-wake 容器。"""
    return AgentContext(features=_round_features())


@pytest.fixture(autouse=True)
def _stub_handlers(monkeypatch):
    """stub 现成 handler，专测工具薄 wrap 的副作用，不碰真库。"""
    delivered: list[dict] = []

    async def fake_deliver_event(**kwargs):
        delivered.append(kwargs)
        return 1

    world_writes: list[dict] = []

    async def fake_write_world_state(*, lane, world_time, detail):
        world_writes.append({"lane": lane, "world_time": world_time, "detail": detail})

    arc_writes: list[dict] = []

    async def fake_write_world_arc(*, lane, narrative, turned_at):
        arc_writes.append(
            {"lane": lane, "narrative": narrative, "turned_at": turned_at}
        )

    monkeypatch.setattr(tools_mod, "deliver_event", fake_deliver_event)
    monkeypatch.setattr(tools_mod, "write_world_state", fake_write_world_state)
    monkeypatch.setattr(tools_mod, "write_world_arc", fake_write_world_arc)

    tools_mod._test_delivered = delivered  # type: ignore[attr-defined]
    tools_mod._test_world_writes = world_writes  # type: ignore[attr-defined]
    tools_mod._test_arc_writes = arc_writes  # type: ignore[attr-defined]
    # sleep 不直接 emit_delayed（它把待办 self-wake 记进 round state、由 engine
    # 收口后 emit），这个空列表只用来断言"tool 层没有任何直接 self-wake 发生"。
    tools_mod._test_self_wakes = []  # type: ignore[attr-defined]
    yield


# ---------------------------------------------------------------------------
# update_world
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_world_writes_detail_with_self_filled_time(_ctx):
    """update_world 落 detail durable，world_time 由工具体自填现实当前 CST。"""
    with agent_context(_ctx):
        await update_world.invoke(
            {"detail": "清晨厨房有了动静，千凪在烧水手冲，屋里飘着咖啡香。"}
        )

    assert len(tools_mod._test_world_writes) == 1
    w = tools_mod._test_world_writes[0]
    assert w["lane"] == "coe-t2"
    assert w["detail"] == "清晨厨房有了动静，千凪在烧水手冲，屋里飘着咖啡香。"
    # world_time 由工具体自填现实 CST（不让模型编）：非空、带 CST 偏移
    assert w["world_time"]
    assert "+08:00" in w["world_time"]


@pytest.mark.asyncio
async def test_update_world_time_is_not_modeled(_ctx, monkeypatch):
    """world_time 取现实当前 CST（cst_time.now_cst_iso），客观时间不让模型给。"""
    monkeypatch.setattr(
        tools_mod.cst_time, "now_cst_iso", lambda: "2026-06-05T09:00:00+08:00"
    )
    with agent_context(_ctx):
        await update_world.invoke({"detail": "上午的光照进客厅。"})

    assert tools_mod._test_world_writes[0]["world_time"] == "2026-06-05T09:00:00+08:00"


# ---------------------------------------------------------------------------
# update_arc — 世界长弧的「翻页」工具（与 update_world 同族、分两层钟）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_arc_writes_narrative_with_self_filled_turned_at(_ctx):
    """update_arc 落 narrative durable（write_world_arc），turned_at 由工具体自填现实 CST。

    与 update_world 对 world_time 的处理同族对称：翻页时刻是客观时间、不让模型编，
    由工具体按现实当前 CST 自填。
    """
    with agent_context(_ctx):
        await update_arc.invoke(
            {"narrative": "三姐妹家已经搬进新小区，妹妹换了新学校，眼下是初夏。"}
        )

    assert len(tools_mod._test_arc_writes) == 1
    w = tools_mod._test_arc_writes[0]
    assert w["lane"] == "coe-t2"
    assert (
        w["narrative"] == "三姐妹家已经搬进新小区，妹妹换了新学校，眼下是初夏。"
    )
    # turned_at 由工具体自填现实 CST（不让模型编）：非空、带 CST 偏移
    assert w["turned_at"]
    assert "+08:00" in w["turned_at"]


@pytest.mark.asyncio
async def test_update_arc_turned_at_is_not_modeled(_ctx, monkeypatch):
    """turned_at 取现实当前 CST（cst_time.now_cst_iso），客观时间不让模型给。"""
    monkeypatch.setattr(
        tools_mod.cst_time, "now_cst_iso", lambda: "2026-06-10T09:00:00+08:00"
    )
    with agent_context(_ctx):
        await update_arc.invoke({"narrative": "换季了，初夏的节律落进这个家。"})

    assert tools_mod._test_arc_writes[0]["turned_at"] == "2026-06-10T09:00:00+08:00"


@pytest.mark.asyncio
async def test_update_arc_does_not_touch_state_or_mailbox(_ctx):
    """update_arc 只写长弧：不碰 WorldState 快照、不投递任何信箱（与既有工具互不干扰）。"""
    with agent_context(_ctx):
        await update_arc.invoke({"narrative": "长弧翻了一页。"})

    assert tools_mod._test_world_writes == [], "update_arc 不该写 WorldState 快照"
    assert tools_mod._test_delivered == [], "update_arc 不该投递任何信箱 event"
    assert len(tools_mod._test_arc_writes) == 1


@pytest.mark.asyncio
async def test_update_world_does_not_touch_arc(_ctx):
    """反向互不干扰：update_world 只写此刻快照，不碰长弧。"""
    with agent_context(_ctx):
        await update_world.invoke({"detail": "午后客厅很安静。"})

    assert tools_mod._test_arc_writes == [], "update_world 不该写长弧"
    assert len(tools_mod._test_world_writes) == 1


def test_update_arc_only_in_reflect_tools_not_world_tools():
    """update_arc 归反思环节独占：在 WORLD_REFLECT_TOOLS、不在 WORLD_TOOLS。

    续写姿态发现不了「页翻了」（coe 实证），翻页能力从续写剥离——互不干扰不靠
    嘱咐，靠工具集物理隔离：续写无手碰长弧，反思无手碰 detail / notify / sense /
    sleep。
    """
    from app.world.tools import WORLD_REFLECT_TOOLS, WORLD_TOOLS, update_attention

    assert update_arc not in WORLD_TOOLS, "续写工具集不得含 update_arc（翻页归反思）"
    assert WORLD_REFLECT_TOOLS == [update_arc, update_attention], (
        "反思工具集 = 翻页 + 关注两件"
    )


@pytest.mark.asyncio
async def test_update_arc_write_failure_propagates(_ctx, monkeypatch):
    """write_world_arc 抛错必须穿透 update_arc 向上炸（不包 @tool_error）。

    update_arc 是反思环节独占的 durable 写。写库失败若被 @tool_error 包成
    tool result 字符串喂回模型，Agent.run 会正常返回 → run_arc_reflection 误判
    成功 → mark_arc_reflected 落当日标记 → 同日重试被吃掉（假成功落标记）。
    所以 durable 写失败必须让异常穿透工具、炸掉整次反思——run_arc_reflection
    的 fail-open 接住它：不落标记、同日后续轮重试（durable mutation 失败要可见）。
    """

    async def boom_write(*, lane, narrative, turned_at):
        raise RuntimeError("pg down during arc write")

    monkeypatch.setattr(tools_mod, "write_world_arc", boom_write)

    with agent_context(_ctx):
        with pytest.raises(RuntimeError, match="pg down during arc write"):
            await update_arc.invoke({"narrative": "这一页翻不动了。"})


def test_update_arc_docstring_pins_arc_vs_detail_boundary():
    """update_arc 的 docstring（喂给 LLM 的工具说明）必须钉死长弧与 detail 的边界。

    长弧与 detail 都是 world 写、world 读的自然语言快照，不在工具说明里钉住边界
    会互相污染。必须含：① 两层钟分界（detail 写「此刻」明天就过时 / 长弧写「跨周月
    仍然成立的世界进展」）；② 一句话判据（这句话下周还成立吗）；③ 翻页粒度（以周月
    计的翻页级转变才动、日常起居不动）；④ 整篇重写语义（翻过去的页被取代不是被追加、
    不写历史流水账）。
    """
    doc = update_arc.definition.description
    # ① 两层钟分界
    assert "此刻" in doc
    assert "跨周月" in doc
    # ② 一句话判据
    assert "下周" in doc and "成立" in doc
    # ③ 翻页粒度：翻页级转变才动、日常不动
    assert "翻页" in doc
    assert "日常" in doc
    # ④ 整篇重写、不是追加、不写流水账
    assert "重写" in doc
    assert "流水账" in doc


# ---------------------------------------------------------------------------
# notify
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_delivers_observation_to_each_recipient(_ctx):
    """notify 把 observation 投给 world 推演指定的每个 recipient（落 summary 字段）。"""
    with agent_context(_ctx):
        await notify.invoke(
            {
                "recipients": ["chinagi", "ayana"],
                "observation": "厨房飘来煎蛋和咖啡的香味",
            }
        )

    recipients = {d["persona_id"] for d in tools_mod._test_delivered}
    assert recipients == {"chinagi", "ayana"}
    for d in tools_mod._test_delivered:
        # observation 落进 EventEnvelope 的 summary（life 侧读 summary）
        assert d["summary"] == "厨房飘来煎蛋和咖啡的香味"
        assert d["kind"] == "ambient"
        assert d["source"] == "world"
        assert d["lane"] == "coe-t2"


@pytest.mark.asyncio
async def test_notify_no_recipients_delivers_to_nobody(_ctx):
    """空 recipients → 不投给任何人（world 推演没人够得着这条动静）。"""
    with agent_context(_ctx):
        await notify.invoke(
            {"recipients": [], "observation": "巷子里有只猫走过"}
        )
    assert tools_mod._test_delivered == []


@pytest.mark.asyncio
async def test_notify_same_observation_same_event_id_across_recipients(_ctx):
    """同一 observation 投多个 recipient 用同一 event_id（一条动静一个 id）。"""
    with agent_context(_ctx):
        await notify.invoke(
            {
                "recipients": ["chinagi", "ayana", "akao"],
                "observation": "玄关传来开关门的声音",
            }
        )
    ids = {d["event_id"] for d in tools_mod._test_delivered}
    assert len(ids) == 1, "同一条 observation 投给多人共享同一 event_id"


@pytest.mark.asyncio
async def test_notify_event_id_idempotent_per_round(_ctx):
    """同一 (lane, observation, round_id) 派生同一 event_id —— 整轮重放幂等命门。"""
    with agent_context(_ctx):
        await notify.invoke(
            {"recipients": ["chinagi"], "observation": "厨房飘来煎蛋香味"}
        )
    first = {d["event_id"] for d in tools_mod._test_delivered}

    tools_mod._test_delivered.clear()
    with agent_context(_ctx):
        await notify.invoke(
            {"recipients": ["chinagi"], "observation": "厨房飘来煎蛋香味"}
        )
    second = {d["event_id"] for d in tools_mod._test_delivered}
    assert second == first, "同输入重放应派生同一 event_id（deliver_event 幂等去重）"


@pytest.mark.asyncio
async def test_notify_event_id_differs_per_observation(_ctx):
    """不同 observation → 不同 event_id（不同的动静是不同的 event），不含房间。"""
    id_a = derive_event_id(lane="coe-t2", observation="A", round_id="r")
    id_b = derive_event_id(lane="coe-t2", observation="B", round_id="r")
    assert id_a != id_b


@pytest.mark.asyncio
async def test_notify_one_recipient_failure_does_not_strand_others(_ctx, caplog):
    """notify 对 recipients 逐个独立投递：中途一人失败不影响其他人 + log 失败的 persona。"""
    import logging

    delivered: list[dict] = []

    async def flaky_deliver(**kwargs):
        if kwargs["persona_id"] == "akao":
            raise RuntimeError("akao 信箱暂时挂了")
        delivered.append(kwargs)
        return 1

    monkeypatch_target = tools_mod
    orig = monkeypatch_target.deliver_event
    monkeypatch_target.deliver_event = flaky_deliver  # type: ignore[assignment]
    try:
        with agent_context(_ctx), caplog.at_level(logging.WARNING):
            result = await notify.invoke(
                {
                    "recipients": ["chinagi", "akao", "ayana"],
                    "observation": "厨房水声",
                }
            )
    finally:
        monkeypatch_target.deliver_event = orig  # type: ignore[assignment]

    got = {d["persona_id"] for d in delivered}
    assert "chinagi" in got and "ayana" in got
    assert "akao" not in got
    # 失败的 persona 被 log
    assert any("akao" in rec.message for rec in caplog.records)
    # 整条 notify 不抛、不被 @tool_error 包成错误
    assert not (isinstance(result, dict) and result.get("kind"))


# ---------------------------------------------------------------------------
# sense — 1C Task 2：world 五官，给单个角色投她此刻的周遭客观切片
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sense_delivers_surroundings_to_single_recipient(_ctx):
    """sense 把一份周遭客观切片投给**单个** recipient（落 summary、kind=surroundings）。

    周遭切片是 world 为这一个角色逐角色推演的「此刻你在哪、谁在你身边、环境怎样」，
    本质 per-person（绫奈的周遭 ≠ 赤尾的周遭），所以收件人是单数——区别于 notify
    那种"一条动静多人够得着"的广播形态。这逼 world 分别推演每个人的切片（信息差
    的守门：每人只拿到为她推演的那份）。
    """
    with agent_context(_ctx):
        await sense.invoke(
            {
                "recipient": "ayana",
                "surroundings": "你在客厅写作业，厨房飘来赤尾做饭的香味，午后的光斜照进来。",
            }
        )

    assert len(tools_mod._test_delivered) == 1
    d = tools_mod._test_delivered[0]
    assert d["persona_id"] == "ayana"
    assert d["summary"] == "你在客厅写作业，厨房飘来赤尾做饭的香味，午后的光斜照进来。"
    assert d["kind"] == "surroundings"
    assert d["source"] == "world"
    assert d["lane"] == "coe-t2"


@pytest.mark.asyncio
async def test_sense_event_id_idempotent_per_round(_ctx):
    """同一 (lane, recipient, surroundings, round_id) 派生同一 event_id（整轮重放幂等）。"""
    args = {
        "recipient": "ayana",
        "surroundings": "你在客厅写作业，厨房有动静。",
    }
    with agent_context(_ctx):
        await sense.invoke(args)
    first = tools_mod._test_delivered[0]["event_id"]

    tools_mod._test_delivered.clear()
    with agent_context(_ctx):
        await sense.invoke(args)
    second = tools_mod._test_delivered[0]["event_id"]
    assert second == first, "同输入重放应派生同一 event_id（deliver_event 幂等去重）"


@pytest.mark.asyncio
async def test_sense_event_id_differs_per_recipient(_ctx):
    """同一轮给不同角色投周遭切片 → 不同 event_id（per-person 切片不互相覆盖）。

    周遭切片 per-person：绫奈和赤尾这一轮的切片即便文字偶然一样，也是两条独立 event，
    不能因共享 id 在 deliver_event 幂等里互相吞掉。event_id 把 recipient 纳入派生源。
    """
    id_ayana = derive_surroundings_event_id(
        lane="coe-t2", recipient="ayana", surroundings="一样的文字", round_id="r"
    )
    id_akao = derive_surroundings_event_id(
        lane="coe-t2", recipient="akao", surroundings="一样的文字", round_id="r"
    )
    assert id_ayana != id_akao


@pytest.mark.asyncio
async def test_sense_event_id_distinct_from_notify(_ctx):
    """周遭切片与动静的 event_id 命名空间不撞（同文字也不互相幂等吞掉）。

    sense 投的周遭切片和 notify 投的动静走不同语义；即便文字偶然相同，也是两类
    不同 event，不能因派生命名空间重叠而在 deliver_event 幂等里互相覆盖。
    """
    notify_id = derive_event_id(lane="coe-t2", observation="同一句话", round_id="r")
    sense_id = derive_surroundings_event_id(
        lane="coe-t2", recipient="ayana", surroundings="同一句话", round_id="r"
    )
    assert notify_id != sense_id


@pytest.mark.asyncio
async def test_sense_in_world_tools():
    """sense 是 world 的工具之一（WORLD_TOOLS 含 sense）。"""
    from app.world.tools import WORLD_TOOLS

    assert sense in WORLD_TOOLS


# ---------------------------------------------------------------------------
# sleep — 1A 完全不动（保留原行为）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sleep_within_limit_records_pending_self_wake(_ctx):
    """sleep ≤ 1h 合法 → 把待办 self-wake 记进 round-scoped state（不直接 emit）。"""
    with agent_context(_ctx):
        await sleep.invoke({"seconds": 1800})

    assert tools_mod._test_self_wakes == []
    assert _ctx.features[FEATURE_SELF_WAKE]["delay_ms"] == 1_800_000


@pytest.mark.asyncio
async def test_sleep_at_limit_is_allowed(_ctx):
    """sleep == 1h 上限 → 合法（边界含上限），记进 round state。"""
    with agent_context(_ctx):
        await sleep.invoke({"seconds": WORLD_SLEEP_MAX_SECONDS})
    assert (
        _ctx.features[FEATURE_SELF_WAKE]["delay_ms"]
        == WORLD_SLEEP_MAX_SECONDS * 1000
    )


@pytest.mark.asyncio
async def test_multi_sleep_in_round_does_not_accumulate_last_wins(_ctx):
    """一轮内多次 sleep 不累积 self-wake —— 最后一次为准（唤醒风暴命门）。"""
    with agent_context(_ctx):
        await sleep.invoke({"seconds": 300})
        await sleep.invoke({"seconds": 600})
        await sleep.invoke({"seconds": 900})

    assert tools_mod._test_self_wakes == []
    assert _ctx.features[FEATURE_SELF_WAKE]["delay_ms"] == 900_000


@pytest.mark.asyncio
async def test_sleep_over_limit_returns_error_no_pending_wake(_ctx):
    """sleep > 1h → 返回错误喂回模型让它重调（不静默夹）、不留待办 self-wake。"""
    with agent_context(_ctx):
        result = await sleep.invoke({"seconds": WORLD_SLEEP_MAX_SECONDS + 1})

    assert isinstance(result, dict)
    assert result.get("kind") == "tool_error"
    assert tools_mod._test_self_wakes == []
    assert _ctx.features[FEATURE_SELF_WAKE] == {}


@pytest.mark.asyncio
async def test_sleep_at_min_floor_is_allowed(_ctx):
    """sleep == 60s 下限 → 合法（边界含下限），记进 round state。"""
    with agent_context(_ctx):
        await sleep.invoke({"seconds": WORLD_SLEEP_MIN_SECONDS})
    assert (
        _ctx.features[FEATURE_SELF_WAKE]["delay_ms"]
        == WORLD_SLEEP_MIN_SECONDS * 1000
    )


@pytest.mark.asyncio
async def test_sleep_under_floor_returns_error_no_pending_wake(_ctx):
    """sleep < 60s → 返回错误喂回模型让它重调（跟上限超限处理风格一致）、不留待办。"""
    with agent_context(_ctx):
        result = await sleep.invoke({"seconds": 30})

    assert isinstance(result, dict)
    assert result.get("kind") == "tool_error"
    assert tools_mod._test_self_wakes == []
    assert _ctx.features[FEATURE_SELF_WAKE] == {}


# ---------------------------------------------------------------------------
# WORLD_TOOLS 集合
# ---------------------------------------------------------------------------


def test_world_tools_are_notify_update_world_sense_sleep():
    """WORLD_TOOLS = [notify, update_world, sense, sleep]（续写四工具）。

    没有 move_persona / emit_event（旧导演范式）。sense 是 1C 加的「投周遭客观切片
    给单个角色」的五官工具，与 notify（广播一条动静给够得着的多人）分工不同。
    update_arc（世界长弧的「翻页」工具）**不在这里**——翻页归独立的反思环节独占
    （WORLD_REFLECT_TOOLS），续写与反思靠工具集物理隔离互不干扰。
    """
    from app.world.tools import WORLD_TOOLS

    assert WORLD_TOOLS == [notify, update_world, sense, sleep]
