"""她进入一缝时读到的东西 —— 状态快照，不是历史回放。

四层，每层各有**结构性**的上界，所以它永远不会像 transcript 那样撞顶、也永远不需要
模型折叠（折叠才会失真）：

  ==================  ====================  ==========================
  层                  来源                  界
  ==================  ====================  ==========================
  手上正在做的事      最新一条 Whereabouts  1 行
  挂着没了结的事      LooseEnd 还开着的     她自己列多少就是多少
  她刚做过 / 说过     她自己的 Happening    最近 N 条（**回声在这里**）
  这段时间感知到的    read_perceived_by     一条游标 + 每缝的条数上限
  ==================  ====================  ==========================

第三层单独存在的理由：``read_perceived_by`` 抑制回声（``actor == persona_id`` 直接
丢），所以她**看不见自己刚说过什么**。少了这一层，她上一缝答应姐姐的话下一缝就凭空
消失，"接得上昨天"永远无从谈起。
"""
from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.living.happening import record_happening
from app.living.loose_ends import LooseEnd, rewrite_loose_ends
from app.living.records import (
    KIND_ACT,
    KIND_SPEECH,
    MEDIUM_PHONE,
    OUTBOUND_HAPPENING_PREFIX,
)
from app.living.snapshot import OWN_RECENT_LIMIT, all_whereabouts, read_snapshot
from app.living.whereabouts import note_whereabouts

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


async def _say(
    actor: str, content: str, at: dt.datetime, *, place: str, to=(), kind=KIND_SPEECH
):
    return await record_happening(
        lane=LANE,
        happening_id=f"{actor}-{at.isoformat()}-{content[:6]}",
        actor=actor,
        place=place,
        kind=kind,
        content=content,
        occurred_at=at,
        audience=list(to),
    )


# --------------------------------------------------------------------------
# 一 · 手上正在做的事
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_snapshot_opens_with_what_is_in_her_hands(snap_db):
    await _stand("akao", "家/客厅", "看昨天拍的胶片", _at(14))

    snap = await read_snapshot(
        lane=LANE, persona_id="akao", after_seq=0, now=_at(14, 10)
    )

    assert snap.doing is not None
    assert (snap.doing.place, snap.doing.doing) == ("家/客厅", "看昨天拍的胶片")
    assert "看昨天拍的胶片" in snap.render()


@pytest.mark.integration
async def test_she_can_be_nowhere_yet_and_the_snapshot_says_so_plainly(snap_db):
    """冷启动第一缝她还没定下在哪 —— 不许编一个位置，也不许渲染出一片空白。"""
    snap = await read_snapshot(lane=LANE, persona_id="akao", after_seq=0, now=_at(8))

    assert snap.doing is None
    assert snap.render().strip() != ""


# --------------------------------------------------------------------------
# 二 · 挂着没了结的事（跨缝续接的载体）
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_things_on_her_mind_ride_into_the_snapshot(snap_db):
    await rewrite_loose_ends(
        lane=LANE,
        persona_id="akao",
        moment_id=_at(12).isoformat(timespec="minutes"),
        now=_at(12),
        still_on_my_mind=["周末陪绫奈去祭典"],
    )

    snap = await read_snapshot(lane=LANE, persona_id="akao", after_seq=0, now=_at(20))

    assert [e.what for e in snap.open_ends] == ["周末陪绫奈去祭典"]
    text = snap.render()
    assert "周末陪绫奈去祭典" in text
    assert _at(12).isoformat(timespec="minutes") in text, (
        "快照没告诉她这件事是从哪一缝带过来的 —— 光有钟点在跨天之后就分不清是哪一天"
    )


async def _keep(*items: str, at: dt.datetime, persona: str = "akao") -> None:
    await rewrite_loose_ends(
        lane=LANE,
        persona_id=persona,
        moment_id=at.isoformat(timespec="minutes"),
        now=at,
        still_on_my_mind=items,
    )


@pytest.mark.integration
async def test_a_thing_with_no_hour_reads_exactly_as_it_always_did(snap_db):
    """没有时刻的那条一个字都不该变 —— 她眼前绝大多数线头都是这一种。"""
    await _keep("周末陪绫奈去祭典", at=_at(12))

    snap = await read_snapshot(lane=LANE, persona_id="akao", after_seq=0, now=_at(20))

    assert (
        "- 周末陪绫奈去祭典 · 从 2026-07-25T12:00+08:00 那一缝起挂着"
        in snap.render()
    )


