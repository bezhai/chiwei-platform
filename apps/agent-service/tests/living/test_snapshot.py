"""她进入这一轮时读到的东西 —— 状态快照，不是历史回放。

几层全是她自己的，每层各有**结构性**的上界，所以它永远不会像 transcript 那样撞顶、
也永远不需要模型折叠（折叠才会失真）：

  ==================  ====================  ==========================
  层                  来源                  界
  ==================  ====================  ==========================
  手上正在做的事      最新一条 Whereabouts  1 行
  上一次写下的那天    LivingDayPage         1 页
  她刚做过 / 说过     她自己的 Happening    最近 N 条
  ==================  ====================  ==========================

别人做了什么不从这里读：传到她这里的那一段在 :mod:`app.living.received`，由
``run_due_moment`` 另摆进这一轮（``tests/living/test_received.py``）。"她刚做过、说过"那层
单独存在的理由：她的上下文会被裁剪，传到她这里的消息也只摆一次。少了这一层，她上一轮
答应姐姐的话下一轮就凭空消失，"接得上昨天"永远无从谈起。

但它只在上下文接不住她最近说过的话时才渲染（上下文是空的、断过、上一轮没存下来）：
上下文连着的时候她说过的话原样都在，状态里再抄一份就是同一句话在一轮输入里出现好几遍。
"""
from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.agent.round import Round
from app.living.her_records.deeds import (
    OWN_RECENT_LIMIT,
    recent_own_happenings,
    record_happening,
)
from app.living.her_records.whereabouts import note_whereabouts
from app.living.loose_ends.mind import OnHerMind
from app.living.loose_ends.threads import LooseEnd, rewrite_loose_ends
from app.living.services import (
    KIND_ACT,
    KIND_SPEECH,
    MEDIUM_PHONE,
    OUTBOUND_HAPPENING_PREFIX,
)
from app.living.snapshot import read_snapshot
from tests.living.conftest import own_records

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))


def _at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 7, 25, hour, minute, tzinfo=_CST)


@pytest.fixture
async def snap_db(living_db):
    from tests.runtime.conftest import migrate

    await migrate(LooseEnd, living_db)
    return living_db


async def _stand(persona: str, place: str, doing: str, at: dt.datetime) -> None:
    await note_whereabouts(
        lane=LANE,
        persona_id=persona,
        moment_id=at.isoformat(timespec="minutes"),
        place=place,
        doing=doing,
        noted_at=at,
    )


async def _say(actor: str, content: str, at: dt.datetime, *, to=(), kind=KIND_SPEECH):
    return await record_happening(
        lane=LANE,
        happening_id=f"{actor}-{at.isoformat()}-{content[:6]}",
        actor=actor,
        kind=kind,
        content=content,
        occurred_at=at,
        audience=list(to),
    )


# 挂念和刚到点的那两段由 loose_ends 插件摆（:mod:`app.living.loose_ends.mind`），这里拿它交出去的
# 那个对象，跟快照的几段拼在一起看她一轮里的样子。
_MIND = OnHerMind()
_RECORDS = own_records()


def _round(snap, previous_at: dt.datetime | None = None) -> Round:
    """快照所在的那一轮（只有段要用的那几样）。"""
    return Round(
        app="agent-service",
        lane=snap.lane,
        participant=snap.persona_id,
        profile="resident",
        round_id=snap.now.isoformat(timespec="minutes"),
        now=snap.now,
        reason="常规节奏",
        due=None,
        previous_at=previous_at,
    )


async def _state_sections(snap) -> str:
    """她此刻的样子那三段（日记页、挂念、刚做过说过）连在一起，查某句话在不在里面。"""
    return "\n\n".join(
        (
            snap.render_day_page(),
            await _MIND.open_section(_round(snap)),
            await _RECORDS.recent_section(_round(snap)),
        )
    )


