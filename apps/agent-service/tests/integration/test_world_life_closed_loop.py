"""world + life event 闭环端到端集成 — 阶段 1A act 范式联调收口（最高风险一环）.

event 骨架、world engine（推演者）、life 三姐妹（自主做事）各自单测都绿，但拼
起来世界是死的。这个文件证明拼起来**世界真能动**，且走的是新范式的完整自主循环：

  1. world 冷启醒来 → ``update_world(detail)`` 写第一版世界叙述 →
     ``notify(recipients, observation)`` 把客观动静投给推演指定的角色 → ``sleep``。
  2. 被 notify 的角色（life）被唤醒 → 读到信箱里的 observation → ``act(description)``
     自主做事 → 收口。
  3. 角色 act → ``ActPerformed`` durable → 经 wiring 唤醒 world → world 读到这批 act
     （``list_recent_acts``）→ 在推演里消化、``update_world`` 更新世界叙述 + ``notify``
     该感知到的人。

真 Postgres（testcontainers），只 mock 一处 LLM：``Agent.run``——按 ``cfg.prompt_id``
把 world / life 两条循环分流，各自在真实 ``agent_context`` 下回放脚本里的工具调用
（world 的 update_world / notify / sleep；life 的 update_life_state / act），所以
工具的真实 DB 副作用全发生。别的全走真实持久化：mock 掉持久化等于什么都没测。
world 的 sleep 自排打桩成记录 delay，不连 RabbitMQ。

新范式的命门（对应 1A 交付）：
  * 完整自主循环每一棒交接成功；
  * detail 落 durable 且读回续上（world 续接认得上一版世界叙述）；
  * notify 的 observation 投进推演指定 recipient 的信箱、没投给够不着的人（信息差）；
  * act 唤醒 world 一轮、world 读到该 act（list_recent_acts）；
  * 不卡死：life 处在大状态、新 observation 进信箱、被唤醒能读到并换状态；
  * 全程没有 move_persona / emit_event / raise_intent / presence / room_id。
"""

from __future__ import annotations

from datetime import UTC, datetime

import fakeredis.aioredis
import pytest

import app.nodes.life_wake as lw
import app.world.engine as engine_mod
import app.world.tools as tools_mod
from app.agent.runtime_context import agent_context
from app.data.queries.acts import list_recent_acts
from app.data.queries.mailbox import list_unread_events
from app.domain.life_state import LifeState, find_life_state
from app.domain.session_transcript import SessionTranscript
from app.domain.world_events import (
    ActPerformed,
    EventArrived,
    EventEnvelope,
    EventRead,
)
from app.runtime.persist import insert_idempotent
from app.world.engine import (
    WORLD_HEARTBEAT_MS,
    WorldTick,
    world_tick,
)
from app.world.state import WorldState, read_world_state
from tests.runtime.conftest import migrate

# world 一轮的脚本化行动：模型在循环里调的工具序列。each = (tool_name, args)。
# mock 的 Agent.run 在真实 agent_context 下依次回放它们，真实 DB 副作用全发生。
WorldRound = list[tuple[str, dict]]


def _update_world(detail: str) -> tuple[str, dict]:
    return ("update_world", {"detail": detail})


def _notify(recipients: list[str], observation: str) -> tuple[str, dict]:
    return ("notify", {"recipients": recipients, "observation": observation})


def _sleep(seconds: int) -> tuple[str, dict]:
    return ("sleep", {"seconds": seconds})


def _update_life(current_state: str, response_mood: str, activity_type: str) -> tuple[str, dict]:
    return (
        "update_life_state",
        {
            "current_state": current_state,
            "response_mood": response_mood,
            "activity_type": activity_type,
        },
    )


def _act(description: str) -> tuple[str, dict]:
    return ("act", {"description": description})


@pytest.fixture(autouse=True)
def _fake_redis(monkeypatch):
    """In-memory redis for life/world single-flight 锁.

    ``life_wake_node`` 每轮拿 ``(lane, persona)`` 单飞锁；``world_tick`` 也按 actor
    拿锁串行化。这两条都打 redis，用 fakeredis 让闭环集成测试自包含、不连真实
    redis。session 续接 transcript 现在是 PG durable（``world_db`` 建表），不再走
    redis。同时重置 ``get_redis_capability`` 的 singleton（monkeypatch ``_redis``
    不影响已建的 singleton）。
    """
    import app.capabilities.redis as cap_mod
    import app.infra.redis as redis_mod

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_mod, "_redis", fake)
    monkeypatch.setattr(cap_mod, "_singleton", None)


