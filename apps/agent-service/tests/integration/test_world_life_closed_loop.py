"""world + life event 闭环端到端集成 — stage3 联调收口（最高风险一环）.

task1（event 骨架）、task2（world engine）、task3（life 三姐妹）各自单测都绿，
但拼起来世界是死的。这个文件证明拼起来**世界真能动**：一条 event 从 world 产生
→ 投给在场者 → life 信箱收到 → life 被唤醒想一轮 → 更新主观快照 + 起意图 →
意图回灌翻成 WorldTick → world 被 intent 唤醒裁决 → 应用在场变更 / 产新 event。

真 Postgres（testcontainers），只 mock 一处 LLM：``Agent.run``——按 ``cfg.prompt_id``
把 world / life 两条循环分流，各自在真实 ``agent_context`` 下回放脚本里的工具调用
（world 的 move_persona / emit_event / sleep；life 的 update_life_state /
raise_intent），所以工具的真实 DB 副作用全发生。别的全走真实持久化：mock 掉持久化
等于什么都没测。world 的 sleep 自排打桩成记录 delay，不连 RabbitMQ。

钉死的验证点（对应 stage3 交付 C）：
  * 完整闭环每一棒交接成功；
  * 不卡死：life 处在大状态、新 event 进信箱、被唤醒能读到并换状态；
  * 信息差：不在场的姐妹收不到 event；life_wake 输入不含 WorldState；
  * world 最长不睡过 10 分钟（自排 delay ≤ 600000ms）；
  * 在场会动：节律驱动 + 意图裁决两条路径都证明 set_presence 真被调、在场真变。
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest

import app.nodes.life_wake as lw
import app.world.engine as engine_mod
import app.world.tools as tools_mod
from app.agent.runtime_context import agent_context
from app.data.queries.mailbox import list_unread_events
from app.domain.life_state import LifeState, find_life_state
from app.domain.world_events import (
    EventArrived,
    EventEnvelope,
    EventRead,
    IntentRaised,
)
from app.world.engine import (
    WORLD_HEARTBEAT_MS,
    WorldTick,
    intent_to_world_tick,
    world_tick,
)
from app.world.state import (
    RoomPresence,
    WorldState,
    read_presence,
)
from tests.runtime.conftest import migrate

# world 一轮的脚本化行动：模型在循环里调的工具序列。each = (tool_name, args)。
# mock 的 Agent.run 在真实 agent_context 下依次回放它们，真实 DB 副作用全发生。
WorldRound = list[tuple[str, dict]]


def _move(persona_id: str, room_id: str) -> tuple[str, dict]:
    return ("move_persona", {"persona_id": persona_id, "room_id": room_id})


def _emit(room_id: str, summary: str) -> tuple[str, dict]:
    return ("emit_event", {"room_id": room_id, "summary": summary})


def _sleep(seconds: int) -> tuple[str, dict]:
    return ("sleep", {"seconds": seconds})


@pytest.fixture(autouse=True)
def _fake_redis(monkeypatch):
    """In-memory redis for the life_wake single-flight lock (必改 2).

    ``life_wake_node`` takes a ``(lane, persona)`` single-flight lock every
    round; this闭环集成测试直接调它，得有 redis。用 fakeredis 保持测试自包含、
    不连真实 redis（与 capability / single_flight 测试同款模式）。
    """
    import app.infra.redis as redis_mod

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_mod, "_redis", fake)


@pytest.fixture
async def world_db(test_db):
    """建齐闭环需要的所有真实表：world 快照 / 在场 / 信箱 / 已读 / life 快照。"""
    await migrate(WorldState, test_db)
    await migrate(RoomPresence, test_db)
    await migrate(EventEnvelope, test_db)
    await migrate(EventRead, test_db)
    await migrate(LifeState, test_db)
    yield test_db


class _AgentRunController:
    """一处 mock ``Agent.run``，按 ``cfg.prompt_id`` 把 world / life 两条循环分流。

    world 和 life 都跑 ``Agent.run``——共享同一个 Agent 类。所以这里只 patch 一次
    run，按 ``self._cfg.prompt_id``（"world_deliberate" / "life_wake"）分到各自的
    脚本回放。回放在 run 拿到的真实 ``context`` 下、用真实 ``self._tools`` invoke
    工具，所以工具的真实 DB 副作用全发生（不 mock 持久化）。

    world 脚本：每次唤醒回放一轮工具调用（move/emit/sleep）。
    life 脚本：单轮工具调用（update_life_state / raise_intent），用 ``life_round``。
    """

    def __init__(self) -> None:
        self.world_rounds: list[WorldRound] = []
        self.world_calls: list[dict] = []
        self.life_round: list[tuple[str, dict]] = []
        self.life_calls: list[dict] = []

    async def run(
        self, agent, messages, *, prompt_vars=None, context=None, max_retries=2
    ):
        from app.agent.neutral import Message, Role

        prompt_id = agent._cfg.prompt_id
        by_name = {t.name: t for t in agent._tools}
        if prompt_id == "world_deliberate":
            blob = "".join(m.text() for m in messages)
            self.world_calls.append({"messages_text": blob, "context": context})
            script = self.world_rounds.pop(0) if self.world_rounds else []
        else:  # life_wake
            self.life_calls.append(
                {
                    "prompt_vars": prompt_vars,
                    "context": context,
                    "persona_id": context.persona_id if context else None,
                }
            )
            script = list(self.life_round)
        with agent_context(context):
            for tool_name, args in script:
                await by_name[tool_name].invoke(args)
        return Message(role=Role.ASSISTANT, content="")


@pytest.fixture(autouse=True)
def _agent_run(monkeypatch):
    """安装统一的 ``Agent.run`` mock（world + life 分流），整文件共用。"""
    ctl = _AgentRunController()

    async def fake_run(self, messages, *, prompt_vars=None, context=None, max_retries=2):
        return await ctl.run(
            self, messages, prompt_vars=prompt_vars, context=context,
            max_retries=max_retries,
        )

    monkeypatch.setattr(engine_mod.Agent, "run", fake_run)
    return ctl


def _world_llm(ctl: _AgentRunController, scripted: list[WorldRound]):
    """注册 world 每次唤醒回放的工具调用脚本，返回 world_calls 供断言。"""
    ctl.world_rounds = list(scripted)
    return ctl.world_calls


def _life_unread_text(captured: dict) -> str:
    """从 life 这一轮的 prompt_vars 取她信箱里那批未读 event 的文字（验信息差 / 攒批）。"""
    return str((captured.get("prompt_vars") or {}).get("unread_events", ""))


@pytest.fixture(autouse=True)
def _stub_self_wake(monkeypatch):
    """world sleep 自排打桩成记录 delay（不连 RabbitMQ）。

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
    world_db, _stub_persona, _agent_run, monkeypatch
):
    """整条闭环从头跑到尾，断言每一棒交接成功（最致命的一条集成测试）。

    棒次：
      1. world 冷启动 → set 三姐妹初始在场 + 产 event 投给在场的 akao。
      2. life（akao）被唤醒 → 读信箱拿到那条 event → 想一轮 → 存新 LifeState +
         起意图。
      3. 意图回灌 → 翻成 WorldTick(reason=intent) → world 被 intent 唤醒裁决 →
         应用在场变更（akao→kitchen）+ 产新 event。
    """
    lane = "coe-loop"

    # --- 棒 1：world 冷启动 ---
    world_calls = _world_llm(
        _agent_run,
        [
            # 第一次唤醒（冷启动）：放置三姐妹 + 在 akao 房间产一条 event。
            # 先挪人再 emit（顺序命门：刚放置好的人要收到锚她房间的 event）。
            [
                _move("chinagi", "kitchen"),
                _move("akao", "akao_room"),
                _move("ayana", "ayana_room"),
                _emit("akao_room", "晌午的光照进房间"),
                _sleep(600),
            ],
            # 第二次唤醒（intent 裁决）：把 akao 挪进 kitchen + 产新 event。
            # 赤尾刚进厨房煮咖啡，world 想几分钟后再看一眼出锅没 → sleep(300)。
            [
                _move("akao", "kitchen"),
                _emit("kitchen", "赤尾走进了厨房"),
                _sleep(300),
            ],
        ],
    )

    await world_tick(WorldTick(lane=lane, reason="heartbeat"))

    # 棒 1 交接证据：三姐妹被放置（在场真写进库）
    assert await read_presence(lane=lane, persona_id="chinagi") == "kitchen"
    assert await read_presence(lane=lane, persona_id="akao") == "akao_room"
    assert await read_presence(lane=lane, persona_id="ayana") == "ayana_room"
    # 冷启动确实走了 agent 循环、缘由告诉模型这是首次醒来（不是硬编死表）
    assert "冷启动" in world_calls[0]["messages_text"] or "首次" in world_calls[0]["messages_text"]
    # event 投进了在场的 akao 信箱（锚 akao_room）
    akao_unread = await list_unread_events(lane=lane, persona_id="akao")
    assert [e.summary for e in akao_unread] == ["晌午的光照进房间"]

    # --- 棒 2：life（akao）被唤醒想一轮 ---
    # life 这一轮的工具调用：更新状态 + 起意图（去厨房煮咖啡）。
    _agent_run.life_round = [
        ("update_life_state", {
            "current_state": "醒了，想去厨房找吃的",
            "response_mood": "迷糊",
            "activity_type": "move",
        }),
        ("raise_intent", {"summary": "我想去厨房煮咖啡"}),
    ]

    intents: list[IntentRaised] = []

    async def capture_raise_intent(*, lane, intent_id, persona_id, summary, occurred_at):
        intents.append(
            IntentRaised(
                lane=lane,
                intent_id=intent_id,
                persona_id=persona_id,
                summary=summary,
                occurred_at=occurred_at,
            )
        )

    # raise_intent handler 由 life_tools 模块级引用，patch 那里才拦得住。
    import app.nodes.life_tools as life_tools_mod

    monkeypatch.setattr(life_tools_mod, "raise_intent", capture_raise_intent)

    await lw.life_wake_node(EventArrived(lane=lane, persona_id="akao"))

    # 棒 2 交接证据：新 LifeState 落库且可读到最新
    snap = await find_life_state(lane=lane, persona_id="akao")
    assert snap is not None
    assert snap.current_state == "醒了，想去厨房找吃的"
    assert snap.response_mood == "迷糊"
    # 那条 event 被标已读（不再未读）
    assert await list_unread_events(lane=lane, persona_id="akao") == []
    # 起了意图、回灌
    assert len(intents) == 1
    assert intents[0].summary == "我想去厨房煮咖啡"
    # 信息差命门：喂 life 的输入不含 WorldState
    blob = repr(_agent_run.life_calls[-1]["prompt_vars"]).lower()
    assert "worldstate" not in blob and "world_state" not in blob

    # --- 棒 3：意图回灌 → 翻成 WorldTick → world 被 intent 唤醒裁决 ---
    # intent_to_world_tick emit 的 WorldTick 经 in-process 边接回 world_tick。
    # 这里直接喂回 world_tick 验业务裁决（wiring 路由由 wiring 测试覆盖）。
    intent = intents[0]
    await world_tick(
        WorldTick(
            lane=lane,
            reason="intent",
            intent_persona_id=intent.persona_id,
            intent_summary=intent.summary,
        )
    )

    # 棒 3 交接证据：意图被透给 world 循环裁决
    assert "煮咖啡" in world_calls[1]["messages_text"]
    # 在场真的动了：akao 从 akao_room 挪进 kitchen
    assert await read_presence(lane=lane, persona_id="akao") == "kitchen"
    # 裁决产的新 event 投进了厨房在场者（chinagi + 刚挪进来的 akao）
    chinagi_unread = await list_unread_events(lane=lane, persona_id="chinagi")
    akao_unread2 = await list_unread_events(lane=lane, persona_id="akao")
    assert "赤尾走进了厨房" in [e.summary for e in chinagi_unread]
    assert "赤尾走进了厨房" in [e.summary for e in akao_unread2]

    # 两轮 world 各调一次 sleep 定下次几时醒：第一轮 sleep(600)、第二轮 sleep(300)。
    # 都 ≤ 10 分钟保底心跳（sleep 工具上限 1h，这里更紧）。world 用 sleep 自排，
    # 不许排长闹钟把世界睡死。
    assert engine_mod._test_self_wakes == [600_000, 300_000]
    for delay in engine_mod._test_self_wakes:
        assert 0 < delay <= WORLD_HEARTBEAT_MS