@pytest.mark.integration
async def test_a_thing_she_should_do_at_a_certain_hour_shows_that_hour(snap_db):
    """那一行整个钉住：她既要读得出「该在几点、到了没有」，又要抄得回原样。"""
    await _keep("[2026-07-25 15:00] 家属谈话会", at=_at(12))

    snap = await read_snapshot(lane=LANE, persona_id="akao", after_seq=0, now=_at(14))
    text = snap.render()

    assert (
        "- [2026-07-25 15:00] 家属谈话会 · 还没到 · "
        "从 2026-07-25T12:00+08:00 那一缝起挂着" in text
    ), f"她读不出这条该在几点、也抄不回那个形状。拿到：\n{text}"


@pytest.mark.integration
async def test_when_the_hour_has_come_the_snapshot_says_so(snap_db):
    """「到点了」是渲染时当场跟 ``now`` 比出来的，库里没有任何人替她改过状态。"""
    await _keep("[2026-07-25 15:00] 家属谈话会", at=_at(12))

    early = await read_snapshot(lane=LANE, persona_id="akao", after_seq=0, now=_at(14))
    due = await read_snapshot(lane=LANE, persona_id="akao", after_seq=0, now=_at(15))

    assert "还没到" in early.render() and "到点了" not in early.render()
    assert "到点了" in due.render() and "还没到" not in due.render()


@pytest.mark.integration
async def test_a_thing_that_came_due_stays_until_she_stops_listing_it(snap_db):
    """到期交付一次就被消费掉是 ``Upcoming`` 的语义。

    她自己的安排不是那样：时间过了，那个会她还是没去开。所以到点之后那条继续挂在她
    眼前，直到她自己不再列它——了结与否是她的判断，不是钟的。
    """
    await _keep("[2026-07-25 15:00] 家属谈话会", at=_at(12))

    for hour in (16, 20, 23):
        later = await read_snapshot(
            lane=LANE, persona_id="akao", after_seq=0, now=_at(hour)
        )
        assert "到点了" in later.render(), f"{hour} 点那一缝它自己消失了"

    await _keep(at=_at(23, 10))

    gone = await read_snapshot(
        lane=LANE, persona_id="akao", after_seq=0, now=_at(23, 20)
    )
    assert "家属谈话会" not in gone.render()


@pytest.mark.integration
async def test_another_sisters_mind_never_leaks_into_hers(snap_db):
    await rewrite_loose_ends(
        lane=LANE,
        persona_id="ayana",
        moment_id=_at(12).isoformat(timespec="minutes"),
        now=_at(12),
        still_on_my_mind=["绫奈自己的心事"],
    )

    snap = await read_snapshot(lane=LANE, persona_id="akao", after_seq=0, now=_at(13))

    assert snap.open_ends == []
    assert "绫奈自己的心事" not in snap.render()


# --------------------------------------------------------------------------
# 三 · 她刚做过 / 说过什么（回声只有这一条路）
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_she_can_see_what_she_herself_just_said(snap_db):
    """``read_perceived_by`` 抑制回声；少了这一层她上一缝的承诺就凭空消失。"""
    await _stand("akao", "家/客厅", "待着", _at(13))
    await _say("akao", "周末祭典我陪你去。", _at(13, 50), place="家/客厅", to=["ayana"])

    snap = await read_snapshot(
        lane=LANE, persona_id="akao", after_seq=0, now=_at(14)
    )

    assert [h.content for h in snap.own_recent] == ["周末祭典我陪你去。"]
    assert "周末祭典我陪你去。" in snap.render()
    assert snap.perceived.items == [], "自己说的话不该同时从感知那条路再来一遍"


@pytest.mark.integration
async def test_her_own_acts_show_up_too(snap_db):
    await _say("akao", "把胶片摊在茶几上", _at(14, 5), place="家/客厅", kind=KIND_ACT)

    snap = await read_snapshot(lane=LANE, persona_id="akao", after_seq=0, now=_at(14, 6))

    assert [h.content for h in snap.own_recent] == ["把胶片摊在茶几上"]


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
        place="家/我房间",
        kind=KIND_SPEECH,
        content="你去过那家抹茶店吗？",
        occurred_at=_at(14, 32),
        audience=["bezhai"],
        medium=MEDIUM_PHONE,
        channel_id=str(uuid.uuid5(uuid.NAMESPACE_OID, "conv-dm")),
    )

    snap = await read_snapshot(
        lane=LANE, persona_id="akao", after_seq=0, now=_at(14, 40)
    )
    text = snap.render()

    assert (
        f"- 14:32 CST 你对 bezhai 说：「你去过那家抹茶店吗？」［{oid}］" in text
    ), f"她发出去那条没带编号 —— 撤回时她指不动任何一条。拿到：\n{text}"


