"""world + life event 闭环端到端集成 — pull 范式联调收口（最高风险一环）.

event 骨架、world engine（推演者）、life 三姐妹（自主做事）各自单测都绿，但拼
起来世界是死的。这个文件证明拼起来**世界真能动**，且走的是 pull 范式的完整自主循环：

  1. world 冷启醒来 → ``update_world(detail)`` 写第一版世界叙述 →
     ``notify(scope, observation)`` 产一条客观动静、标它的客观作用域，由在场匹配
     （Task 3）按角色客观位置定谁收到 → ``sleep``。
  2. 被 notify 的角色（life）被唤醒 → 读到信箱里的 observation → ``act(description)``
     自主做事 → 收口。``act`` 直接 ``insert_idempotent(ActPerformed)`` 落 PG，**不唤醒
     world**（pull 范式）。
  3. world 按自己 sleep 的节奏（self / 心跳）下次醒来 → 从游标批量 pull 这段时间攒下
     的 act（``list_recent_acts``）→ 在推演里消化、``update_world`` 更新世界叙述 +
     ``notify`` 标作用域、在场匹配投给在场的人 → 收口推进游标到本批末尾。

真 Postgres（testcontainers），只 mock 一处 LLM：``Agent.run``——按 ``cfg.prompt_id``
把 world / life 两条循环分流，各自在真实 ``agent_context`` 下回放脚本里的工具调用
（world 的 update_world / notify / sleep；life 的 update_life_state / act），所以
工具的真实 DB 副作用全发生。别的全走真实持久化：mock 掉持久化等于什么都没测。
world 的 sleep 自排打桩成记录 delay，不连 RabbitMQ。

pull 范式的命门：
  * 完整自主循环每一棒交接成功；
  * detail 落 durable 且读回续上（world 续接认得上一版世界叙述）；
  * notify 的 observation 经在场匹配投进在场角色的信箱、没投给够不着的人（信息差）；
  * act 落 PG 不唤醒 world；world 下次自排醒来从游标 pull 到该 act、推完推进游标；
  * 同一批失败重读不重复推演 / 不重复投递（round_id 从本批集合稳定派生 + turn 幂等）；
  * 不卡死：life 处在大状态、新 observation 进信箱、被唤醒能读到并换状态；
  * 全程没有 move_persona / emit_event / raise_intent / presence / room_id。
"""

from __future__ import annotations

import json as _json
from datetime import UTC, datetime, timedelta

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
from app.fetch.materials import DailyMaterials
from app.life.pages import DayPage, RelationshipPage
from app.runtime.persist import insert_idempotent
from app.world.arc import WorldArc
from app.world.attention import WorldAttention
from app.world.engine import (
    WORLD_HEARTBEAT_MS,
    WorldTick,
    world_tick,
)
from app.world.npc_roster import NPCRoster
from app.world.outline import WorldOutline
from app.world.state import WorldState, read_world_state
from tests.runtime.conftest import migrate

# world 一轮的脚本化行动：模型在循环里调的工具序列。each = (tool_name, args)。
# mock 的 Agent.run 在真实 agent_context 下依次回放它们，真实 DB 副作用全发生。
WorldRound = list[tuple[str, dict]]


def _update_world(detail: str) -> tuple[str, dict]:
    return ("update_world", {"detail": detail})


# Task 3：notify 不再由 world 主观挑 recipients——它标客观作用域、由在场匹配（一道
# LLM 判断）按角色客观位置决定谁收到。集成测试里 LLM 不可用，所以 ``_stub_presence_
# match`` 把 ``match_present_personas`` 打桩成「从 scope 里读出本测试想要的收件人」。
# ``_notify`` 仍以 recipients 为作者便利入参，但把它编进 scope 字符串（JSON，测试约定），
# 由打桩的在场匹配解出来——这样既走真实的「标作用域 + 调在场匹配 + 按结果投递」路径，
# 又让每个测试能确定性控制「这条动静该到谁手里」。
_PRESENCE_SCOPE_PREFIX = "[present:"


def _notify(recipients: list[str], observation: str) -> tuple[str, dict]:
    scope = f"{_PRESENCE_SCOPE_PREFIX}{_json.dumps(recipients)}]{observation}"
    return ("notify", {"scope": scope, "observation": observation})


def _sense(recipient: str, surroundings: str) -> tuple[str, dict]:
    return ("sense", {"recipient": recipient, "surroundings": surroundings})


def _npc_visit(
    npc_name: str, sister: str, what_npc_says: str, world_fact: str
) -> tuple[str, dict]:
    return (
        "npc_visit",
        {
            "npc_name": npc_name,
            "sister": sister,
            "what_npc_says": what_npc_says,
            "world_fact": world_fact,
        },
    )


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