@pytest.mark.integration
async def test_info_gap_absent_sister_gets_nothing(world_db, _agent_run, monkeypatch):
    """信息差：event 锚 kitchen，只投给在场 kitchen 的人；不在场的姐妹信箱空。"""
    lane = "coe-gap"
    _world_llm(
        _agent_run,
        [
            [
                _move("chinagi", "kitchen"),
                _move("akao", "akao_room"),
                _emit("kitchen", "厨房飘来煎蛋的香味"),
                _sleep(600),
            ]
        ],
    )

    await world_tick(WorldTick(lane=lane, reason="heartbeat"))

    # chinagi 在 kitchen → 收到；akao 在房间睡 → 收不到（物理上够不着）
    chinagi_unread = await list_unread_events(lane=lane, persona_id="chinagi")
    akao_unread = await list_unread_events(lane=lane, persona_id="akao")
    assert [e.summary for e in chinagi_unread] == ["厨房飘来煎蛋的香味"]
    assert akao_unread == []


@pytest.mark.integration
async def test_big_state_interrupted_not_stuck(
    world_db, _stub_persona, _agent_run, monkeypatch
):
    """不卡死：life 处在大状态、新 event 进信箱、被唤醒能读到并换状态（不干等）。

    先让 akao 处在"在上课"的大状态（旧设计会锁死干等到 state_end_at）。world
    产一条打断的 event 投进她信箱，唤醒她 → 她读到、重想、换了状态。
    """
    lane = "coe-stuck"

    # akao 已在 classroom、处在"在上课"大状态
    from app.domain.life_state import save_life_state
    from app.world.state import set_presence

    await set_presence(lane=lane, persona_id="akao", room_id="classroom")
    await save_life_state(
        lane=lane,
        persona_id="akao",
        current_state="在上课",
        response_mood="专注",
        activity_type="study",
        observed_at="2026-06-03T08:05:00+08:00",
    )

    # world 在 classroom 产一条"下课铃响了"投给在场的 akao
    _world_llm(
        _agent_run,
        [
            [
                _emit("classroom", "下课铃响了"),
                _sleep(600),
            ]
        ],
    )
    await world_tick(WorldTick(lane=lane, reason="heartbeat"))

    # 信箱里确实有那条打断的 event
    unread = await list_unread_events(lane=lane, persona_id="akao")
    assert [e.summary for e in unread] == ["下课铃响了"]

    # 唤醒 akao：她读到打断 event、重想、换状态（不干等到原"在上课"结束）
    _agent_run.life_round = [
        ("update_life_state", {
            "current_state": "下课了，伸个懒腰",
            "response_mood": "轻松",
            "activity_type": "rest",
        }),
    ]

    await lw.life_wake_node(EventArrived(lane=lane, persona_id="akao"))

    # 她读到了打断的 event（旧"在上课"被推醒重想，不卡死）
    assert "下课铃响了" in _life_unread_text(_agent_run.life_calls[-1])
    # 状态真的换了
    snap = await find_life_state(lane=lane, persona_id="akao")
    assert snap.current_state == "下课了，伸个懒腰"