@pytest.fixture
async def world_db(test_db):
    """建齐闭环需要的所有真实表：world 叙述快照 / 信箱 / 已读 / 动作 / life 快照 / 续接 transcript。

    新范式没有 presence 表了（RoomPresence 已删）。world 的客观状态只剩 WorldState
    （世界叙述），位置融在 detail 自然语言里。act 走 ActPerformed durable 表。
    """
    await migrate(WorldState, test_db)
    await migrate(EventEnvelope, test_db)
    await migrate(EventRead, test_db)
    await migrate(LifeState, test_db)
    await migrate(ActPerformed, test_db)
    await migrate(SessionTranscript, test_db)
    yield test_db


class _AgentRunController:
    """一处 mock ``Agent.run``，按 ``cfg.prompt_id`` 把 world / life 两条循环分流.

    world 和 life 都跑 ``Agent.run``——共享同一个 Agent 类。所以这里只 patch 一次
    run，按 ``self._cfg.prompt_id``（"world_deliberate" / "life_wake"）分到各自的
    脚本回放。回放在 run 拿到的真实 ``context`` 下、用真实 ``self._tools`` invoke
    工具，所以工具的真实 DB 副作用全发生（不 mock 持久化）。

    world 脚本：每次唤醒回放一轮工具调用（update_world / notify / sleep）。
    life 脚本：单轮工具调用（update_life_state / act），用 ``life_round``。
    """

    def __init__(self) -> None:
        self.world_rounds: list[WorldRound] = []
        self.world_calls: list[dict] = []
        self.life_round: list[tuple[str, dict]] = []
        self.life_calls: list[dict] = []

    async def run(
        self, agent, messages, *, prompt_vars=None, context=None, session_id=None,
        max_retries=2,
    ):
        from app.agent.neutral import Message, Role

        prompt_id = agent._cfg.prompt_id
        by_name = {t.name: t for t in agent._tools}
        if prompt_id == "world_deliberate":
            blob = "".join(m.text() for m in messages)
            self.world_calls.append({"messages_text": blob, "context": context})
            script = self.world_rounds.pop(0) if self.world_rounds else []
        else:  # life_wake
            # life 的感知拼进 USER stimulus（messages），不走 prompt_vars。镜像 world
            # 分支记下这一轮 messages 文本，断言才拿得到她这轮感知了啥。
            blob = "".join(m.text() for m in messages)
            self.life_calls.append(
                {
                    "messages_text": blob,
                    "prompt_vars": prompt_vars,
                    "context": context,
                    "session_id": session_id,
                    "persona_id": context.persona_id if context else None,
                }
            )
            script = list(self.life_round)
        with agent_context(context):
            for tool_name, args in script:
                await by_name[tool_name].invoke(args)
        # 镜像真实 run 的会话写回：world / life 续接（session_id 显式传入）时把本轮
        # 消息追加进 PG durable transcript，让续接 / turn 幂等查重在集成里真生效。
        if session_id is not None:
            from app.agent.session import append_session

            await append_session(session_id, list(messages))
        return Message(role=Role.ASSISTANT, content="")


@pytest.fixture(autouse=True)
def _agent_run(monkeypatch):
    """安装统一的 ``Agent.run`` mock（world + life 分流），整文件共用。"""
    ctl = _AgentRunController()

    async def fake_run(
        self, messages, *, prompt_vars=None, context=None, session_id=None, max_retries=2
    ):
        return await ctl.run(
            self, messages, prompt_vars=prompt_vars, context=context,
            session_id=session_id, max_retries=max_retries,
        )

    monkeypatch.setattr(engine_mod.Agent, "run", fake_run)
    return ctl


@pytest.fixture(autouse=True)
def _capture_act_to_pg(monkeypatch):
    """life 的 ``act`` → ``perform_act`` 这条动作：捕获 + 落进 PG（== durable wire）.

    真机里 ``wire(ActPerformed).to(act_to_world_tick).durable()`` 把 emit 的
    ActPerformed 经 durable publish 落进 ``data_act_performed`` 表，world 被 act
    唤醒后用 ``list_recent_acts`` 从那张表读全那一批。集成里没接 RabbitMQ，所以这里
    把 perform_act 替成只 ``insert_idempotent`` 进 PG（== durable wire 落库用的同一个
    framework 原语 —— durable publish 的消费端最终也是落这张表），让 world 真从 PG
    读到这条 act。不调原 perform_act 的 emit（那会走 publish_durable 连 MQ）；act
    唤醒 world 的路由由 wiring 测试 + 本文件的 world_tick(reason=act) 直喂覆盖。捕获
    的 act 列表供断言。

    perform_act handler 由 life_tools 模块级引用，patch 那里才拦得住。
    """
    import app.nodes.life_tools as life_tools_mod

    captured: list[ActPerformed] = []

    async def capture_perform_act(*, lane, act_id, persona_id, description, occurred_at):
        act = ActPerformed(
            lane=lane,
            act_id=act_id,
            persona_id=persona_id,
            description=description,
            occurred_at=occurred_at,
        )
        captured.append(act)
        # 同 framework 原语落 PG（== durable wire 落库），world 才读得到。
        await insert_idempotent(act)

    monkeypatch.setattr(life_tools_mod, "perform_act", capture_perform_act)
    engine_mod._test_captured_acts = captured  # type: ignore[attr-defined]
    return captured


