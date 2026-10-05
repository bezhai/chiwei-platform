"""moment —— 每十分钟她回到自己身上一次，默认答「继续」。

五条硬边界，各有对应的用例：

  * **默认是最便宜的那种轮次。** 一个词、零工具、零写库；但这个 moment 跑过要留痕，
    不然"哪些 moment 是继续、哪些换了事情"根本算不出来。
  * **换的是一件事，不是一个时长。** 工具签名里不许出现任何分钟数 —— 真人对
    "多久"没有内感受，问她要一个数字就是在替她把生活切成日程表。
  * **她记得住。** 状态快照跨 moment 续接：一件事被她列进心上之后，中间隔多少个"继续"
    都还在，而且指得出是从哪个 moment 带过来的。
  * **说话必须带真内容。** 上一代 4143 条记录里 62% 是同一句"我和 X 说了几句话"，
    world 因此完全看不见姐妹之间发生了什么。
  * **动作只说自己做了什么，不替世界宣布。** ``act`` 里捎带的世界断言（别人的身体、
    外面出的事、测出来的结果）在这一版**没有任何人能否认**——直接原样成为姐妹眼里
    的客观动静。上一代每一条"家人生病"剧情都是这么起来的。
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.agent.core import _normalise_tool_result
from app.agent.neutral import Message, Role, ToolCall, ToolResult
from app.agent.runtime_context import agent_context
from app.living.loose_ends import LooseEnd, list_open_loose_ends
from app.living.moment import (
    DEFAULT_LIFE_MOMENT_MINUTES,
    LIFE_MOMENT_PROMPT_ID,
    MOMENT_TOOLS,
    LifeMoment,
    LifeMomentTick,
    act,
    keep_in_mind,
    latest_moment,
    life_moment_minutes,
    life_moment_tick,
    look_around,
    move_to,
    run_moment,
    say,
    switch_to,
)
from app.living.persona import LIVING_PERSONAS
from app.living.records import KIND_SPEECH, MEDIUM_IN_PERSON
from app.living.whereabouts import current_whereabouts, note_whereabouts
from app.runtime.schema_types import pg_type
from tests.living.conftest import clock_at

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))
_STEP = dt.timedelta(minutes=DEFAULT_LIFE_MOMENT_MINUTES)

_TOOLS = {t.name: t for t in MOMENT_TOOLS}


async def _wipe_transcripts() -> None:
    """把连续上下文整条清掉 —— 模拟刚重启、或者刚清过库的那一轮。"""
    from sqlalchemy import text as _text

    from app.data.session import get_session
    from app.domain.session_transcript import SessionTranscript
    from app.runtime.migrator import _table_name

    async with get_session() as s:
        await s.execute(_text(f"DELETE FROM {_table_name(SessionTranscript)}"))


def _at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 7, 25, hour, minute, tzinfo=_CST)


@pytest.fixture
async def moment_db(living_db):
    from tests.runtime.conftest import migrate

    for cls in (LooseEnd, LifeMoment):
        await migrate(cls, living_db)
    return living_db


class FakeMoment:
    """替身 life：这个 moment 她调了哪些工具是写死的，只有模型那一步是假的。

    走真工具 + 真 context 绑定，所以写库、派生 id、lane 隔离都是被真的验到的。

    ``transcript_sink`` 按真 ReAct 循环的口径填：每个工具一条带 tool_call 的
    ASSISTANT + 一条 TOOL 返回，最后是她那句话。连续上下文存的就是这个列表，填得
    不像真的，验上下文的用例就是在验一个不存在的形状。
    """

    def __init__(self, *calls: tuple[str, dict], said: str = "继续") -> None:
        self.calls = list(calls)
        self.said = said
        self.runs: list[tuple[list[Message], dict]] = []
        self.results: list[object] = []

    async def run(self, messages, **kwargs):
        self.runs.append((messages, kwargs))
        sink = kwargs.get("transcript_sink")
        with agent_context(kwargs["context"]):
            for i, (name, args) in enumerate(self.calls):
                result = await _TOOLS[name].invoke(args)
                self.results.append(result)
                if sink is None:
                    continue
                call_id = f"call-{i}"
                sink.append(
                    Message(
                        role=Role.ASSISTANT,
                        content="",
                        tool_calls=[
                            ToolCall(id=call_id, name=name, arguments=args)
                        ],
                    )
                )
                sink.append(
                    _normalise_tool_result(
                        ToolResult(tool_call_id=call_id, content=result)
                    ).to_message()
                )
        reply = Message(role=Role.ASSISTANT, content=self.said)
        if sink is not None:
            sink.append(reply)
        return reply


@pytest.fixture
def stub_moment(monkeypatch):
    """装一个替身 life + 固定 moment 间隔 + 一份不碰真库的 persona。

    打桩打在 ``app.living.persona`` 上：moment 自己不查 ``bot_persona`` 了，那两个
    prompt 变量由那个模块一处组装（版本链优先、主表 fallback）。这里链是空的，所以
    落到 ``persona_core`` 这一层。
    """
    from app.living import persona as persona_mod

    persona = SimpleNamespace(
        display_name="赤尾",
        persona_core="她拍胶片、写角色分析、逛论坛、收周边、cos、泡抹茶店、打视觉小说。",
    )

    async def fake_find_persona(persona_id: str):
        return persona

    monkeypatch.setattr(persona_mod, "find_persona", fake_find_persona)

    from app.living import moment as moment_mod

    async def fixed_minutes() -> int:
        return DEFAULT_LIFE_MOMENT_MINUTES

    monkeypatch.setattr(moment_mod, "life_moment_minutes", fixed_minutes)

    def install(*calls: tuple[str, dict], said: str = "继续") -> FakeMoment:
        runner = FakeMoment(*calls, said=said)
        monkeypatch.setattr(moment_mod, "build_moment_runner", lambda: runner)
        return runner

    install.persona = persona  # type: ignore[attr-defined]
    return install


async def _stand(persona: str, place: str, doing: str, at: dt.datetime) -> None:
    await note_whereabouts(
        lane=LANE,
        persona_id=persona,
        moment_id=at.isoformat(timespec="minutes"),
        place=place,
        doing=doing,
        noted_at=at,
    )


async def _reaches_her(persona: str, sender: str, body: str, at: dt.datetime) -> None:
    """一条传到她这里的消息，跟收件箱存下的一样（:func:`app.living.received.receive`）。"""
    from app.living.received import ReceivedMessage
    from app.runtime.persist import insert_idempotent

    await insert_idempotent(
        ReceivedMessage(
            lane=LANE,
            persona_id=persona,
            message_id=f"{persona}:{at.isoformat()}:{body[:8]}",
            sender=sender,
            body=body,
            message_time=at,
            wakes_recipient=True,
        )
    )


# --------------------------------------------------------------------------
# 一 · 默认「继续」，而且这个 moment 留得下痕
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_moment_that_carries_on_costs_one_word_and_writes_nothing(
    moment_db, stub_moment
):
    stub_moment(said="继续")

    moment = await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    assert moment is not None
    assert moment.switched is False
    assert moment.said == "继续"
    assert moment.recorded == 0
    assert moment.pulled_by == ""
    assert await current_whereabouts(lane=LANE, persona_id="akao") is None


@pytest.mark.integration
async def test_every_moment_is_countable_afterwards(moment_db, stub_moment):
    """验收要逐个 moment 看：哪些继续、哪些换了事情、换的理由是什么。"""
    stub_moment(
        (
            "switch_to",
            {
                "doing": "去洗澡",
                "place": "家/浴室",
                "because": "浴室的热水烧好了",
            },
        ),
        said="去洗了",
    )

    moment = await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(21, 40)))

    assert (moment.switched, moment.pulled_by, moment.doing) == (
        True,
        "浴室的热水烧好了",
        "去洗澡",
    )
    assert moment.lane == LANE and moment.persona_id == "akao"
    assert moment.began_at == _at(21, 40)


# --------------------------------------------------------------------------
# 二 · 换的是一件事，不是一个时长
# --------------------------------------------------------------------------


def test_keeping_something_in_mind_does_not_require_changing_what_she_is_doing():
    """挂线头是独立的一件事，签名里不该出现任何"你改去做什么"。"""
    assert keep_in_mind in MOMENT_TOOLS
    assert set(keep_in_mind.definition.parameters["properties"]) == {
        "still_on_my_mind"
    }
    assert "still_on_my_mind" not in switch_to.definition.parameters["properties"], (
        "线头还绑在 switch_to 上 —— 她答「继续」的那些 moment 就永远记不住任何事"
    )


def test_she_has_a_hand_that_ends_this_round():
    """她自己说「就到这儿」的那只手 —— 不然这一轮什么时候结束只能由代码判。"""
    from app.living.moment import stop_for_now

    assert stop_for_now in MOMENT_TOOLS
    assert stop_for_now.definition.parameters.get("properties", {}) == {}, (
        "这只手有参数 —— 她只决定停，别的什么都不该问她"
    )


def test_ending_the_round_really_ends_it():
    """名字要在终止工具那份名单上，不然调了它循环照样往下跑。

    名单在 :mod:`app.agent.core`，改名不同步就是**静默**失效：她调了这只手，模型接着
    被问下一轮，谁也不会报错。
    """
    from app.agent.core import _TERMINAL_TOOL_NAMES
    from app.living.moment import stop_for_now

    assert stop_for_now.name in _TERMINAL_TOOL_NAMES


def test_no_tool_ever_asks_her_how_long_something_takes():
    """真人对"多久"没有内感受。问她要一个分钟数就是把生活切成日程表。"""
    banned = ("minute", "duration", "how_long", "seconds", "until", "hour")
    for t in MOMENT_TOOLS:
        params = t.definition.parameters.get("properties", {})
        for pname in params:
            assert not any(b in pname.lower() for b in banned), (
                f"{t.name} 的参数 {pname} 在问她一个时长 —— 她换的是一件事"
            )


@pytest.mark.integration
async def test_switching_puts_her_somewhere_doing_something(moment_db, stub_moment):
    stub_moment(
        (
            "switch_to",
            {"doing": "煮抹茶", "place": "家/厨房", "because": "想喝点热的"},
        )
    )

    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    where = await current_whereabouts(lane=LANE, persona_id="akao")
    assert (where.place, where.doing) == ("家/厨房", "煮抹茶")


@pytest.mark.integration
async def test_switching_nowhere_is_refused_instead_of_silently_losing_her(
    moment_db, stub_moment
):
    """位置是旁听的全部依据；空位置会让她从此谁也听不见，而且一句报错都没有。"""
    runner = stub_moment(
        ("switch_to", {"doing": "发呆", "place": "  ", "because": "没事干"})
    )

    moment = await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    assert isinstance(runner.results[0], dict), "空位置被接受了"
    assert await current_whereabouts(lane=LANE, persona_id="akao") is None
    assert moment.switched is False


# --------------------------------------------------------------------------
# 三 · 说话和做动作 —— 必须带真内容
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_what_she_says_reaches_her_sister_word_for_word(
    moment_db, stub_moment, post
):
    """按世界里的名字说给姐妹的，原话直接送到她那里，跟她在哪没关系（:mod:`app.living.outgoing`）。"""
    await _stand("akao", "家/客厅", "待着", _at(13))
    await _stand("ayana", "家/楼上/绫奈房间", "画画", _at(13))
    stub_moment(("say", {"what": "周末祭典我陪你去。", "to": ["绫奈"]}))

    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    assert [(s.sender, s.recipient, s.body) for s in post.sent] == [
        ("赤尾", "绫奈", "当面对你说：「周末祭典我陪你去。」")
    ]


@pytest.mark.integration
async def test_speaking_face_to_face_is_what_the_house_can_overhear(
    moment_db, stub_moment
):
    await _stand("akao", "家/客厅", "待着", _at(13))
    stub_moment(("say", {"what": "抹茶好了。", "to": ["绫奈"]}))

    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    from app.living.snapshot import recent_own_happenings

    said = await recent_own_happenings(lane=LANE, persona_id="akao", limit=5)
    assert (said[0].kind, said[0].medium, said[0].audience) == (
        KIND_SPEECH,
        MEDIUM_IN_PERSON,
        ["绫奈"],
    )


@pytest.mark.integration
async def test_speaking_to_two_sisters_at_once_is_one_thing_not_two(
    moment_db, stub_moment, post
):
    """她做的是一件事（一条经历）；送到的是两个人，一人一条。"""
    await _stand("akao", "家/客厅", "待着", _at(13))
    stub_moment(("say", {"what": "抹茶煮多了，谁要。", "to": ["绫奈", "千凪"]}))

    moment = await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    assert moment.recorded == 1
    assert sorted(s.recipient for s in post.sent) == ["千凪", "绫奈"]
    assert all("「抹茶煮多了，谁要。」" in s.body for s in post.sent)


@pytest.mark.integration
async def test_she_can_speak_to_someone_the_code_never_heard_of(
    moment_db, stub_moment
):
    """这个世界里住着谁由 world 写，不由代码里的名单说了算。

    旧行为是拿 :data:`~app.living.persona.LIVING_PERSONAS` 当收件人白名单：``to`` 里
    出现三姐妹之外的任何名字直接报错。于是 world 可以写"许阿姨下午来敲门"，而她物理
    上对许阿姨开不了口 —— 代码里存着一份"世界上有谁"的断言，越写越不是世界的样子。
    """
    await _stand("akao", "家/玄关", "应门", _at(13))
    runner = stub_moment(("say", {"what": "许阿姨，进来坐。", "to": ["xu_yi"]}))

    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    assert not isinstance(runner.results[0], dict), (
        f"她对世界里的第四个人开不了口：{runner.results[0]!r}"
    )
    from app.living.snapshot import recent_own_happenings

    said = await recent_own_happenings(lane=LANE, persona_id="akao", limit=5)
    assert [(h.content, h.audience) for h in said] == [
        ("许阿姨，进来坐。", ["xu_yi"])
    ]


@pytest.mark.integration
async def test_saying_nothing_is_not_saying(moment_db, stub_moment):
    await _stand("akao", "家/客厅", "待着", _at(13))
    runner = stub_moment(("say", {"what": "   ", "to": ["绫奈"]}))

    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    assert isinstance(runner.results[0], dict)


@pytest.mark.integration
async def test_she_cannot_act_before_she_is_anywhere(moment_db, stub_moment):
    """还没定下位置就动作 —— world 不知道这件事发生在哪，谁也察觉不到。"""
    runner = stub_moment(("act", {"what": "发了会儿呆"}))

    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    assert isinstance(runner.results[0], dict)


def test_an_act_does_not_get_to_declare_what_the_world_is_like():
    """``act`` 是"我做了什么"的通道，不是"世界是什么样"的通道。

    上一代 prod 审计（06-11..08-30）里每一条"家人生病"剧情都是同一个起法：life 在
    ``act`` 里顺带塞一句关于世界的断言（别人的身体、外面出的事、测出来的结果），
    下游把它当既成事实吃进去。这一版**没有任何人能否认**——:func:`_record` 不裁定，
    她的原话逐字进 world 的汇总，world 当成她做了的事，所以这条边界只能立在
    喂给模型的那份工具描述上。

    两层都得在，少一层就坏一边：动作**直接**造成的结果仍要照写（掐掉的话她的动作
    描述只剩半句），只有不由动作直接产生的世界断言不许她在这里宣布。而且得给她一
    个合法出口——想让人知道就用 ``say`` 说出口，说出口的话别人读到的是"你说的"。
    """
    desc = act.definition.description
    assert "直接造成的结果" in desc, (
        "正面那半没了 —— 她会把动作描述掐得只剩半句，把饭端上桌都不敢写"
    )
    assert "替世界" in desc and "宣布" in desc, (
        "没点明别替世界宣布不由这个动作产生的事"
    )
    assert "别人的身体" in desc and "外面" in desc, (
        "没点出最常被顺带塞进来的那几类断言（别人的身体 / 外面出的事）"
    )
    assert "say" in desc, (
        "没给出口 —— 不给 say 这条路，她要么憋着不写，要么照样塞进 act"
    )


def test_the_moment_tool_descriptions_carry_no_medical_examples():
    """守门：moment 模块的工具文案里不许出现医疗类示例词（与旧引擎同一条守门线）。

    这类词进了工具描述会被模型当成"这个家里正常会发生的事"照着编，本身就是那条脏
    剧情的输入源之一。所以上一条边界只能用**类目**说（"别人的身体怎么样""测出来是
    多少"），不能拿一个具体病例当例子——那等于一边立边界一边递剧本。

    只守 :mod:`app.living.moment` 自己定义的六件工具；手机和嘴住在别的模块里，各自
    的文案归各自的用例守。
    """
    from app.living.moment import move_to

    for t in (switch_to, move_to, keep_in_mind, say, act, look_around):
        desc = t.definition.description
        for word in ("发烧", "医院", "急诊", "生病", "体温", "多少度"):
            assert word not in desc, (
                f"{t.name} 的文案里出现了医疗示例词「{word}」—— 立边界的同时把剧本递了出去"
            )


# --------------------------------------------------------------------------
# 四 · 环顾四周 —— 问 world，用例在 ``tests/living/test_look_around.py``
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# 五 · 她记得住 —— 状态快照跨 moment 续接（T2 成败所系）
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_what_a_sister_said_can_be_kept_in_a_moment_that_carries_on(
    moment_db, stub_moment
):
    """**验收正条，也是整个实验最想验证的那条：跨 moment 因果延续。**

    绫奈跟她说「周末陪我去祭典」。她手上的书没放下（这个 moment 答「继续」，``switched``
    是 False），但她心里记住了。接下来她一个 moment 接一个 moment 什么都没做，跨过一个
    清理点之后那件事还在她眼前，而且指得出是从哪个 moment 带过来的。

    「是否换事」不等于「是否记住」——把挂心事绑在 ``switch_to`` 上，这句话在她看过之后
    就永久消失了：传到她这里的消息只摆一次，她自己最近那十二条里只有她**自己**说做的，
    别人说的话不在里面，谁也救不回来。

    **看的是她这一轮读到的全部，不只是最后那条刺激。** 每轮的刺激只送新发生的事；她挂
    着什么是状态，跟着连续上下文走，清理那一下再重铺一次
    （:mod:`app.agent.continuity`）。
    """
    await _stand("akao", "家/客厅", "看书", _at(13))
    await _reaches_her("akao", "绫奈", "当面对你说：「周末陪我去祭典好不好」", _at(13, 58))

    stub_moment(
        ("keep_in_mind", {"still_on_my_mind": ["绫奈问我周末陪不陪她去祭典"]}),
        said="继续",
    )
    first = await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    assert first.switched is False, "她手上的事没变 —— 这个 moment 就是「继续」"
    assert first.open_ends == 1

    quiet = stub_moment(said="继续")
    for step in range(1, 7):  # 14:10 一路到 15:10，跨过 15:00 那个清理点
        await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14) + _STEP * step))

    read = _all_she_read(quiet.runs[-1])
    assert "绫奈问我周末陪不陪她去祭典" in read, (
        "隔了六个「继续」她就忘了绫奈跟她说过什么 —— 这正是上一代的死法"
    )
    assert first.moment_id in read, (
        "她眼前说不出这件事是从哪个 moment 带过来的 —— 延续就成了没有证据的断言"
    )
    still = await list_open_loose_ends(lane=LANE, persona_id="akao")
    assert still[0].opened_moment_id == first.moment_id


@pytest.mark.integration
async def test_switching_does_not_by_itself_wipe_what_she_is_keeping_in_mind(
    moment_db, stub_moment
):
    """换事情跟记不记得住是两件事：换个事做不该把心里挂着的东西清空。"""
    await _stand("akao", "家/客厅", "看书", _at(13))
    stub_moment(("keep_in_mind", {"still_on_my_mind": ["洗的衣服还在阳台"]}))
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    stub_moment(
        ("switch_to", {"doing": "去厨房", "place": "家/厨房", "because": "饿了"})
    )
    second = await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14) + _STEP))

    assert second.switched is True
    assert [e.what for e in await list_open_loose_ends(lane=LANE, persona_id="akao")] == [
        "洗的衣服还在阳台"
    ]
    assert second.open_ends == 1


@pytest.mark.integration
async def test_the_list_she_reports_replaces_the_whole_list(moment_db, stub_moment):
    """全量替换：这次没列的就是了结了。她的省略在生效，不是代码判过期。"""
    await _stand("akao", "家/客厅", "看书", _at(13))
    stub_moment(
        ("keep_in_mind", {"still_on_my_mind": ["洗的衣服还在阳台", "回绫奈的祭典"]})
    )
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    stub_moment(("keep_in_mind", {"still_on_my_mind": ["回绫奈的祭典"]}))
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14) + _STEP))

    assert [e.what for e in await list_open_loose_ends(lane=LANE, persona_id="akao")] == [
        "回绫奈的祭典"
    ]


@pytest.mark.integration
async def test_keeping_nothing_in_mind_is_a_thing_she_can_say(moment_db, stub_moment):
    await _stand("akao", "家/客厅", "看书", _at(13))
    stub_moment(("keep_in_mind", {"still_on_my_mind": ["洗的衣服还在阳台"]}))
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    stub_moment(("keep_in_mind", {"still_on_my_mind": []}))
    third = await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14) + _STEP))

    assert await list_open_loose_ends(lane=LANE, persona_id="akao") == []
    assert third.open_ends == 0


# --------------------------------------------------------------------------
# 五 bis · 她挂的事可以带一个「该在几点」
# --------------------------------------------------------------------------


def _what_she_read(run) -> str:
    """这个 moment **新**摆到她眼前的那条 USER 消息（增量 + 手机信封）。

    喂给模型的列表是"连续上下文 + 这一轮的刺激"，刺激永远是最后一条
    （:func:`app.living.moment.run_moment`）。验"这个 moment 有没有**新**看到 X"只能
    看这一条：把整个列表连起来读的话，前几个 moment 的输入也算进来，答案从此永远是 yes。
    """
    stimulus = run[0][-1]
    assert stimulus.role is Role.USER, f"最后一条不是这一轮的刺激：{stimulus!r}"
    return stimulus.content


def _all_she_read(run) -> str:
    """这个 moment 眼前的全部 —— 连续上下文加这一轮的刺激。

    验"她还记得 X 吗"用这个：状态跟着上下文走，每轮的刺激只送新发生的事，全量状态由
    清理时那根界桩重铺（:mod:`app.agent.continuity`）。
    """
    return "\n".join(m.text() for m in run[0])


@pytest.mark.integration
async def test_a_moment_only_puts_what_is_new_in_front_of_her(
    moment_db, stub_moment
):
    """醒来只送新发生的事：几点了、隔了多久、这期间别人做了什么、手机上来了什么。

    她此刻的样子读一百遍字字一样，而且她上一轮已经读过、还在上下文里。每轮重发一份就
    是把同一段话抄二十四遍。
    """
    await _stand("akao", "家/客厅", "看昨天拍的胶片", _at(13))
    stub_moment(("keep_in_mind", {"still_on_my_mind": ["洗的衣服还在阳台"]}))
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    await _reaches_her("akao", "绫奈", "当面对你说：「姐，抹茶还有吗」", _at(14, 5))
    quiet = stub_moment(said="继续")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14) + _STEP))

    fresh = _what_she_read(quiet.runs[-1])
    assert "姐，抹茶还有吗" in fresh, "这期间别人说的话没送到"
    assert "离上一次过了 10 分钟" in fresh, f"没说隔了多久。拿到：\n{fresh}"
    assert "看昨天拍的胶片" in fresh, (
        "她这一轮不知道自己在干嘛 —— 位置和手上的事每轮都可能被她自己改，"
        "不能只在界桩上铺"
    )
    assert "洗的衣服还在阳台" not in fresh, (
        "「心里挂着什么」每一轮都在重发 —— 它在上下文里已经有了"
    )


@pytest.mark.integration
async def test_a_cold_start_still_tells_her_where_she_stands(
    moment_db, stub_moment
):
    """上下文是空的那一轮（一天的开头、刚重启），全量状态必须还在她眼前。

    它由清理时那根界桩给（:func:`app.agent.continuity.trim_for_round`），不是由刺激
    重新塞一份 —— 两个地方各渲染一份全量状态，迟早只改一处。
    """
    await _stand("akao", "家/客厅", "看昨天拍的胶片", _at(13))
    stub_moment(("keep_in_mind", {"still_on_my_mind": ["洗的衣服还在阳台"]}))
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    quiet = stub_moment(said="继续")
    # 上下文不再按天切，所以"历史是空的"要另外造：清一遍那张表，模拟刚重启 / 刚清库。
    await _wipe_transcripts()
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14) + dt.timedelta(days=1)))

    read = _all_she_read(quiet.runs[-1])
    assert "看昨天拍的胶片" in read, "冷启动那一轮她不知道自己在哪、在做什么"
    assert "洗的衣服还在阳台" in read, "冷启动那一轮她不知道自己心里挂着什么"
    assert read.count("洗的衣服还在阳台") == 1, (
        "「心里挂着什么」出现了两次 —— 全量只该有界桩一个出处"
    )


@pytest.mark.integration
async def test_a_thing_she_hung_an_hour_on_comes_due_in_front_of_her(
    moment_db, stub_moment
):
    """**验收正条**：她挂一件该在几点的事，还没到的时候它在她眼前，到点那一下当场说到了。

    她的安排是她自己心里挂着的事，不是一张到期交付一次就被消费掉的时刻表。"我该去开的
    那个会"在她真的去之前不会因为时间过了就不算数。

    **"到点了"必须在到点那一个 moment 就送到她手上**，不能等下一次重铺。挂着的清单跟
    着状态走、清理时才重铺（默认一小时），所以"刚到点的"单独走每轮的增量
    （:meth:`app.living.snapshot.MomentSnapshot.render_new`）；少了那一段，一件 15:30
    该做的事要到 16:00 她才看得见。
    """
    await _stand("akao", "家/客厅", "看书", _at(13))
    stub_moment(
        ("keep_in_mind", {"still_on_my_mind": ["[2026-07-25 15:00] 家属谈话会"]})
    )
    first = await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))
    assert first.open_ends == 1

    quiet = stub_moment(said="继续")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14) + _STEP))
    before = _all_she_read(quiet.runs[-1])
    assert "[2026-07-25 15:00] 家属谈话会" in before, (
        f"她眼前那条没带上该在几点。拿到：\n{before}"
    )
    assert "到点了" not in before, f"还没到就说到了。拿到：\n{before}"

    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(15, 0)))
    due = _what_she_read(quiet.runs[-1])
    assert "到点了" in due or "刚到点的" in due, (
        f"到点那一下她眼前新来的东西里没有任何变化。拿到：\n{due}"
    )
    assert "[2026-07-25 15:00] 家属谈话会" in due


@pytest.mark.integration
async def test_rescheduling_is_just_listing_a_different_hour(moment_db, stub_moment):
    """改期不需要第二只手：整份重写里这次列的时刻不一样，就是改期。"""
    await _stand("akao", "家/客厅", "看书", _at(13))
    runner = stub_moment(
        ("keep_in_mind", {"still_on_my_mind": ["[2026-07-25 15:00] 家属谈话会"]})
    )
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))
    assert "[2026-07-25 15:00] 家属谈话会" in runner.results[0], (
        "当场那句确认没把时刻回给她 —— 她无从知道自己写的时刻收下了没有"
    )

    stub_moment(
        ("keep_in_mind", {"still_on_my_mind": ["[2026-07-25 17:30] 家属谈话会"]})
    )
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14) + _STEP))

    ends = await list_open_loose_ends(lane=LANE, persona_id="akao")
    assert len(ends) == 1, "改期开出了第二条线头"
    assert ends[0].due_at == _at(17, 30)


@pytest.mark.integration
async def test_an_hour_she_wrote_wrong_is_handed_back_instead_of_swallowed(
    moment_db, stub_moment
):
    """写不成时刻时报错喂回去让她改，不静默当成"这条没有时刻"。"""
    await _stand("akao", "家/客厅", "看书", _at(13))
    runner = stub_moment(
        ("keep_in_mind", {"still_on_my_mind": ["[明天下午三点] 家属谈话会"]})
    )

    moment = await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    assert isinstance(runner.results[0], dict), "写不成的时刻被静默吞掉了"
    assert await list_open_loose_ends(lane=LANE, persona_id="akao") == []
    assert moment.open_ends == 0


@pytest.mark.integration
async def test_writing_only_a_time_does_not_empty_what_she_keeps_in_mind(
    moment_db, stub_moment
):
    """她漏写了那件事本身，心里挂着的东西不许因此被一次性清空。

    ``keep_in_mind`` 是**整份重写**：一条只有时刻、没有正文的条目要是被当成空行跳过，
    这一份就成了空清单——她上一个 moment 挂着的全部走进关闭流程，而她收到的是一句成功。
    """
    await _stand("akao", "家/客厅", "看书", _at(13))
    stub_moment(("keep_in_mind", {"still_on_my_mind": ["洗的衣服还在阳台"]}))
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    runner = stub_moment(("keep_in_mind", {"still_on_my_mind": ["[2026-07-25 15:00]"]}))
    second = await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14) + _STEP))

    assert isinstance(runner.results[0], dict), "写了一半的条目被当成成功收下了"
    assert [
        e.what for e in await list_open_loose_ends(lane=LANE, persona_id="akao")
    ] == ["洗的衣服还在阳台"], "她心里挂着的被静默清空了"
    assert second.open_ends == 1


def test_the_shape_she_is_taught_to_write_is_the_shape_that_parses():
    """工具文案里那个例子必须真的解析得出来 —— 教的和收的是同一个形状。"""
    from app.living.loose_ends import DUE_EXAMPLE, parse_entry

    assert DUE_EXAMPLE in keep_in_mind.definition.description, (
        "工具描述里没有那个可照抄的例子 —— 她只能猜时刻该怎么写"
    )
    assert parse_entry(f"{DUE_EXAMPLE} 家属谈话会")[1] is not None


@pytest.mark.integration
async def test_what_a_sister_does_beside_her_reaches_her_only_through_her_inbox(
    moment_db, stub_moment, post
):
    """别的姐妹做了什么、说了什么，是她们自己的经历。同一间屋里也一样：谁会察觉由 world
    判断后告诉她，传到她这里的只有收件箱里那一段，没有一条路按位置从别人的记录里读。"""
    await _stand("akao", "家/客厅", "看胶片", _at(13))
    await _stand("ayana", "家/客厅", "看书", _at(13))
    stub_moment(
        ("say", {"what": "这本书好难懂", "to": []}),
        ("act", {"what": "把书合上了"}),
    )
    await run_moment(lane=LANE, persona_id="ayana", clock=clock_at(_at(14)))

    runner = stub_moment(said="继续")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    everything = _all_she_read(runner.runs[0])
    assert "这本书好难懂" not in everything and "把书合上了" not in everything, (
        f"绫奈的经历进了她这一轮：\n{everything}"
    )
    seen = _what_she_read(runner.runs[0])
    assert "这段时间你感知到的" not in seen, seen
    assert "这段时间传到你这里的" in seen, seen


# --------------------------------------------------------------------------
# 六 · 她是谁 —— persona_core 运行时注入
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_her_hobbies_are_in_front_of_her_every_moment(moment_db, stub_moment):
    """全仓只有每周一次的 persona_review 读过 persona_core；她"想不起自己想做什么"
    的第二个独立病因就是这个。"""
    runner = stub_moment(said="继续")

    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    prompt_vars = runner.runs[0][1]["prompt_vars"]
    assert prompt_vars["persona_core"] == stub_moment.persona.persona_core
    assert prompt_vars["persona_name"] == "赤尾"


@pytest.mark.integration
async def test_a_blank_core_says_so_instead_of_rendering_a_hole(
    moment_db, stub_moment, monkeypatch
):
    from app.living import persona as persona_mod

    async def blank(persona_id: str):
        return SimpleNamespace(display_name="赤尾", persona_core="   ")

    runner = stub_moment(said="继续")
    monkeypatch.setattr(persona_mod, "find_persona", blank)

    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    core = runner.runs[0][1]["prompt_vars"]["persona_core"]
    assert core.strip() != ""


@pytest.mark.integration
async def test_the_prompt_variables_are_exactly_three(moment_db, stub_moment):
    """变量改名会**静默**渲染成字面量，所以能少一个就少一个；每个 moment 都变的东西走 USER。

    第三个（手边有哪些说明可读）是例外：它会变（注册表每 30 秒热加载），而工具
    schema 在 import 时定死，装不下一份会变的清单，只能从 prompt 变量进。
    """
    runner = stub_moment(said="继续")

    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    assert set(runner.runs[0][1]["prompt_vars"]) == {
        "persona_name",
        "persona_core",
        "guides_you_can_read",
    }


@pytest.mark.integration
async def test_the_guides_she_can_read_are_listed_in_front_of_her(
    moment_db, stub_moment, tmp_path
):
    """她要知道手边有哪些说明可读 —— 不然读说明那只手她永远猜不出该填什么名字。

    清单跟着盘上实际有的那几份走：这里真的写一份到盘上、真的加载进注册表，断言她
    这个 moment 的输入里出现的就是它。
    """
    from app.living.guides import GUIDES_VAR
    from app.skills.registry import SkillRegistry

    folder = tmp_path / "drawing"
    folder.mkdir()
    (folder / "SKILL.md").write_text(
        "---\nname: drawing\ndescription: 人物画图指南\n---\n\n黑长直。\n",
        encoding="utf-8",
    )
    SkillRegistry.load_all(tmp_path)
    try:
        runner = stub_moment(said="继续")
        await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))
    finally:
        # 注册表是 class-level 全局状态，留着会漏进别的用例。
        SkillRegistry.load_all(tmp_path / "这个目录不存在")

    listed = runner.runs[0][1]["prompt_vars"][GUIDES_VAR]
    assert "drawing" in listed and "人物画图指南" in listed


# --------------------------------------------------------------------------
# 七 · 循环：间隔可调、串行、幂等、三个人都跑
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_second_moment_too_soon_does_not_run(moment_db, stub_moment):
    runner = stub_moment(said="继续")

    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))
    skipped = await run_moment(
        lane=LANE, persona_id="akao", clock=clock_at(_at(14) + _STEP - dt.timedelta(minutes=1))
    )

    assert skipped is None
    assert len(runner.runs) == 1


@pytest.mark.integration
async def test_a_moment_runs_again_once_the_gap_has_passed(moment_db, stub_moment):
    runner = stub_moment(said="继续")

    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))
    later = await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14) + _STEP))

    assert later is not None
    assert len(runner.runs) == 2


@pytest.mark.integration
async def test_one_sisters_moment_does_not_gate_another(moment_db, stub_moment):
    runner = stub_moment(said="继续")

    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))
    hers = await run_moment(lane=LANE, persona_id="ayana", clock=clock_at(_at(14)))

    assert hers is not None
    assert len(runner.runs) == 2


@pytest.mark.integration
async def test_the_gap_comes_from_dynamic_config(moment_db, monkeypatch):
    from app.living import moment as moment_mod

    seen: dict[str, str] = {}

    def fake_get(key: str, *, default: str = "") -> str:
        seen["key"] = key
        return "5"

    monkeypatch.setattr(moment_mod.dynamic_config, "get", fake_get)
    assert await life_moment_minutes() == 5
    assert seen["key"] == moment_mod.LIVING_LIFE_MOMENT_MINUTES_KEY


@pytest.mark.integration
async def test_a_junk_gap_falls_back_to_ten_minutes(moment_db, monkeypatch):
    from app.living import moment as moment_mod

    monkeypatch.setattr(
        moment_mod.dynamic_config, "get", lambda key, *, default="": "十分钟"
    )
    assert await life_moment_minutes() == DEFAULT_LIFE_MOMENT_MINUTES


@pytest.mark.integration
async def test_replaying_the_same_moment_lands_one_row(moment_db, stub_moment):
    """同一个 moment 被重放（崩在落记录之前、durable 重投）只该在账上占一行。

    重放大概率**不会**产出一模一样的内容，所以幂等必须落在自然键上、跟内容无关。
    """
    stub_moment(said="继续")
    first = await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    from app.runtime.persist import insert_idempotent, select_all_versions

    replayed = LifeMoment(
        **{**first.model_dump(), "said": "这一遍她说了别的", "recorded": 99}
    )
    assert await insert_idempotent(replayed) == 0

    rows = await select_all_versions(
        LifeMoment,
        {"lane": LANE, "persona_id": "akao", "moment_id": first.moment_id},
    )
    assert len(rows) == 1
    assert rows[0].said == "继续", "重放把第一遍的记录覆盖掉了"


@pytest.mark.integration
async def test_the_tick_walks_all_three_sisters(moment_db, monkeypatch):
    from app.living import moment as moment_mod

    walked: list[tuple[str, str]] = []

    async def spy(*, lane: str, persona_id: str, clock):
        walked.append((lane, persona_id))
        return None

    monkeypatch.setattr(moment_mod, "run_moment", spy)
    monkeypatch.setattr(moment_mod, "living_lane", lambda: LANE)

    await life_moment_tick(LifeMomentTick(ts=_at(14).isoformat()))

    assert sorted(p for _, p in walked) == sorted(LIVING_PERSONAS)
    assert {lane for lane, _ in walked} == {LANE}


@pytest.mark.integration
async def test_one_sister_blowing_up_does_not_stop_the_others(moment_db, monkeypatch):
    from app.living import moment as moment_mod

    walked: list[str] = []

    async def flaky(*, lane: str, persona_id: str, clock):
        walked.append(persona_id)
        if persona_id == LIVING_PERSONAS[0]:
            raise RuntimeError("她那边炸了")
        return None

    monkeypatch.setattr(moment_mod, "run_moment", flaky)
    monkeypatch.setattr(moment_mod, "living_lane", lambda: LANE)

    await life_moment_tick(LifeMomentTick(ts=_at(14).isoformat()))

    assert sorted(walked) == sorted(LIVING_PERSONAS)


@pytest.mark.integration
async def test_moments_of_another_lane_do_not_gate_this_one(moment_db, stub_moment):
    runner = stub_moment(said="继续")

    await run_moment(lane="prod", persona_id="akao", clock=clock_at(_at(14)))
    mine = await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14, 1)))

    assert mine is not None
    assert len(runner.runs) == 2


@pytest.mark.integration
async def test_the_latest_moment_is_the_one_that_ran_last(moment_db, stub_moment):
    stub_moment(said="继续")

    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14) + _STEP))

    latest = await latest_moment(lane=LANE, persona_id="akao")
    assert latest.began_at == _at(14) + _STEP


@pytest.mark.integration
async def test_a_moment_is_not_replayed_by_the_agent_retry(moment_db, stub_moment):
    """durable mutation：整轮 ReAct 被 @retry 包着，重放会把已经写过的库再写一遍。"""
    runner = stub_moment(said="继续")

    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    assert runner.runs[0][1]["max_retries"] == 1


# --------------------------------------------------------------------------
# 七 bis · moment 的时间锚跨重试稳定（副作用写完、收尾前崩，不许重来一遍）
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_crash_before_the_record_lands_does_not_redo_her_actions(
    moment_db, stub_moment, monkeypatch
):
    """工具都写完了、落记录时崩掉 —— 下一拍必须落回同一格，动作一件都不许多。

    没有稳定时间锚的话：下一拍 ``now`` 变了 → ``moment_id`` 变了 → ``happening_id``
    / whereabouts 的自然键全跟着变 → 她把同一句话又说了一遍、同一次移动又走了一遍，
    而且两条记录长得不一样，事后查不出来是重复。
    """
    from app.living import moment as moment_mod
    from app.living.snapshot import recent_own_happenings

    await _stand("akao", "家/客厅", "看书", _at(13))
    stub_moment(
        ("say", {"what": "我去煮点抹茶。", "to": ["ayana"]}),
        ("switch_to", {"doing": "煮抹茶", "place": "家/厨房", "because": "想喝点热的"}),
        ("keep_in_mind", {"still_on_my_mind": ["锅还在火上"]}),
        said="去煮了",
    )

    real_insert = moment_mod.insert_idempotent

    async def crash(row, **_kw):
        raise RuntimeError("落一个 moment 记录时崩了")

    monkeypatch.setattr(moment_mod, "insert_idempotent", crash)
    with pytest.raises(RuntimeError):
        await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14, 0)))

    monkeypatch.setattr(moment_mod, "insert_idempotent", real_insert)
    again = await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14, 1)))

    assert again is not None, "上一个 moment 没留下记录，这一拍该重跑"
    assert again.moment_id == _at(14, 0).isoformat(timespec="minutes")
    said = await recent_own_happenings(lane=LANE, persona_id="akao", limit=10)
    assert [h.content for h in said] == ["我去煮点抹茶。"], (
        "重试把她说过的话又说了一遍"
    )
    assert [
        e.what for e in await list_open_loose_ends(lane=LANE, persona_id="akao")
    ] == ["锅还在火上"]
    where = await current_whereabouts(lane=LANE, persona_id="akao")
    assert (where.place, where.doing) == ("家/厨房", "煮抹茶")


@pytest.mark.integration
async def test_a_moment_is_named_by_its_grid_cell_and_lives_at_the_time_it_ran(
    moment_db, stub_moment
):
    """moment 的身份是格子，不是钟表上那一瞬 —— 落在格上才可能跨重试对得上。她这一轮的
    『现在』却是它真正跑起来的那一刻：一格里晚了几分钟才轮到她，她看到的、做的都在那几分钟之后。"""
    stub_moment(said="继续")

    moment = await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14, 7)))

    assert moment.moment_id == _at(14, 0).isoformat(timespec="minutes")
    assert moment.began_at == _at(14, 7)


@pytest.mark.integration
async def test_a_cell_that_already_ran_late_does_not_run_again(moment_db, stub_moment):
    """一格只跑一次：14:07 才跑的 14:00 那一格，14:09 那一拍不再跑它，14:10 跑下一格。"""
    runner = stub_moment(said="继续")

    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14, 7)))
    again = await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14, 9)))
    next_cell = await run_moment(
        lane=LANE, persona_id="akao", clock=clock_at(_at(14, 10))
    )

    assert again is None
    assert next_cell is not None
    assert next_cell.moment_id == _at(14, 10).isoformat(timespec="minutes")
    assert len(runner.runs) == 2


def test_one_wake_has_room_for_more_than_one_whole_thing():
    """上限的含义是"她一口气能做几件事"，不是"一次思考多深"。

    一件完整的事要三四次模型调用（拿到中间结果 → 用上它 → 说出来 / 做出来），所以
    8 只够两件；而她 2026-09-11 实测那 12 只需要中间结果的工具一次都没用过。上限本身
    不是节奏——节奏由 ``stop_for_now`` 交给她自己，这个数只是兜底。
    """
    from app.living.moment import _MOMENT_CFG

    assert _MOMENT_CFG.recursion_limit == 12


def test_the_moment_runs_on_the_life_model():
    """life 一天 432 个 moment × 三个人 —— 这条高频线走 ``life-model``，不占离线调用的档位。"""
    from app.living.moment import _MOMENT_CFG

    assert _MOMENT_CFG.model_id == "life-model"


# --------------------------------------------------------------------------
# 七 ter · 谁是"最近一个 moment"由**落地顺序**说了算，常规节奏只数常规的格子
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_an_early_moment_that_lands_first_is_not_taken_as_the_last_one(
    moment_db, stub_moment
):
    """提前来的 moment 先落地、排在它后面的常规 moment 后落地 —— "上一个"是后落地的那个。

    两条钟并发打到同一个人时 :func:`app.living.serial.hold` 让后到的**排队**而不是
    丢掉：21:34 被叫来的那个 moment 先拿到占用、先跑完，21:35 那一拍的常规 moment（21:30
    那一格）随后才轮到。它的『现在』是它真正跑起来的 21:35，不是它那一格的 21:30。

    下一轮问"离上一次过了多久"、"上一轮的上下文落地了没有"，问的都是**最后落地**的那个。
    """
    await _stand("akao", "家/客厅", "看书", _at(21, 20))
    runner = stub_moment(said="继续")

    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(21, 20)))
    early = await run_moment(
        lane=LANE, persona_id="akao", clock=clock_at(_at(21, 34)), nudged_by="msg-1"
    )
    assert early is not None and early.began_at == _at(21, 34)
    regular = await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(21, 35)))
    assert regular is not None
    assert regular.moment_id == _at(21, 30).isoformat(timespec="minutes")
    assert regular.began_at == _at(21, 35)
    assert regular.seq > early.seq

    latest = await latest_moment(lane=LANE, persona_id="akao")
    assert latest.moment_id == regular.moment_id, (
        "「最近一个 moment」取成了先落地的那个 moment，不是最后落地的那个 moment"
    )

    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(21, 45)))
    assert "离上一次过了 10 分钟" in _what_she_read(runner.runs[-1]), (
        "「离上一次」没从最后落地的那个 moment 算"
    )


@pytest.mark.integration
async def test_the_regular_rhythm_counts_only_the_regular_cells(moment_db, stub_moment):
    """常规节奏认的是「最近跑过的那一格」，提前来的 moment 不算一格。

    这条和上一条是**两个问题**：游标问"最后落地的那个 moment"，节奏问"最近跑过的那一格"。
    21:35 才跑的 21:30 那一格跑过了，21:39 那一拍不再跑它；21:40 是新的一格，照常来。
    """
    from app.living.moment import latest_regular_moment

    stub_moment(said="继续")

    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(21, 20)))
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(21, 34)), nudged_by="m-1")
    regular = await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(21, 35)))

    last_regular = await latest_regular_moment(lane=LANE, persona_id="akao")
    assert last_regular.moment_id == regular.moment_id
    assert last_regular.nudged is False, "节奏判断认了提前来的 moment"

    assert (
        await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(21, 39)))
        is None
    ), "21:30 那一格跑了两遍"
    on_time = await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(21, 40)))
    assert on_time is not None, "21:40 那一格被吞了"
    assert on_time.began_at == _at(21, 40)


@pytest.mark.integration
async def test_each_moment_carries_the_order_it_landed_in(moment_db, stub_moment):
    """落地顺序是一列**单调递增**的数，跟她的钟没有关系。

    这条是上面两条的地基：``began_at`` 只说她这个 moment 的『现在』是几点，谁先谁后落库
    是另一个问题，得有自己的一列去答。现在每一轮都在拿到占用时读钟，新写下的几轮钟点先后
    跟落地先后一致；加这一列之前、常规 moment 还记着格子的那些旧行没有这个保证
    （``tests/living/test_registered.py`` 里钟点故意造反的那一条）。
    """
    stub_moment(said="继续")

    first = await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(21, 20)))
    early = await run_moment(
        lane=LANE, persona_id="akao", clock=clock_at(_at(21, 34)), nudged_by="m-1"
    )
    regular = await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(21, 35)))

    assert [m.seq for m in (first, early, regular)] == [1, 2, 3]
    # 每个人一条轴，互不牵连。
    hers = await run_moment(lane=LANE, persona_id="ayana", clock=clock_at(_at(21, 35)))
    assert hers.seq == 1


@pytest.mark.integration
async def test_ticks_while_her_round_is_stuck_do_not_pile_up_behind_it(
    moment_db, stub_moment
):
    """她那一轮挂住了（最长占着 15 分钟），钟每分钟一拍。排在后面等的只有一拍：后面的拍看见
    已经有一拍在等，直接过去。等着的那一拍轮到时按那一刻判这一格跑没跑过。"""
    import asyncio

    from app.living.moment import life_moment_lock_key
    from app.living.serial import _lock_for, hold
    from tests.living.conftest import queued_behind

    stub_moment(said="继续")
    key = life_moment_lock_key(LANE, "akao")
    async with hold(key):  # 挂住的那一轮
        ticks = [
            asyncio.create_task(
                run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14, m)))
            )
            for m in (0, 1, 2)
        ]
        await queued_behind(key)
        await asyncio.sleep(0.2)  # 后两拍都走到了占用门口
        assert len(_lock_for(key)._waiters) == 1, "每一拍都排在了挂住的那一轮后面"
        assert [t.done() for t in ticks] == [False, True, True]
        assert [t.result() for t in ticks[1:]] == [None, None]

    assert (await ticks[0]) is not None


# --------------------------------------------------------------------------
# 八 · 建表 / 挂钟：错了就静默，或者错了就起不来
# --------------------------------------------------------------------------


def _in_a_fresh_process(expr: str, *, lane: str = "coe-living") -> str:
    """泳道是输入：living 的三条钟只在 ``coe-*`` 上注册（见 app/wiring/living.py）。"""
    proc = subprocess.run(
        [sys.executable, "-c", f"import app.wiring;{expr}"],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "LANE": lane},
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_the_life_tables_reach_the_registry_via_app_wiring():
    out = _in_a_fresh_process(
        "from app.runtime.data import DATA_REGISTRY;"
        "print(sorted(c.__name__ for c in DATA_REGISTRY))"
    )
    for name in ("LooseEnd", "LifeMoment"):
        assert f"'{name}'" in out, (
            f"{name} 没进 DATA_REGISTRY —— migrate_schema 不会建它的表。registry: {out}"
        )


def test_the_life_clock_is_wired_to_an_interval_source():
    out = _in_a_fresh_process(
        "from app.runtime.wire import WIRING_REGISTRY;"
        "print([(s.data_type.__name__,"
        " sorted(c.__name__ for c in s.consumers),"
        " [(x.kind, x.params) for x in s.sources])"
        " for s in WIRING_REGISTRY"
        " if s.data_type.__name__ == 'LifeMomentTick'])"
    )
    assert "LifeMomentTick" in out and "life_moment_tick" in out, out
    assert "'interval'" in out, f"life 那条钟没挂上时间源 —— 她永远不会醒。拿到：{out}"


def test_the_life_tick_is_constructible_from_ts_alone():
    """框架源循环只喂一个 ``ts``；多一个必填字段 = 每一拍 ValidationError 杀 Pod。"""
    assert LifeMomentTick(ts="2026-07-25T14:00:00+08:00").ts == (
        "2026-07-25T14:00:00+08:00"
    )


def test_the_life_tick_is_transient():
    assert LifeMomentTick.Meta.transient is True


def test_the_life_column_shapes_are_pinned():
    """additive-only：加列可以，改类型 / 删列会让已建表的 lane 启动时 MigrationError。"""
    assert {n: pg_type(f) for n, f in LooseEnd.model_fields.items()} == {
        "lane": "TEXT",
        "persona_id": "TEXT",
        "thread_id": "TEXT",
        "ver": "BIGINT",
        "what": "TEXT",
        # 她自己挂的「该在几点」。可空列、additive —— 加列之前的行留 NULL，读出来
        # 就是"这条没有时刻"，跟她本来就没写时刻是同一个意思。
        "due_at": "TIMESTAMPTZ",
        "opened_at": "TIMESTAMPTZ",
        "opened_moment_id": "TEXT",
        "closed_at": "TIMESTAMPTZ",
        "closed_moment_id": "TEXT",
    }
    assert {n: pg_type(f) for n, f in LifeMoment.model_fields.items()} == {
        "lane": "TEXT",
        "persona_id": "TEXT",
        "moment_id": "TEXT",
        "seq": "BIGINT",
        "began_at": "TIMESTAMPTZ",
        "switched": "BOOLEAN",
        "pulled_by": "TEXT",
        "recorded": "BIGINT",
        "doing": "TEXT",
        "open_ends": "BIGINT",
        "said": "TEXT",
        "context_ver": "BIGINT",
        "nudged": "BOOLEAN",
    }


def test_the_life_records_refuse_a_naive_instant():
    from pydantic import ValidationError

    naive = dt.datetime(2026, 7, 25, 14, 0)
    with pytest.raises(ValidationError, match="时区"):
        LifeMoment(
            lane=LANE,
            persona_id="akao",
            moment_id="m",
            seq=1,
            began_at=naive,
            switched=False,
            pulled_by="",
            recorded=0,
            doing="",
            open_ends=0,
            said="继续",
            context_ver=1,
        )
    with pytest.raises(ValidationError, match="时区"):
        LooseEnd(
            lane=LANE,
            persona_id="akao",
            thread_id="t",
            ver=1,
            what="x",
            opened_at=naive,
            opened_moment_id="m",
        )


def test_the_prompt_lives_in_langfuse_under_its_own_id():
    """新引擎用新 prompt id，只发泳道 label，绝不碰 production。"""
    assert LIFE_MOMENT_PROMPT_ID == "living_life_moment"


# --------------------------------------------------------------------------
# 六 · 人挪了，事情没变
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_moving_without_changing_what_she_is_doing_updates_where_she_is(
    moment_db, stub_moment
):
    """走到别处但手上的事没变 —— 位置得跟着走，doing 原样留着。

    实测（coe-living，2026-09-01 09:00–11:20）：绫奈的位置在「学校/教学楼走廊」
    冻了 2 小时 20 分，``data_whereabouts`` 整个上午只有两行。她的 act 一直在写
    「走回二年三班的教室，在靠窗位子坐下」——**至少写了 6 次**。因为位置只由
    ``switch_to`` 写，而她手上那件事（找教室、等上课）从头到尾没变过，所以她一次
    都没"换事情"，位置也就一次都没更新。
    """
    await _stand("ayana", "学校/教学楼走廊", "等第一节课", _at(9))
    stub_moment(("move_to", {"place": "学校/二年三班教室"}))

    await run_moment(lane=LANE, persona_id="ayana", clock=clock_at(_at(9, 20)))

    where = await current_whereabouts(lane=LANE, persona_id="ayana")
    assert where.place == "学校/二年三班教室", "人挪了位置没跟着"
    assert where.doing == "等第一节课", "手上的事被这一步改掉了"


@pytest.mark.integration
async def test_moving_is_not_switching_to_something_else(moment_db, stub_moment):
    """挪个地方不算「换了事情」。

    ``switch_to`` 的语义是"什么把你从刚才那件事里带走了"，走动没有这回事。混进去
    会让逐个 moment 复盘里的"换事率"把单纯的走动也算成换事情。
    """
    await _stand("ayana", "学校/教学楼走廊", "等第一节课", _at(9))
    stub_moment(("move_to", {"place": "学校/二年三班教室"}))

    moment = await run_moment(lane=LANE, persona_id="ayana", clock=clock_at(_at(9, 20)))

    assert moment.switched is False, "走一步被算成了换事情"
    assert moment.doing == "等第一节课"


@pytest.mark.integration
async def test_moving_nowhere_is_refused_like_switching_nowhere(
    moment_db, stub_moment
):
    """空位置一样得顶回去 —— 理由跟 switch_to 那条一模一样。"""
    await _stand("ayana", "学校/教学楼走廊", "等第一节课", _at(9))
    runner = stub_moment(("move_to", {"place": "   "}))

    await run_moment(lane=LANE, persona_id="ayana", clock=clock_at(_at(9, 20)))

    assert isinstance(runner.results[0], dict), "空位置被接受了"
    where = await current_whereabouts(lane=LANE, persona_id="ayana")
    assert where.place == "学校/教学楼走廊", "空位置把她挪走了"


@pytest.mark.integration
async def test_moving_before_she_ever_stood_anywhere_says_so(
    moment_db, stub_moment
):
    """从没落过位置时挪不动 —— 没有"手上那件事"可以原样带走。"""
    runner = stub_moment(("move_to", {"place": "学校/二年三班教室"}))

    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(9, 20)))

    assert isinstance(runner.results[0], dict), "凭空挪了一个没有位置的人"
    assert await current_whereabouts(lane=LANE, persona_id="akao") is None


def test_moving_is_one_of_the_hands_she_actually_has():
    """没注册 = 她永远调不到，症状跟原 bug 一模一样但更难查。"""
    from app.living.moment import move_to

    assert move_to in MOMENT_TOOLS
    assert set(move_to.definition.parameters["properties"]) == {"place"}


# --------------------------------------------------------------------------
# 七 · 这个 moment 花了多少 token，得能查
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_seam_records_what_it_spent_where_it_can_be_counted(
    moment_db, stub_moment
):
    """一个 moment 的 token 用量要落 durable PG，不能只指望 langfuse。

    ``app.agent.trace`` 里写着实测结论：langfuse **会系统性丢 trace**（这次实测
    整夜 225 个 moment 只到了 125 条，丢 44%），所以「真相在 PG」——用 ``collect_usage``
    包住 run、``record_round_cost`` 落库。这一刀一开始漏了，于是「一晚上花了多少」
    根本查不出来。
    """
    from app.domain.thinking_cost import ThinkingTokensSpent
    from app.runtime.persist import select_all_versions
    from tests.runtime.conftest import migrate

    await migrate(ThinkingTokensSpent, moment_db)
    await _stand("akao", "家/客厅", "看书", _at(13))
    stub_moment(("act", {"what": "把胶片摊了一茶几"}))

    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    spent = await select_all_versions(
        ThinkingTokensSpent,
        {
            "lane": LANE,
            "actor": "akao",
            "round_id": _at(14).isoformat(timespec="minutes"),
        },
    )
    assert spent, "这个 moment 没有留下任何用量记录 —— 成本无从统计"


@pytest.mark.integration
async def test_the_conversations_she_can_see_are_settled_once_for_the_whole_moment(
    moment_db, stub_moment, monkeypatch
):
    """"她这个 moment 看得见哪些会话"在信封那一眼定下来，整个 moment 共用那一份。

    信封是她这个 moment 看到的第一样东西，所以名单也在那儿定（:mod:`app.living.whitelist`）。
    信封摆在这个 moment 的 context 外面算的话会有两个后果：这份名单被算两遍（它是这个功能
    最贵的一项），而且两遍之间到达的消息会让一条会话在信封上没有、她后半程却搜得到、
    发得出去。
    """
    from app.living import whitelist as whitelist_mod
    from tests.living.test_phone import _DM, _incoming, _seed_world

    await _seed_world()
    await _incoming(_DM, text_body="在吗", at=_at(21, 0))

    real = whitelist_mod.count_summons_since
    counted: list[dict] = []

    async def counting(**kw):
        counted.append(kw)
        return await real(**kw)

    monkeypatch.setattr(whitelist_mod, "count_summons_since", counting)

    stub_moment(
        ("look_at_phone", {"channel_id": str(_DM)}),
        ("look_up_contact", {"name": "bezhai"}),
    )
    moment = await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(21, 30)))

    assert moment is not None
    assert len(counted) == 1, (
        f"这个 moment 把名单算了 {len(counted)} 遍 —— 信封和她手里的工具用的不是同一份"
    )


@pytest.mark.integration
async def test_a_notification_she_ignores_is_not_put_in_front_of_her_again(
    moment_db, stub_moment
):
    """她没看手机，下一轮的刺激里不再有那条通知 —— 上一轮那份还在她眼前。

    上下文一直连着（:mod:`app.agent.continuity`），所以"她还不知道有人找她"这件事
    只需要说一次。每轮重摆一遍同一份未读清单，就是同一段话一小时抄六遍。
    """
    from tests.living.test_phone import _DM, _incoming, _seed_world

    await _seed_world()
    await _incoming(_DM, text_body="在吗", at=_at(21, 0))

    first = stub_moment(said="嗯")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(21, 30)))
    assert "bezhai" in _what_she_read(first.runs[-1]), (
        "消息到了的那一轮，通知都没摆到她眼前"
    )

    second = stub_moment(said="继续")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(21, 30) + _STEP))

    fresh = _what_she_read(second.runs[-1])
    assert "bezhai" not in fresh, (
        f"这一轮手机没动静，通知却又摆了一遍。拿到：\n{fresh}"
    )
    assert "bezhai" in _all_she_read(second.runs[-1]), (
        "上一轮那份通知也不在她眼前了 —— 那她就真的不知道有人找过她了"
    )


@pytest.mark.integration
async def test_what_she_still_has_not_read_comes_back_on_the_checkpoint(
    moment_db, stub_moment
):
    """她一直不看，那条通知不能随着旧刺激一起被裁掉。

    每轮只给新到的，摆出它的那一轮刺激走 ``own_minutes``，到期整组删。界桩不重铺的话
    之后再没有第二处说得出有人找过她 —— 她不看手机就永远不知道。
    """
    from app.agent.continuity import CHECKPOINT_HEAD
    from tests.living.test_phone import _DM, _incoming, _seed_world

    await _seed_world()
    # 消息落在 21:50：跨过 22:00 那个清理点之后它仍在"她看得见的会话"那一档里
    # （:mod:`app.living.whitelist`，一小时内一句就够），不然这条用例验的是名单过期。
    await _incoming(_DM, text_body="在吗", at=_at(21, 50))

    stub_moment(said="嗯")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(21, 55)))

    # 跨过下一个清理点（默认一小时一次，落在整点上）：这一轮会立一根新界桩。
    later = stub_moment(said="继续")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(22, 5)))

    posts = [
        m.text()
        for m in later.runs[-1][0]
        if m.text().startswith(CHECKPOINT_HEAD)
    ]
    assert posts, "这一轮跨过了清理点却没立界桩"
    assert "bezhai" in posts[-1], (
        f"界桩上没有她还没看的那条 —— 摆出它的那一轮刺激一到期，这件事就无声消失了。"
        f"拿到：\n{posts[-1]}"
    )


# --------------------------------------------------------------------------
# place 这个参数不给她任何地名样本
#
# 她唯一见过的地名样本就是这只手的参数说明，而**举例就是词表**：``家/楼上/我房间``
# 被两个人同时抄走（同一分钟判成同处一室），``学校/二年三班教室`` 被一字不差抄走
# （设定集里那间叫 ``学校/初二三班教室``）。换一批新的写死字符串只是把过期时间往后
# 推 —— 世界随时会改名、删掉、重写那些地方，而举例不跟着变。
#
# 说清楚路径的形状不需要样本：层级、分隔符、从哪一层写起，都能直说。
# --------------------------------------------------------------------------


@pytest.mark.parametrize("hand", ["switch_to", "move_to"])
def test_the_place_parameter_hands_her_no_place_name(hand):
    """落位置那两只手交给她的每一段字，都不含一条具体路径、也不含一个具体地名。"""
    from tests.living.conftest import (
        model_facing_text,
        names_of_places_in,
        path_samples,
    )

    for where, text in model_facing_text(_TOOLS[hand]).items():
        assert not path_samples(text), (
            f"{hand} 的{where}里摆着一条路径样本 {path_samples(text)!r} —— "
            f"她会逐字抄走它。原文：\n{text}"
        )
        assert not names_of_places_in(text), (
            f"{hand} 的{where}里写着具体地名 {names_of_places_in(text)!r} —— "
            f"那是世界的内容，世界改名之后这几个字还在教她写一个不存在的地方。"
            f"原文：\n{text}"
        )


@pytest.mark.parametrize("hand", ["switch_to", "move_to"])
def test_the_place_parameter_quotes_no_example_at_all(hand):
    """place 的描述里不出现引号 —— 这一档挡的是**没有斜杠的**那种样本。

    ``path_samples`` 判的是"斜杠两边贴着字"，可一个顶层地名只有一段、根本没有斜杠，
    它照样是样本，照样会被逐字抄走（``超市`` 那次就是一段）。在这个参数的描述里，
    引号引起来的东西只可能是一个可以照着填的名字。
    """
    described = _TOOLS[hand].definition.parameters["properties"]["place"]["description"]

    assert "「" not in described, f"place 的描述里引着一个样本：{described!r}"
    assert "例如" not in described, f"place 的描述里在举例：{described!r}"


@pytest.mark.parametrize("hand", ["switch_to", "move_to"])
def test_the_place_parameter_still_says_what_shape_a_path_is(hand):
    """清掉样本不等于不说形状 —— 分隔符是什么，仍然要在参数描述里说出来。

    只钉"没有样本"的话，把整段描述删空也能绿，而那时她连该写成几层都不知道。
    """
    described = _TOOLS[hand].definition.parameters["properties"]["place"]["description"]

    assert "/" in described, f"没告诉她层与层之间用什么隔开：{described!r}"
    assert "层" in described, f"没告诉她这是一条层级路径：{described!r}"


@pytest.mark.integration
async def test_refusing_an_empty_place_hands_her_no_place_name(
    moment_db, in_a_moment
):
    """空 place 那句报错也是喂给她的字 —— 它同样不能夹一个地名进去。

    这一句比参数描述更容易被漏掉：它只在她写错的时候出现，而她写错的那一刻正是最
    可能照着眼前这句话改的时候。
    """
    from tests.living.conftest import names_of_places_in, path_samples

    async with in_a_moment("akao"):
        refused = await switch_to.invoke(
            {"doing": "走走", "place": "   ", "because": "闲"}
        )
        moved = await move_to.invoke({"place": ""})

    for said in (str(refused), str(moved)):
        assert not path_samples(said), f"报错里摆着路径样本：{said!r}"
        assert not names_of_places_in(said), f"报错里写着具体地名：{said!r}"


# --------------------------------------------------------------------------
# 落位置的两只手只写位置
#
# ``switch_to`` / ``move_to`` 把她落到一个地方就返回那一句确认。它们不读 world 的任何
# 东西：地方文档、那儿还在持续的事都不从这里来。life 不直接读 world 的存储，两边唯一
# 的连接是通信机制。
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_landing_somewhere_hands_back_only_where_she_is(
    moment_db, in_a_moment
):
    """刚有人在那儿做过事，两只手也一个字都不带出来：返回的就是那一句确认。"""
    from app.living.happening import record_happening

    await record_happening(
        lane=LANE,
        happening_id="ayana-on-the-field",
        actor="ayana",
        kind="act",
        content="在操场边上跑圈",
        occurred_at=_at(9),
    )

    async with in_a_moment("akao", now=_at(10)):
        switched = await switch_to.invoke(
            {"doing": "找吃的", "place": "家/厨房", "because": "饿了"}
        )
        moved = await move_to.invoke({"place": "学校/操场"})

    assert switched == "你在 家/厨房，找吃的。"
    assert moved == "你在 学校/操场，还在找吃的。"
