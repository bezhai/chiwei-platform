"""每周回看 —— 她回头看上一周别人对她说了什么、她身边发生了什么，重写一版「我是谁」。

跨天沉淀让她记得住昨天；这一层是**她会因为过去这一周而变**。链上最新一版就是她每
一轮、写日记时读到的自己（``persona_prompt_vars``），所以这里写下的东西下一
分钟就在她眼前。

八件必须真的成立，各占一节：

  * **取的是上一个完整自然周**，不是本周。周一早上跑的时候取的是上周一零点到本周一零点。
  * **一周只写一版**。判据是链上本周有没有 ``source='review'`` 的版本，不另设标记。
  * **能不调模型就不调**：不在窗口、本周写过了、上一周没有一个人对她说过话、她身边什么都
    没发生，三种都在模型前面返回。她自己写的日记页和她自己说的话不算这一周发生了什么。
  * **正文空不落版本**。拿一版空白把这周记成"写过了"是最坏的一种：这一周她永远丢
    了，而且看起来一切正常。
  * **链空先 seed 一版**，内容是她在有自己的版本之前实际读到的那份
    （``bot_persona.persona_core``）。不然链上的历史是断的：v1 记的是她从没读过的
    东西。
  * **材料是别人对她说的、她身边发生的**：每一条原文、带着什么时候、从哪儿来、是谁，按天排；
    她当前那一版是底稿；外部锚在 prompt 变量里。她的日记页、她自己的话一个字都不在里面。
  * **锚是 ``bot_persona.persona_core`` 那个扁平列，不是链上最新版**。她每周基于自
    己上一版改自己，没有一个不参与循环的外部参照，几个月就漂到没边——而且没有任何
    报错。这一节是防止以后有人来"统一"掉那个锚的门。
  * **泳道隔离、队列和钟**：收件按泳道存，别的泳道的收件不是她这一周；三姐妹各一条回看队列。
"""
from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.data.persist import insert_idempotent
from app.data.tx import current_session, tx
from app.host import Signal
from app.living.day_page.pages import LivingDayPage
from app.living.household.received import ReceivedMessage
from app.living.persona.chain import PersonaChain, PersonaVersion
from app.living.persona_review.review import (
    PERSONA_REVIEW_FROM,
    PERSONA_REVIEW_UNTIL,
    last_full_week,
    week_evidence,
)
from app.living.services import WORLD, PersonaText
from tests.data.read_back import all_versions
from tests.living.conftest import (
    Calls,
    clock_at,
    her_mailbox,
    her_phone,
    her_queue,
    her_round_runs,
    settled,
)
from tests.living.test_phone import (
    _AKAO_BOT_UID,
    _DM,
    _GROUP,
    _SOMEONE,
    _her_own,
    _incoming,
    _seed_world,
)

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))

# 上一个完整自然周：2026-08-31（周一）～ 2026-09-06（周日）。
_LAST_MONDAY = dt.date(2026, 8, 31)
_THIS_MONDAY = dt.date(2026, 9, 7)

# 跑这一轮的时刻：本周一早上 06:30 CST，落在 [06:00, 08:00) 窗口里。
_NOW = dt.datetime(2026, 9, 7, 6, 30, tzinfo=_CST)

_CORE = "主表那份出厂正文：她是刚考完高考的赤尾。"


def _at(hour: int, minute: int = 0) -> dt.datetime:
    """本周一那天的某个钟点（换窗口用）。"""
    return dt.datetime(2026, 9, 7, hour, minute, tzinfo=_CST)


def _on(day: dt.date, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=_CST)


# persona 服务：这个文件里的回看读写的就是它那条链（:mod:`app.living.persona`）。
CHAIN = PersonaChain()


def _reviews():
    """每周回看这个唤醒来源，跟 persona_review 插件给她登记的一样（:mod:`app.living.persona_review`）：
    那一周别人在手机上对她说的从 ``phone`` 服务（phone 插件对象）读，传到她这里的从 ``received``
    服务（household 插件对象）读。"""
    from app.living.persona_review import persona_review_wake

    return persona_review_wake(LANE, CHAIN, her_phone(), her_mailbox())