def _chat(recipient: str, content: str) -> tuple[str, dict]:
    return ("chat", {"recipient": recipient, "content": content})


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
    """建齐闭环需要的所有真实表：world 叙述快照 / 世界阶段 / 关注 / 信箱 / 已读 / 动作 / life 快照 / 续接 transcript。

    新范式没有 presence 表了（RoomPresence 已删）。world 的客观状态是 WorldState
    （此刻的世界叙述，位置融在 detail 自然语言里）+ WorldArc（世界阶段的慢层快照，
    world_tick 每个放行轮都先 read_world_arc）。act 走 ActPerformed durable 表。
    """
    await migrate(WorldState, test_db)
    await migrate(WorldArc, test_db)
    # world 续写每轮 read_world_outline 读自维护的大纲（task2 工作记忆）——不建它，
    # 闭环里每个放行轮都死在 UndefinedTableError（即便大纲为空也要表存在才能 SELECT）。
    await migrate(WorldOutline, test_db)
    await migrate(WorldAttention, test_db)
    await migrate(EventEnvelope, test_db)
    await migrate(EventRead, test_db)
    await migrate(LifeState, test_db)
    await migrate(ActPerformed, test_db)
    await migrate(SessionTranscript, test_db)
    # world 每轮按 (lane, 今天) 查当天外部底料（engine 的 find_daily_materials 真打
    # 这张表）——不建它，闭环里每个 world_tick 都死在 UndefinedTableError。
    await migrate(DailyMaterials, test_db)
    # world 每轮 list NPC 名册（engine 的 list_npc_roster 真打这张表，NPC 层第一刀）
    # ——不建它，闭环里每个 world_tick 都死在 UndefinedTableError（即便名册为空也要
    # 表存在才能 SELECT 出空表）。
    await migrate(NPCRoster, test_db)
    # 睡前回顾的两张页（昨天页 + 关系页）。当前闭环还没接回顾触发（Task 2 接线），
    # 先把表建齐——接上后 life 轮收口会真打它们，缺表同样死 UndefinedTableError。
    await migrate(DayPage, test_db)
    await migrate(RelationshipPage, test_db)
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
def _stub_persona_roster(monkeypatch):
    """world 每轮读三姐妹此刻状态（world-driven wake）会先 list_all_persona_ids —— 它
    查 SQLAlchemy 的 bot_persona 表，闭环 world_db 没建这张表（它是 Base 模型、不走
    runtime migrate）。这里 stub 成三姐妹名单（akao / chinagi / ayana），让 world 真去
    读她们各自插进真库的 LifeState（find_life_state 不打桩、走真库），闭环里世界看见
    意愿这条路真生效。"""
    async def fake_list_all_persona_ids():
        return ["akao", "chinagi", "ayana"]

    monkeypatch.setattr(
        engine_mod, "list_all_persona_ids", fake_list_all_persona_ids
    )


@pytest.fixture(autouse=True)
def _stub_presence_match(monkeypatch):
    """notify 的在场匹配（Task 3）打桩：从 scope 里读出本测试想要的收件人.

    真实 notify 走「读各角色 current_state → match_present_personas（LLM）→ 投在场角色」。
    集成里 LLM 不可用，所以：

      * ``tools_mod.list_all_persona_ids``：notify 的 ``_current_persona_locations``
        用它列候选——查 SQLAlchemy bot_persona 表（闭环没建），桩成三姐妹。
      * ``tools_mod.match_present_personas``：从 scope 的 ``[present:[...]]`` 前缀解出
        ``_notify`` 编进去的收件人；只返真候选里的（对齐真实候选过滤）。直接用真实
        world 工具投递的测试（scope 不带前缀）则默认返全部有位置的候选。

    这样测试照旧用 recipients 控制「谁收到」，但代码真的走了在场匹配那条路径。
    """
    async def fake_list_all_persona_ids():
        return ["akao", "chinagi", "ayana"]

    async def fake_match_present_personas(
        *, scope, persona_locations, trace_session_id=None
    ):
        if scope.startswith(_PRESENCE_SCOPE_PREFIX):
            # ``_notify`` 把收件人编成 ``[present:<json array>]<observation>``。json
            # array 本身带 ``]``（``["akao","ayana"]``），所以不能用 ``split("]")`` 截
            # ——那会停在 array 自己的 ``]`` 上、截出缺闭合括号的残片。用 ``raw_decode``
            # 从 prefix 之后直接吃掉一个完整的 json 值（它返回解析结束的偏移量），天然
            # 只消费那个 array、不被 array 内部或 observation 里的 ``]`` 干扰。
            wanted, _end = _json.JSONDecoder().raw_decode(
                scope[len(_PRESENCE_SCOPE_PREFIX):]
            )
            return [p for p in wanted if p in persona_locations]
        # 无前缀（直接 invoke 真实 notify 的测试）：默认全部有位置的候选在场。
        return list(persona_locations)

    monkeypatch.setattr(tools_mod, "list_all_persona_ids", fake_list_all_persona_ids)
    monkeypatch.setattr(
        tools_mod, "match_present_personas", fake_match_present_personas
    )