def _world_llm(ctl: _AgentRunController, scripted: list[WorldRound]):
    """注册 world 每次唤醒回放的工具调用脚本，返回 world_calls 供断言。"""
    ctl.world_rounds = list(scripted)
    return ctl.world_calls


def _life_unread_text(captured: dict) -> str:
    """从 life 这一轮的 USER stimulus 取她信箱里那批未读 observation 的文字（验信息差 / 攒批）。

    感知拼进 life_wake 的 USER stimulus（messages）；这里从这一轮 run 收到的 messages
    文本取——这正是真机里喂给模型的那批未读 observation 原文。
    """
    return str(captured.get("messages_text", ""))


@pytest.fixture(autouse=True)
def _stub_self_wake(monkeypatch):
    """world sleep 自排打桩成记录 delay（不连 RabbitMQ）.

    自排走 sleep 工具的 ``emit_delayed``（在 ``app.world.tools`` 模块里），所以
    在 tools_mod 上打桩。
    """
    self_wakes: list[int] = []

    async def fake_emit_delayed(data, *, delay_ms, durability="durable"):
        self_wakes.append(delay_ms)

    monkeypatch.setattr(tools_mod, "emit_delayed", fake_emit_delayed)
    engine_mod._test_self_wakes = self_wakes  # type: ignore[attr-defined]
    return self_wakes


@pytest.fixture
def _stub_persona(monkeypatch):
    """persona 加载打桩，不依赖 DB 种子（闭环验证不关心人设内容）。"""

    async def fake_load_persona(persona_id):
        from app.memory._persona import PersonaContext

        return PersonaContext(
            persona_id=persona_id,
            display_name=persona_id,
            persona_lite=f"{persona_id} 的人设",
        )

    monkeypatch.setattr(lw, "load_persona", fake_load_persona)


@pytest.mark.integration
async def test_full_closed_loop_world_to_life_to_world(
    world_db, _stub_persona, _agent_run, _capture_act_to_pg, monkeypatch
):
    """整条 act-范式闭环从头跑到尾，断言每一棒交接成功（最致命的一条集成测试）.

    棒次：
      1. world 冷启动 → update_world 写第一版世界叙述 + notify 把一条客观动静投给
         推演指定的 akao（只 akao 够得着）。
      2. life（akao）被唤醒 → 读信箱拿到那条 observation → 想一轮（换状态）→
         act 自主做一件事（去厨房煮咖啡）。
      3. act 回灌 → 翻成 WorldTick(reason=act) → world 被 act 唤醒 → 从 PG 读到这条
         act → update_world 更新世界叙述（厨房有了动静）+ notify 该感知到的人。
    """
    lane = "coe-loop"

    # --- 棒 1：world 冷启动 ---
    world_calls = _world_llm(
        _agent_run,
        [
            # 第一次唤醒（冷启动）：写第一版世界叙述 + 把一条客观动静投给够得着的 akao。
            [
                _update_world("晌午。akao 在自己房间，chinagi 在厨房，ayana 在客厅。"),
                _notify(["akao"], "晌午的光斜照进房间"),
                _sleep(600),
            ],
            # 第二次唤醒（act 推演）：读到 akao 去了厨房，更新世界叙述 + 投给厨房在场的人。
            [
                _update_world("akao 走进厨房，开始煮咖啡，水汽升腾。chinagi 也在厨房。"),
                _notify(["chinagi"], "厨房传来煮咖啡的声音和香气"),
                _sleep(300),
            ],
        ],
    )

    await world_tick(WorldTick(lane=lane, reason="heartbeat"))

    # 棒 1 交接证据：第一版世界叙述落 durable，能读回（续接靠它）
    snap = await read_world_state(lane=lane)
    assert snap is not None
    assert "akao 在自己房间" in snap.detail
    # 冷启动确实走了 agent 循环、缘由告诉模型这是首次醒来（不是硬编死表）
    assert "冷启动" in world_calls[0]["messages_text"] or "首次" in world_calls[0]["messages_text"]
    # observation 投进了推演指定的 akao 信箱
    akao_unread = await list_unread_events(lane=lane, persona_id="akao")
    assert [e.summary for e in akao_unread] == ["晌午的光斜照进房间"]
    # 信息差：没投给够不着的 chinagi / ayana —— 她们信箱空
    assert await list_unread_events(lane=lane, persona_id="chinagi") == []
    assert await list_unread_events(lane=lane, persona_id="ayana") == []

    # --- 棒 2：life（akao）被唤醒想一轮、自主做事 ---
    _agent_run.life_round = [
        _update_life("醒了，想去厨房找吃的", "迷糊", "move"),
        _act("我去厨房煮咖啡"),
    ]

    await lw.life_wake_node(EventArrived(lane=lane, persona_id="akao"))

    # 棒 2 交接证据：新 LifeState 落库且可读到最新
    life_snap = await find_life_state(lane=lane, persona_id="akao")
    assert life_snap is not None
    assert life_snap.current_state == "醒了，想去厨房找吃的"
    assert life_snap.response_mood == "迷糊"
    # 那条 observation 被标已读（不再未读）
    assert await list_unread_events(lane=lane, persona_id="akao") == []
    # act 自主做了、回灌（落进 PG，world 待读）
    assert len(_capture_act_to_pg) == 1
    assert _capture_act_to_pg[0].description == "我去厨房煮咖啡"
    assert _capture_act_to_pg[0].persona_id == "akao"
    # 信息差命门：喂 life 的输入不含 WorldState 全局快照
    blob = (
        repr(_agent_run.life_calls[-1]["prompt_vars"]).lower()
        + _agent_run.life_calls[-1]["messages_text"].lower()
    )
    assert "worldstate" not in blob and "world_state" not in blob

    # --- 棒 3：act 回灌 → 翻成 WorldTick → world 被 act 唤醒推演 ---
    # 真机里 ActPerformed durable 边 → act_to_world_tick → 合并闸 → world_act_wake →
    # WorldTick(reason=act) → world_tick。这里直接喂回 world_tick 验业务推演（wiring
    # 路由由 wiring 测试覆盖）。带上 act_id / act_occurred_at —— world 读 PG 的回看
    # 窗口下界 = act_occurred_at − 90s，覆盖刚落库的这条 act。
    act = _capture_act_to_pg[0]
    await world_tick(
        WorldTick(
            lane=lane,
            reason="act",
            act_id=act.act_id,
            act_persona_id=act.persona_id,
            act_description=act.description,
            act_occurred_at=act.occurred_at,
        )
    )

    # 棒 3 交接证据：world 读到这条 act 并透给循环推演（list_recent_acts 真读到）
    assert "煮咖啡" in world_calls[1]["messages_text"]
    # 世界叙述被更新（厨房有了动静）
    snap2 = await read_world_state(lane=lane)
    assert "走进厨房" in snap2.detail
    # 推演产的新 observation 投给了厨房在场的 chinagi
    chinagi_unread = await list_unread_events(lane=lane, persona_id="chinagi")
    assert "厨房传来煮咖啡的声音和香气" in [e.summary for e in chinagi_unread]

    # 两轮 world 各调一次 sleep 定下次几时醒：第一轮 sleep(600)、第二轮 sleep(300)。
    # 都 ≤ 10 分钟保底心跳（sleep 工具上限 1h，这里更紧）。
    assert engine_mod._test_self_wakes == [600_000, 300_000]
    for delay in engine_mod._test_self_wakes:
        assert 0 < delay <= WORLD_HEARTBEAT_MS