@pytest.mark.integration
async def test_what_she_said_face_to_face_carries_no_handle(snap_db):
    """当面说的话撤不了 —— 给它一个编号就是给她一个指了会失败的东西。

    判据是 ``happening_id`` 的前缀，不是 ``kind``：当面说和发消息的 ``kind`` 都是
    ``speech``，只有走过嘴那条路的才有 ``outbound_id``。
    """
    await _say("akao", "布丁我吃了。", _at(14, 5), place="家/客厅", to=["ayana"])

    snap = await read_snapshot(
        lane=LANE, persona_id="akao", after_seq=0, now=_at(14, 10)
    )
    text = snap.render()

    assert "- 14:05 CST 你对 ayana 说：「布丁我吃了。」" in text
    assert "［" not in text, (
        f"当面说的话也带上了编号 —— 她照它去撤只会撤了个空。拿到：\n{text}"
    )


@pytest.mark.integration
async def test_her_own_trail_is_bounded_by_count_not_by_a_clock(snap_db):
    """条数封顶（不是按时间切）—— 她安静一整天时最近这几条仍然读得到。"""
    for i in range(OWN_RECENT_LIMIT + 5):
        await _say("akao", f"第{i}件事", _at(10, i), place="家/客厅", kind=KIND_ACT)

    snap = await read_snapshot(lane=LANE, persona_id="akao", after_seq=0, now=_at(20))

    assert len(snap.own_recent) == OWN_RECENT_LIMIT
    assert [h.content for h in snap.own_recent] == [
        f"第{i}件事" for i in range(5, OWN_RECENT_LIMIT + 5)
    ], "最近的应该在最后，而且按发生先后排"


@pytest.mark.integration
async def test_another_sisters_acts_are_not_her_own_trail(snap_db):
    await _say("ayana", "在厨房煮东西", _at(14), place="家/厨房", kind=KIND_ACT)

    snap = await read_snapshot(lane=LANE, persona_id="akao", after_seq=0, now=_at(14, 5))

    assert snap.own_recent == []


# --------------------------------------------------------------------------
# 四 · 这段时间她感知到的（走 T1 的现成入口，裁剪不在这里重做）
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_what_was_said_to_her_arrives_with_its_words(snap_db):
    await _stand("akao", "家/客厅", "待着", _at(13))
    await _stand("ayana", "家/厨房", "煮东西", _at(13))
    await _say(
        "ayana", "冰箱里还有布丁，你要吗", _at(14, 12), place="家/厨房", to=["akao"]
    )

    snap = await read_snapshot(lane=LANE, persona_id="akao", after_seq=0, now=_at(14, 20))

    assert [p.content for p in snap.perceived.items] == ["冰箱里还有布丁，你要吗"]
    assert "冰箱里还有布丁，你要吗" in snap.render()


@pytest.mark.integration
async def test_a_line_said_to_two_people_shows_that_the_other_was_there_too(snap_db):
    """"对你说"会让她看不见姐姐也在场 —— 一句话说给两个人是一件事，不是两件。"""
    await _stand("akao", "家/客厅", "待着", _at(13))
    await _stand("ayana", "家/客厅", "待着", _at(13))
    await _say(
        "ayana",
        "冰箱里还有布丁，你们要吗",
        _at(14, 12),
        place="家/客厅",
        to=["akao", "chinagi"],
    )

    snap = await read_snapshot(lane=LANE, persona_id="akao", after_seq=0, now=_at(14, 20))

    assert "ayana 对你和 chinagi 说：「冰箱里还有布丁，你们要吗」" in snap.render()


@pytest.mark.integration
async def test_only_a_noise_renders_as_only_a_noise(snap_db):
    """同一栋别处只知道有动静 —— 渲染层不许把原话漏出来。"""
    await _stand("akao", "家/客厅", "待着", _at(13))
    await _stand("ayana", "家/楼上/绫奈房间", "待着", _at(13))
    await _say("ayana", "我讨厌死这个了", _at(14, 15), place="家/楼上/绫奈房间")

    snap = await read_snapshot(lane=LANE, persona_id="akao", after_seq=0, now=_at(14, 20))

    assert [p.content for p in snap.perceived.items] == [None]
    assert "我讨厌死这个了" not in snap.render()


@pytest.mark.integration
async def test_the_cursor_moves_so_the_next_moment_starts_where_this_one_stopped(
    snap_db,
):
    await _stand("akao", "家/客厅", "待着", _at(13))
    await _stand("ayana", "家/客厅", "待着", _at(13))
    first = await _say("ayana", "早", _at(14), place="家/客厅", to=["akao"])

    snap = await read_snapshot(lane=LANE, persona_id="akao", after_seq=0, now=_at(14, 5))
    assert snap.perceived.next_cursor == first.seq

    again = await read_snapshot(
        lane=LANE, persona_id="akao", after_seq=snap.perceived.next_cursor, now=_at(14, 5)
    )
    assert again.perceived.items == []