@pytest.fixture(autouse=True)
def _capture_act_to_pg(monkeypatch):
    """life 的 ``act`` → ``perform_act`` 这条动作：落进 PG（真原语）+ 捕获供断言.

    pull 范式：``perform_act`` 本身就是 ``insert_idempotent(ActPerformed)`` 落
    ``data_act_performed`` 表、不唤醒 world。这里包一层真 perform_act（不替换它的
    行为，只在前面记一条供断言），让 world 下次自排醒来真从 PG 读到这条 act。

    perform_act handler 由 life_tools 模块级引用，patch 那里才拦得住。
    """
    import app.nodes.life_tools as life_tools_mod
    from app.domain.world_events import perform_act as real_perform_act

    captured: list[ActPerformed] = []

    async def capture_perform_act(*, lane, act_id, persona_id, description, occurred_at):
        captured.append(
            ActPerformed(
                lane=lane,
                act_id=act_id,
                persona_id=persona_id,
                description=description,
                occurred_at=occurred_at,
            )
        )
        # 真原语落 PG（pull 范式：insert_idempotent、不唤醒），world 才读得到。
        await real_perform_act(
            lane=lane,
            act_id=act_id,
            persona_id=persona_id,
            description=description,
            occurred_at=occurred_at,
        )

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


@pytest.mark.integration
async def test_life_wake_includes_realtime_recent_chat_context(
    world_db, _stub_persona, _agent_run
):
    """life 真醒来时实时拉 common_message，把最近聊过的对话放进 stimulus。

    旧 chat→mailbox 回灌只写用户原话，且写入时赤尾自己的回复可能还没由
    chat-response-worker 落库。新路径不再信箱回灌内容，而是在 life 醒来时实时查
    common_message：这时库里已有真人话 + 赤尾回复，stimulus 应按会话分组并把她自己的
    回复显示成「我」。
    """
    import uuid

    from sqlalchemy import text

    import app.data.session as session_mod
    from app.data.models import (
        Base,
        CommonAgentResponse,
        CommonConversation,
        CommonMessage,
    )

    tables = [
        CommonMessage.__table__,
        CommonAgentResponse.__table__,
        CommonConversation.__table__,
    ]
    async with world_db.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables)
        )
        await conn.execute(
            text(
                "CREATE TABLE bot_config ("
                "  bot_name VARCHAR(50) PRIMARY KEY,"
                "  persona_id VARCHAR(50),"
                "  is_active BOOLEAN NOT NULL DEFAULT TRUE"
                ")"
            )
        )

    lane = "coe-recent-chat"
    persona = "akao"
    chat_id = uuid.uuid4()
    user_id = uuid.uuid4()
    user_msg_id = uuid.uuid4()
    bot_msg_id = uuid.uuid4()
    response_session_id = f"recent-chat-{uuid.uuid4().hex}"
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    async with session_mod.get_session() as s:
        s.add(
            CommonConversation(
                common_conversation_id=chat_id,
                channel="lark",
                scope="direct",
                display_name=None,
            )
        )
        s.add(
            CommonAgentResponse(
                response_id=uuid.uuid4(),
                session_id=response_session_id,
                trigger_common_message_id=user_msg_id,
                common_conversation_id=chat_id,
                persona_id=persona,
            )
        )
        s.add(
            CommonMessage(
                common_message_id=user_msg_id,
                channel="lark",
                common_conversation_id=chat_id,
                common_user_id=user_id,
                sender_display_name="贝壳",
                role="user",
                content=[{"kind": "text", "text": "赤尾刚才还记得我吗"}],
                content_text="赤尾刚才还记得我吗",
                scope="direct",
                event_time=now_ms - 1_000,
            )
        )
        s.add(
            CommonMessage(
                common_message_id=bot_msg_id,
                channel="lark",
                common_conversation_id=chat_id,
                common_user_id=None,
                sender_display_name=None,
                role="assistant",
                content=[{"kind": "text", "text": "当然记得，刚刚才聊过。"}],
                content_text="当然记得，刚刚才聊过。",
                scope="direct",
                response_id=response_session_id,
                event_time=now_ms,
            )
        )

    await insert_idempotent(
        EventEnvelope(
            lane=lane,
            persona_id=persona,
            event_id="wake:recent-chat",
            kind="ambient",
            source="world",
            summary="窗边有风吹进来",
            occurred_at=datetime.now(UTC).isoformat(),
        )
    )

    _agent_run.life_round = []
    await lw.life_wake_node(EventArrived(lane=lane, persona_id=persona))

    stimulus = _agent_run.life_calls[-1]["messages_text"]
    assert "最近聊过的对话" in stimulus
    # 主动私聊具名化 Task 2：私聊段头部具名 + user:<uuid> 句柄（不再是匿名「一段私聊」），
    # 让她主动发消息时首发 uid 即合法。
    assert f"和 贝壳（user:{user_id}）的私聊里" in stimulus
    assert "贝壳：赤尾刚才还记得我吗" in stimulus
    assert "我：当然记得，刚刚才聊过。" in stimulus
    assert "窗边有风吹进来" in stimulus


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
    """整条 pull-范式闭环从头跑到尾，断言每一棒交接成功（最致命的一条集成测试）.

    棒次：
      1. world 冷启动 → update_world 写第一版世界叙述 + notify 把一条客观动静投给
         推演指定的 akao（只 akao 够得着）。
      2. life（akao）被唤醒 → 读信箱拿到那条 observation → 想一轮（换状态）→
         act 自主做一件事（去厨房煮咖啡）。act 直接落 PG，**不唤醒 world**。
      3. world 按自排节奏下次醒来（这里用 heartbeat 直喂）→ 从游标 pull 到这条 act →
         update_world 更新世界叙述（厨房有了动静）+ notify 该感知到的人 → 推进游标。
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
            # 第二次唤醒（pull 到 akao 的 act）：读到 akao 去了厨房，更新世界叙述 +
            # 投给厨房在场的人。
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

    # --- 棒 3：world 自排醒来从游标 pull 到这条 act 推演 ---
    # pull 范式：act 落 PG 不唤醒 world。world 棒 1 sleep(600) 后排了 next_wake_at。
    # 模拟"排的那次 self 自排到点了"：把 next_wake_at 改写成刚过去的时刻，再喂一条
    # self WorldTick 携带这个目标（== state 当前值、到点、不 stale → gate 放行）。
    # 然后 world 从游标（仍是 None，棒 1 是空批次没推进）批量 pull 到刚落库的 act。
    from app.world.state import set_next_wake_at

    past_target = (datetime.now(engine_mod._CST) - timedelta(seconds=1)).isoformat()
    await set_next_wake_at(lane=lane, next_wake_at=past_target)
    await world_tick(WorldTick(lane=lane, reason="self", target_wake_at=past_target))

    # 棒 3 交接证据：world 从游标读到这条 act 并透给循环推演（list_recent_acts 真读到）
    assert "煮咖啡" in world_calls[1]["messages_text"]
    # 世界叙述被更新（厨房有了动静）
    snap2 = await read_world_state(lane=lane)
    assert "走进厨房" in snap2.detail
    # 推演产的新 observation 投给了厨房在场的 chinagi
    chinagi_unread = await list_unread_events(lane=lane, persona_id="chinagi")
    assert "厨房传来煮咖啡的声音和香气" in [e.summary for e in chinagi_unread]
    # 棒 3 收口把游标推进到这条 act（下轮不重读）
    snap_cursor = await read_world_state(lane=lane)
    assert snap_cursor.act_cursor_act_id == _capture_act_to_pg[0].act_id, (
        "推演成功收口后游标应推进到本批末尾"
    )

    # 两轮 world 各调一次 sleep 定下次几时醒：第一轮 sleep(600)、第二轮 sleep(300)。
    # 都 ≤ 10 分钟保底心跳（sleep 工具上限 1h，这里更紧）。
    assert engine_mod._test_self_wakes == [600_000, 300_000]
    for delay in engine_mod._test_self_wakes:
        assert 0 < delay <= WORLD_HEARTBEAT_MS


@pytest.mark.integration
async def test_info_gap_notify_only_reaches_present_personas(
    world_db, _agent_run, monkeypatch
):
    """信息差：notify 标作用域、在场匹配只投给在场的角色；够不着的姐妹信箱空."""
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

    # chinagi 被在场匹配判为够得着 → 收到；akao 不在场 → 收不到
    chinagi_unread = await list_unread_events(lane=lane, persona_id="chinagi")
    akao_unread = await list_unread_events(lane=lane, persona_id="akao")
    assert [e.summary for e in chinagi_unread] == ["厨房飘来煎蛋的香味"]
    assert akao_unread == []


@pytest.mark.integration
async def test_npc_visit_end_to_end_mailbox_and_world_layer(
    world_db, _stub_persona, _agent_run, monkeypatch
):
    """NPC 层第二刀机制层端到端（真 PG，不靠模型自发）：world 调 npc_visit 一次，断言三条.

    构造一轮 world 推演里调一次 ``npc_visit``（林小满来找绫奈），全程走真实工具 + 真实
    持久化，验证 spec Task 2 的三条机制：

      ① 投进对应姐妹信箱、带 NPC 来源标识：绫奈信箱里这条 event 的 ``source`` 是
         ``npc:林小满``、``kind`` 是 speech —— 既不是真人（user:xxx / external）、也不是
         world 环境动静（ambient）。
      ② 对应姐妹 list_unread_events 拿得到、且 life 侧能识别出 NPC 来源（speech 段
         「林小满 对你说」、机读前缀 npc: 不漏给模型）。
      ③ 同一件事同步留在世界层：同轮 world detail 含这件 NPC 事实（收件人感知 + 世界
         状态不分叉）。

    全程没有让模型「自发」决定投——脚本里直接编排 npc_visit 这一步，证的是机制能力
    （Task 2 不负责让 world 愿意投，那是第三刀 prompt 的事）。
    """
    lane = "coe-npc"
    _world_llm(
        _agent_run,
        [
            [
                _update_world("午后。ayana 在客厅写作业，akao 在房间，chinagi 在厨房。"),
                _npc_visit(
                    npc_name="林小满",
                    sister="ayana",
                    what_npc_says="绫奈周末有空吗？一起去图书馆复习吧。",
                    world_fact="ayana 的手机响了，是林小满发来的消息。",
                ),
                _sleep(600),
            ],
        ],
    )

    await world_tick(WorldTick(lane=lane, reason="heartbeat"))

    # ① + ② 投进绫奈信箱、带 NPC 来源标识，list_unread 拿得到
    ayana_unread = await list_unread_events(lane=lane, persona_id="ayana")
    assert len(ayana_unread) == 1
    npc_ev = ayana_unread[0]
    assert npc_ev.source == "npc:林小满", "NPC event 来源必须是 npc:名字（不是真人 / 不是 world）"
    assert npc_ev.kind == "speech", "NPC event 是 speech（有具名说话人）、不是 ambient 环境动静"
    assert npc_ev.summary == "绫奈周末有空吗？一起去图书馆复习吧。"
    # NPC event 没误投给别的姐妹（指向单个收件人）
    assert await list_unread_events(lane=lane, persona_id="akao") == []
    assert await list_unread_events(lane=lane, persona_id="chinagi") == []

    # ② life 侧识别 NPC 来源：绫奈被唤醒，stimulus 呈现「林小满 对你说」、不漏 npc: 前缀
    _agent_run.life_round = []  # 看一眼即可，不需要她做什么
    await lw.life_wake_node(EventArrived(lane=lane, persona_id="ayana"))
    life_blob = _agent_run.life_calls[-1]["messages_text"]
    assert "林小满 对你说" in life_blob, "life 应把 NPC speech 呈现成「林小满 对你说」"
    assert "绫奈周末有空吗？一起去图书馆复习吧。" in life_blob
    assert "npc:林小满" not in life_blob, "npc: 机读前缀不该漏进喂给模型的 stimulus"

    # ③ 同一件事同步留世界层：同轮 world detail 含这件 NPC 事实（不分叉）
    snap = await read_world_state(lane=lane)
    assert snap is not None
    assert "林小满" in snap.detail
    assert "ayana 的手机响了，是林小满发来的消息。" in snap.detail
    # 不丢同轮 update_world 写的上一段叙述（追加而非覆盖）
    assert "ayana 在客厅写作业" in snap.detail


@pytest.mark.integration
async def test_world_senses_per_recipient_surroundings_with_info_gap(
    world_db, _stub_persona, _agent_run, monkeypatch
):
    """world 五官（1C Task 2）：逐角色投不同周遭切片，每人只拿到自己那份（信息差）。

    world 有全局视角，但用 sense 逐角色投——给客厅的绫奈投她的客厅周遭、给厨房的赤尾
    投她的厨房周遭。切片由 world 逐角色推演产出（不是按某结构裁的全局），每人信箱里
    只有投给她的那份：

      * 正例：绫奈拿到她的客厅切片、赤尾拿到她的厨房切片，且 kind=surroundings。
      * 负例（信息差命门）：睡着的千凪没被 sense → 她信箱空，不会收到任何旁白式全局
        世界信息；绫奈的切片里是她够得着的（厨房飘来的香味），赤尾的切片是厨房视角，
        两份互不为对方的全局视角——代码层只把投给某人的 event 放进她信箱，world 全局
        状态绝不整个喂给某个 life。
    """
    from app.domain.world_events import EVENT_KIND_SURROUNDINGS

    lane = "coe-sense"
    _world_llm(
        _agent_run,
        [
            [
                _update_world("午后。绫奈在客厅写作业，赤尾在厨房做饭，千凪在卧室睡觉。"),
                _sense("ayana", "你在客厅写作业，厨房飘来赤尾做饭的香味，午后的光斜照进来。"),
                _sense("akao", "你在厨房做饭，灶上煮着汤，客厅那头隐约有翻书的声音。"),
                _sleep(600),
            ]
        ],
    )

    await world_tick(WorldTick(lane=lane, reason="heartbeat"))

    # 正例：绫奈、赤尾各拿到自己那份周遭切片（kind=surroundings）
    ayana_unread = await list_unread_events(lane=lane, persona_id="ayana")
    akao_unread = await list_unread_events(lane=lane, persona_id="akao")
    assert [e.summary for e in ayana_unread] == [
        "你在客厅写作业，厨房飘来赤尾做饭的香味，午后的光斜照进来。"
    ]
    assert [e.kind for e in ayana_unread] == [EVENT_KIND_SURROUNDINGS]
    assert [e.summary for e in akao_unread] == [
        "你在厨房做饭，灶上煮着汤，客厅那头隐约有翻书的声音。"
    ]
    assert [e.kind for e in akao_unread] == [EVENT_KIND_SURROUNDINGS]

    # 负例（信息差命门）：睡着的千凪没被 sense → 信箱空，没有旁白式全局世界信息
    assert await list_unread_events(lane=lane, persona_id="chinagi") == []

    # 信息差命门：绫奈的切片不含赤尾那份的厨房视角（各拿各的切片，全局不反向泄露）
    ayana_blob = ayana_unread[0].summary
    assert "客厅那头隐约有翻书的声音" not in ayana_blob


@pytest.mark.integration
async def test_world_never_reads_chat_original_speech_end_to_end(
    world_db, _stub_persona, _agent_run, _capture_act_to_pg, monkeypatch
):
    """端到端「world 不读对话原话」（codex 建议 3，真 PG 集成、非 mock 假测）.

    现有 world 不读原话的单测是 mock 假测（手造一条不含原话的 meta act 再断言）。这里
    走真链路证承重红线在真 PG 上成立：

      1. life（akao）真调 ``chat(ayana, 绝密原话)`` —— 双轨真发生：原话经真实 deliver_event
         直投 ayana 信箱（speech）；不含原话的 meta 经真实 perform_act 落进 PG ActPerformed。
      2. 断言 PG 里的 ActPerformed.description **不含**对话原话（只「我和 ayana 说了几句话」）。
      3. world 自排醒来从游标真 pull（``list_recent_acts``）→ 断言 world 读到的批次 /
         喂给 world 的 stimulus **不含**对话原话（红线在真链路上钉死）。
      4. 反向证据：原话确实送达了 ayana 信箱（说明红线不是靠"根本没投递"蒙混）。
    """
    from app.world.state import read_world_state, set_next_wake_at, write_world_state

    lane = "coe-chat-redline"
    secret_line = "绫奈姐姐你在做什么好吃的呀这句是绝密对话原话"

    # 先种一版 WorldState（含已过 next_wake_at）：让随后的 world self 唤醒到点放行、不冷启。
    past_target = (datetime.now(engine_mod._CST) - timedelta(seconds=1)).isoformat()
    await write_world_state(
        lane=lane, world_time="2026-06-03T14:00:00+08:00", detail="厨房里有人在忙活。"
    )
    await set_next_wake_at(lane=lane, next_wake_at=past_target)

    # --- life（akao）真调 chat：原话直投 ayana、不含原话的 meta 落 PG ---
    # world notify 一条动静起头唤醒 akao（用真实 world notify 投进她信箱）。
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
            # Task 3：notify 标作用域、由在场匹配定收件人；scope 的 [present:[...]] 前缀
            # 被 _stub_presence_match 解成想要的收件人（这里 akao）。
            scope = f"{_PRESENCE_SCOPE_PREFIX}{_json.dumps(['akao'])}]{observation}"
            await notify.invoke({"scope": scope, "observation": observation})

    await _notify_akao("厨房飘来做饭的香味")

    _agent_run.life_round = [_chat("ayana", secret_line)]
    await lw.life_wake_node(EventArrived(lane=lane, persona_id="akao"))

    # 棒 1 真链路证据：原话直投了 ayana 信箱（红线不是靠"没投递"蒙混）。
    ayana_unread = await list_unread_events(lane=lane, persona_id="ayana")
    assert secret_line in [e.summary for e in ayana_unread], (
        "对话原话应真送达 ayana 信箱（speech 直投）"
    )

    # 棒 2 真链路证据：PG ActPerformed（chat 的 world meta）里绝不含对话原话。
    meta_acts = await list_recent_acts(
        lane=lane, cursor_created_at=None, cursor_act_id=None, limit=10
    )
    assert meta_acts, "chat 应落一条不含原话的 world meta act 进 PG"
    for a, _created in meta_acts:
        assert secret_line not in a.description, (
            "承重红线：PG ActPerformed.description 绝不含对话原话（只『和谁交谈』的事实）"
        )
    assert any("ayana" in a.description for a, _c in meta_acts), (
        "meta 应记『和 ayana 交谈』让 world 能反映氛围"
    )

    # --- world 自排醒来从游标真 pull 这条 meta act ---
    world_calls = _world_llm(_agent_run, [[_update_world("厨房那头有人在低声交谈。"), _sleep(600)]])
    await world_tick(WorldTick(lane=lane, reason="self", target_wake_at=past_target))

    # 棒 3 承重红线：world 真 pull 到的批次 / 喂给 world 的 stimulus 绝不含对话原话。
    assert world_calls, "world self 唤醒应真醒来跑一轮"
    world_blob = world_calls[0]["messages_text"]
    assert secret_line not in world_blob, (
        "world 绝不读对话原话——真 pull 到的批次 / 喂给 world 的 context 里不能有逐句原话"
    )
    # world 仍从 meta 知道「有人在交谈」（反映氛围），即便读不到原话。
    assert ("交谈" in world_blob) or ("说了几句" in world_blob), (
        "world 应从 meta 知道有一场对话在发生（反映氛围）"
    )
    # world 推演成功收口（世界叙述被更新），佐证它真消化了这条 meta。
    snap = await read_world_state(lane=lane)
    assert "低声交谈" in snap.detail


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
async def test_world_self_wake_pulls_recent_acts_from_cursor(
    world_db, _agent_run, _capture_act_to_pg, monkeypatch
):
    """pull 范式：world 自排醒来从游标 pull 到攒下的 act、推完推进游标。

    act 落 PG 不唤醒 world（pull 范式）。world 按自己 sleep 排的 next_wake_at 到点
    self 醒来 → 从游标（None=冷启读全既有）pull 到这条 act → 透给循环推演 → 收口
    推进游标到这条 act。
    """
    from app.world.state import read_world_state, set_next_wake_at, write_world_state

    lane = "coe-route"

    # 先种一版 WorldState（含已过的 next_wake_at）：让这次 self 唤醒到点放行、不冷启。
    past_target = (datetime.now(engine_mod._CST) - timedelta(seconds=1)).isoformat()
    await write_world_state(
        lane=lane,
        world_time="2026-06-03T14:00:00+08:00",
        detail="客厅安静，akao 在阳台边。",
    )
    await set_next_wake_at(lane=lane, next_wake_at=past_target)

    world_calls = _world_llm(
        _agent_run,
        [[_sleep(600)]],  # 推演：顺着世界、只 sleep 不广播，验醒来 + 读到 act
    )

    # life act → perform_act → insert_idempotent 落 PG（pull 范式：不唤醒）。occurred_at
    # 取当下，world 醒来从游标读全既有时读得到。
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

    # world 自排到点醒来（self 携带匹配 target、到点、不 stale → gate 放行）。
    await world_tick(WorldTick(lane=lane, reason="self", target_wake_at=past_target))

    # world_tick 真醒来：跑了循环、act 从游标 pull 到透给模型推演
    assert world_calls, "self 自排→world 空转：world_tick 没醒来"
    assert "看花" in world_calls[0]["messages_text"]
    # world 确实从 PG 读到了这批 act（list_recent_acts 命中）。返回 (act, created_at) 元组。
    recent = await list_recent_acts(
        lane=lane, cursor_created_at=None, cursor_act_id=None, limit=10
    )
    assert [a.description for a, _c in recent] == ["我走到阳台看花"]
    # 收口把游标推进到这条 act（下轮不重读）
    snap = await read_world_state(lane=lane)
    assert snap.act_cursor_act_id == "a1", "推演成功收口后游标应推进到本批末尾"


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

    # 这两轮不调 sleep —— 专测 transcript 续接，不掺到点 gate。若第一轮 sleep(600)
    # 会把 next_wake_at 排到 10 分钟后，紧接着的第二轮 heartbeat 会被 gate 正确判废
    # （阶段 1B），那是另一条用例（test_world_self_wake_gate）覆盖的行为。这里只想
    # 跑满两轮验续接：第一轮不排下次醒（next_wake_at 保持 None）→ 第二轮心跳放行。
    _world_llm(
        _agent_run,
        [
            [_update_world("第一版世界叙述")],
            [_update_world("第二版世界叙述")],
        ],
    )

    # 第一轮（heartbeat）
    await world_tick(WorldTick(lane=lane, reason="heartbeat"))
    # 极短间隔起第二轮（heartbeat，round_id 随时刻变、不会被 turn 幂等跳过；第一轮
    # 没排 next_wake_at → 心跳不被 gate）
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
async def test_same_batch_replay_no_duplicate_emit_or_append(
    world_db, _agent_run, monkeypatch
):
    """同批重读幂等：游标没推进时重读同一批 act，world 不重复追加 transcript、不重复 notify.

    pull 范式失败重读场景（必改 2 的崩溃场景③）：world 第一轮跑成功（update_world +
    notify + 写回带 round 标记的 transcript），但游标推进没落（模拟进程在 transcript
    写回与游标推进之间挂了——这里把 advance_act_cursor 打成 no-op 模拟）。第二轮重读
    同一**游标起点** → 从游标起点派生得**同一** round_id → world_tick load_session
    查到本轮标记 → 推进游标到 marker 记的终点后跳过，不再 run、不重复 notify、不重复
    追加 transcript（turn 幂等）。
    """
    from app.world.state import set_next_wake_at, write_world_state

    lane = "coe-replay"

    # 非冷启动 + 已过 next_wake_at（让两次 self 唤醒都到点放行），让 act 推演能真
    # update_world + notify。
    past_target = (datetime.now(engine_mod._CST) - timedelta(seconds=1)).isoformat()
    await write_world_state(
        lane=lane, world_time="2026-06-03T14:00:00+08:00", detail="客厅安静。"
    )
    await set_next_wake_at(lane=lane, next_wake_at=past_target)

    # 游标推进打成 no-op（模拟"transcript 写回成功、游标推进没落"），两轮都读同一批。
    async def noop_advance(*, lane, created_at, act_id):
        return None

    monkeypatch.setattr(engine_mod, "advance_act_cursor", noop_advance)

    # 先落进 PG 这条 act，world 醒来从游标读得到。occurred_at 取当下。
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
    # 若第二次重读也跑一轮，world_rounds 会被 pop 空、第二轮变成"无脚本空跑"也仍会
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

    await world_tick(WorldTick(lane=lane, reason="self", target_wake_at=past_target))
    # chinagi 收到那条 observation 一次
    first = await list_unread_events(lane=lane, persona_id="chinagi")
    assert [e.summary for e in first] == ["厨房传来煮咖啡的声音"]

    # 游标没推进 → 第二次重读同一游标起点（同起点 → 同 round_id）：应被 turn 幂等跳过
    await world_tick(WorldTick(lane=lane, reason="self", target_wake_at=past_target))
    second = await list_unread_events(lane=lane, persona_id="chinagi")
    # observation 没被重复投（仍只一条；event_id 幂等 + turn 幂等双保险）
    assert [e.summary for e in second] == ["厨房传来煮咖啡的声音"]
    # 只跑过一轮（第二次重读没再 run）—— world_calls 只有一条
    assert len(_agent_run.world_calls) == 1, (
        f"同一批 act 重读不该再跑一轮 world，实际 {len(_agent_run.world_calls)} 次"
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
    # 不调 sleep —— 这条专测「并发串行化 + 续接不被破坏」，不掺到点 gate。若各轮
    # sleep(600) 会把 next_wake_at 排到 10 分钟后，随后立刻起的串行 heartbeat 会被
    # gate 正确判废（阶段 1B 行为，由 test_world_self_wake_gate 覆盖），这里跑不满
    # 两轮。第一轮不排 next_wake_at（保持 None）→ 随后心跳放行，验续接增长。
    _world_llm(
        _agent_run,
        [
            [_update_world("v1")],
            [_update_world("v2")],
            [_update_world("v3")],
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
            # Task 3：notify 标作用域、由在场匹配定收件人；scope 的 [present:[...]] 前缀
            # 被 _stub_presence_match 解成想要的收件人（这里 persona）。
            scope = f"{_PRESENCE_SCOPE_PREFIX}{_json.dumps([persona])}]{observation}"
            await notify.invoke({"scope": scope, "observation": observation})

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
            # Task 3：notify 标作用域、由在场匹配定收件人；scope 的 [present:[...]] 前缀
            # 被 _stub_presence_match 解成想要的收件人（这里 persona）。
            scope = f"{_PRESENCE_SCOPE_PREFIX}{_json.dumps([persona])}]{observation}"
            await notify.invoke({"scope": scope, "observation": observation})

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