@pytest.mark.integration
async def test_batched_events_consumed_in_one_life_round(
    world_db, _stub_persona, _agent_run, monkeypatch
):
    """攒批唤醒：唤醒前积压的多条 event，被唤醒的 life 一轮一次性读光、标光。

    debounce 在 wiring 层把"来一条醒一次"压成"攒批醒一次"（窗口语义由 runtime
    debounce 承载、由其单测覆盖）。这里在业务层验：一次唤醒确实把信箱里所有未读
    打成一批消化（不是只读一条、留一堆），且只标这一批。
    """
    lane = "coe-batch"

    # world 一轮产了三条 event 投给在场的 akao（模拟想一轮前积压的多条）
    _world_llm(
        _agent_run,
        [
            [
                _move("akao", "akao_room"),
                _emit("akao_room", "水壶在响"),
                _emit("akao_room", "走廊有脚步声"),
                _emit("akao_room", "窗外鸟叫"),
                _sleep(600),
            ]
        ],
    )
    await world_tick(WorldTick(lane=lane, reason="heartbeat"))

    assert len(await list_unread_events(lane=lane, persona_id="akao")) == 3

    # life 这一轮只更新状态、不起意图
    _agent_run.life_round = [
        ("update_life_state", {
            "current_state": "被吵醒", "response_mood": "烦", "activity_type": "rest",
        }),
    ]

    # 一次唤醒 = 一轮 = 一次性读光这三条
    await lw.life_wake_node(EventArrived(lane=lane, persona_id="akao"))

    # 一轮喂给她的未读 event 文字里这三条全在（攒批一次性读到，不是只读一条）
    unread_text = _life_unread_text(_agent_run.life_calls[-1])
    for s in ("水壶在响", "走廊有脚步声", "窗外鸟叫"):
        assert s in unread_text, f"攒批的 event {s!r} 没被这一轮一次性读到"
    # 三条都被标已读 → 信箱清空
    assert await list_unread_events(lane=lane, persona_id="akao") == []