@pytest.mark.integration
async def test_a_phone_message_beside_her_is_not_something_she_can_overhear(snap_db):
    """渠道差别由 T1 裁；这里只证明快照没有绕过它开一条后门。"""
    await _stand("akao", "家/客厅", "待着", _at(13))
    await _stand("ayana", "家/客厅", "待着", _at(13))
    await record_happening(
        lane=LANE,
        happening_id="phone-1",
        actor="ayana",
        place="家/客厅",
        kind=KIND_SPEECH,
        content="发给别人的私聊",
        occurred_at=_at(14),
        medium=MEDIUM_PHONE,
        audience=["chinagi"],
    )

    snap = await read_snapshot(lane=LANE, persona_id="akao", after_seq=0, now=_at(14, 5))

    assert snap.perceived.items == []
    assert "发给别人的私聊" not in snap.render()


# --------------------------------------------------------------------------
# 五 · 够得着的地方现在什么样（look_around 的底层查询）
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_everyone_now_is_each_persons_latest_spot(snap_db):
    await _stand("akao", "家/客厅", "待着", _at(9))
    await _stand("akao", "家/厨房", "煮抹茶", _at(13))
    await _stand("ayana", "家/楼上/绫奈房间", "画画", _at(12))

    now = {w.persona_id: (w.place, w.doing) for w in await all_whereabouts(lane=LANE)}

    assert now == {
        "akao": ("家/厨房", "煮抹茶"),
        "ayana": ("家/楼上/绫奈房间", "画画"),
    }


@pytest.mark.integration
async def test_everyone_now_stays_inside_this_lane(snap_db):
    await note_whereabouts(
        lane="prod",
        persona_id="akao",
        moment_id="m",
        place="线上/某处",
        doing="线上的事",
        noted_at=_at(13),
    )

    assert await all_whereabouts(lane=LANE) == []


# --------------------------------------------------------------------------
# 六 · 跨夜之后，昨晚那行不能读成今晚
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
        place="家/我房间",
        kind=KIND_ACT,
    )

    snap = await read_snapshot(
        lane=LANE, persona_id="akao", after_seq=0, now=_at(23, 45)
    )
    text = snap.render()

    assert "07-24 23:41" in text, (
        f"昨晚 23:41 那行渲染成了裸时分，跟今晚 23:41 一个形状 —— "
        f"她读到的是「几分钟前刚看完」。拿到：\n{text}"
    )


@pytest.mark.integration
async def test_todays_own_lines_stay_undated(snap_db):
    """同一天的**刻意不带**日期：全带上会稀释掉「这条是昨天的」这个真正的信号。"""
    await _say("akao", "把胶片摊在茶几上", _at(14, 5), place="家/客厅", kind=KIND_ACT)

    snap = await read_snapshot(
        lane=LANE, persona_id="akao", after_seq=0, now=_at(14, 10)
    )
    text = snap.render()

    assert "- 14:05 CST 你 把胶片摊在茶几上" in text
    assert "07-25 14:05" not in text, f"当天的行不该带日期。拿到：\n{text}"


@pytest.mark.integration
async def test_what_she_heard_before_midnight_carries_its_day(snap_db):
    """刚过午夜那一段是最容易错标的：按 UTC 比会说「同一天」，按 CST 才是昨天。

    昨晚 23:50 CST = 07-24 15:50 UTC，此刻 00:20 CST = 07-24 16:20 UTC —— UTC 日历日
    完全相同。跨天判定必须按 CST 日历日走。
    """
    yesterday_evening = _at(20) - dt.timedelta(days=1)
    await _stand("akao", "家/客厅", "待着", yesterday_evening)
    await _stand("ayana", "家/客厅", "待着", yesterday_evening)
    await _say(
        "ayana",
        "我先去睡了",
        _at(23, 50) - dt.timedelta(days=1),
        place="家/客厅",
        to=["akao"],
    )

    snap = await read_snapshot(
        lane=LANE, persona_id="akao", after_seq=0, now=_at(0, 20)
    )
    text = snap.render()

    assert "07-24 23:50" in text, (
        f"刚过午夜，昨晚那句被渲染成裸时分 —— 她会当成半小时前刚说的。拿到：\n{text}"
    )


@pytest.mark.integration
async def test_the_now_line_says_which_calendar_day_it_is(snap_db):
    """带日期的行只有在「今天是几号」也说了的时候才读得懂。

    这一缝喂给她的全部输入就是快照 + 信封（见 ``app.living.moment.run_moment``），
    没有别的地方告诉她今天几号、星期几。只给 ``07-24`` 而不说今天是 ``07-25``，她算
    不出那是昨天还是上个月；日程 / 提醒要填绝对日期时更是只能瞎填。
    """
    snap = await read_snapshot(
        lane=LANE, persona_id="akao", after_seq=0, now=_at(23, 45)
    )
    text = snap.render()

    assert "2026-07-25" in text, f"她不知道今天是几号。拿到：\n{text}"
    assert "周六" in text, f"她不知道今天星期几。拿到：\n{text}"