async def _tick(at: dt.datetime, persona_id: str = "akao") -> PersonaText | None:
    """回看的钟在 ``at`` 响的一拍：送到她一条新的回看队列上（一个刚起来的进程），轮到她时钟
    也停在 ``at``。交回她写下的那一版，这一拍不写就是 ``None``。"""
    return await her_queue(persona_id, "persona-review", clock=clock_at(at)).take(
        _reviews(), Signal(at=at)
    )


@pytest.fixture
async def review_db(living_db):
    """``living_db`` 建齐了收件、手机那几张表和 ``PersonaVersion``，再补 ``bot_persona``。

    ``bot_persona`` 是外部锚和 seed 的来源，这一轮**真的去查它**（不 monkeypatch
    ``find_persona``）：这个文件里最要紧的一条断言是"喂进 prompt 的锚等于那个扁平列
    的原文"，把查询替掉的话这条断言验的就只是替身自己。
    """
    from app.data.models import Base, BotPersona

    async with living_db.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(c, tables=[BotPersona.__table__])
        )
    async with tx():
        s = current_session()
        for persona_id, name in (
            ("akao", "赤尾"),
            ("ayana", "绫奈"),
            ("chinagi", "千凪"),
        ):
            s.add(
                BotPersona(
                    persona_id=persona_id,
                    display_name=name,
                    persona_core=_CORE,
                    persona_lite="出厂的那份 lite 正文。",
                    default_reply_style="自然",
                    error_messages={},
                    appearance_detail="红发",
                )
            )
    return living_db


@pytest.fixture
def stub_review(monkeypatch, models):
    """装一个模型替身：这一轮她写下的那一版是钉死的（:class:`tests.living.conftest.Calls`）；
    ``material`` 是最后一轮真的摆到她眼前的那段文字，``prompt_vars`` 是那一轮 prompt 编译时拿到的
    变量。"""

    def install(said: str = "她这周跟千凪走得近了些。") -> Calls:
        runner = Calls(said=said)
        from app.living.persona_review import review as review_mod

        models[review_mod.PERSONA_REVIEW_PROMPT_ID] = runner.client
        return runner

    return install


async def _heard(
    at: dt.datetime,
    body: str,
    *,
    sender: str = "千凪",
    persona_id: str = "akao",
    lane: str = LANE,
) -> None:
    """传到她这里的一条（姐妹当面说的、world 告诉她察觉到的），跟收件箱存下的一样。"""
    await insert_idempotent(
        ReceivedMessage(
            lane=lane,
            persona_id=persona_id,
            message_id=str(uuid.uuid4()),
            sender=sender,
            body=body,
            message_time=at,
            wakes_recipient=True,
        )
    )


async def _a_week_of_evidence(persona_id: str = "akao", *, lane: str = LANE) -> None:
    """上一个完整自然周那七天，每天千凪当面跟她说一句。"""
    for i in range(7):
        day = _LAST_MONDAY + dt.timedelta(days=i)
        await _heard(
            _on(day, 19),
            f"{day.strftime('%m-%d')} 千凪：一起吃晚饭吗",
            persona_id=persona_id,
            lane=lane,
        )


async def _page(persona_id: str, day: dt.date, text: str, *, lane: str = LANE) -> None:
    """直接落一页日记（这个文件验的不是日记怎么写出来的）。"""
    await insert_idempotent(
        LivingDayPage(
            lane=lane,
            persona_id=persona_id,
            day=day,
            text=text,
            written_at=_on(day + dt.timedelta(days=1), 4, 30),
            happenings=3,
        )
    )


# --------------------------------------------------------------------------
# 一 · 取的是上一个完整自然周
# --------------------------------------------------------------------------


def test_the_window_is_six_to_eight_in_the_morning():
    """窗口没动：CST [06:00, 08:00)。"""
    assert (PERSONA_REVIEW_FROM, PERSONA_REVIEW_UNTIL) == (dt.time(6, 0), dt.time(8, 0))


def test_on_monday_morning_last_full_week_is_the_seven_days_before():
    """周一早上跑的时候取的是上周一到上周日那七天，不是本周。"""
    since, until = last_full_week(_NOW)

    assert since == _LAST_MONDAY
    assert until == _THIS_MONDAY, "右开界必须是本周一 —— 本周的日子一天都不进来"
    assert (until - since).days == 7