async def _new_sections(snap, *, previous_at) -> str:
    """这一轮的实况那三段（现在、手上、刚到点的）连在一起，查某句话在不在里面。"""
    parts = (
        snap.render_now(previous_at),
        await _RECORDS.whereabouts_section(_round(snap)),
        await _MIND.just_due_section(_round(snap, previous_at)),
    )
    return "\n\n".join(part for part in parts if part is not None)


# --------------------------------------------------------------------------
# 〇 · 两半分工：她此刻的样子 vs 这一轮新发生的
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_state_she_stands_in_carries_nothing_that_just_happened(snap_db):
    """重铺那一半只有她自己当下的事实，不含这一轮新发生的东西，也不含别人的经历。

    上下文连续之后，每轮重发一份全量状态就是把她上一轮读过的东西再抄一遍。全量只在
    清理那一下重铺一次（:func:`app.agent.continuity.trim_for_round`），所以这一半必须
    不含每轮都变的东西。
    """
    await _stand("akao", "家/客厅", "看昨天拍的胶片", _at(14))
    await _stand("ayana", "家/厨房", "煮东西", _at(13))
    await _keep("周末陪绫奈去祭典", at=_at(12))
    await _say("akao", "布丁我吃了。", _at(13, 50), to=["ayana"])
    await _say("ayana", "冰箱里还有布丁", _at(14, 2), to=["akao"])

    snap = await read_snapshot(
        lane=LANE, persona_id="akao", now=_at(14, 10)
    )
    state = await _state_sections(snap)

    assert "看昨天拍的胶片" not in state, (
        "她在哪、在干嘛归这一轮的实况 —— 位置每轮都可能被她自己改，重铺那份会过期"
    )
    assert "周末陪绫奈去祭典" in state
    assert "布丁我吃了。" in state
    assert "你上一次写下的那一天" in state
    assert "冰箱里还有布丁" not in state, "绫奈的经历进了她的状态"
    assert "现在 2026-07-25" not in state, (
        "重铺那一半自己报了一次时刻 —— 界桩头上已经有一个，这里是第二个"
    )


@pytest.mark.integration
async def test_what_is_new_is_the_clock_the_gap_and_where_she_is(snap_db):
    """每轮送到她眼前的实况：几点了、隔了多久、她在哪在做什么。别人做了什么不在这里：
    传到她这里的那一段由收件箱来（:mod:`app.living.received`）。"""
    await _stand("akao", "家/客厅", "看昨天拍的胶片", _at(14))
    await _stand("ayana", "家/厨房", "煮东西", _at(13))
    await _keep("周末陪绫奈去祭典", at=_at(12))
    await _say("akao", "布丁我吃了。", _at(13, 50), to=["ayana"])
    await _say("ayana", "冰箱里还有布丁", _at(14, 2), to=["akao"])

    snap = await read_snapshot(
        lane=LANE, persona_id="akao", now=_at(14, 10)
    )
    new = await _new_sections(snap, previous_at=_at(14))

    assert "2026-07-25" in new and "周六" in new, "她不知道今天是几号、星期几"
    assert "10 分钟" in new, f"没告诉她离上一次隔了多久。拿到：\n{new}"
    assert "冰箱里还有布丁" not in new, "绫奈的经历按位置进了她这一轮"
    assert "看昨天拍的胶片" in new, "她这一轮不知道自己在干嘛"
    for repeated in ("周末陪绫奈去祭典", "布丁我吃了。"):
        assert repeated not in new, (
            f"「{repeated}」每轮都在重发 —— 它在上下文里已经有了"
        )


@pytest.mark.integration
async def test_the_very_first_moment_says_nothing_about_a_gap(snap_db):
    """一个 moment 都没跑过时不编一句"隔了多久"，只报时刻。"""
    snap = await read_snapshot(
        lane=LANE, persona_id="akao", now=_at(14, 10)
    )
    new = await _new_sections(snap, previous_at=None)

    assert "2026-07-25" in new
    assert "上一次" not in new, f"凭空造了一个上一次。拿到：\n{new}"