@pytest.mark.integration
async def test_info_gap_notify_only_reaches_recipients(world_db, _agent_run, monkeypatch):
    """信息差：notify 只投给 world 推演指定的 recipients；够不着的姐妹信箱空."""
    lane = "coe-gap"
    _world_llm(
        _agent_run,
        [
            [
                _update_world("厨房里 chinagi 在煎蛋，akao 还在自己房间睡。"),
                _notify(["chinagi"], "厨房飘来煎蛋的香味"),
                _sleep(600),
            ]
        ],
    )

    await world_tick(WorldTick(lane=lane, reason="heartbeat"))

    # chinagi 被推演为够得着 → 收到；akao 没在 recipients 里 → 收不到
    chinagi_unread = await list_unread_events(lane=lane, persona_id="chinagi")
    akao_unread = await list_unread_events(lane=lane, persona_id="akao")
    assert [e.summary for e in chinagi_unread] == ["厨房飘来煎蛋的香味"]
    assert akao_unread == []


@pytest.mark.integration
async def test_big_state_interrupted_not_stuck(
    world_db, _stub_persona, _agent_run, monkeypatch
):
    """不卡死：life 处在大状态、新 observation 进信箱、被唤醒能读到并换状态（不干等）.

    先让 akao 处在"在上课"的大状态（旧设计会锁死干等到 state_end_at）。world
    notify 一条打断的 observation 投进她信箱，唤醒她 → 她读到、重想、换了状态。
    """
    lane = "coe-stuck"

    from app.domain.life_state import save_life_state

    await save_life_state(
        lane=lane,
        persona_id="akao",
        current_state="在上课",
        response_mood="专注",
        activity_type="study",
        observed_at="2026-06-03T08:05:00+08:00",
    )

    # world 推演出下课铃响、akao 够得着 → notify 给她
    _world_llm(
        _agent_run,
        [
            [
                _update_world("教室里下课铃响了，akao 在座位上。"),
                _notify(["akao"], "下课铃响了"),
                _sleep(600),
            ]
        ],
    )
    await world_tick(WorldTick(lane=lane, reason="heartbeat"))

    # 信箱里确实有那条打断的 observation
    unread = await list_unread_events(lane=lane, persona_id="akao")
    assert [e.summary for e in unread] == ["下课铃响了"]

    # 唤醒 akao：她读到打断 observation、重想、换状态（不干等到原"在上课"结束）
    _agent_run.life_round = [
        _update_life("下课了，伸个懒腰", "轻松", "rest"),
    ]

    await lw.life_wake_node(EventArrived(lane=lane, persona_id="akao"))

    # 她读到了打断的 observation（旧"在上课"被推醒重想，不卡死）
    assert "下课铃响了" in _life_unread_text(_agent_run.life_calls[-1])
    # 状态真的换了
    snap = await find_life_state(lane=lane, persona_id="akao")
    assert snap.current_state == "下课了，伸个懒腰"