def test_mid_week_still_looks_at_the_same_last_full_week():
    """周三跑（补跑的那种）取的还是同一个完整周，不会滑成"最近七天"。"""
    wednesday = dt.datetime(2026, 9, 9, 6, 30, tzinfo=_CST)

    assert last_full_week(wednesday) == (_LAST_MONDAY, _THIS_MONDAY)


def test_late_sunday_night_still_belongs_to_the_old_week():
    """周日 23:59 还在旧一周里：那时的"上一个完整周"是再往前那七天。"""
    sunday = dt.datetime(2026, 9, 6, 23, 59, tzinfo=_CST)

    assert last_full_week(sunday) == (dt.date(2026, 8, 24), _LAST_MONDAY)


@pytest.mark.integration
async def test_only_last_weeks_evidence_is_material(review_db):
    """一周是周一零点到下周一零点（CST），左闭右开：上周日深夜的、本周一零点的都不进来，两头
    手机上的和传到她这里的都一样。"""
    await _seed_world()
    await _heard(_on(dt.date(2026, 8, 30), 23, 59), "上上周日深夜那句")
    await _heard(_on(_LAST_MONDAY, 0), "这一周第一句")
    await _heard(_on(dt.date(2026, 9, 6), 23, 59), "这一周最后一句")
    await _heard(_on(_THIS_MONDAY, 0), "本周一零点那句")
    await _incoming(_DM, text_body="上周日的私聊", at=_on(dt.date(2026, 8, 30), 22))
    await _incoming(_DM, text_body="这一周的私聊", at=_on(dt.date(2026, 9, 2), 9, 12))

    evidence = await week_evidence(
        lane=LANE,
        persona_id="akao",
        since=_LAST_MONDAY,
        until=_THIS_MONDAY,
        phone=her_phone(),
        received=her_mailbox(),
    )

    lines = [line for _, line in evidence.heard]
    assert lines == [
        "【身边】千凪：这一周第一句",
        '【手机】私聊 <msg from="bezhai" rel="owner">这一周的私聊</msg>',
        "【身边】千凪：这一周最后一句",
    ], f"取回来的不是那一周、或者没按时间排：{lines}"


# --------------------------------------------------------------------------
# 二 · 一周只写一版
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_she_rewrites_who_she_is_from_last_week(review_db, stub_review):
    await _a_week_of_evidence()
    stub_review("她还是那个拍胶片的人，这周跟千凪走得近了些。")

    got = await _tick(_NOW)

    assert got is not None, "到点了、上周也有人跟她说过话，却什么都没写下"
    assert got.source == "review", "来源必须仍然是 review —— prod 那条链上认的就是它"
    assert got.narrative == "她还是那个拍胶片的人，这周跟千凪走得近了些。"

    latest = await CHAIN.latest(lane=LANE, persona_id="akao")
    assert latest.narrative == got.narrative, "写下的那一版没有成为她读到的自己"


@pytest.mark.integration
async def test_a_week_already_reviewed_is_not_reviewed_again(review_db, stub_review):
    """本周已经有 review 版 → 不写第二版，而且一次模型都不叫。"""
    await _a_week_of_evidence()
    runner = stub_review()

    first = await _tick(_NOW)
    again = await _tick(_at(7, 0))

    assert first is not None
    assert again is None, "同一周写了第二版"
    assert len(runner.runs) == 1, f"第二拍又叫了一次模型：{len(runner.runs)} 次"


@pytest.mark.integration
async def test_a_missed_monday_is_picked_up_later_in_the_week(review_db, stub_review):
    """周一早上服务没跑（部署 / 崩溃）→ 周二那一拍照样补上，取的还是同一个完整周。

    窗口是**每天都开的钟点窗口**，什么时候停由"本周已经有 review 版"说了算，不由
    日子说了算。当成"周一那两个钟头"来实现的话，一次周一早上的部署就让她整整一周
    白过，而且一句报错都没有。
    """
    await _a_week_of_evidence()
    stub_review("周二才补上的这一版。")

    tuesday = dt.datetime(2026, 9, 8, 6, 30, tzinfo=_CST)
    got = await _tick(tuesday)

    assert got is not None and got.source == "review"