@pytest.mark.integration
async def test_a_long_gap_reads_in_hours(snap_db):
    """停机几小时再起来，"隔了多久"不能只报一个三位数的分钟。"""
    snap = await read_snapshot(
        lane=LANE, persona_id="akao", now=_at(14, 10)
    )
    new = await _new_sections(snap, previous_at=_at(9, 40))

    assert "4 小时 30 分钟" in new, f"拿到：\n{new}"


@pytest.mark.integration
async def test_a_moment_that_lands_out_of_order_does_not_report_a_negative_gap(
    snap_db,
):
    """常规 moment 的钟点可能比先落地那个提前来的 moment 还早（见 ``LifeMoment``）。

    那时 ``now - previous_at`` 是负数，照直渲染就是往她眼前塞一句"离上一次过了 -4
    分钟"。
    """
    snap = await read_snapshot(
        lane=LANE, persona_id="akao", now=_at(21, 30)
    )
    new = await _new_sections(snap, previous_at=_at(21, 34))

    assert "离上一次过了" not in new, f"报了一个负数的间隔。拿到：\n{new}"
    assert "刚才" in new, f"拿到：\n{new}"


# --------------------------------------------------------------------------
# 一 · 手上正在做的事
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_snapshot_opens_with_what_is_in_her_hands(snap_db):
    await _stand("akao", "家/客厅", "看昨天拍的胶片", _at(14))

    snap = await read_snapshot(
        lane=LANE, persona_id="akao", now=_at(14, 10)
    )

    doing = await _RECORDS.current_whereabouts(lane=snap.lane, persona_id=snap.persona_id)
    assert doing is not None
    assert (doing.place, doing.doing) == ("家/客厅", "看昨天拍的胶片")
    assert "看昨天拍的胶片" in await _new_sections(snap, previous_at=_at(14))


@pytest.mark.integration
async def test_she_can_be_nowhere_yet_and_the_snapshot_says_so_plainly(snap_db):
    """冷启动第一轮她还没定下在哪 —— 不许编一个位置，也不许渲染出一片空白。"""
    snap = await read_snapshot(lane=LANE, persona_id="akao", now=_at(8))

    assert await _RECORDS.current_whereabouts(lane=snap.lane, persona_id=snap.persona_id) is None
    assert all(
        text.strip()
        for text in (
            snap.render_day_page(),
            await _MIND.open_section(_round(snap)),
            await _RECORDS.recent_section(_round(snap)),
        )
    )


# --------------------------------------------------------------------------
# 二 · 挂着没了结的事：她自己列、loose_ends 插件摆（``tests/living/test_loose_ends.py``）；
# 这里只用它看几段拼在一起时各在哪一半
# --------------------------------------------------------------------------


async def _keep(*items: str, at: dt.datetime, persona: str = "akao") -> None:
    await rewrite_loose_ends(
        lane=LANE,
        persona_id=persona,
        moment_id=at.isoformat(timespec="minutes"),
        now=at,
        still_on_my_mind=items,
    )


# --------------------------------------------------------------------------
# 三 · 她刚做过 / 说过什么（回声只有这一条路）
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_she_can_see_what_she_herself_just_said(snap_db):
    """少了这一层她上一轮的承诺就凭空消失。"""
    await _stand("akao", "家/客厅", "待着", _at(13))
    await _say("akao", "周末祭典我陪你去。", _at(13, 50), to=["ayana"])

    snap = await read_snapshot(
        lane=LANE, persona_id="akao", now=_at(14)
    )

    assert [h.content for h in (await recent_own_happenings(lane=snap.lane, persona_id=snap.persona_id))] == ["周末祭典我陪你去。"]
    assert "周末祭典我陪你去。" in await _RECORDS.recent_section(_round(snap))