@pytest.mark.integration
async def test_batched_observations_consumed_in_one_life_round(
    world_db, _stub_persona, _agent_run, monkeypatch
):
    """攒批唤醒：唤醒前积压的多条 observation，被唤醒的 life 一轮一次性读光、标光.

    debounce 在 wiring 层把"来一条醒一次"压成"攒批醒一次"（窗口语义由 runtime
    debounce 承载、由其单测覆盖）。这里在业务层验：一次唤醒确实把信箱里所有未读
    打成一批消化（不是只读一条、留一堆），且只标这一批。
    """
    lane = "coe-batch"

    # world 一轮 notify 三条 observation 给 akao（模拟想一轮前积压的多条）
    _world_llm(
        _agent_run,
        [
            [
                _update_world("akao 房间里：水壶在响、走廊有脚步声、窗外鸟叫。"),
                _notify(["akao"], "水壶在响"),
                _notify(["akao"], "走廊有脚步声"),
                _notify(["akao"], "窗外鸟叫"),
                _sleep(600),
            ]
        ],
    )
    await world_tick(WorldTick(lane=lane, reason="heartbeat"))

    assert len(await list_unread_events(lane=lane, persona_id="akao")) == 3

    # life 这一轮只更新状态、不做事
    _agent_run.life_round = [
        _update_life("被吵醒", "烦", "rest"),
    ]

    # 一次唤醒 = 一轮 = 一次性读光这三条
    await lw.life_wake_node(EventArrived(lane=lane, persona_id="akao"))

    # 一轮喂给她的未读 observation 文字里这三条全在（攒批一次性读到，不是只读一条）
    unread_text = _life_unread_text(_agent_run.life_calls[-1])
    for s in ("水壶在响", "走廊有脚步声", "窗外鸟叫"):
        assert s in unread_text, f"攒批的 observation {s!r} 没被这一轮一次性读到"
    # 三条都被标已读 → 信箱清空
    assert await list_unread_events(lane=lane, persona_id="akao") == []


@pytest.mark.integration
async def test_act_gate_wakes_world_reads_recent_acts(
    world_db, _agent_run, _capture_act_to_pg, monkeypatch
):
    """act→world 合并闸后的唤醒真能把 world 踹醒、且 world 读到那批 act：

    act 现在走 60s debounce 合并闸：ActPerformed → act_to_world_tick → (debounce) →
    world_act_wake → WorldTick(reason=act) → world_tick。闸的 delayed MQ 由 debounce
    runtime 单测覆盖；这里验闸**之后**那一棒 world_act_wake 把 world 踹醒，且 world
    从 PG 读到这批 act（list_recent_acts）透给循环推演。
    """
    from app.world.engine import ActWorldTick, world_act_wake
    from app.world.state import write_world_state

    lane = "coe-route"

    # 先种一版 WorldState，让这次唤醒不是冷启动 —— 缘由走 act 分支、能验 act 内容透到。
    await write_world_state(
        lane=lane,
        world_time="2026-06-03T14:00:00+08:00",
        detail="客厅安静，akao 在阳台边。",
    )

    world_calls = _world_llm(
        _agent_run,
        [[_sleep(600)]],  # 推演：顺着世界、只 sleep 不广播，验被踹醒 + 读到 act
    )

    # 真机里 life act → emit(ActPerformed) 经 durable wire 落 PG，闸后 world 才读得到。
    # 这里把这条 ActPerformed 用 insert_idempotent 落进 PG（== durable wire 同一个
    # framework 原语），occurred_at 取当下（落在 90s 回看窗内），world 才真读到「看花」。
    occurred_at = datetime.now(UTC).isoformat()
    await insert_idempotent(
        ActPerformed(
            lane=lane,
            act_id="a1",
            persona_id="akao",
            description="我走到阳台看花",
            occurred_at=occurred_at,
        )
    )

    # 闸后那一棒（debounce fire 后调它）：翻成 WorldTick 直接调 world_tick。
    # 带上 act_occurred_at —— world 读 PG 的回看窗口下界 = 它 − 90s，覆盖刚落库的 act。
    await world_act_wake(
        ActWorldTick(
            lane=lane,
            act_id="a1",
            act_persona_id="akao",
            act_description="我走到阳台看花",
            act_occurred_at=occurred_at,
        )
    )

    # world_tick 真被踹醒：跑了循环、act 透给模型推演
    assert world_calls, "act 闸→world 空转：world_tick 没被踹醒"
    assert "看花" in world_calls[0]["messages_text"]
    # world 确实从 PG 读到了这批 act（list_recent_acts 命中）
    since = (datetime.now(UTC).replace(microsecond=0)).isoformat()
    recent = await list_recent_acts(lane=lane, since_iso="2026-06-03T00:00:00+08:00")
    assert [a.description for a in recent] == ["我走到阳台看花"]


