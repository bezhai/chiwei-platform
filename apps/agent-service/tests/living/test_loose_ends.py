"""她惦记着没了结的事 —— 状态快照里唯一由她自己维护的那一层。

这一层是整个 T2 的命门：滚动窗口会把东西滚出去，清单是她**主动从窗口里救出来**
的东西。所以三件事必须真的成立：

  * 她列出来的东西会一直跟着她，跨多少轮都不掉；
  * 每一条能指出**是从哪一轮带过来的**（``opened_moment_id`` 第一次写下就不再变）；
  * 她下一次不再列出来的，就是关掉了 —— 关掉是**她的省略**在生效，不是代码替她
    判断"这条过期了"。
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.agent.round import Round
from app.living.loose_ends.mind import OnHerMind
from app.living.loose_ends.threads import (
    DUE_EXAMPLE,
    LooseEnd,
    derive_thread_id,
    format_entry,
    list_open_loose_ends,
    parse_entry,
    rewrite_loose_ends,
)

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))


def _at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 7, 25, hour, minute, tzinfo=_CST)


def _moment(hour: int, minute: int = 0) -> str:
    return _at(hour, minute).isoformat(timespec="minutes")


@pytest.fixture
async def ends_db(living_db):
    from tests.runtime.conftest import migrate

    await migrate(LooseEnd, living_db)
    return living_db


async def _rewrite(*items: str, at: dt.datetime, persona: str = "akao"):
    return await rewrite_loose_ends(
        lane=LANE,
        persona_id=persona,
        moment_id=at.isoformat(timespec="minutes"),
        now=at,
        still_on_my_mind=items,
    )


# --------------------------------------------------------------------------
# 一 · 写下来的东西跟着她走
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_something_she_listed_stays_on_her_mind(ends_db):
    await _rewrite("周末陪绫奈去祭典", at=_at(14))

    open_now = await list_open_loose_ends(lane=LANE, persona_id="akao")
    assert [e.what for e in open_now] == ["周末陪绫奈去祭典"]
    assert open_now[0].closed_at is None


@pytest.mark.integration
async def test_a_thing_carried_across_many_moments_names_the_moment_it_came_from(
    ends_db,
):
    """验收正条：跨多轮没被遗忘，而且指得出是从哪一轮带过来的。"""
    await _rewrite("周末陪绫奈去祭典", at=_at(14, 0))
    for minute in (10, 20, 30, 40, 50):
        await _rewrite("周末陪绫奈去祭典", at=_at(14, minute))

    open_now = await list_open_loose_ends(lane=LANE, persona_id="akao")
    assert len(open_now) == 1
    assert open_now[0].opened_moment_id == _moment(14, 0), (
        "重写清单把「从哪一轮带过来的」冲掉了 —— 这条一丢，跨轮延续就没有证据了"
    )
    assert open_now[0].opened_at == _at(14, 0)


@pytest.mark.integration
async def test_listing_it_again_does_not_pile_up_duplicates(ends_db):
    await _rewrite("洗的衣服还在阳台", at=_at(12))
    await _rewrite("洗的衣服还在阳台", at=_at(13))

    assert len(await list_open_loose_ends(lane=LANE, persona_id="akao")) == 1


# --------------------------------------------------------------------------
# 二 · 她不再列出来的，就是关掉了
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_leaving_it_out_next_time_closes_it(ends_db):
    await _rewrite("洗的衣服还在阳台", "周末陪绫奈去祭典", at=_at(12))

    await _rewrite("周末陪绫奈去祭典", at=_at(13))

    assert [e.what for e in await list_open_loose_ends(lane=LANE, persona_id="akao")] == [
        "周末陪绫奈去祭典"
    ]


@pytest.mark.integration
async def test_an_empty_list_closes_everything(ends_db):
    """一件都不列 = 她说心里空了。代码不许自作主张替她留着。"""
    await _rewrite("洗的衣服还在阳台", "周末陪绫奈去祭典", at=_at(12))

    await _rewrite(at=_at(13))

    assert await list_open_loose_ends(lane=LANE, persona_id="akao") == []


@pytest.mark.integration
async def test_a_closed_thing_records_which_moment_closed_it(ends_db):
    await _rewrite("洗的衣服还在阳台", at=_at(12))
    await _rewrite(at=_at(13))

    from app.data.persist import select_latest

    latest = await select_latest(
        LooseEnd,
        {
            "lane": LANE,
            "persona_id": "akao",
            "thread_id": derive_thread_id("洗的衣服还在阳台"),
        },
    )
    assert latest.closed_at == _at(13)
    assert latest.closed_moment_id == _moment(13)


@pytest.mark.integration
async def test_picking_a_closed_thing_back_up_keeps_its_original_moment(ends_db):
    """她又惦记起来 = 这件事从最早那一轮起就一直在她心上，不是今天新长出来的。"""
    await _rewrite("洗的衣服还在阳台", at=_at(12))
    await _rewrite(at=_at(13))

    await _rewrite("洗的衣服还在阳台", at=_at(14))

    open_now = await list_open_loose_ends(lane=LANE, persona_id="akao")
    assert [e.what for e in open_now] == ["洗的衣服还在阳台"]
    assert open_now[0].opened_moment_id == _moment(12)
    assert open_now[0].closed_at is None


# --------------------------------------------------------------------------
# 三 · 边界：空白、重复、隔离
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_blank_entries_are_not_things_on_her_mind(ends_db):
    await _rewrite("  ", "", "周末陪绫奈去祭典", at=_at(14))

    assert [e.what for e in await list_open_loose_ends(lane=LANE, persona_id="akao")] == [
        "周末陪绫奈去祭典"
    ]


@pytest.mark.integration
async def test_the_same_thing_said_twice_in_one_list_is_one_thing(ends_db):
    await _rewrite("周末陪绫奈去祭典", " 周末陪绫奈去祭典 ", at=_at(14))

    assert len(await list_open_loose_ends(lane=LANE, persona_id="akao")) == 1


@pytest.mark.integration
async def test_one_sisters_mind_is_not_another_sisters(ends_db):
    await _rewrite("周末陪绫奈去祭典", at=_at(14), persona="akao")
    await _rewrite(at=_at(14, 1), persona="ayana")

    assert len(await list_open_loose_ends(lane=LANE, persona_id="akao")) == 1
    assert await list_open_loose_ends(lane=LANE, persona_id="ayana") == []


@pytest.mark.integration
async def test_another_lane_is_another_world(ends_db):
    await rewrite_loose_ends(
        lane="prod",
        persona_id="akao",
        moment_id=_moment(14),
        now=_at(14),
        still_on_my_mind=["线上那件事"],
    )

    assert await list_open_loose_ends(lane=LANE, persona_id="akao") == []


@pytest.mark.integration
async def test_rewrite_hands_back_what_is_open_now(ends_db):
    """调用方（这一轮）要能立刻报出"轮末还挂着几件"，不用再查一次库。"""
    got = await _rewrite("洗的衣服还在阳台", "周末陪绫奈去祭典", at=_at(12))

    assert sorted(e.what for e in got) == sorted(
        ["洗的衣服还在阳台", "周末陪绫奈去祭典"]
    )


# --------------------------------------------------------------------------
# 四 · 该在几点 —— 她自己给一条线头挂上的时刻
# --------------------------------------------------------------------------


def test_the_line_she_reads_back_is_a_line_she_can_write_again():
    """渲染给她看的那个形状，和她下一轮抄回来时被解析的形状，必须是同一个。

    整份重写意味着她**每一轮**都要把带时刻的那条原样抄一遍。抄回来解析不出同一件事
    的话，那条会在她眼皮底下被关掉、再以另一个身份重开，而她什么都没做错。
    """
    assert parse_entry(format_entry("家属谈话会", _at(15))) == ("家属谈话会", _at(15))
    assert parse_entry(format_entry("洗的衣服还在阳台", None)) == (
        "洗的衣服还在阳台",
        None,
    )


def test_the_shape_she_is_taught_is_the_shape_that_parses():
    """教她写的那个例子必须真的解析得出来。

    docstring 不能是 f-string，所以"教的形状"只能在工具文案里写死一份字面量。跟解析
    分成两处写就会有一天对不上——而对不上的表现是她照着说明写、每次都被顶回来。
    """
    assert parse_entry(f"{DUE_EXAMPLE} 家属谈话会")[0] == "家属谈话会"
    assert parse_entry(f"{DUE_EXAMPLE} 家属谈话会")[1] is not None


@pytest.mark.integration
async def test_a_thing_can_carry_the_hour_it_should_happen_at(ends_db):
    await _rewrite("[2026-07-25 15:00] 家属谈话会", at=_at(12))

    open_now = await list_open_loose_ends(lane=LANE, persona_id="akao")
    assert [(e.what, e.due_at) for e in open_now] == [("家属谈话会", _at(15))]


@pytest.mark.integration
async def test_something_without_an_hour_simply_has_none(ends_db):
    await _rewrite("洗的衣服还在阳台", at=_at(12))

    open_now = await list_open_loose_ends(lane=LANE, persona_id="akao")
    assert open_now[0].due_at is None


@pytest.mark.integration
async def test_the_hour_is_not_part_of_what_the_thing_is(ends_db):
    """身份仍然只从那句话派生 —— 时刻掺进去，改一次期就是"旧的关掉、新开一条"。"""
    await _rewrite("[2026-07-25 15:00] 家属谈话会", at=_at(12))

    open_now = await list_open_loose_ends(lane=LANE, persona_id="akao")
    assert open_now[0].thread_id == derive_thread_id("家属谈话会")
    assert open_now[0].what == "家属谈话会", "时刻被当成那句话的一部分存下来了"


@pytest.mark.integration
async def test_listing_a_different_hour_reschedules_the_same_thing(ends_db):
    """改期由"整份重写"顺带解决：这次列的时刻不一样，就是改期。"""
    await _rewrite("[2026-07-25 15:00] 家属谈话会", at=_at(12))
    await _rewrite("[2026-07-25 17:30] 家属谈话会", at=_at(13))

    open_now = await list_open_loose_ends(lane=LANE, persona_id="akao")
    assert len(open_now) == 1, "改期开出了第二条线头"
    assert open_now[0].due_at == _at(17, 30)
    assert open_now[0].opened_moment_id == _moment(12), "改期把出处冲掉了"


@pytest.mark.integration
async def test_leaving_the_hour_out_drops_the_hour_and_keeps_the_thing(ends_db):
    """撤销时刻也是整份重写顺带解决的：这次不写方括号 = 不再有该在几点。"""
    await _rewrite("[2026-07-25 15:00] 家属谈话会", at=_at(12))
    await _rewrite("家属谈话会", at=_at(13))

    open_now = await list_open_loose_ends(lane=LANE, persona_id="akao")
    assert [(e.what, e.due_at) for e in open_now] == [("家属谈话会", None)]
    assert open_now[0].opened_moment_id == _moment(12)


@pytest.mark.integration
async def test_relisting_the_same_hour_does_not_pile_up_versions(ends_db):
    """她每十分钟重列一遍整份清单 —— 没变的那条不该每轮 append 一版。"""
    from tests.data.read_back import all_versions

    for minute in (0, 10, 20):
        await _rewrite("[2026-07-25 15:00] 家属谈话会", at=_at(12, minute))

    rows = await all_versions(
        LooseEnd,
        {
            "lane": LANE,
            "persona_id": "akao",
            "thread_id": derive_thread_id("家属谈话会"),
        },
    )
    assert len(rows) == 1


@pytest.mark.integration
async def test_a_hung_up_thing_keeps_its_hour_when_she_picks_it_back_up(ends_db):
    """复活时时刻按**这次列的**来，不是把关掉之前那个原样翻出来。"""
    await _rewrite("[2026-07-25 15:00] 家属谈话会", at=_at(12))
    await _rewrite(at=_at(13))

    await _rewrite("[2026-07-26 09:00] 家属谈话会", at=_at(14))

    open_now = await list_open_loose_ends(lane=LANE, persona_id="akao")
    assert open_now[0].due_at == dt.datetime(2026, 7, 26, 9, 0, tzinfo=_CST)
    assert open_now[0].opened_moment_id == _moment(12)


def test_a_time_with_nothing_after_it_is_not_an_entry():
    """光有时刻没有正文 —— 她本来要挂一件事，只写了一半。

    这条**不能**当成空行跳过：整份重写之下，跳过它跟她"这次没列任何东西"是同一个
    结果，而她写的明明是一件事的一半。
    """
    for half_written in ("[2026-07-25 15:00]", "[2026-07-25 15:00]   "):
        with pytest.raises(ValueError, match="什么事"):
            parse_entry(half_written)


def test_an_empty_bracket_is_not_a_time_either():
    """``[]`` / ``[   ]`` 连时刻都不是 —— 跟写坏时刻走同一条路。"""
    for empty in ("[]", "[   ]"):
        with pytest.raises(ValueError):
            parse_entry(empty)


def test_a_blank_line_is_still_just_a_blank_line():
    """两种"空"不是一回事，这条钉住它们不许合流。

    她敲了个空行 = 什么都没写，忽略；写了时刻没写事 = 写了一半，顶回去。
    """
    assert parse_entry("   ") == ("", None)
    assert parse_entry("") == ("", None)


@pytest.mark.integration
async def test_a_time_with_nothing_after_it_does_not_quietly_wipe_her_list(ends_db):
    """**这一条是命门**：静默跳过的话，她心里挂着的东西会被一次性清空且没有报错。

    她这一轮只列了「[2026-07-25 15:00]」（时刻写了、那件事忘了写）。跳过它 = 这份
    清单是空的 = 她原有的两条全部走进关闭流程，而工具还回她一句"心里挂着：没有"。
    """
    await _rewrite("洗的衣服还在阳台", "周末陪绫奈去祭典", at=_at(12))

    with pytest.raises(ValueError, match="什么事"):
        await _rewrite("[2026-07-25 15:00]", at=_at(13))

    assert sorted(
        e.what for e in await list_open_loose_ends(lane=LANE, persona_id="akao")
    ) == ["周末陪绫奈去祭典", "洗的衣服还在阳台"], (
        "她只是漏写了那件事本身，心里挂着的却被清空了"
    )


@pytest.mark.integration
async def test_something_that_is_not_a_time_is_refused_and_nothing_is_written(ends_db):
    """写不成时刻就顶回去，而且**这一份整个不落** —— 她上一份清单原样还在。

    半份落地才是真正危险的：她列的三条里第二条写坏了，前一条改期生效、后两条被当成
    "这次没列"关掉，而她只看到一句报错。
    """
    await _rewrite("洗的衣服还在阳台", at=_at(12))

    with pytest.raises(ValueError):
        await _rewrite("[明天下午三点] 家属谈话会", "洗的衣服还在阳台", at=_at(13))
    with pytest.raises(ValueError):
        await _rewrite("[2026-07-25 15:00 家属谈话会", at=_at(13))

    assert [
        e.what for e in await list_open_loose_ends(lane=LANE, persona_id="akao")
    ] == ["洗的衣服还在阳台"]


# --------------------------------------------------------------------------
# 五 · 摆到她眼前的两段（loose_ends 插件对象）
# --------------------------------------------------------------------------

# loose_ends 插件交出去、登记段和工具的那个对象。它不带状态，这里每条测试共用一个。
MIND = OnHerMind()


def _round(now: dt.datetime, *, previous_at: dt.datetime | None = None) -> Round:
    """她在 ``now`` 开始的一轮（只有段要用的那几样：泳道、她、此刻、上一轮的时刻）。"""
    return Round(
        app="agent-service",
        lane=LANE,
        participant="akao",
        profile="resident",
        round_id=now.isoformat(timespec="minutes"),
        now=now,
        reason="常规节奏",
        due=None,
        previous_at=previous_at,
    )


@pytest.mark.integration
async def test_things_on_her_mind_ride_into_her_round(ends_db):
    await _rewrite("周末陪绫奈去祭典", at=_at(12))

    text = await MIND.open_section(_round(_at(20)))

    assert "周末陪绫奈去祭典" in text
    assert _moment(12) in text, (
        "她眼前那段没告诉她这件事是从哪一轮带过来的 —— 光有钟点在跨天之后就分不清是哪一天"
    )


@pytest.mark.integration
async def test_nothing_on_her_mind_reads_as_nothing_not_as_a_hole(ends_db):
    assert await MIND.open_section(_round(_at(8))) == "心里挂着没了结的：（没有）"


@pytest.mark.integration
async def test_a_thing_with_no_hour_reads_exactly_as_it_always_did(ends_db):
    """没有时刻的那条一个字都不该变 —— 她眼前绝大多数线头都是这一种。"""
    await _rewrite("周末陪绫奈去祭典", at=_at(12))

    assert "- 周末陪绫奈去祭典 · 从 2026-07-25T12:00+08:00 那一刻起挂着" in (
        await MIND.open_section(_round(_at(20)))
    )


@pytest.mark.integration
async def test_a_thing_she_should_do_at_a_certain_hour_shows_that_hour(ends_db):
    """那一行整个钉住：她既要读得出「该在几点、到了没有」，又要抄得回原样。"""
    await _rewrite("[2026-07-25 15:00] 家属谈话会", at=_at(12))

    text = await MIND.open_section(_round(_at(14)))

    assert (
        "- [2026-07-25 15:00] 家属谈话会 · 还没到 · "
        "从 2026-07-25T12:00+08:00 那一刻起挂着" in text
    ), f"她读不出这条该在几点、也抄不回那个形状。拿到：\n{text}"


@pytest.mark.integration
async def test_when_the_hour_has_come_her_round_says_so(ends_db):
    """「到点了」是渲染时当场跟 ``now`` 比出来的，库里没有任何人替她改过状态。"""
    await _rewrite("[2026-07-25 15:00] 家属谈话会", at=_at(12))

    before = await MIND.open_section(_round(_at(14)))
    after = await MIND.open_section(_round(_at(15)))

    assert "还没到" in before and "到点了" not in before
    assert "到点了" in after and "还没到" not in after


@pytest.mark.integration
async def test_a_thing_that_came_due_stays_until_she_stops_listing_it(ends_db):
    """她自己的安排不是一张到期交付一次就被消费掉的时刻表：时间过了，那个会她还是
    没去开。所以到点之后那条继续挂在她眼前，直到她自己不再列它——了结与否是她的判断，
    不是钟的。
    """
    await _rewrite("[2026-07-25 15:00] 家属谈话会", at=_at(12))

    for hour in (16, 20, 23):
        later = await MIND.open_section(_round(_at(hour)))
        assert "到点了" in later, f"{hour} 点那一轮它自己消失了"

    await _rewrite(at=_at(23, 10))

    assert "家属谈话会" not in await MIND.open_section(_round(_at(23, 20)))


@pytest.mark.integration
async def test_another_sisters_mind_never_leaks_into_hers(ends_db):
    await _rewrite("绫奈自己的心事", at=_at(12), persona="ayana")

    assert "绫奈自己的心事" not in await MIND.open_section(_round(_at(13)))
    assert await MIND.open_count(lane=LANE, persona_id="akao") == 0


@pytest.mark.integration
async def test_a_thing_that_just_came_due_is_something_new(ends_db):
    """挂着的那份清单每轮不重发，但"刚到点"是这一轮真的发生了的事，必须当场送到。

    清单本身跟着状态走、清理时才重铺一次（默认一小时）。一件 15:30 该做的事要是也等
    重铺，她会在 16:00 才看见"到点了" —— ``keep_in_mind`` 的文案对她的承诺就落空了。
    """
    await _rewrite("[2026-07-25 15:00] 家属谈话会", "洗的衣服还在阳台", at=_at(12))

    assert await MIND.just_due_section(_round(_at(14, 50), previous_at=_at(14, 40))) is None, (
        "还没到点就先报了"
    )

    just_due = await MIND.just_due_section(_round(_at(15, 0), previous_at=_at(14, 50)))
    assert just_due == "刚到点的：\n- [2026-07-25 15:00] 家属谈话会", f"拿到：\n{just_due}"

    assert await MIND.just_due_section(_round(_at(15, 30), previous_at=_at(15, 20))) is None, (
        "到点那一下报过了还在每轮重报"
    )


@pytest.mark.integration
async def test_nothing_comes_due_on_the_very_first_moment(ends_db):
    """没有上一次就没有"这期间"，不拿一堆早就过期的事当成刚刚发生。"""
    await _rewrite("[2026-07-25 09:00] 早就过去的那件事", at=_at(8))

    assert await MIND.just_due_section(_round(_at(14))) is None


@pytest.mark.integration
async def test_both_sections_of_one_round_read_her_list_once(ends_db, monkeypatch):
    """两段读的是同一份清单，一轮只读一次：段在模型之前算，那时她还没动过它。"""
    import app.living.loose_ends.mind as mind_mod

    await _rewrite("[2026-07-25 15:00] 家属谈话会", at=_at(12))
    reads = []
    real = mind_mod.list_open_loose_ends

    async def counted(**kwargs):
        reads.append(kwargs)
        return await real(**kwargs)

    monkeypatch.setattr(mind_mod, "list_open_loose_ends", counted)
    her_round = _round(_at(15), previous_at=_at(14, 50))

    await MIND.open_section(her_round)
    await MIND.just_due_section(her_round)

    assert reads == [{"lane": LANE, "persona_id": "akao"}]


@pytest.mark.integration
async def test_open_count_is_how_many_she_still_carries(ends_db):
    """落地那一行的 ``open_ends``：此刻还挂着几件（``loose_ends`` 服务）。"""
    await _rewrite("洗的衣服还在阳台", "回绫奈的祭典", at=_at(12))
    await _rewrite("回绫奈的祭典", at=_at(13))

    assert await MIND.open_count(lane=LANE, persona_id="akao") == 1


async def test_the_loose_ends_plugin_places_her_hand_and_both_sections(app_host):
    """loose_ends 插件登记 ``keep_in_mind``（她自己的话，裁剪时留着）、两段，交出 ``loose_ends``
    服务；清单 ``resident`` 把它们排在各自的位置。"""
    host = await app_host("agent-service")
    mine = {
        (r.kind, r.name)
        for r in host.registered()
        if r.plugin == "loose_ends"
    }

    assert mine == {
        ("service", "loose_ends"),
        ("tool", "keep_in_mind"),
        ("section", "loose_ends.open"),
        ("section", "loose_ends.just_due"),
    }
    (spec,) = [
        r.detail["spec"]
        for r in host.registered()
        if r.kind == "tool" and r.name == "keep_in_mind"
    ]
    assert (spec.trim, spec.ends_round) == ("kept", False)