@pytest.mark.integration
async def test_once_written_the_rest_of_the_week_does_nothing(review_db, stub_review):
    """周一写过之后，这一周剩下每一天的那一拍都不再叫模型。"""
    await _a_week_of_evidence()
    runner = stub_review()
    await _tick(_NOW)

    for day in (8, 9, 10, 11, 12, 13):
        at = dt.datetime(2026, 9, day, 6, 30, tzinfo=_CST)
        assert await _tick(at) is None

    assert len(runner.runs) == 1, f"这一周叫了 {len(runner.runs)} 次模型"


@pytest.mark.integration
async def test_an_owner_version_this_week_does_not_block_the_review(
    review_db, stub_review
):
    """bezhai 本周盖过版不挡自动班 —— 幂等只认 ``source='review'``。"""
    await _a_week_of_evidence()
    await CHAIN.write_version(
        lane=LANE,
        persona_id="akao",
        narrative="bezhai 本周手写的一版。",
        source="owner",
        written_at="2026-09-07T05:00:00+08:00",
    )
    stub_review("她自己写的这一版。")

    got = await _tick(_NOW)

    assert got is not None and got.source == "review"


# --------------------------------------------------------------------------
# 三 · 能不调模型就不调
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_outside_the_window_she_does_not_look_back(review_db, stub_review):
    """凌晨三点、中午都不是回看的时候。"""
    await _a_week_of_evidence()
    runner = stub_review()

    assert await _tick(_at(3)) is None
    assert await _tick(_at(12)) is None
    assert runner.runs == [], "没到点却叫了模型"
    assert await CHAIN.latest(lane=LANE, persona_id="akao") is None


@pytest.mark.integration
async def test_a_week_nobody_said_anything_to_her_gets_no_version(review_db, stub_review):
    """上一周没有一个人对她说过话、她身边什么都没传来：不该有那一版，更不该为一片空白叫一次模型
    （她会凭空编出一周的变化）。"""
    runner = stub_review()

    assert await _tick(_NOW) is None
    assert runner.runs == [], "一周什么都没发生，却还是叫了模型"
    assert await CHAIN.latest(lane=LANE, persona_id="akao") is None, (
        "没材料的一周连 seed 都不该留下 —— 那一版记的是没人读过的东西"
    )


@pytest.mark.integration
async def test_her_own_pages_and_words_are_not_what_happened_to_her(review_db, stub_review):
    """她每天都写了日记、在私聊和群里说了不少话，可没有一个人对她说过什么：这一周照样算什么都
    没发生。日记页是她自己的复述，她说的话是她自己的腔调，拿它们当这一周的证据，就是让她的口头禅
    和自称给自己作证（2026-09 prod 的 v14、v15、v18 就是这么来的）。"""
    await _seed_world()
    for i in range(7):
        day = _LAST_MONDAY + dt.timedelta(days=i)
        await _page("akao", day, f"{day.strftime('%m-%d')}：本学霸今天又赢了。")
        await _her_own(_DM, text_body="本学霸来了", at=_on(day, 20))
        await _her_own(_GROUP, text_body="本学霸觉得不行", at=_on(day, 21))
    runner = stub_review()

    assert await _tick(_NOW) is None
    assert runner.runs == [], "她自己写的、自己说的被当成了这一周发生的事"
    assert await CHAIN.latest(lane=LANE, persona_id="akao") is None


@pytest.mark.integration
async def test_a_round_that_writes_nothing_leaves_no_version(review_db, stub_review):
    """她这一轮一个字都没写：不落版本，下一拍还在窗口里就再来一次。"""
    await _a_week_of_evidence()
    stub_review("   ")

    assert await _tick(_NOW) is None

    versions = await all_versions(
        PersonaVersion, {"lane": LANE, "persona_id": "akao"}
    )
    assert [v.source for v in versions] == ["seed"], (
        f"空正文落成了版本，这一周被记成写过了：{[v.source for v in versions]}"
    )

    stub_review("补上了：她这周跟千凪走得近了些。")
    later = await _tick(_at(7, 0))
    assert later is not None, "上一拍空了就再也不写了 —— 这一周她永远丢了"
    assert later.narrative == "补上了：她这周跟千凪走得近了些。"