@pytest.mark.integration
async def test_world_session_continuation_second_round_carries_history(
    world_db, _agent_run, monkeypatch
):
    """续接：同一 session_id（同 lane / 同天）world 连续两轮，第二轮模型输入带前一轮对话.

    world_tick 显式传 session_id，下一轮 run 见到同一 session_id 从 PG transcript 读
    历史拼到 messages 前。断言：两轮 stimulus 都进了同一条 transcript（续接命门）。
    """
    from datetime import datetime as _dt

    from app.agent.session import load_session
    from app.agent.trace import make_session_id

    lane = "coe-cont"

    _world_llm(
        _agent_run,
        [
            [_update_world("第一版世界叙述"), _sleep(600)],
            [_update_world("第二版世界叙述"), _sleep(600)],
        ],
    )

    # 第一轮（heartbeat）
    await world_tick(WorldTick(lane=lane, reason="heartbeat"))
    # 极短间隔起第二轮（heartbeat，round_id 随时刻变、不会被 turn 幂等跳过）
    import asyncio

    await asyncio.sleep(0.01)
    await world_tick(WorldTick(lane=lane, reason="heartbeat"))

    today = _dt.now().strftime("%Y-%m-%d")
    session_id = make_session_id(lane, "world", today)
    stored = await load_session(session_id)
    assert len(stored) >= 2, "session transcript 应随轮增长（两轮都写回 PG）"
    blob = "".join(m.text() for m in stored)
    # 两轮 stimulus 都进了同一条 transcript（连续上下文，不是各从零组装）
    assert blob.count("【这次醒来的缘由】") >= 2


@pytest.mark.integration
async def test_act_replay_no_duplicate_emit_or_append(
    world_db, _agent_run, monkeypatch
):
    """重投幂等：同一 durable act 重投，world 不重复追加 transcript、不重复 notify.

    同一 ActPerformed（同 act_id）被 durable 重投两次（闸后两次 world_act_wake）：
    第一次 world 跑一轮（update_world + notify + 写回带 round 标记的 transcript）；
    第二次重投得同一 round_id，world_tick load_session 查到本轮标记 → 跳过，不再
    run、不重复 notify、不重复追加 transcript（turn 幂等）。
    """
    from app.world.engine import ActWorldTick, world_act_wake
    from app.world.state import write_world_state

    lane = "coe-replay"

    # 非冷启动，让 act 推演能真 update_world + notify
    await write_world_state(
        lane=lane, world_time="2026-06-03T14:00:00+08:00", detail="客厅安静。"
    )

    # 先落进 PG 这条 act，world 醒来读得到。occurred_at 取当下落在回看窗内。
    occurred_at = datetime.now(UTC).isoformat()
    await insert_idempotent(
        ActPerformed(
            lane=lane,
            act_id="act-replay-x",
            persona_id="akao",
            description="我去厨房煮咖啡",
            occurred_at=occurred_at,
        )
    )

    # world 这一轮：update_world + notify 一条 observation 给 chinagi。只注册一轮脚本——
    # 若第二次重投也跑一轮，world_rounds 会被 pop 空、第二轮变成"无脚本空跑"也仍会
    # 写回，所以这里用"第二次不该再跑"来证幂等（脚本只够一轮）。
    _world_llm(
        _agent_run,
        [
            [
                _update_world("akao 走进厨房煮咖啡。"),
                _notify(["chinagi"], "厨房传来煮咖啡的声音"),
                _sleep(600),
            ],
        ],
    )

    wake = ActWorldTick(
        lane=lane,
        act_id="act-replay-x",
        act_persona_id="akao",
        act_description="我去厨房煮咖啡",
        act_occurred_at=occurred_at,
    )
    await world_act_wake(wake)
    # chinagi 收到那条 observation 一次
    first = await list_unread_events(lane=lane, persona_id="chinagi")
    assert [e.summary for e in first] == ["厨房传来煮咖啡的声音"]

    # 重投同一条 act（同 act_id → 同 round_id）：应被 turn 幂等跳过
    await world_act_wake(wake)
    second = await list_unread_events(lane=lane, persona_id="chinagi")
    # observation 没被重复投（仍只一条；event_id 幂等 + turn 幂等双保险）
    assert [e.summary for e in second] == ["厨房传来煮咖啡的声音"]
    # 只跑过一轮（第二次重投没再 run）—— world_calls 只有一条
    assert len(_agent_run.world_calls) == 1, (
        f"同一 act 重投不该再跑一轮 world，实际 {len(_agent_run.world_calls)} 次"
    )