@pytest.mark.integration
async def test_intent_translation_routes_to_world_in_process(
    world_db, _agent_run, monkeypatch
):
    """意图翻译边真能把 world 踹醒：intent_to_world_tick emit 的 WorldTick 经
    in-process wiring 边落到 world_tick，world 读快照、按 intent 裁决。

    这条走真实 ``emit`` + 编译图，证明 stage3 的 intent→world 回环不是空转。
    """
    import importlib

    import app.wiring.life_dataflow as ld
    from app.runtime.emit import reset_emit_runtime
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring

    # 重建 wiring + 重置编译图，让 emit(WorldTick) 走真实图
    clear_wiring()
    clear_bindings()
    importlib.reload(ld)
    reset_emit_runtime()

    lane = "coe-route"

    # 先种一版 WorldState，让这次唤醒不是冷启动 —— 这样缘由走 intent 分支、能验
    # 意图内容经真实 emit 路由透到了 world 循环。
    from app.world.state import write_world_state

    await write_world_state(
        lane=lane,
        world_time="2026-06-03T14:00:00+08:00",
        situation="",
    )

    world_calls = _world_llm(
        _agent_run,
        [[_sleep(600)]],  # 裁决：克制不产 event，只 sleep，验被踹醒
    )

    try:
        # 走真实翻译节点 → 它 emit WorldTick → in-process 边落到 world_tick
        await intent_to_world_tick(
            IntentRaised(
                lane=lane,
                intent_id="i1",
                persona_id="akao",
                summary="我想去阳台看花",
                occurred_at="2026-06-03T14:00:00+08:00",
            )
        )
    finally:
        reset_emit_runtime()

    # world_tick 真被踹醒：跑了循环、意图透给模型裁决
    assert world_calls, "intent→world 回环空转：world_tick 没被 emit 路由踹醒"
    assert "看花" in world_calls[0]["messages_text"]