@pytest.mark.integration
async def test_when_her_words_are_already_in_view_the_state_does_not_copy_them(
    snap_db,
):
    """她自己的话还在上下文里时，状态不再抄一遍「你刚做过、说过」，其余两段照旧。

    每小时清理那一下，历史里有标记消息、上一轮也存下来了：她 4 小时内说过的话、发出去的
    每一条都原样在上下文里。2026-09-23 每小时的状态块各带最近 12 句，同一句话在一轮
    输入里出现了好几遍。这一段只在她自己的话不在眼前时给（上下文是空的、断过、上一轮
    没存下来）—— 判据不在这里，摆不摆由她这一轮的那一段判
    （:func:`app.living.moment.own_recent_section`，``tests/living/test_her_round_profile.py``）。
    """
    await _stand("akao", "家/客厅", "看昨天拍的胶片", _at(14))
    await _keep("周末陪绫奈去祭典", at=_at(12))
    await _say("akao", "布丁我吃了。", _at(13, 50), to=["ayana"])

    snap = await read_snapshot(lane=LANE, persona_id="akao", now=_at(14, 10))
    page, open_ends = snap.render_day_page(), await _MIND.open_section(_round(snap))

    # 她的话只在「你刚做过、说过」那一段里：那一段不摆，别的两段一个字都不带它们。
    for text in (page, open_ends):
        assert "你刚做过、说过" not in text and "布丁我吃了。" not in text, text
    assert "你上一次写下的那一天" in page
    assert "周末陪绫奈去祭典" in open_ends
    own = await _RECORDS.recent_section(_round(snap))
    assert "你刚做过、说过" in own and "布丁我吃了。" in own


@pytest.mark.integration
async def test_her_own_acts_show_up_too(snap_db):
    await _say("akao", "把胶片摊在茶几上", _at(14, 5), kind=KIND_ACT)

    snap = await read_snapshot(lane=LANE, persona_id="akao", now=_at(14, 6))

    assert [h.content for h in (await recent_own_happenings(lane=snap.lane, persona_id=snap.persona_id))] == ["把胶片摊在茶几上"]


@pytest.mark.integration
async def test_a_message_she_sent_carries_the_handle_that_takes_it_back(snap_db):
    """她发出去的每条消息后面带着它的编号 —— 撤回时她唯一指得动的东西。

    编号就是 ``outbound_id``（``happening_id`` 去掉
    :data:`~app.living.records.OUTBOUND_HAPPENING_PREFIX` 之后那一串），
    :func:`app.living.takeback.take_back_message` 收的是同一个东西。不印出来的话她
    只能拿原话去指，同一句话说过两遍就分不出是哪一次。

    **整串照印，不截断。** 截断要配一套前缀唯一性校验，而那个分支在真实数据量下永
    远不会触发。她照抄一串字符没有负担。
    """
    oid = uuid.uuid5(uuid.NAMESPACE_OID, "she-sent-this-one").hex
    await record_happening(
        lane=LANE,
        happening_id=f"{OUTBOUND_HAPPENING_PREFIX}{oid}",
        actor="akao",
        kind=KIND_SPEECH,
        content="你去过那家抹茶店吗？",
        occurred_at=_at(14, 32),
        audience=["bezhai"],
        medium=MEDIUM_PHONE,
        channel_id=str(uuid.uuid5(uuid.NAMESPACE_OID, "conv-dm")),
    )

    snap = await read_snapshot(
        lane=LANE, persona_id="akao", now=_at(14, 40)
    )
    text = await _RECORDS.recent_section(_round(snap))

    assert (
        f"- 14:32 CST 你对 bezhai 说：「你去过那家抹茶店吗？」［{oid}］" in text
    ), f"她发出去那条没带编号 —— 撤回时她指不动任何一条。拿到：\n{text}"