# --------------------------------------------------------------------------
# 四 · 链空先 seed 一版
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_an_empty_chain_gets_her_starting_point_first(review_db, stub_review):
    """v1 记的必须是她在有自己的版本之前实际读到的那份，不然链上的历史是断的。"""
    await _a_week_of_evidence()
    stub_review("她这周跟千凪走得近了些。")

    await _tick(_NOW)

    versions = await all_versions(
        PersonaVersion, {"lane": LANE, "persona_id": "akao"}
    )
    assert [v.source for v in versions] == ["seed", "review"]
    assert versions[0].narrative == _CORE, (
        "seed 那一版不是她链空时实际读到的那份（``persona_core``），链上的历史断了"
    )


@pytest.mark.integration
async def test_a_chain_that_already_has_versions_is_not_seeded(review_db, stub_review):
    """链非空（prod 就是这样）→ 不补 seed，直接在上一版基础上改。"""
    await _a_week_of_evidence()
    await CHAIN.write_version(
        lane=LANE,
        persona_id="akao",
        narrative="上一版：她在准备去日本读书。",
        source="review",
        written_at="2026-08-31T06:30:00+08:00",
    )
    stub_review("这一版：她把语言学校的事定下来了。")

    await _tick(_NOW)

    versions = await all_versions(
        PersonaVersion, {"lane": LANE, "persona_id": "akao"}
    )
    assert [v.source for v in versions] == ["review", "review"]


# --------------------------------------------------------------------------
# 五 · 材料：别人对她说的、她身边发生的
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_what_others_said_reaches_her_day_by_day_with_who_when_and_where(
    review_db, stub_review
):
    """每一条原文照搬，带着钟点、从哪儿来（【手机】/【身边】）和是谁，按天分段、按时间先后：读起来是
    "这一周我身上发生了什么"。world 告诉她察觉到的不标是谁说的（那不是谁对她说的话）。"""
    await _seed_world()
    await _incoming(_DM, text_body="考完试了吗", at=_on(_LAST_MONDAY, 9, 12))
    await _heard(_on(_LAST_MONDAY, 15, 20), "姐姐你回来啦")
    await _incoming(
        _GROUP, text_body="@赤尾 你来评评理", at=_on(dt.date(2026, 9, 2), 11, 5),
        sender=_SOMEONE, sender_name="路人", names_bot=_AKAO_BOT_UID,
    )
    await _heard(
        _on(dt.date(2026, 9, 2), 14), "窗外开始下雨了，晾在阳台的衣服还没收。", sender=WORLD
    )
    runner = stub_review()

    await _tick(_NOW)

    assert (
        "这一周别人对你说的、你身边发生的，按时间先后。"
        "【手机】是手机上私聊你、在群里点你名或回你话的，"
        "【身边】是你身边发生的、姐妹当面跟你说的：\n\n"
        "08-31\n"
        '- 09:12【手机】私聊 <msg from="bezhai" rel="owner">考完试了吗</msg>\n'
        "- 15:20【身边】千凪：姐姐你回来啦\n\n"
        "09-02\n"
        '- 11:05【手机】群「宅居研究所」里点了你的名 <msg from="路人">@赤尾 你来评评理</msg>\n'
        "- 14:00【身边】窗外开始下雨了，晾在阳台的衣服还没收。"
    ) in runner.material, f"拿到：\n{runner.material}"
    assert runner.material.startswith("刚过去的这一周是 2026-08-31 到 2026-09-06。")


@pytest.mark.integration
async def test_her_own_pages_and_words_are_not_in_the_material(review_db, stub_review):
    """有人跟她说过话的一周里，她自己的日记页和她自己说的话照样一个字都不摆。"""
    await _seed_world()
    await _a_week_of_evidence()
    await _page("akao", _LAST_MONDAY, "周一：本学霸今天又赢了。")
    await _her_own(_DM, text_body="本学霸来了", at=_on(_LAST_MONDAY, 20))
    runner = stub_review()

    await _tick(_NOW)

    assert "一起吃晚饭吗" in runner.material
    assert "本学霸" not in runner.material, f"她自己的话进了材料：\n{runner.material}"


@pytest.mark.integration
async def test_a_long_message_reaches_her_whole(review_db, stub_review):
    """不截断单条：一条很长的话整条摆给她。"""
    long_words = "这是一段很长的话。" * 400
    await _heard(_on(_LAST_MONDAY, 10), long_words)
    runner = stub_review()

    await _tick(_NOW)

    assert f"千凪：{long_words}" in runner.material


