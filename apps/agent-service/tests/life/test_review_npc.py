"""睡前回顾把 NPC 互动纳入关系页生成链路 — NPC 层第四刀（代码层）.

第二刀让 world 以具名 NPC 身份投 speech event（kind=speech、source=`npc:名字`）
进姐妹信箱；life 把它渲染进当轮 USER stimulus、落进意识流 transcript。但睡前回顾
原本只从**真实聊天记录**（``find_persona_spoken_chats_in_window``）抽 other_user_id，
NPC 互动只活在意识流 / 信箱里、抽不到——所以姐妹跟 NPC 反复来往出来的关系无法跨天
长进 RelationshipPage。

第四刀（代码层）让回顾能从信箱里读到本生活日窗口内投给她的 NPC speech event
（kind=speech、source 以 `npc:` 起头），由此：

  * 拿到权威的 ``npc:名字`` 机读键（不靠从 transcript 文本里猜被剥过前缀的人名）；
  * 把这些 NPC 当成「这一天来往过的对象」，和真人 partner 一道读回旧关系页作证据
    （跨天累积的命门：她重写 NPC 那页时手里有上一页底稿）；
  * 把 NPC 互动 + 它的 ``npc:名字`` 键明明白白摆进证据里，让模型知道该用哪个
    other_user_id 写这页（对齐真人证据里显式标 user_id 的写法）。

「也给 NPC 写关系页」的**指令措辞**（instruction / 模板里那句「真正聊过天的每个
真人」）是第三刀 prompt 的事，本刀不碰——本刀只让**代码层有能力**抽取 / 喂回 NPC
关系证据。这些测试钉死代码层能力，不测 prompt 鼓励措辞。

写入 / 读回链路本就接受任意 other_user_id 字符串（RelationshipPage 的 Key 是
str、读写无「只要真人」过滤），所以 `npc:名字` 关系页的写入与读回不需要新代码——
命门只在「回顾的抽取对象」这一处把 NPC 纳进来。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import fakeredis.aioredis
import pytest

import app.life.review as review_mod
from app.agent.neutral import Message, Role
from app.agent.trace import make_session_id
from app.data.message_record import CommonMessageRecord
from app.domain.world_events import ActPerformed, EventEnvelope
from app.life.pages import DayPage, RelationshipPage

_CST = timezone(timedelta(hours=8))

_NOW = datetime(2026, 6, 10, 23, 30, 0, tzinfo=_CST)
_TARGET = "2026-06-10"
_LANE = "coe-t2"
_PERSONA = "ayana"


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    import app.infra.redis as redis_mod

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_mod, "_redis", fake)
    return fake


def _act(description="我把书桌收拾了一遍", occurred_at="2026-06-10T20:15:00+08:00"):
    return ActPerformed(
        lane=_LANE,
        act_id="act-1",
        persona_id=_PERSONA,
        description=description,
        occurred_at=occurred_at,
    )


def _npc_event(
    *,
    npc_name="林小满",
    summary="绫奈周末一起去图书馆复习好不好？",
    occurred_at="2026-06-10T16:30:00+08:00",
    event_id=None,
) -> EventEnvelope:
    """一条投给绫奈的 NPC speech event（kind=speech、source=`npc:名字`，第二刀形态）。"""
    return EventEnvelope(
        lane=_LANE,
        persona_id=_PERSONA,
        event_id=event_id or f"npc-{npc_name}-{occurred_at}",
        kind="speech",
        source=f"npc:{npc_name}",
        summary=summary,
        occurred_at=occurred_at,
    )


@pytest.fixture(autouse=True)
def stub_io(monkeypatch):
    """stub 回顾本体的全部 IO，专测 NPC 抽取 / 喂回编排，不碰真库。"""
    state = {
        "sessions": {
            make_session_id(_LANE, _PERSONA, "2026-06-10"): [
                Message(role=Role.USER, content="现在是 16:35。林小满 对你说：周末一起去图书馆复习好不好？"),
                Message(role=Role.ASSISTANT, content="好呀，那约周六上午！"),
            ],
            make_session_id(_LANE, _PERSONA, "2026-06-11"): [],
        },
        "acts": [_act()],
        "chats": [],
        "npc_events": [],
        "rel_pages": {},
        "day_page": None,
        "notebook_entries": [],
        "marks": [],
        "costs": [],
        "page_lookups": [],
        "npc_windows": [],
    }

    async def fake_load_session(session_id):
        return list(state["sessions"].get(session_id, []))

    async def fake_acts(*, lane, persona_id, start_iso, end_iso):
        return list(state["acts"])

    async def fake_chats(*, persona_id, since_ms, until_ms, per_chat_limit):
        return list(state["chats"])

    async def fake_npc_speech(*, lane, persona_id, start_iso, end_iso):
        state["npc_windows"].append(
            {"lane": lane, "persona_id": persona_id, "start": start_iso, "end": end_iso}
        )
        return list(state["npc_events"])

    async def fake_rel_pages(*, lane, persona_id, other_user_ids):
        state["page_lookups"].append(list(other_user_ids))
        return {
            k: v for k, v in state["rel_pages"].items() if k in other_user_ids
        }

    async def fake_day_page(*, lane, persona_id, date):
        return state["day_page"]

    async def fake_day_page_exists(*, lane, persona_id, date):
        return state["day_page"] is not None

    async def fake_mark(*, lane, persona_id, date):
        state["marks"].append({"lane": lane, "persona_id": persona_id, "date": date})

    async def fake_cost(**kwargs):
        state["costs"].append(kwargs)

    async def fake_load_persona(persona_id):
        from app.memory._persona import PersonaContext

        return PersonaContext(
            persona_id=persona_id, display_name="她自己", persona_lite="一段人设"
        )

    async def fake_notebook(*, lane, persona_id, active_only):
        return list(state["notebook_entries"])

    monkeypatch.setattr(review_mod, "load_session", fake_load_session)
    monkeypatch.setattr(review_mod, "list_persona_acts_between", fake_acts)
    monkeypatch.setattr(review_mod, "find_persona_spoken_chats_in_window", fake_chats)
    monkeypatch.setattr(
        review_mod, "list_persona_npc_speech_in_window", fake_npc_speech
    )
    monkeypatch.setattr(review_mod, "read_relationship_pages", fake_rel_pages)
    monkeypatch.setattr(review_mod, "read_day_page", fake_day_page)
    monkeypatch.setattr(review_mod, "day_page_exists", fake_day_page_exists)
    monkeypatch.setattr(review_mod, "mark_day_reviewed", fake_mark)
    monkeypatch.setattr(review_mod, "record_round_cost", fake_cost)
    monkeypatch.setattr(review_mod, "load_persona", fake_load_persona)
    monkeypatch.setattr(review_mod, "list_notebook_entries", fake_notebook)
    return state


def _written_page() -> DayPage:
    return DayPage(
        lane=_LANE,
        persona_id=_PERSONA,
        date=_TARGET,
        narrative="这一天留下来的几笔。",
        written_at=_NOW.isoformat(),
    )


def _mock_run(monkeypatch, stub_io):
    captured: dict = {}

    async def fake_run(
        self, messages, *, prompt_vars=None, context=None, session_id=None,
        max_retries=2,
    ):
        captured["messages"] = messages
        captured["context"] = context
        stub_io["day_page"] = _written_page()
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(review_mod.Agent, "run", fake_run)
    return captured


async def _review(**overrides):
    kwargs = {
        "lane": _LANE,
        "persona_id": _PERSONA,
        "target_date": _TARGET,
        "now": _NOW,
        "trace_session_id": "sess-1",
        "trigger": "sleep",
    }
    kwargs.update(overrides)
    await review_mod.run_day_review(**kwargs)


def _blob(captured) -> str:
    return "".join(m.text() for m in captured["messages"])


# ---------------------------------------------------------------------------
# NPC speech 读取窗口：和 act 同一个生活日窗口
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_npc_speech_query_receives_living_day_window(stub_io, monkeypatch):
    """NPC speech 查询接的是生活日窗口 [target 04:00 CST, 触发时刻]（同 act 口径）。"""
    stub_io["npc_events"] = [_npc_event()]
    _mock_run(monkeypatch, stub_io)

    await _review()

    win = stub_io["npc_windows"][0]
    assert win["lane"] == _LANE and win["persona_id"] == _PERSONA
    assert win["start"] == "2026-06-10T04:00:00+08:00"
    assert win["end"] == _NOW.isoformat()


# ---------------------------------------------------------------------------
# NPC 互动进证据：带 npc:名字 机读键，让模型知道写哪个 other_user_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_npc_interaction_surfaced_with_machine_key(stub_io, monkeypatch):
    """这一天来访过的 NPC：原话 + ``npc:名字`` 机读键明确进证据（对齐真人证据标 user_id）。"""
    stub_io["npc_events"] = [
        _npc_event(npc_name="林小满", summary="绫奈周末一起去图书馆复习好不好？")
    ]
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    blob = _blob(captured)
    assert "林小满" in blob, "NPC 名字要出现在证据里"
    assert "周末一起去图书馆复习" in blob, "NPC 原话要进证据"
    assert "npc:林小满" in blob, (
        "关系页要写给 npc:林小满，证据里必须明确给出这个机读键——"
        "不让模型从被剥过前缀的 transcript 文本里猜"
    )


@pytest.mark.asyncio
async def test_no_npc_interaction_says_so(stub_io, monkeypatch):
    """这一天没有 NPC 来访 → 证据如实说，不冒充。

    检查范围限在【这一天来找过你的人】这节**证据**里——任务指令本身（第三刀收口）
    会讲到 NPC 那页的 other_user_id 用 ``npc:名字`` 键这个约定，instruction 文本里
    出现 ``npc:`` 是正常的；这里要钉的是「没人来访时证据里不冒充 npc 键」，所以只看
    证据节、不看整段 blob（否则会把合法的指令措辞误判成冒充）。
    """
    stub_io["npc_events"] = []
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    blob = _blob(captured)
    # 切出【这一天来找过你的人】这节**证据**（到下一个【段标题前）。指令措辞里也会
    # 引用这个段名（讲 NPC 关系页的约定），所以用 rsplit 取**最后一次**出现的那处
    # ——那才是真正的证据节标题（指令在前、证据在后）。
    marker = "【这一天来找过你的人】"
    assert marker in blob, "证据应有【这一天来找过你的人】这节"
    after = blob.rsplit(marker, 1)[1]
    npc_section = after.split("【", 1)[0]
    assert "npc:" not in npc_section, "没 NPC 互动时这节证据里不该硬塞 npc 键"
    assert "没有名册里的人来找过你" in npc_section, "没 NPC 来访应如实说，不冒充"


# ---------------------------------------------------------------------------
# 跨天累积命门：NPC 的旧关系页和真人一道读回作证据
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_npc_old_relationship_page_read_back_as_evidence(stub_io, monkeypatch):
    """跨天累积命门：来访过的 NPC 的旧关系页（npc:名字 键）和真人 partner 一道读回。"""
    stub_io["npc_events"] = [_npc_event(npc_name="林小满")]
    stub_io["rel_pages"] = {
        "npc:林小满": RelationshipPage(
            lane=_LANE,
            persona_id=_PERSONA,
            other_user_id="npc:林小满",
            narrative="她是我同桌，总能在我慌的时候稳住我。",
            written_at="2026-06-09T23:40:00+08:00",
        )
    }
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    # 旧 NPC 关系页确实被请求读回了（other_user_ids 含 npc:林小满）
    assert any("npc:林小满" in ids for ids in stub_io["page_lookups"]), (
        "NPC 必须被纳入「读回旧关系页」的对象集，否则她重写时手里没有上一页、无法跨天累积"
    )
    # 而且读回的旧页全文进了证据（重写底稿）
    blob = _blob(captured)
    assert "她是我同桌" in blob, "NPC 旧关系页全文必须作为重写底稿进证据"
    assert "2026-06-09T23:40:00+08:00" in blob, "NPC 旧关系页必须带 written_at 标注"


@pytest.mark.asyncio
async def test_npc_and_human_partners_both_read_back(stub_io, monkeypatch):
    """真人 partner 与 NPC partner 都纳入读回对象集（两类不互相挤掉）。"""

    def _msg(text, user_id, username, role="user"):
        return CommonMessageRecord(
            message_id="m",
            user_id=user_id,
            username=username,
            content=json.dumps({"v": 2, "text": text, "items": []}, ensure_ascii=False),
            role=role,
            root_message_id="m",
            reply_message_id=None,
            chat_id="chat-a",
            chat_type="group",
            create_time=1781190000000,
        )

    stub_io["chats"] = [
        (
            "chat-a",
            "一家人",
            [(_msg("哥哥来消息了", "user-1", "哥哥"), None)],
        )
    ]
    stub_io["npc_events"] = [_npc_event(npc_name="林小满")]
    _mock_run(monkeypatch, stub_io)

    await _review()

    all_looked_up = {oid for ids in stub_io["page_lookups"] for oid in ids}
    assert "user-1" in all_looked_up, "真人 partner 仍要读回"
    assert "npc:林小满" in all_looked_up, "NPC partner 也要读回"


@pytest.mark.asyncio
async def test_same_npc_visiting_twice_deduped(stub_io, monkeypatch):
    """同一 NPC 一天来访多次 → 读回对象集里只出现一次（去重，同真人 partner 口径）。"""
    stub_io["npc_events"] = [
        _npc_event(
            npc_name="林小满",
            summary="第一次：周末一起复习？",
            occurred_at="2026-06-10T16:30:00+08:00",
            event_id="n1",
        ),
        _npc_event(
            npc_name="林小满",
            summary="第二次：那约周六上午吧",
            occurred_at="2026-06-10T18:00:00+08:00",
            event_id="n2",
        ),
    ]
    _mock_run(monkeypatch, stub_io)

    await _review()

    all_looked_up = [oid for ids in stub_io["page_lookups"] for oid in ids]
    assert all_looked_up.count("npc:林小满") == 1, "同一 NPC 多次来访只读回一次旧页"


# ---------------------------------------------------------------------------
# 空证据护栏：只有 NPC 互动也算「这一天有经历」、照常跑
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_npc_interaction_still_reviews(stub_io, monkeypatch):
    """意识流 / act / 真人聊天全空、但有 NPC 来访 → 仍算这一天有经历，照常回顾。

    NPC 互动是真实经历的一部分；空证据护栏不能把「只有 NPC 来访的一天」误判成
    「没有可回看的经历」而跳过——否则 NPC 关系永远长不起来。
    """
    stub_io["sessions"] = {}
    stub_io["acts"] = []
    stub_io["chats"] = []
    stub_io["npc_events"] = [_npc_event(npc_name="林小满")]
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    assert "messages" in captured, "只有 NPC 来访的一天也要回顾，不被空证据护栏跳过"
    assert stub_io["marks"] == [
        {"lane": _LANE, "persona_id": _PERSONA, "date": _TARGET}
    ]