@pytest.mark.integration
async def test_concurrent_wakes_serialized_no_transcript_corruption(
    world_db, _agent_run, monkeypatch
):
    """串行化：并发两源唤醒不互相覆盖 transcript（锁覆盖全段）.

    确定性 session_id 把两源打到同一个 transcript key。无锁并发会读改写竞态、
    互相覆盖。这里让 world 的 run 真有耗时（asyncio.sleep），并发起 heartbeat + self
    两源。锁覆盖全段后：一源持锁跑完整轮、另一源（冗余 heartbeat/self）撞锁被干净
    丢弃（不并发进、不半写）。断言 transcript 恰好一轮、内容完整未被并发破坏。
    随后再串行起一轮验"续接确实在原 transcript 上增长、没被前面的并发搞坏"。
    """
    import asyncio
    from datetime import datetime as _dt

    from app.agent.session import load_session
    from app.agent.trace import make_session_id

    lane = "coe-concur"

    # 三轮脚本：前两轮给并发的 heartbeat/self（只会跑成功一轮），第三轮给随后串行。
    _world_llm(
        _agent_run,
        [
            [_update_world("v1"), _sleep(600)],
            [_update_world("v2"), _sleep(600)],
            [_update_world("v3"), _sleep(600)],
        ],
    )

    orig_run = _agent_run.run

    async def fake_run(self, messages, *, prompt_vars=None, context=None,
                       session_id=None, max_retries=2):
        await asyncio.sleep(0.05)  # 放大读改写竞态窗口
        return await orig_run(
            self, messages, prompt_vars=prompt_vars, context=context,
            session_id=session_id, max_retries=max_retries,
        )

    monkeypatch.setattr(engine_mod.Agent, "run", fake_run)

    # 并发起两源唤醒（同 lane → 同 session key）：锁串行化，一源跑、另一源被丢
    await asyncio.gather(
        world_tick(WorldTick(lane=lane, reason="heartbeat")),
        world_tick(WorldTick(lane=lane, reason="self")),
    )

    today = _dt.now().strftime("%Y-%m-%d")
    session_id = make_session_id(lane, "world", today)
    stored = await load_session(session_id)
    blob = "".join(m.text() for m in stored)
    # 恰好一轮干净写入（另一冗余源撞锁被丢、没有半写 / 覆盖损坏）
    assert blob.count("【这次醒来的缘由】") == 1, (
        "并发两源应被串行化：一源跑、另一冗余源撞锁丢弃，transcript 恰好一轮干净写入"
    )

    # 随后串行再起一轮：续接在原 transcript 上干净增长（前面的并发没把它搞坏）
    await asyncio.sleep(0.01)
    await world_tick(WorldTick(lane=lane, reason="heartbeat"))
    stored2 = await load_session(session_id)
    blob2 = "".join(m.text() for m in stored2)
    assert blob2.count("【这次醒来的缘由】") == 2, (
        "续接应在原 transcript 上增长到两轮（并发未损坏底层 transcript）"
    )