@pytest.mark.integration
async def test_past_the_limit_the_earliest_are_left_out_and_she_is_told(
    review_db, stub_review, monkeypatch
):
    """一周的条数有上限，手机和身边各算各的：超出的是更早的那些，整条不摆，并且如实告诉她还有几条
    没摆 —— 不说的话她会以为那一周就这么安静。"""
    from app.living.persona_review import review as review_mod

    monkeypatch.setattr(review_mod, "WEEK_NEARBY_LIMIT", 3)
    for i in range(5):
        await _heard(_on(_LAST_MONDAY + dt.timedelta(days=i), 10), f"第{i + 1}句")
    runner = stub_review()

    await _tick(_NOW)

    assert "第1句" not in runner.material and "第2句" not in runner.material
    assert all(f"第{i}句" in runner.material for i in (3, 4, 5))
    assert "（更早的还有 2 条没摆在这里：身边 2 条。）" in runner.material, (
        f"没告诉她还有几条没摆：\n{runner.material}"
    )


@pytest.mark.integration
async def test_her_current_version_is_the_draft_she_rewrites(review_db, stub_review):
    """她当前那一版是底稿。不给的话她是凭空写一份，不是"改"。"""
    await _a_week_of_evidence()
    await CHAIN.write_version(
        lane=LANE,
        persona_id="akao",
        narrative="当前这一版：她在准备去日本读书。",
        source="review",
        written_at="2026-08-31T06:30:00+08:00",
    )
    runner = stub_review()

    await _tick(_NOW)

    assert "当前这一版：她在准备去日本读书。" in runner.material, (
        f"要被重写的底稿不在材料里：{runner.material}"
    )


@pytest.mark.integration
async def test_the_anchor_is_the_flat_column_not_the_chain(review_db, stub_review):
    """``{{persona_core}}`` 这一轮装的是 ``bot_persona.persona_core`` **原始那一列**。

    她每周基于自己上一版改自己。没有一个不参与循环的外部参照，几个月就漂到没边，
    而且没有任何报错。所以这一轮**不复用** ``persona_prompt_vars``（那里的同名变量
    装的是链上最新一版）—— 两者同名不同值是刻意的，这条断言就是那道门。
    """
    await _a_week_of_evidence()
    await CHAIN.write_version(
        lane=LANE,
        persona_id="akao",
        narrative="链上最新那一版：她今年不拍胶片了。",
        source="review",
        written_at="2026-08-31T06:30:00+08:00",
    )
    runner = stub_review()

    await _tick(_NOW)

    assert runner.prompt_vars["persona_core"] == _CORE, (
        "锚被换成了链上最新版 —— 自我回流没有外部参照，她会一路漂走"
    )
    assert "她今年不拍胶片了" not in runner.prompt_vars["persona_core"]
    assert runner.prompt_vars["persona_name"] == "赤尾"


@pytest.mark.integration
async def test_the_variables_are_exactly_the_two_the_prompt_names(
    review_db, stub_review
):
    """键名就是 Langfuse 上 ``living_persona_review`` 正文里写着的那两个。

    改键名不会报错，只会让 ``{{persona_core}}`` 原样渲染成字面量出现在她眼前。
    """
    await _a_week_of_evidence()
    runner = stub_review()

    await _tick(_NOW)

    assert set(runner.prompt_vars) == {"persona_name", "persona_core"}


# --------------------------------------------------------------------------
# 六 · 泳道隔离
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_another_lanes_inbox_is_not_her_week(review_db, stub_review):
    """prod 那边传到她这里的不进 coe 这一轮 —— 漏了 lane 的后果是双向的。"""
    await _a_week_of_evidence("akao", lane="prod")
    runner = stub_review()

    assert await _tick(_NOW) is None
    assert runner.runs == [], "读到了别的泳道的收件"
    assert await CHAIN.latest(lane="prod", persona_id="akao") is None


@pytest.mark.integration
async def test_the_new_version_lands_only_on_this_lane(review_db, stub_review):
    await _a_week_of_evidence()
    stub_review("coe 里改出来的一版。")

    await _tick(_NOW)

    assert await CHAIN.latest(lane="prod", persona_id="akao") is None, (
        "coe 里跑实验改出来的人设当场生效在 prod 的她身上了"
    )


