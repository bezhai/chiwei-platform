"""一缝 —— 每十分钟她回到自己身上一次，默认答「继续」。

五条硬边界，各有对应的用例：

  * **默认是最便宜的那种轮次。** 一个词、零工具、零写库；但这一缝跑过要留痕，
    不然"哪些缝是继续、哪些换了事情"根本算不出来。
  * **换的是一件事，不是一个时长。** 工具签名里不许出现任何分钟数 —— 真人对
    "多久"没有内感受，问她要一个数字就是在替她把生活切成日程表。
  * **她记得住。** 状态快照跨缝续接：一件事被她列进心上之后，中间隔多少个"继续"
    都还在，而且指得出是从哪一缝带过来的。
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

from app.agent.neutral import Message, Role
from app.agent.runtime_context import agent_context
from app.living.happening import read_perceived_by
from app.living.loose_ends import LooseEnd, list_open_loose_ends
from app.living.moment import (
    DEFAULT_LIFE_MOMENT_MINUTES,
    LIFE_MOMENT_PROMPT_ID,
    LIVING_PERSONAS,
    MOMENT_TOOLS,
    LifeMoment,
    LifeMomentTick,
    act,
    keep_in_mind,
    latest_moment,
    life_moment_minutes,
    life_moment_tick,
    look_around,
    run_moment,
    say,
    switch_to,
)
from app.living.records import KIND_ACT, KIND_SPEECH, MEDIUM_IN_PERSON
from app.living.whereabouts import current_whereabouts, note_whereabouts
from app.runtime.schema_types import pg_type

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))
_STEP = dt.timedelta(minutes=DEFAULT_LIFE_MOMENT_MINUTES)

_TOOLS = {t.name: t for t in MOMENT_TOOLS}


def _at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 7, 25, hour, minute, tzinfo=_CST)


@pytest.fixture
async def moment_db(living_db):
    from tests.runtime.conftest import migrate

    for cls in (LooseEnd, LifeMoment):
        await migrate(cls, living_db)
    return living_db


class FakeMoment:
    """替身 life：这一缝她调了哪些工具是写死的，只有模型那一步是假的。

    走真工具 + 真 context 绑定，所以写库、派生 id、lane 隔离都是被真的验到的。
    """

    def __init__(self, *calls: tuple[str, dict], said: str = "继续") -> None:
        self.calls = list(calls)
        self.said = said
        self.runs: list[tuple[list[Message], dict]] = []
        self.results: list[object] = []

    async def run(self, messages, **kwargs):
        self.runs.append((messages, kwargs))
        with agent_context(kwargs["context"]):
            for name, args in self.calls:
                self.results.append(await _TOOLS[name].invoke(args))
        return Message(role=Role.ASSISTANT, content=self.said)


@pytest.fixture
def stub_moment(monkeypatch):
    """装一个替身 life + 固定缝间隔 + 一份不碰真库的 persona。"""
    from app.living import moment as moment_mod

    persona = SimpleNamespace(
        display_name="赤尾",
        persona_core="她拍胶片、写角色分析、逛论坛、收周边、cos、泡抹茶店、打视觉小说。",
    )

    async def fake_find_persona(persona_id: str):
        return persona

    monkeypatch.setattr(moment_mod, "find_persona", fake_find_persona)

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


# --------------------------------------------------------------------------
# 一 · 默认「继续」，而且这一缝留得下痕
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_moment_that_carries_on_costs_one_word_and_writes_nothing(
    moment_db, stub_moment
):
    stub_moment(said="继续")

    moment = await run_moment(lane=LANE, persona_id="akao", now=_at(14))

    assert moment is not None
    assert moment.switched is False
    assert moment.said == "继续"
    assert moment.recorded == 0
    assert moment.pulled_by == ""
    assert await current_whereabouts(lane=LANE, persona_id="akao") is None


@pytest.mark.integration
async def test_every_moment_is_countable_afterwards(moment_db, stub_moment):
    """验收要逐缝看：哪些继续、哪些换了事情、换的理由是什么。"""
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

    moment = await run_moment(lane=LANE, persona_id="akao", now=_at(21, 40))

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
        "线头还绑在 switch_to 上 —— 她答「继续」的那些缝就永远记不住任何事"
    )


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

    await run_moment(lane=LANE, persona_id="akao", now=_at(14))

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

    moment = await run_moment(lane=LANE, persona_id="akao", now=_at(14))

    assert isinstance(runner.results[0], dict), "空位置被接受了"
    assert await current_whereabouts(lane=LANE, persona_id="akao") is None
    assert moment.switched is False


# --------------------------------------------------------------------------
# 三 · 说话和做动作 —— 必须带真内容
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_what_she_says_reaches_her_sister_word_for_word(moment_db, stub_moment):
    await _stand("akao", "家/客厅", "待着", _at(13))
    await _stand("ayana", "家/楼上/绫奈房间", "画画", _at(13))
    stub_moment(("say", {"what": "周末祭典我陪你去。", "to": ["ayana"]}))

    await run_moment(lane=LANE, persona_id="akao", now=_at(14))

    heard = await read_perceived_by(lane=LANE, persona_id="ayana")
    assert [(p.actor, p.content, p.directed) for p in heard.items] == [
        ("akao", "周末祭典我陪你去。", True)
    ]


@pytest.mark.integration
async def test_speaking_face_to_face_is_what_the_house_can_overhear(
    moment_db, stub_moment
):
    await _stand("akao", "家/客厅", "待着", _at(13))
    stub_moment(("say", {"what": "抹茶好了。", "to": ["ayana"]}))

    await run_moment(lane=LANE, persona_id="akao", now=_at(14))

    from app.living.snapshot import recent_own_happenings

    said = await recent_own_happenings(lane=LANE, persona_id="akao", limit=5)
    assert (said[0].kind, said[0].medium, said[0].audience) == (
        KIND_SPEECH,
        MEDIUM_IN_PERSON,
        ["ayana"],
    )


@pytest.mark.integration
async def test_speaking_to_two_sisters_at_once_is_one_thing_not_two(
    moment_db, stub_moment
):
    await _stand("akao", "家/客厅", "待着", _at(13))
    stub_moment(("say", {"what": "抹茶煮多了，谁要。", "to": ["ayana", "chinagi"]}))

    moment = await run_moment(lane=LANE, persona_id="akao", now=_at(14))

    assert moment.recorded == 1
    for who in ("ayana", "chinagi"):
        heard = await read_perceived_by(lane=LANE, persona_id=who)
        assert [p.content for p in heard.items] == ["抹茶煮多了，谁要。"]


@pytest.mark.integration
async def test_speaking_to_a_name_that_is_nobody_is_refused(moment_db, stub_moment):
    """收件人写错会静默送不到（audience 谁也匹配不上）。挡在工具里，报错喂回去。"""
    await _stand("akao", "家/客厅", "待着", _at(13))
    runner = stub_moment(("say", {"what": "喂。", "to": ["绫奈"]}))

    await run_moment(lane=LANE, persona_id="akao", now=_at(14))

    assert isinstance(runner.results[0], dict), "一个不存在的收件人被接受了"
    from app.living.snapshot import recent_own_happenings

    assert await recent_own_happenings(lane=LANE, persona_id="akao", limit=5) == []


@pytest.mark.integration
async def test_saying_nothing_is_not_saying(moment_db, stub_moment):
    await _stand("akao", "家/客厅", "待着", _at(13))
    runner = stub_moment(("say", {"what": "   ", "to": ["ayana"]}))

    await run_moment(lane=LANE, persona_id="akao", now=_at(14))

    assert isinstance(runner.results[0], dict)


@pytest.mark.integration
async def test_an_act_is_something_the_room_can_see(moment_db, stub_moment):
    await _stand("akao", "家/客厅", "待着", _at(13))
    await _stand("ayana", "家/客厅", "看书", _at(13))
    stub_moment(("act", {"what": "把胶片摊了一茶几"}))

    await run_moment(lane=LANE, persona_id="akao", now=_at(14))

    heard = await read_perceived_by(lane=LANE, persona_id="ayana")
    assert [(p.kind, p.content) for p in heard.items] == [
        (KIND_ACT, "把胶片摊了一茶几")
    ]


@pytest.mark.integration
async def test_she_cannot_act_before_she_is_anywhere(moment_db, stub_moment):
    """还没定下位置就动作 —— 那条记录会落在一个空地点上，谁也感知不到。"""
    runner = stub_moment(("act", {"what": "发了会儿呆"}))

    await run_moment(lane=LANE, persona_id="akao", now=_at(14))

    assert isinstance(runner.results[0], dict)


def test_an_act_does_not_get_to_declare_what_the_world_is_like():
    """``act`` 是"我做了什么"的通道，不是"世界是什么样"的通道。

    上一代 prod 审计（06-11..08-30）里每一条"家人生病"剧情都是同一个起法：life 在
    ``act`` 里顺带塞一句关于世界的断言（别人的身体、外面出的事、测出来的结果），
    下游把它当既成事实吃进去。旧引擎至少还有 world 那一环能按自己的记录不认
    （#321 给 ``world_deliberate`` 划的那条边界）；这一版**没有任何人能否认**——
    ``app.living.world`` 根本不读姐妹之间发生了什么，:func:`_record` 也不裁定，
    渲染出来更是逐字原话，落在别人快照的"这段时间你感知到的"里。传播路径比旧引擎
    还直，所以这条边界只能立在喂给模型的那份工具描述上。

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
    """守门：这一缝的工具文案里不许出现医疗类示例词（与旧引擎同一条守门线）。

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
# 四 · 查世界 —— 够得着的地方现在怎么样
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_looking_around_sees_who_is_here_and_what_they_are_doing(
    moment_db, stub_moment
):
    await _stand("akao", "家/客厅", "待着", _at(13))
    await _stand("ayana", "家/客厅", "看书", _at(13))
    runner = stub_moment(("look_around", {}))

    await run_moment(lane=LANE, persona_id="akao", now=_at(14))

    seen = runner.results[0]
    assert "ayana" in seen and "看书" in seen


@pytest.mark.integration
async def test_looking_around_only_places_the_ones_elsewhere_in_the_house(
    moment_db, stub_moment
):
    """同一栋别处：知道她在哪，不知道她在干嘛 —— 信息差归位置管。"""
    await _stand("akao", "家/客厅", "待着", _at(13))
    await _stand("ayana", "家/楼上/绫奈房间", "偷偷哭", _at(13))
    runner = stub_moment(("look_around", {}))

    await run_moment(lane=LANE, persona_id="akao", now=_at(14))

    seen = runner.results[0]
    assert "家/楼上/绫奈房间" in seen
    assert "偷偷哭" not in seen


@pytest.mark.integration
async def test_looking_around_does_not_promote_a_vague_location_into_this_room(
    moment_db, stub_moment
):
    """姐姐只定位到「家」，她在「家/客厅」—— 不许判成同处一室、把人家在干嘛吐出来。

    T3 给 ``reach_between`` 加的覆盖档是为**范围事件**服务的（天黑笼罩整栋）。人不是
    范围：「绫奈在家」不代表她就在客厅。拿覆盖档比两个人 = 位置一粗就泄露。
    """
    await _stand("akao", "家/客厅", "待着", _at(13))
    await _stand("ayana", "家", "在家里某处偷偷哭", _at(13))
    runner = stub_moment(("look_around", {}))

    await run_moment(lane=LANE, persona_id="akao", now=_at(14))

    seen = runner.results[0]
    assert "偷偷哭" not in seen, "位置一粗她就把姐姐在干嘛看光了"
    assert "ayana" in seen, "知道姐姐在这栋楼里是对的，不知道的只是她在干嘛"


@pytest.mark.integration
async def test_looking_around_cannot_reach_someone_who_is_out(moment_db, stub_moment):
    await _stand("akao", "家/客厅", "待着", _at(13))
    await _stand("chinagi", "学校/图书馆", "自习", _at(13))
    runner = stub_moment(("look_around", {}))

    await run_moment(lane=LANE, persona_id="akao", now=_at(14))

    seen = runner.results[0]
    assert "chinagi" not in seen and "自习" not in seen


# --------------------------------------------------------------------------
# 五 · 她记得住 —— 状态快照跨缝续接（T2 成败所系）
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_what_a_sister_said_can_be_kept_in_a_moment_that_carries_on(
    moment_db, stub_moment
):
    """**验收正条，也是整个实验最想验证的那条：跨缝因果延续。**

    绫奈跟她说「周末陪我去祭典」。她手上的书没放下（这一缝答「继续」，``switched``
    是 False），但她心里记住了。接下来三缝她什么都没做。第五缝她读到的快照里那件事
    还在，而且指得出是从哪一缝带过来的。

    「是否换事」不等于「是否记住」——把挂心事绑在 ``switch_to`` 上，这条感知在游标
    推进之后就永久消失了：她自己最近那十二条里只有她**自己**说做的，别人说的话不在
    里面，谁也救不回来。
    """
    await _stand("akao", "家/客厅", "看书", _at(13))
    await _stand("ayana", "家/客厅", "待着", _at(13))
    from app.living.happening import record_happening

    await record_happening(
        lane=LANE,
        happening_id="ay-1",
        actor="ayana",
        place="家/客厅",
        kind=KIND_SPEECH,
        content="周末陪我去祭典好不好",
        occurred_at=_at(13, 58),
        audience=["akao"],
    )

    stub_moment(
        ("keep_in_mind", {"still_on_my_mind": ["绫奈问我周末陪不陪她去祭典"]}),
        said="继续",
    )
    first = await run_moment(lane=LANE, persona_id="akao", now=_at(14))

    assert first.switched is False, "她手上的事没变 —— 这一缝就是「继续」"
    assert first.open_ends == 1

    quiet = stub_moment(said="继续")
    for step in (1, 2, 3):
        await run_moment(lane=LANE, persona_id="akao", now=_at(14) + _STEP * step)

    snapshot = "\n".join(
        m.content for m in quiet.runs[-1][0] if m.role is Role.USER
    )
    assert "绫奈问我周末陪不陪她去祭典" in snapshot, (
        "隔了三个「继续」她就忘了绫奈跟她说过什么 —— 这正是上一代的死法"
    )
    assert first.moment_id in snapshot, (
        "快照说不出这件事是从哪一缝带过来的 —— 延续就成了没有证据的断言"
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
    await run_moment(lane=LANE, persona_id="akao", now=_at(14))

    stub_moment(
        ("switch_to", {"doing": "去厨房", "place": "家/厨房", "because": "饿了"})
    )
    second = await run_moment(lane=LANE, persona_id="akao", now=_at(14) + _STEP)

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
    await run_moment(lane=LANE, persona_id="akao", now=_at(14))

    stub_moment(("keep_in_mind", {"still_on_my_mind": ["回绫奈的祭典"]}))
    await run_moment(lane=LANE, persona_id="akao", now=_at(14) + _STEP)

    assert [e.what for e in await list_open_loose_ends(lane=LANE, persona_id="akao")] == [
        "回绫奈的祭典"
    ]


@pytest.mark.integration
async def test_keeping_nothing_in_mind_is_a_thing_she_can_say(moment_db, stub_moment):
    await _stand("akao", "家/客厅", "看书", _at(13))
    stub_moment(("keep_in_mind", {"still_on_my_mind": ["洗的衣服还在阳台"]}))
    await run_moment(lane=LANE, persona_id="akao", now=_at(14))

    stub_moment(("keep_in_mind", {"still_on_my_mind": []}))
    third = await run_moment(lane=LANE, persona_id="akao", now=_at(14) + _STEP)

    assert await list_open_loose_ends(lane=LANE, persona_id="akao") == []
    assert third.open_ends == 0


# --------------------------------------------------------------------------
# 五 bis · 她挂的事可以带一个「该在几点」
# --------------------------------------------------------------------------


def _what_she_read(run) -> str:
    """她那一缝真正读到的 USER 消息（快照 + 手机信封）。"""
    return "\n".join(m.content for m in run[0] if m.role is Role.USER)


@pytest.mark.integration
async def test_a_thing_she_hung_an_hour_on_comes_due_in_front_of_her(
    moment_db, stub_moment
):
    """**验收正条**：她挂一件该在几点的事，下一缝显示还没到，到点之后那一缝显示到点了。

    她的安排不进 ``Upcoming``——那是世界的客观时刻表（快递到门口、天黑），到期交付
    一次就被消费掉。"我该去开的那个会"在她真的去之前不会因为时间过了就不算数，而且
    把她的安排塞进世界的账本等于 life 单方面替世界宣布将要发生什么。
    """
    await _stand("akao", "家/客厅", "看书", _at(13))
    stub_moment(
        ("keep_in_mind", {"still_on_my_mind": ["[2026-07-25 15:00] 家属谈话会"]})
    )
    first = await run_moment(lane=LANE, persona_id="akao", now=_at(14))
    assert first.open_ends == 1

    quiet = stub_moment(said="继续")
    await run_moment(lane=LANE, persona_id="akao", now=_at(14) + _STEP)
    before = _what_she_read(quiet.runs[-1])
    assert "[2026-07-25 15:00] 家属谈话会" in before, (
        f"她眼前那条没带上该在几点。拿到：\n{before}"
    )
    assert "还没到" in before

    await run_moment(lane=LANE, persona_id="akao", now=_at(15, 10))
    after = _what_she_read(quiet.runs[-1])
    assert "到点了" in after, f"到点了她眼前没有任何变化。拿到：\n{after}"


@pytest.mark.integration
async def test_rescheduling_is_just_listing_a_different_hour(moment_db, stub_moment):
    """改期不需要第二只手：整份重写里这次列的时刻不一样，就是改期。"""
    await _stand("akao", "家/客厅", "看书", _at(13))
    runner = stub_moment(
        ("keep_in_mind", {"still_on_my_mind": ["[2026-07-25 15:00] 家属谈话会"]})
    )
    await run_moment(lane=LANE, persona_id="akao", now=_at(14))
    assert "[2026-07-25 15:00] 家属谈话会" in runner.results[0], (
        "当场那句确认没把时刻回给她 —— 她无从知道自己写的时刻收下了没有"
    )

    stub_moment(
        ("keep_in_mind", {"still_on_my_mind": ["[2026-07-25 17:30] 家属谈话会"]})
    )
    await run_moment(lane=LANE, persona_id="akao", now=_at(14) + _STEP)

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

    moment = await run_moment(lane=LANE, persona_id="akao", now=_at(14))

    assert isinstance(runner.results[0], dict), "写不成的时刻被静默吞掉了"
    assert await list_open_loose_ends(lane=LANE, persona_id="akao") == []
    assert moment.open_ends == 0


@pytest.mark.integration
async def test_writing_only_a_time_does_not_empty_what_she_keeps_in_mind(
    moment_db, stub_moment
):
    """她漏写了那件事本身，心里挂着的东西不许因此被一次性清空。

    ``keep_in_mind`` 是**整份重写**：一条只有时刻、没有正文的条目要是被当成空行跳过，
    这一份就成了空清单——她上一缝挂着的全部走进关闭流程，而她收到的是一句成功。
    """
    await _stand("akao", "家/客厅", "看书", _at(13))
    stub_moment(("keep_in_mind", {"still_on_my_mind": ["洗的衣服还在阳台"]}))
    await run_moment(lane=LANE, persona_id="akao", now=_at(14))

    runner = stub_moment(("keep_in_mind", {"still_on_my_mind": ["[2026-07-25 15:00]"]}))
    second = await run_moment(lane=LANE, persona_id="akao", now=_at(14) + _STEP)

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
async def test_a_moment_only_sees_what_happened_since_the_last_one(
    moment_db, stub_moment
):
    await _stand("akao", "家/客厅", "待着", _at(13))
    await _stand("ayana", "家/客厅", "看书", _at(13))
    runner = stub_moment(said="继续")

    await run_moment(lane=LANE, persona_id="akao", now=_at(14))
    from app.living.happening import record_happening

    await record_happening(
        lane=LANE,
        happening_id="h-later",
        actor="ayana",
        place="家/客厅",
        kind=KIND_SPEECH,
        content="你在看什么",
        occurred_at=_at(14, 5),
        audience=["akao"],
    )
    second = await run_moment(lane=LANE, persona_id="akao", now=_at(14) + _STEP)
    third = await run_moment(lane=LANE, persona_id="akao", now=_at(14) + _STEP * 2)

    second_input = "\n".join(
        m.content for m in runner.runs[1][0] if m.role is Role.USER
    )
    third_input = "\n".join(m.content for m in runner.runs[2][0] if m.role is Role.USER)
    assert "你在看什么" in second_input
    assert "你在看什么" not in third_input, "游标没推进 —— 同一句话每缝重读一遍"
    assert third.after_seq == second.next_seq


@pytest.mark.integration
async def test_the_cursor_is_carried_by_the_moment_record(moment_db, stub_moment):
    await _stand("akao", "家/客厅", "待着", _at(13))
    await _stand("ayana", "家/客厅", "看书", _at(13))
    stub_moment(said="继续")
    from app.living.happening import record_happening

    said = await record_happening(
        lane=LANE,
        happening_id="h1",
        actor="ayana",
        place="家/客厅",
        kind=KIND_SPEECH,
        content="早",
        occurred_at=_at(13, 59),
        audience=["akao"],
    )

    first = await run_moment(lane=LANE, persona_id="akao", now=_at(14))
    second = await run_moment(lane=LANE, persona_id="akao", now=_at(14) + _STEP)

    assert (first.after_seq, first.next_seq, first.perceived) == (0, said.seq, 1)
    assert (second.after_seq, second.perceived) == (said.seq, 0)


# --------------------------------------------------------------------------
# 六 · 她是谁 —— persona_core 运行时注入
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_her_hobbies_are_in_front_of_her_every_moment(moment_db, stub_moment):
    """全仓只有每周一次的 persona_review 读过 persona_core；她"想不起自己想做什么"
    的第二个独立病因就是这个。"""
    runner = stub_moment(said="继续")

    await run_moment(lane=LANE, persona_id="akao", now=_at(14))

    prompt_vars = runner.runs[0][1]["prompt_vars"]
    assert prompt_vars["persona_core"] == stub_moment.persona.persona_core
    assert prompt_vars["persona_name"] == "赤尾"


@pytest.mark.integration
async def test_a_blank_core_says_so_instead_of_rendering_a_hole(
    moment_db, stub_moment, monkeypatch
):
    from app.living import moment as moment_mod

    async def blank(persona_id: str):
        return SimpleNamespace(display_name="赤尾", persona_core="   ")

    runner = stub_moment(said="继续")
    monkeypatch.setattr(moment_mod, "find_persona", blank)

    await run_moment(lane=LANE, persona_id="akao", now=_at(14))

    core = runner.runs[0][1]["prompt_vars"]["persona_core"]
    assert core.strip() != ""


@pytest.mark.integration
async def test_the_prompt_variables_are_exactly_two(moment_db, stub_moment):
    """变量改名会**静默**渲染成字面量，所以能少一个就少一个；每缝都变的东西走 USER。"""
    runner = stub_moment(said="继续")

    await run_moment(lane=LANE, persona_id="akao", now=_at(14))

    assert set(runner.runs[0][1]["prompt_vars"]) == {"persona_name", "persona_core"}


# --------------------------------------------------------------------------
# 七 · 循环：间隔可调、串行、幂等、三个人都跑
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_second_moment_too_soon_does_not_run(moment_db, stub_moment):
    runner = stub_moment(said="继续")

    await run_moment(lane=LANE, persona_id="akao", now=_at(14))
    skipped = await run_moment(
        lane=LANE, persona_id="akao", now=_at(14) + _STEP - dt.timedelta(minutes=1)
    )

    assert skipped is None
    assert len(runner.runs) == 1


@pytest.mark.integration
async def test_a_moment_runs_again_once_the_gap_has_passed(moment_db, stub_moment):
    runner = stub_moment(said="继续")

    await run_moment(lane=LANE, persona_id="akao", now=_at(14))
    later = await run_moment(lane=LANE, persona_id="akao", now=_at(14) + _STEP)

    assert later is not None
    assert len(runner.runs) == 2


@pytest.mark.integration
async def test_one_sisters_moment_does_not_gate_another(moment_db, stub_moment):
    runner = stub_moment(said="继续")

    await run_moment(lane=LANE, persona_id="akao", now=_at(14))
    hers = await run_moment(lane=LANE, persona_id="ayana", now=_at(14))

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
    """同一缝被重放（崩在落记录之前、durable 重投）只该在账上占一行。

    重放大概率**不会**产出一模一样的内容，所以幂等必须落在自然键上、跟内容无关。
    """
    stub_moment(said="继续")
    first = await run_moment(lane=LANE, persona_id="akao", now=_at(14))

    from app.runtime.persist import insert_idempotent, select_all_versions

    replayed = LifeMoment(
        **{**first.model_dump(), "said": "这一遍她说了别的", "perceived": 99}
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

    async def spy(*, lane: str, persona_id: str, now):
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

    async def flaky(*, lane: str, persona_id: str, now):
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

    await run_moment(lane="prod", persona_id="akao", now=_at(14))
    mine = await run_moment(lane=LANE, persona_id="akao", now=_at(14, 1))

    assert mine is not None
    assert len(runner.runs) == 2


@pytest.mark.integration
async def test_the_latest_moment_is_the_one_that_ran_last(moment_db, stub_moment):
    stub_moment(said="继续")

    await run_moment(lane=LANE, persona_id="akao", now=_at(14))
    await run_moment(lane=LANE, persona_id="akao", now=_at(14) + _STEP)

    latest = await latest_moment(lane=LANE, persona_id="akao")
    assert latest.began_at == _at(14) + _STEP


@pytest.mark.integration
async def test_a_moment_is_not_replayed_by_the_agent_retry(moment_db, stub_moment):
    """durable mutation：整轮 ReAct 被 @retry 包着，重放会把已经写过的库再写一遍。"""
    runner = stub_moment(said="继续")

    await run_moment(lane=LANE, persona_id="akao", now=_at(14))

    assert runner.runs[0][1]["max_retries"] == 1


# --------------------------------------------------------------------------
# 七 bis · 一缝的时间锚跨重试稳定（副作用写完、收尾前崩，不许重来一遍）
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
        raise RuntimeError("落一缝记录时崩了")

    monkeypatch.setattr(moment_mod, "insert_idempotent", crash)
    with pytest.raises(RuntimeError):
        await run_moment(lane=LANE, persona_id="akao", now=_at(14, 0))

    monkeypatch.setattr(moment_mod, "insert_idempotent", real_insert)
    again = await run_moment(lane=LANE, persona_id="akao", now=_at(14, 1))

    assert again is not None, "上一缝没留下记录，这一拍该重跑"
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
async def test_a_moment_is_stamped_on_its_grid_cell(moment_db, stub_moment):
    """缝的身份是格子，不是钟表上那一瞬 —— 落在格上才可能跨重试对得上。"""
    stub_moment(said="继续")

    moment = await run_moment(lane=LANE, persona_id="akao", now=_at(14, 7))

    assert moment.began_at == _at(14, 0)
    assert moment.moment_id == _at(14, 0).isoformat(timespec="minutes")


@pytest.mark.integration
async def test_the_cursor_only_advances_when_the_record_lands(
    moment_db, stub_moment, monkeypatch
):
    """记录没落地 = 游标没推进 = 那批感知不会被静默吞掉。"""
    from app.living import moment as moment_mod
    from app.living.happening import record_happening

    await _stand("akao", "家/客厅", "看书", _at(13))
    await _stand("ayana", "家/客厅", "待着", _at(13))
    await record_happening(
        lane=LANE,
        happening_id="ay-x",
        actor="ayana",
        place="家/客厅",
        kind=KIND_SPEECH,
        content="你在看什么",
        occurred_at=_at(13, 59),
        audience=["akao"],
    )

    runner = stub_moment(said="继续")

    async def crash(row, **_kw):
        raise RuntimeError("崩")

    real_insert = moment_mod.insert_idempotent
    monkeypatch.setattr(moment_mod, "insert_idempotent", crash)
    with pytest.raises(RuntimeError):
        await run_moment(lane=LANE, persona_id="akao", now=_at(14, 0))

    monkeypatch.setattr(moment_mod, "insert_idempotent", real_insert)
    again = await run_moment(lane=LANE, persona_id="akao", now=_at(14, 1))

    assert again.after_seq == 0
    retried_input = "\n".join(m.content for m in runner.runs[-1][0] if m.role is Role.USER)
    assert "你在看什么" in retried_input, "崩掉那一缝的感知被静默吞了"


def test_the_moment_runs_on_the_life_model():
    """life 一天 432 缝 × 三个人 —— 这条高频线走 ``life-model``，不占 world 的档位。"""
    from app.living.moment import _MOMENT_CFG

    assert _MOMENT_CFG.model_id == "life-model"


# --------------------------------------------------------------------------
# 七 ter · 谁是"最近一缝"由**落地顺序**说了算，不由钟点
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_an_early_moment_that_lands_first_does_not_rewind_the_cursor(
    moment_db, stub_moment
):
    """提前缝先落地、常规缝后落地 —— 游标不许退回提前缝那一格。

    两条钟并发打到同一个人时 :func:`app.living.serial.hold` 让后到的**排队**而不是
    丢掉，所以这个次序完全正常：21:34 被叫来的那一缝先拿到占用、先跑完
    （``began_at`` 是真实时刻 21:34），21:35 那一拍的常规缝随后才轮到、跑完
    （``began_at`` 是它的格子 21:30）。**落地顺序和 ``began_at`` 顺序是反的。**

    "读到哪了"问的是"**最后落地**的那一缝读到哪"。按 ``began_at`` 排会取回 21:34
    那一行的游标，把常规缝已经读过的那一段整个丢回去——她把同一批动静又感知一遍，
    而且一句报错都没有。
    """
    from app.living.happening import record_happening

    await _stand("akao", "家/客厅", "看书", _at(21, 20))
    await _stand("ayana", "家/客厅", "待着", _at(21, 20))
    stub_moment(said="继续")

    await run_moment(lane=LANE, persona_id="akao", now=_at(21, 20))

    await record_happening(
        lane=LANE,
        happening_id="h-before-nudge",
        actor="ayana",
        place="家/客厅",
        kind=KIND_SPEECH,
        content="姐我出门了。",
        occurred_at=_at(21, 31),
        audience=["akao"],
    )
    early = await run_moment(
        lane=LANE, persona_id="akao", now=_at(21, 34), nudged_by="msg-1"
    )
    assert early is not None and early.began_at == _at(21, 34)

    await record_happening(
        lane=LANE,
        happening_id="h-after-nudge",
        actor="ayana",
        place="家/客厅",
        kind=KIND_SPEECH,
        content="我回来了。",
        occurred_at=_at(21, 34) + dt.timedelta(seconds=30),
        audience=["akao"],
    )
    regular = await run_moment(lane=LANE, persona_id="akao", now=_at(21, 35))

    # 前提：常规缝的格子比提前缝的真实时刻早，而它读到了提前缝之后的新动静。
    assert regular is not None and regular.began_at == _at(21, 30)
    assert regular.next_seq > early.next_seq

    latest = await latest_moment(lane=LANE, persona_id="akao")
    assert latest.moment_id == regular.moment_id, (
        "「最近一缝」取成了钟点最靠后的那一缝，不是最后落地的那一缝"
    )

    nxt = await run_moment(lane=LANE, persona_id="akao", now=_at(21, 45))
    assert nxt.after_seq == regular.next_seq, "游标退回提前缝那一格了"
    assert nxt.perceived == 0, "游标退回去 —— 同一句话她又听了一遍"


@pytest.mark.integration
async def test_the_regular_rhythm_measures_from_the_latest_cell_not_the_last_write(
    moment_db, stub_moment
):
    """乱序落地之后，常规节奏认的仍然是「最近那一格」。

    这条和上一条是**两个问题、两种排序**：游标问"最后落地的那一缝"，节奏问"最近跑
    过的那一格"。合成一个的话，21:30 那一格（最后落地）会被当成最近一格，21:40 明明
    该来的那一缝就得再等十分钟。
    """
    from app.living.moment import latest_regular_moment

    stub_moment(said="继续")

    await run_moment(lane=LANE, persona_id="akao", now=_at(21, 20))
    await run_moment(lane=LANE, persona_id="akao", now=_at(21, 34), nudged_by="m-1")
    regular = await run_moment(lane=LANE, persona_id="akao", now=_at(21, 35))

    last_regular = await latest_regular_moment(lane=LANE, persona_id="akao")
    assert last_regular.moment_id == regular.moment_id
    assert last_regular.nudged is False, "节奏判断认了提前缝"
    assert last_regular.began_at == _at(21, 30)

    on_time = await run_moment(lane=LANE, persona_id="akao", now=_at(21, 40))
    assert on_time is not None, "21:40 那一格被吞了"
    assert on_time.began_at == _at(21, 40)


@pytest.mark.integration
async def test_each_moment_carries_the_order_it_landed_in(moment_db, stub_moment):
    """落地顺序是一列**单调递增**的数，跟她的钟没有关系。

    这条是上面两条的地基：``began_at`` 只说她这一缝的『现在』是几点，谁先谁后落库
    是另一个问题，得有自己的一列去答。
    """
    stub_moment(said="继续")

    first = await run_moment(lane=LANE, persona_id="akao", now=_at(21, 20))
    early = await run_moment(
        lane=LANE, persona_id="akao", now=_at(21, 34), nudged_by="m-1"
    )
    regular = await run_moment(lane=LANE, persona_id="akao", now=_at(21, 35))

    assert [m.seq for m in (first, early, regular)] == [1, 2, 3]
    assert regular.began_at < early.began_at, (
        "前提没造出来：这条要的就是「后落地的那一缝钟点更早」"
    )
    # 每个人一条轴，互不牵连。
    hers = await run_moment(lane=LANE, persona_id="ayana", now=_at(21, 35))
    assert hers.seq == 1


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
        "after_seq": "BIGINT",
        "next_seq": "BIGINT",
        "perceived": "BIGINT",
        "switched": "BOOLEAN",
        "pulled_by": "TEXT",
        "recorded": "BIGINT",
        "doing": "TEXT",
        "open_ends": "BIGINT",
        "said": "TEXT",
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
            after_seq=0,
            next_seq=0,
            perceived=0,
            switched=False,
            pulled_by="",
            recorded=0,
            doing="",
            open_ends=0,
            said="继续",
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

    await run_moment(lane=LANE, persona_id="ayana", now=_at(9, 20))

    where = await current_whereabouts(lane=LANE, persona_id="ayana")
    assert where.place == "学校/二年三班教室", "人挪了位置没跟着"
    assert where.doing == "等第一节课", "手上的事被这一步改掉了"


@pytest.mark.integration
async def test_moving_is_not_switching_to_something_else(moment_db, stub_moment):
    """挪个地方不算「换了事情」。

    ``switch_to`` 的语义是"什么把你从刚才那件事里带走了"，走动没有这回事。混进去
    会让逐缝复盘里的"换事率"把单纯的走动也算成换事情。
    """
    await _stand("ayana", "学校/教学楼走廊", "等第一节课", _at(9))
    stub_moment(("move_to", {"place": "学校/二年三班教室"}))

    moment = await run_moment(lane=LANE, persona_id="ayana", now=_at(9, 20))

    assert moment.switched is False, "走一步被算成了换事情"
    assert moment.doing == "等第一节课"


@pytest.mark.integration
async def test_after_she_moves_the_others_can_reach_her_there(
    moment_db, stub_moment, in_a_moment
):
    """位置是别人能不能感知到她的全部依据 —— 挪完，同处的人就该看得见她。

    这条是上面那个 bug 真正的代价：绫奈叙述自己在教室里，而全家看到的她一直在
    走廊。她不是"状态标签滞后"，是**两小时对所有人不可见**。
    """
    await _stand("ayana", "学校/教学楼走廊", "等第一节课", _at(9))
    await _stand("akao", "学校/二年三班教室", "趴桌上发呆", _at(9))
    stub_moment(("move_to", {"place": "学校/二年三班教室"}))

    await run_moment(lane=LANE, persona_id="ayana", now=_at(9, 20))

    async with in_a_moment("ayana", lane=LANE, now=_at(9, 30)):
        seen = await look_around.invoke({})

    assert "akao" in seen, f"挪过来了却还是看不见同一个地方的人：\n{seen}"


@pytest.mark.integration
async def test_moving_nowhere_is_refused_like_switching_nowhere(
    moment_db, stub_moment
):
    """空位置一样得顶回去 —— 理由跟 switch_to 那条一模一样。"""
    await _stand("ayana", "学校/教学楼走廊", "等第一节课", _at(9))
    runner = stub_moment(("move_to", {"place": "   "}))

    await run_moment(lane=LANE, persona_id="ayana", now=_at(9, 20))

    assert isinstance(runner.results[0], dict), "空位置被接受了"
    where = await current_whereabouts(lane=LANE, persona_id="ayana")
    assert where.place == "学校/教学楼走廊", "空位置把她挪走了"


@pytest.mark.integration
async def test_moving_before_she_ever_stood_anywhere_says_so(
    moment_db, stub_moment
):
    """从没落过位置时挪不动 —— 没有"手上那件事"可以原样带走。"""
    runner = stub_moment(("move_to", {"place": "学校/二年三班教室"}))

    await run_moment(lane=LANE, persona_id="akao", now=_at(9, 20))

    assert isinstance(runner.results[0], dict), "凭空挪了一个没有位置的人"
    assert await current_whereabouts(lane=LANE, persona_id="akao") is None


def test_moving_is_one_of_the_hands_she_actually_has():
    """没注册 = 她永远调不到，症状跟原 bug 一模一样但更难查。"""
    from app.living.moment import move_to

    assert move_to in MOMENT_TOOLS
    assert set(move_to.definition.parameters["properties"]) == {"place"}


# --------------------------------------------------------------------------
# 七 · 这一缝花了多少 token，得能查
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_seam_records_what_it_spent_where_it_can_be_counted(
    moment_db, stub_moment
):
    """一缝的 token 用量要落 durable PG，不能只指望 langfuse。

    ``app.agent.trace`` 里写着实测结论：langfuse **会系统性丢 trace**（这次实测
    整夜 225 缝只到了 125 条，丢 44%），所以「真相在 PG」——用 ``collect_usage``
    包住 run、``record_round_cost`` 落库。这一刀一开始漏了，于是「一晚上花了多少」
    根本查不出来。
    """
    from app.domain.thinking_cost import ThinkingTokensSpent
    from app.runtime.persist import select_all_versions
    from tests.runtime.conftest import migrate

    await migrate(ThinkingTokensSpent, moment_db)
    await _stand("akao", "家/客厅", "看书", _at(13))
    stub_moment(("act", {"what": "把胶片摊了一茶几"}))

    await run_moment(lane=LANE, persona_id="akao", now=_at(14))

    spent = await select_all_versions(
        ThinkingTokensSpent,
        {
            "lane": LANE,
            "actor": "akao",
            "round_id": _at(14).isoformat(timespec="minutes"),
        },
    )
    assert spent, "这一缝没有留下任何用量记录 —— 成本无从统计"