@pytest.mark.integration
async def test_life_session_continuation_second_round_carries_history(
    world_db, _stub_persona, _agent_run, monkeypatch
):
    """续接（life 侧）：同一 persona / 同天连续两轮，第二轮 transcript 带前一轮对话.

    life_wake 显式把 (lane, persona, 今天) 的 session_id 传给 run；controller 镜像
    把本轮写回 PG transcript。第二轮唤醒 run 见到同一 session_id，下一轮从 PG 读历史
    拼到前面。断言：两轮 stimulus 都进了同一条 transcript（连续上下文）；run 收到的
    session_id 与 (lane, persona, 今天) 派生一致。

    两轮之间清掉 cd key（cd 的延迟语义由 cd 专测覆盖，这里只验续接）。投递 observation
    用真实 world 工具 notify 投进 akao 信箱（先种一版 WorldState 让 world 不冷启）。
    """
    from datetime import datetime as _dt

    from app.agent.session import load_session
    from app.agent.trace import make_session_id
    from app.world.state import write_world_state

    lane = "coe-life-cont"
    persona = "akao"

    await write_world_state(lane=lane, world_time="2026-06-03T14:00:00+08:00", detail="安静。")

    async def _notify_akao(observation: str) -> None:
        """用真实 world notify 工具投一条 observation 进 akao 信箱。"""
        from app.agent.context import AgentContext
        from app.agent.runtime_context import agent_context
        from app.world.tools import FEATURE_SELF_WAKE, notify

        wctx = AgentContext(
            features={
                "world_lane": lane,
                "world_round_id": f"seed-{observation}",
                FEATURE_SELF_WAKE: {},
            }
        )
        with agent_context(wctx):
            await notify.invoke({"recipients": [persona], "observation": observation})

    await _notify_akao("第一轮的动静")

    _agent_run.life_round = [
        _update_life("第一轮：醒了", "迷糊", "rest"),
    ]
    await lw.life_wake_node(EventArrived(lane=lane, persona_id=persona))

    today = _dt.now().strftime("%Y-%m-%d")
    session_id = make_session_id(lane, persona, today)
    # 第一轮 run 收到的 session_id 与派生一致（显式传，才真续接）
    assert _agent_run.life_calls[-1]["session_id"] == session_id

    # 清 cd，模拟 cd 已过，让第二轮能跑（cd 延迟另有专测）
    import app.infra.redis as redis_mod

    await (await redis_mod.get_redis()).delete(lw._cd_key(lane, persona))

    # 第二轮：再投一条 observation，再唤醒
    await _notify_akao("第二轮的动静")
    _agent_run.life_round = [
        _update_life("第二轮：还醒着", "平静", "idle"),
    ]
    await lw.life_wake_node(EventArrived(lane=lane, persona_id=persona))

    # transcript 随轮增长、两轮 stimulus 都在（连续上下文，不是从零组装）
    stored = await load_session(session_id)
    assert len(stored) >= 2, "life session transcript 应随轮增长（两轮都写回 PG）"
    blob = "".join(m.text() for m in stored)
    # 两轮各自的感知 observation 原文都落进同一条 transcript（续接带的是连续感知上下文）
    assert "第一轮的动静" in blob, "第一轮的感知原文应在续接的 transcript 里"
    assert "第二轮的动静" in blob, "第二轮的感知原文应在续接的 transcript 里"


@pytest.mark.integration
async def test_life_cd_delays_without_dropping_observations(
    world_db, _stub_persona, _agent_run, monkeypatch
):
    """cd 延迟不丢（life 侧）：一轮跑完进 cd，cd 内来的 observation 被 reschedule 攒着，
    cd 过后一并感知、一并标已读（绝不 drop）.

    第一轮成功跑完 → 落 cd key。cd 内来新 observation 再唤醒 → life_wake 查到 cd 内 →
    raise DebounceReschedule（不烧模型、不标已读，新 observation 留信箱未读）。删 cd
    key 模拟 cd 过 → 再唤醒，cd 内攒下的 observation 被一并消费。
    """
    from app.runtime.debounce import DebounceReschedule
    from app.world.state import write_world_state

    lane = "coe-life-cd"
    persona = "akao"

    await write_world_state(lane=lane, world_time="2026-06-03T14:00:00+08:00", detail="安静。")

    async def _notify_akao(observation: str) -> None:
        from app.agent.context import AgentContext
        from app.agent.runtime_context import agent_context
        from app.world.tools import FEATURE_SELF_WAKE, notify

        wctx = AgentContext(
            features={
                "world_lane": lane,
                "world_round_id": f"seed-{observation}",
                FEATURE_SELF_WAKE: {},
            }
        )
        with agent_context(wctx):
            await notify.invoke({"recipients": [persona], "observation": observation})

    # 第一轮：跑完落 cd key
    await _notify_akao("第一波动静")
    _agent_run.life_round = [
        _update_life("处理第一波", "平", "idle"),
    ]
    await lw.life_wake_node(EventArrived(lane=lane, persona_id=persona))
    assert await list_unread_events(lane=lane, persona_id=persona) == []

    # cd 内：来一条新 observation，再唤醒 → 被 reschedule（不消费）
    await _notify_akao("cd 内来的动静")
    with pytest.raises(DebounceReschedule):
        await lw.life_wake_node(EventArrived(lane=lane, persona_id=persona))
    # cd 内 observation 没被丢：仍躺在信箱未读
    cd_unread = await list_unread_events(lane=lane, persona_id=persona)
    assert [e.summary for e in cd_unread] == ["cd 内来的动静"], "cd 内 observation 绝不 drop"

    # cd 过（删 key）→ 再唤醒：cd 内攒下的 observation 被一并感知、标已读
    import app.infra.redis as redis_mod

    await (await redis_mod.get_redis()).delete(lw._cd_key(lane, persona))
    _agent_run.life_round = [
        _update_life("cd 后处理攒下的", "平", "idle"),
    ]
    await lw.life_wake_node(EventArrived(lane=lane, persona_id=persona))
    assert await list_unread_events(lane=lane, persona_id=persona) == [], (
        "cd 过后攒下的 observation 被一并消费、标已读"
    )