@pytest.mark.integration
async def test_sisters_do_not_share_a_week(review_db, stub_review):
    await _a_week_of_evidence("akao")
    stub_review("赤尾这一版。")

    await _tick(_NOW)

    assert await CHAIN.latest(lane=LANE, persona_id="ayana") is None


# --------------------------------------------------------------------------
# 七 · 她的回看队列和那条钟
# --------------------------------------------------------------------------


async def test_each_sister_has_a_review_queue_the_clock_signals_every_five_minutes(app_host):
    """persona_review 插件给三姐妹每人登记一条回看队列（占用上限 900 秒），回看的钟每 5 分钟给它发一个信号；
    每个信号都排队（``QUEUE_EACH``）。它和她的轮次、她的日记都不在一条队列上。忘了登记的症状是
    静默的：模块写好了、测试全绿，而线上一版都不会有。"""
    from app.host import Coalesce
    from app.living.persona_review.review import write_due_review
    from tests.living.conftest import her_residents

    host = await app_host("agent-service")
    queues = {r.name: r.detail["turns"] for r in host.registered() if r.kind == "turns"}
    wakes = [
        r
        for r in host.registered()
        if r.kind == "wake" and r.detail["source"].name == "persona-review"
    ]

    assert sorted(
        (w.plugin, w.detail["turns"].participant, w.detail["turns"].line, w.detail["every"])
        for w in wakes
    ) == sorted(
        ("persona_review", p, "persona-review", dt.timedelta(minutes=5)) for p in her_residents()
    )
    for w in wakes:
        assert w.detail["turns"] is queues[f"{w.detail['turns'].participant}/persona-review"]
        assert w.detail["turns"].hold_limit == dt.timedelta(seconds=900)
        assert w.detail["source"].coalesce is Coalesce.QUEUE_EACH
        assert w.detail["source"].run.func is write_due_review
        assert isinstance(w.detail["source"].run.keywords["persona"], PersonaChain)


@pytest.mark.integration
async def test_one_tick_reviews_each_of_them(review_db, stub_review, app_host, monkeypatch):
    """钟的一拍同时送到三个人的回看队列上（宿主的钟就是这样发的），三个人各回看各的那一周。"""
    import asyncio

    for who in ("akao", "ayana", "chinagi"):
        await _a_week_of_evidence(who)
    stub_review("这一周。")
    monkeypatch.setenv("LANE", LANE)
    monkeypatch.setattr("app.host.host.now_cst", lambda: _NOW)

    host = await app_host("agent-service")
    await asyncio.gather(
        *(
            r.detail["turns"].take(r.detail["source"], Signal(at=_NOW))
            for r in host.registered()
            if r.kind == "wake" and r.detail["source"].name == "persona-review"
        )
    )

    for who in ("akao", "ayana", "chinagi"):
        latest = await CHAIN.latest(lane=LANE, persona_id=who)
        assert latest is not None and latest.source == "review", (
            f"{who} 没有这一周的版本 —— 那条钟没把三个人都推到"
        )


@pytest.mark.integration
async def test_a_review_that_waited_behind_a_stuck_one_is_written_when_it_gets_her(
    review_db, stub_review
):
    """前一次回看挂住了（模型调用卡住，最长占着 15 分钟），06:30 那一拍排在后面，06:46 才轮到。
    这一版是 06:46 写下的，记的就是这一刻，不是那一拍敲响的 06:30。"""
    import asyncio

    await _a_week_of_evidence("akao")
    stub_review("这一周。")
    clock = [_at(6, 30)]
    reviews = her_queue("akao", "persona-review", clock=lambda: clock[0])

    async with her_round_runs(reviews):  # 挂住的那一次
        tick = asyncio.create_task(reviews.take(_reviews(), Signal(at=_at(6, 30))))
        await settled()
        clock[0] = _at(6, 46)
    await tick

    latest = await CHAIN.latest(lane=LANE, persona_id="akao")
    assert latest is not None and latest.source == "review"
    assert dt.datetime.fromisoformat(latest.written_at) == _at(6, 46)