@pytest.mark.integration
async def test_what_she_said_face_to_face_carries_no_handle(snap_db):
    """当面说的话撤不了 —— 给它一个编号就是给她一个指了会失败的东西。

    判据是 ``happening_id`` 的前缀，不是 ``kind``：当面说和发消息的 ``kind`` 都是
    ``speech``，只有走过嘴那条路的才有 ``outbound_id``。
    """
    await _say("akao", "布丁我吃了。", _at(14, 5), to=["ayana"])

    snap = await read_snapshot(
        lane=LANE, persona_id="akao", now=_at(14, 10)
    )
    text = await _RECORDS.recent_section(_round(snap))

    assert "- 14:05 CST 你对 绫奈 说：「布丁我吃了。」" in text
    assert "［" not in text, (
        f"当面说的话也带上了编号 —— 她照它去撤只会撤了个空。拿到：\n{text}"
    )


@pytest.mark.integration
async def test_a_sister_named_by_id_in_her_records_reads_as_her_name(snap_db):
    """她以前的记录里，说给谁记的是 life 内部的 id（``say`` 当时收的就是 id）。摆到她眼前时
    换成姐妹在世界里的名字：她看到的是 ``chinagi``，下一次就照着 ``chinagi`` 去叫人。
    不是三姐妹的名字原样摆；同一个人写了两种叫法只摆一次。"""
    await _say("akao", "布丁我吃了。", _at(14, 5), to=["ayana"])
    await _say("akao", "开饭了。", _at(14, 6), to=["绫奈", "chinagi", "许阿姨"])
    await _say("akao", "汤好了。", _at(14, 7), to=["千凪", "chinagi"])

    snap = await read_snapshot(lane=LANE, persona_id="akao", now=_at(14, 10))
    text = await _RECORDS.recent_section(_round(snap))

    assert "- 14:05 CST 你对 绫奈 说：「布丁我吃了。」" in text
    assert "- 14:06 CST 你对 绫奈、千凪、许阿姨 说：「开饭了。」" in text
    assert "- 14:07 CST 你对 千凪 说：「汤好了。」" in text
    assert "ayana" not in text and "chinagi" not in text, text


@pytest.mark.integration
async def test_her_own_trail_is_bounded_by_count_not_by_a_clock(snap_db):
    """条数封顶（不是按时间切）—— 她安静一整天时最近这几条仍然读得到。"""
    for i in range(OWN_RECENT_LIMIT + 5):
        await _say("akao", f"第{i}件事", _at(10, i), kind=KIND_ACT)

    snap = await read_snapshot(lane=LANE, persona_id="akao", now=_at(20))

    assert len(await recent_own_happenings(lane=snap.lane, persona_id=snap.persona_id)) == OWN_RECENT_LIMIT
    assert [h.content for h in (await recent_own_happenings(lane=snap.lane, persona_id=snap.persona_id))] == [
        f"第{i}件事" for i in range(5, OWN_RECENT_LIMIT + 5)
    ], "最近的应该在最后，而且按发生先后排"


@pytest.mark.integration
async def test_another_sisters_acts_are_not_her_own_trail(snap_db):
    await _say("ayana", "在厨房煮东西", _at(14), kind=KIND_ACT)

    snap = await read_snapshot(lane=LANE, persona_id="akao", now=_at(14, 5))

    assert (await recent_own_happenings(lane=snap.lane, persona_id=snap.persona_id)) == []


# --------------------------------------------------------------------------
# 四 · 跨夜之后，昨晚那行不能读成今晚
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_last_nights_line_does_not_read_as_tonight(snap_db):
    """``own_recent`` 只按条数取、**不设时间窗** —— 昨晚那行会一直挂到被挤出去为止。

    裸时分下 ``23:41 看完了那本书`` 跟今晚 23:41 长得一模一样，她无从分辨那是几分钟前
    还是一整天前。同一个病在线上炸过（2026-08-03：中午 13:18 往群里发「大半夜的发什么
    疯、赶紧滚去睡觉」），出口是 :func:`app.infra.cst_time.to_cst_dated`。
    """
    await _say(
        "akao",
        "看完了那本书",
        _at(23, 41) - dt.timedelta(days=1),
        kind=KIND_ACT,
    )

    snap = await read_snapshot(
        lane=LANE, persona_id="akao", now=_at(23, 45)
    )
    text = await _RECORDS.recent_section(_round(snap))

    assert "07-24 23:41" in text, (
        f"昨晚 23:41 那行渲染成了裸时分，跟今晚 23:41 一个形状 —— "
        f"她读到的是「几分钟前刚看完」。拿到：\n{text}"
    )


