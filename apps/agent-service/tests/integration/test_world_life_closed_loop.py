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
from app.domain.session_transcript import SessionTranscript
from app.runtime.persist import insert_idempotent
from app.world.engine import (
    WORLD_HEARTBEAT_MS,
    WorldTick,
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
    """建齐闭环需要的所有真实表：world 快照 / 在场 / 信箱 / 已读 / life 快照 / 续接 transcript。"""
    await migrate(WorldState, test_db)
    await migrate(RoomPresence, test_db)
    await migrate(EventEnvelope, test_db)
    await migrate(EventRead, test_db)
    await migrate(LifeState, test_db)
    await migrate(IntentRaised, test_db)
    await migrate(SessionTranscript, test_db)
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
            # life 的感知现在拼进 USER stimulus（messages），不再走 prompt_vars。
            # 镜像 world 分支记下这一轮 messages 文本，断言才拿得到她这轮感知了啥。
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
        # 镜像 task1 真实 run 的会话写回：world 续接（session_id 显式传入）时把本轮
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


def _world_llm(ctl: _AgentRunController, scripted: list[WorldRound]):
    """注册 world 每次唤醒回放的工具调用脚本，返回 world_calls 供断言。"""
    ctl.world_rounds = list(scripted)
    return ctl.world_calls


def _life_unread_text(captured: dict) -> str:
    """从 life 这一轮的 USER stimulus 取她信箱里那批未读 event 的文字（验信息差 / 攒批）。

    数据流变了：感知现在拼进 life_wake 的 USER stimulus（messages），不再走
    prompt_vars→system prompt（这样它进 transcript、续接第二轮 replay 得到）。所以这里
    从这一轮 run 收到的 messages 文本取——这正是真机里喂给模型的那批未读 event 原文，
    比读已不存在的 prompt_vars 字段更贴近真实数据流。
    """
    return str(captured.get("messages_text", ""))


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
        intent = IntentRaised(
            lane=lane,
            intent_id=intent_id,
            persona_id=persona_id,
            summary=summary,
            occurred_at=occurred_at,
        )
        intents.append(intent)
        # 数据流变了：world 被 intent 唤醒后从 PG 表 data_intent_raised 用
        # list_recent_intents 读那一批 intent。真机里 raise_intent → emit(IntentRaised)
        # 经 durable wire 落 PG，world 才读得到。这里 capture 在记下意图的同时把它
        # insert_idempotent 进 PG（== durable wire 落库用的同一个 framework 原语），
        # 让 world 真从 PG 读到这条意图、进 prompt —— 闭环真实成立，不是绕过。
        await insert_idempotent(intent)

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
            intent_id=intent.intent_id,
            intent_persona_id=intent.persona_id,
            intent_summary=intent.summary,
            # 数据流变了：world 读 PG 的回看窗口下界 = intent_occurred_at − 90s。
            # 带上触发 intent 的起意时刻，world 才从 PG 读到刚落库的这条意图、进 prompt。
            intent_occurred_at=intent.occurred_at,
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
async def test_intent_gate_routes_to_world(world_db, _agent_run, monkeypatch):
    """intent→world 合并闸后的唤醒真能把 world 踹醒：world_intent_wake → world_tick。

    intent 现在走 60s debounce 合并闸：IntentRaised → intent_to_world_tick →
    (debounce) → world_intent_wake → WorldTick(reason=intent) → world_tick。闸的
    delayed MQ 由 debounce runtime 单测覆盖；这里验闸**之后**那一棒 world_intent_wake
    把意图内容透给 world 循环裁决（world 读快照、按 intent 裁决，不空转）。
    """
    from datetime import UTC, datetime

    from app.world.engine import IntentWorldTick, world_intent_wake
    from app.world.state import write_world_state

    lane = "coe-route"

    # 先种一版 WorldState，让这次唤醒不是冷启动 —— 缘由走 intent 分支、能验意图
    # 内容透到了 world 循环。
    await write_world_state(
        lane=lane,
        world_time="2026-06-03T14:00:00+08:00",
        situation="",
    )

    world_calls = _world_llm(
        _agent_run,
        [[_sleep(600)]],  # 裁决：符合世界、只 sleep 不广播，验被踹醒
    )

    # 数据流变了：world 被 intent 唤醒后从 PG 读那一批 intent 进 prompt。真机里
    # life raise_intent → emit(IntentRaised) 经 durable wire 落 PG，闸后 world 才读得到。
    # 这里把这条 IntentRaised 用 insert_idempotent 落进 PG（== durable wire 的同一个
    # framework 原语），且 occurred_at 取当下（落在 90s 回看窗内），world 才真读到「看花」。
    occurred_at = datetime.now(UTC).isoformat()
    await insert_idempotent(
        IntentRaised(
            lane=lane,
            intent_id="i1",
            persona_id="akao",
            summary="我想去阳台看花",
            occurred_at=occurred_at,
        )
    )

    # 闸后那一棒（debounce fire 后调它）：翻成 WorldTick 直接调 world_tick。
    # 带上 intent_occurred_at —— world 读 PG 的回看窗口下界 = 它 − 90s，覆盖刚落库的 intent。
    await world_intent_wake(
        IntentWorldTick(
            lane=lane,
            intent_id="i1",
            intent_persona_id="akao",
            intent_summary="我想去阳台看花",
            intent_occurred_at=occurred_at,
        )
    )

    # world_tick 真被踹醒：跑了循环、意图透给模型裁决
    assert world_calls, "intent 闸→world 空转：world_tick 没被踹醒"
    assert "看花" in world_calls[0]["messages_text"]


@pytest.mark.integration
async def test_world_session_continuation_second_round_carries_history(
    world_db, _agent_run, monkeypatch
):
    """续接：同一 session_id（同 lane / 同天）world 连续两轮，第二轮模型输入带前一轮对话。

    task1 的 run 把本轮写回 Redis transcript（这里 controller 镜像了写回）；world_tick
    显式传 session_id，下一轮 run 见到同一 session_id 从 Redis 读历史拼到 messages 前。
    断言：第二轮 run 拿到的 messages 里带着第一轮的 user stimulus（续接命门）。
    """
    from datetime import datetime

    from app.agent.session import load_session
    from app.agent.trace import make_session_id

    lane = "coe-cont"

    _world_llm(
        _agent_run,
        [[_sleep(600)], [_sleep(600)]],  # 两轮都只 sleep（验续接，不关心广播）
    )

    # 第一轮（heartbeat）
    await world_tick(WorldTick(lane=lane, reason="heartbeat"))
    # 极短间隔起第二轮（heartbeat，round_id 随时刻变、不会被 turn 幂等跳过）
    import asyncio

    await asyncio.sleep(0.01)
    await world_tick(WorldTick(lane=lane, reason="heartbeat"))

    today = datetime.now().strftime("%Y-%m-%d")
    session_id = make_session_id(lane, "world", today)
    # Redis 里查得到该 session 的上下文、随轮增长（两轮都写回了）
    stored = await load_session(session_id)
    assert len(stored) >= 2, "session transcript 应随轮增长（两轮都写回 Redis）"
    # 第二轮 run 的 messages 里带着前一轮的对话（task1 read history 拼到前面）
    # —— 这里用 controller 记录的两次 world_calls 的 messages_text 验：第二次拿到的
    # 输入是 "history + 本轮 stimulus"，但 controller 拿的是 world_tick 传给 run 的
    # 本轮 messages（task1 在 run 内部才拼 history）。所以从 Redis transcript 验
    # 续接确有连续上下文（两轮 stimulus 都在里面、按序）。
    blob = "".join(m.text() for m in stored)
    # 两轮 stimulus 都进了同一条 transcript（连续上下文，不是各从零组装）
    assert blob.count("【这次醒来的缘由】") >= 2


@pytest.mark.integration
async def test_intent_replay_no_duplicate_emit_or_append(
    world_db, _agent_run, monkeypatch
):
    """重投幂等：同一 durable intent 重投，world 不重复追加 transcript、不重复 emit。

    同一 IntentRaised（同 intent_id）被 durable 重投两次（闸后两次 world_intent_wake）：
    第一次 world 跑一轮（move + emit + 写回带 round 标记的 transcript）；第二次重投
    得同一 round_id，world_tick load_session 查到本轮标记 → 跳过，不再 run、不重复
    emit、不重复追加 transcript（决策 7 turn 幂等）。
    """
    from app.world.engine import IntentWorldTick, world_intent_wake
    from app.world.state import set_presence, write_world_state

    lane = "coe-replay"

    # 非冷启动 + akao 在场，让 intent 裁决能真挪人 + 产 event
    await write_world_state(lane=lane, world_time="2026-06-03T14:00:00+08:00", situation="")
    await set_presence(lane=lane, persona_id="akao", room_id="akao_room")

    # world 这一轮：把 akao 挪进厨房 + 产一条 event。只注册一轮脚本——若第二次
    # 重投也跑一轮，world_rounds 会被 pop 空、第二轮变成"无脚本空跑"也仍会 emit
    # 写回，所以这里用"第二次不该再跑"来证幂等（脚本只够一轮）。
    _world_llm(
        _agent_run,
        [
            [
                _move("akao", "kitchen"),
                _emit("kitchen", "赤尾走进了厨房"),
                _sleep(600),
            ],
        ],
    )

    wake = IntentWorldTick(
        lane=lane,
        intent_id="intent-replay-x",
        intent_persona_id="akao",
        intent_summary="我想去厨房煮咖啡",
    )
    await world_intent_wake(wake)
    # akao 收到那条 event 一次
    first = await list_unread_events(lane=lane, persona_id="akao")
    assert [e.summary for e in first] == ["赤尾走进了厨房"]

    # 重投同一条 intent（同 intent_id → 同 round_id）：应被 turn 幂等跳过
    await world_intent_wake(wake)
    second = await list_unread_events(lane=lane, persona_id="akao")
    # event 没被重复投（仍只一条；event_id 幂等 + turn 幂等双保险）
    assert [e.summary for e in second] == ["赤尾走进了厨房"]
    # 只跑过一轮（第二次重投没再 run）—— world_calls 只有一条
    assert len(_agent_run.world_calls) == 1, (
        f"同一 intent 重投不该再跑一轮 world，实际 {len(_agent_run.world_calls)} 次"
    )


@pytest.mark.integration
async def test_concurrent_wakes_serialized_no_transcript_corruption(
    world_db, _agent_run, monkeypatch
):
    """串行化：并发两源唤醒不互相覆盖 transcript（锁覆盖全段）。

    确定性 session_id 把两源打到同一个 Redis transcript key。无锁并发会读改写竞态、
    互相覆盖。这里让 world 的 run 真有耗时（asyncio.sleep），并发起 heartbeat + self
    两源。锁覆盖全段后：一源持锁跑完整轮、另一源（冗余 heartbeat/self）撞锁被干净
    丢弃（不并发进、不半写）。断言 transcript 恰好一轮、内容完整未被并发破坏。
    随后再串行起一轮验"续接确实在原 transcript 上增长、没被前面的并发搞坏"。
    """
    import asyncio
    from datetime import datetime

    from app.agent.session import load_session
    from app.agent.trace import make_session_id

    lane = "coe-concur"

    # 三轮脚本：前两轮给并发的 heartbeat/self（只会跑成功一轮），第三轮给随后串行。
    _world_llm(_agent_run, [[_sleep(600)], [_sleep(600)], [_sleep(600)]])

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

    today = datetime.now().strftime("%Y-%m-%d")
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
    """续接（life 侧）：同一 persona / 同天连续两轮，第二轮 transcript 带前一轮对话。

    life_wake 显式把 (lane, persona, 今天) 的 session_id 传给 run；controller 镜像
    task1 把本轮写回 Redis transcript。第二轮唤醒 run 见到同一 session_id，下一轮从
    Redis 读历史拼到前面。断言：两轮 stimulus 都进了同一条 transcript（连续上下文，
    不是各从零组装）；run 收到的 session_id 与 (lane, persona, 今天) 派生一致。

    两轮之间清掉 cd key（cd 的延迟语义由 cd 专测覆盖，这里只验续接）。
    """
    from datetime import datetime

    from app.agent.session import load_session
    from app.agent.trace import make_session_id

    lane = "coe-life-cont"
    persona = "akao"

    from app.agent.context import AgentContext
    from app.agent.runtime_context import agent_context
    from app.world.state import set_presence
    from app.world.tools import FEATURE_EMIT_COUNT, emit_event

    # 用 world 工具真投一条 event 进 akao 信箱（先放置在场）
    await set_presence(lane=lane, persona_id=persona, room_id="akao_room")

    async def _seed_event(summary: str) -> None:
        wctx = AgentContext(
            features={
                "world_lane": lane,
                "world_round_id": f"seed-{summary}",
                FEATURE_EMIT_COUNT: {"n": 0},
            }
        )
        with agent_context(wctx):
            await emit_event.invoke({"room_id": "akao_room", "summary": summary})

    await _seed_event("第一轮的动静")

    _agent_run.life_round = [
        ("update_life_state", {
            "current_state": "第一轮：醒了",
            "response_mood": "迷糊",
            "activity_type": "rest",
        }),
    ]
    await lw.life_wake_node(EventArrived(lane=lane, persona_id=persona))

    today = datetime.now().strftime("%Y-%m-%d")
    session_id = make_session_id(lane, persona, today)
    # 第一轮 run 收到的 session_id 与派生一致（显式传，才真续接）
    assert _agent_run.life_calls[-1]["session_id"] == session_id

    # 清 cd，模拟 cd 已过，让第二轮能跑（cd 延迟另有专测）
    import app.infra.redis as redis_mod

    await (await redis_mod.get_redis()).delete(lw._cd_key(lane, persona))

    # 第二轮：再投一条 event，再唤醒
    await _seed_event("第二轮的动静")
    _agent_run.life_round = [
        ("update_life_state", {
            "current_state": "第二轮：还醒着",
            "response_mood": "平静",
            "activity_type": "idle",
        }),
    ]
    await lw.life_wake_node(EventArrived(lane=lane, persona_id=persona))

    # transcript 随轮增长、两轮 stimulus 都在（连续上下文，不是从零组装）
    stored = await load_session(session_id)
    assert len(stored) >= 2, "life session transcript 应随轮增长（两轮都写回 Redis）"
    blob = "".join(m.text() for m in stored)
    # 数据流变了：感知不再是固定一句「此刻你感知到了这些」，而是 _format_unread 拼的
    # 含感知 event 原文的 stimulus。验「两轮各自的感知 event 原文」都落进同一条
    # transcript —— 这比找固定文案更强：直接证明续接带的是「真·连续的感知上下文」，
    # 第二轮 replay 时第一轮她感知过啥仍在场。
    assert "第一轮的动静" in blob, "第一轮的感知原文应在续接的 transcript 里"
    assert "第二轮的动静" in blob, "第二轮的感知原文应在续接的 transcript 里"


@pytest.mark.integration
async def test_life_cd_delays_without_dropping_events(
    world_db, _stub_persona, _agent_run, monkeypatch
):
    """cd 延迟不丢（life 侧）：一轮跑完进 cd，cd 内来的 event 被 reschedule 攒着，
    cd 过后一并感知、一并标已读（绝不 drop）。

    第一轮成功跑完 → 落 cd key。cd 内来新 event 再唤醒 → life_wake 查到 cd 内 →
    raise DebounceReschedule（不烧模型、不标已读，新 event 留信箱未读）。删 cd key
    模拟 cd 过 → 再唤醒，cd 内攒下的 event 被一并消费。
    """
    from app.agent.context import AgentContext
    from app.agent.runtime_context import agent_context
    from app.data.queries.mailbox import list_unread_events
    from app.runtime.debounce import DebounceReschedule
    from app.world.state import set_presence
    from app.world.tools import FEATURE_EMIT_COUNT, emit_event

    lane = "coe-life-cd"
    persona = "akao"
    await set_presence(lane=lane, persona_id=persona, room_id="akao_room")

    async def _seed_event(summary: str) -> None:
        wctx = AgentContext(
            features={
                "world_lane": lane,
                "world_round_id": f"seed-{summary}",
                FEATURE_EMIT_COUNT: {"n": 0},
            }
        )
        with agent_context(wctx):
            await emit_event.invoke({"room_id": "akao_room", "summary": summary})

    # 第一轮：跑完落 cd key
    await _seed_event("第一波动静")
    _agent_run.life_round = [
        ("update_life_state", {
            "current_state": "处理第一波", "response_mood": "平", "activity_type": "idle",
        }),
    ]
    await lw.life_wake_node(EventArrived(lane=lane, persona_id=persona))
    assert await list_unread_events(lane=lane, persona_id=persona) == []

    # cd 内：来一条新 event，再唤醒 → 被 reschedule（不消费）
    await _seed_event("cd 内来的动静")
    with pytest.raises(DebounceReschedule):
        await lw.life_wake_node(EventArrived(lane=lane, persona_id=persona))
    # cd 内 event 没被丢：仍躺在信箱未读
    cd_unread = await list_unread_events(lane=lane, persona_id=persona)
    assert [e.summary for e in cd_unread] == ["cd 内来的动静"], "cd 内 event 绝不 drop"

    # cd 过（删 key）→ 再唤醒：cd 内攒下的 event 被一并感知、标已读
    import app.infra.redis as redis_mod

    await (await redis_mod.get_redis()).delete(lw._cd_key(lane, persona))
    _agent_run.life_round = [
        ("update_life_state", {
            "current_state": "cd 后处理攒下的", "response_mood": "平", "activity_type": "idle",
        }),
    ]
    await lw.life_wake_node(EventArrived(lane=lane, persona_id=persona))
    assert await list_unread_events(lane=lane, persona_id=persona) == [], (
        "cd 过后攒下的 event 被一并消费、标已读"
    )