@pytest.mark.integration
async def test_a_review_whose_tick_was_in_the_window_runs_even_if_it_gets_her_after(
    review_db, stub_review
):
    """窗口是 [06:00, 08:00)。07:59 那一拍还在窗口里，排在挂住的那一次后面，08:10 才轮到：
    已经过了窗口，这一版照样写，记的是轮到时的 08:10。窗口按那一拍响的时刻判，只管这一拍要不要
    去试；按轮到时的钟判的话，这一拍就不写了。"""
    import asyncio

    assert (PERSONA_REVIEW_FROM, PERSONA_REVIEW_UNTIL) == (dt.time(6, 0), dt.time(8, 0))
    await _a_week_of_evidence("akao")
    stub_review("这一周。")
    clock = [_at(7, 59)]
    reviews = her_queue("akao", "persona-review", clock=lambda: clock[0])

    async with her_round_runs(reviews):  # 挂住的那一次
        tick = asyncio.create_task(reviews.take(_reviews(), Signal(at=_at(7, 59))))
        await settled()
        clock[0] = _at(8, 10)
    await tick

    latest = await CHAIN.latest(lane=LANE, persona_id="akao")
    assert latest is not None and latest.source == "review", (
        "排在窗口末尾的那一拍轮到时过了窗口，这一周的回看没写"
    )
    assert dt.datetime.fromisoformat(latest.written_at) == _at(8, 10)


@pytest.mark.integration
async def test_a_tick_before_the_window_does_not_review_even_if_it_gets_her_inside(
    review_db, stub_review
):
    """反过来也按那一拍响的时刻判：05:55 那一拍不在窗口里，排在挂住的那一次后面、06:01 才轮到，
    也不写。"""
    import asyncio

    await _a_week_of_evidence("akao")
    runner = stub_review("这一周。")
    clock = [_at(5, 55)]
    reviews = her_queue("akao", "persona-review", clock=lambda: clock[0])

    async with her_round_runs(reviews):  # 挂住的那一次
        tick = asyncio.create_task(reviews.take(_reviews(), Signal(at=_at(5, 55))))
        await settled()
        clock[0] = _at(6, 1)

    assert await tick is None
    assert runner.runs == [], "窗口之前响的那一拍叫了模型"
    assert await CHAIN.latest(lane=LANE, persona_id="akao") is None


@pytest.mark.integration
async def test_every_tick_waits_its_turn_behind_a_stuck_review(review_db, stub_review):
    """每一拍都排队：两拍都排在挂住的那一次后面，各自轮到时再判本周写过没有。第一拍写下这一版，
    第二拍看见本周已经写了。"""
    import asyncio

    await _a_week_of_evidence("akao")
    runner = stub_review("这一周。")
    reviews = her_queue("akao", "persona-review", clock=clock_at(_at(6, 40)))

    async with her_round_runs(reviews):  # 挂住的那一次
        ticks = [
            asyncio.create_task(reviews.take(_reviews(), Signal(at=_at(6, m))))
            for m in (30, 35)
        ]
        await settled()
        assert [t.done() for t in ticks] == [False, False], "有一拍没排上就被打发走了"

    first, second = await asyncio.gather(*ticks)
    assert first is not None and first.source == "review"
    assert second is None
    assert len(runner.runs) == 1


@pytest.mark.integration
async def test_a_week_not_reviewed_by_sunday_is_given_up(review_db, stub_review):
    """这一周的回看一直没写成（整周的窗口都没跑上）：过了周日这一周就放弃。下周一回看的是刚过去
    的那一周，不回头补更早的那一周。"""
    await _a_week_of_evidence("akao")  # 08-31 ～ 09-06，本该在 09-07 那一周里回看
    for i in range(7):
        day = _THIS_MONDAY + dt.timedelta(days=i)
        await _heard(_on(day, 19), f"{day.strftime('%m-%d')} 绫奈：下一周的事", sender="绫奈")
    runner = stub_review("下一周的这一版。")

    got = await _tick(dt.datetime(2026, 9, 14, 6, 30, tzinfo=_CST))

    assert got is not None and got.source == "review"
    assert "下一周的事" in runner.material
    assert "一起吃晚饭吗" not in runner.material, "过了周日还在回看那一周"
    assert len(runner.runs) == 1