@pytest.mark.integration
async def test_todays_own_lines_stay_undated(snap_db):
    """同一天的**刻意不带**日期：全带上会稀释掉「这条是昨天的」这个真正的信号。"""
    await _say("akao", "把胶片摊在茶几上", _at(14, 5), kind=KIND_ACT)

    snap = await read_snapshot(
        lane=LANE, persona_id="akao", now=_at(14, 10)
    )
    text = await _RECORDS.recent_section(_round(snap))

    assert "- 14:05 CST 你 把胶片摊在茶几上" in text
    assert "07-25 14:05" not in text, f"当天的行不该带日期。拿到：\n{text}"


@pytest.mark.integration
async def test_the_now_line_says_which_calendar_day_it_is(snap_db):
    """带日期的行只有在「今天是几号」也说了的时候才读得懂。

    这一轮喂给她的全部输入就是快照 + 信封（见 ``app.living.moment.run_due_moment``），
    没有别的地方告诉她今天几号、星期几。只给 ``07-24`` 而不说今天是 ``07-25``，她算
    不出那是昨天还是上个月；日程 / 提醒要填绝对日期时更是只能瞎填。
    """
    snap = await read_snapshot(
        lane=LANE, persona_id="akao", now=_at(23, 45)
    )
    text = await _new_sections(snap, previous_at=_at(23, 40))

    assert "2026-07-25" in text, f"她不知道今天是几号。拿到：\n{text}"
    assert "周六" in text, f"她不知道今天星期几。拿到：\n{text}"


# ---------------------------------------------------------------------------
# 「手上」搬到每轮刺激里
#
# 她在哪、在做什么读一百遍字字一样，所以它本来在界桩上重铺 —— 那条推理对"上一次写下
# 的那一天""心里挂着什么"成立，对这一条**不成立**：位置每一轮都可能被她自己改（move_to
# / switch_to），改完之后到下一个清理点之间，她眼前那份状态说的还是旧位置。而她在哪是
# 这一轮里最该准的一样东西（world 按她报的位置判断谁看得见她）。
#
# 所以它归到这一轮的实况（:meth:`app.living.her_records.book.OwnRecords.whereabouts_section`）。
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_where_she_is_comes_with_every_round(snap_db):
    await _stand("akao", "家/厨房", "煮抹茶", _at(14))

    snap = await read_snapshot(
        lane=LANE, persona_id="akao", now=_at(14, 10)
    )
    new = await _new_sections(snap, previous_at=_at(14))

    assert "家/厨房" in new
    assert "煮抹茶" in new


@pytest.mark.integration
async def test_where_she_is_is_not_also_in_the_checkpoint(snap_db):
    """一份输入里不该有两处说她在哪 —— 而且界桩那份还可能是过期的那一个。"""
    await _stand("akao", "家/厨房", "煮抹茶", _at(14))

    snap = await read_snapshot(
        lane=LANE, persona_id="akao", now=_at(14, 10)
    )

    assert "家/厨房" not in await _state_sections(snap)


@pytest.mark.integration
async def test_having_never_stood_anywhere_still_says_so(snap_db):
    """空的时候如实说空，不留白洞 —— 她第一轮得知道"我还没定下自己在哪"。"""
    snap = await read_snapshot(
        lane=LANE, persona_id="akao", now=_at(14, 10)
    )

    assert "还没定下" in await _new_sections(snap, previous_at=None)
