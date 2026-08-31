"""「谁在哪、对谁、做了什么说了什么」的读写契约。

四件必须成立的事都在这个文件里：
  * 定向送达一定到（跟位置无关，位置数据错了也照送），而且可以一次说给好几个人
  * 旁听按**事情发生时她在不在场**判，不是按她读取时在哪
  * 渠道是客观事实：当面说的话旁边的人听得见，手机 / 群聊发的消息旁边的人看不见
  * 读取按提交顺序，不漏晚提交的行
"""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from pydantic import ValidationError

from app.living.happening import (
    _scan,
    happening_seq_lock_key,
    read_directed_to,
    read_overheard_by,
    read_perceived_by,
    record_happening,
)
from app.living.place import Reach
from app.living.records import (
    MEDIUM_GROUP_CHAT,
    MEDIUM_IN_PERSON,
    MEDIUM_PHONE,
)
from app.living.serial import hold
from app.living.whereabouts import note_whereabouts

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))
_TEN_AM = dt.datetime(2026, 7, 25, 10, 0, tzinfo=_CST)


async def _stand(persona_id: str, place: str, *, moment: str = "m1") -> None:
    await note_whereabouts(
        lane=LANE,
        persona_id=persona_id,
        moment_id=moment,
        place=place,
        doing="待着",
        noted_at=_TEN_AM,
    )


async def _say(
    happening_id: str,
    *,
    actor: str = "akao",
    place: str = "家/客厅",
    content: str = "绫奈，周末一起去祭典吧",
    audience: tuple[str, ...] = (),
    medium: str = MEDIUM_IN_PERSON,
    occurred_at: dt.datetime = _TEN_AM,
):
    return await record_happening(
        lane=LANE,
        happening_id=happening_id,
        actor=actor,
        place=place,
        kind="speech",
        content=content,
        audience=audience,
        medium=medium,
        occurred_at=occurred_at,
    )


# --------------------------------------------------------------------------
# 一 · 定向送达
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_addressee_reads_the_words_even_when_her_place_is_wrong(living_db):
    """赤尾在客厅对绫奈说一句话——绫奈一定读到原话，哪怕位置数据是错的。"""
    await _stand("ayana", "学校")  # 位置数据错到够不着的地方
    await _say("h1", audience=("ayana",))

    window = await read_perceived_by(lane=LANE, persona_id="ayana", after_seq=0)

    assert len(window.items) == 1
    got = window.items[0]
    assert got.directed is True
    assert got.content == "绫奈，周末一起去祭典吧"


@pytest.mark.integration
async def test_addressee_reads_the_words_with_no_whereabouts_at_all(living_db):
    """从没写过位置（定位不到她）也一定送到——定向不查位置。"""
    await _say("h1", audience=("ayana",))

    window = await read_perceived_by(lane=LANE, persona_id="ayana", after_seq=0)

    assert [(i.directed, i.content) for i in window.items] == [
        (True, "绫奈，周末一起去祭典吧")
    ]


@pytest.mark.integration
async def test_read_directed_to_only_returns_words_aimed_at_her(living_db):
    await _stand("mio", "家/客厅")
    await _say("h1", audience=("ayana",))
    await _say("h2", content="哼着歌洗碗")

    directed = await read_directed_to(lane=LANE, persona_id="mio", after_seq=0)
    assert directed.items == []

    directed_ayana = await read_directed_to(
        lane=LANE, persona_id="ayana", after_seq=0
    )
    assert [i.happening_id for i in directed_ayana.items] == ["h1"]


# --------------------------------------------------------------------------
# 二 · 按位置旁听
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_third_person_in_the_same_room_reads_the_words(living_db):
    """同在客厅的第三人读到原话。"""
    await _stand("mio", "家/客厅")
    await _say("h1", audience=("ayana",))

    window = await read_perceived_by(lane=LANE, persona_id="mio", after_seq=0)

    assert len(window.items) == 1
    got = window.items[0]
    assert got.directed is False
    assert got.reach is Reach.SAME_PLACE
    assert got.content == "绫奈，周末一起去祭典吧"


@pytest.mark.integration
async def test_person_upstairs_behind_a_door_does_not_read_the_words(living_db):
    """在楼上关着门的读不到原话——只知道楼下有动静。"""
    await _stand("mio", "家/楼上/三妹房间")
    await _say("h1", audience=("ayana",))

    window = await read_perceived_by(lane=LANE, persona_id="mio", after_seq=0)

    assert len(window.items) == 1
    got = window.items[0]
    assert got.reach is Reach.SAME_BUILDING
    assert got.content is None
    assert got.actor == "akao"
    assert got.place == "家/客厅"


@pytest.mark.integration
async def test_person_out_of_the_building_perceives_nothing(living_db):
    await _stand("mio", "学校")
    await _say("h1", audience=("ayana",))

    window = await read_perceived_by(lane=LANE, persona_id="mio", after_seq=0)
    assert window.items == []


@pytest.mark.integration
async def test_actor_does_not_perceive_her_own_happening(living_db):
    """她自己说的话不该再回灌给她自己（回声）。"""
    await _stand("akao", "家/客厅")
    await _say("h1", actor="akao", audience=("ayana",))

    window = await read_perceived_by(lane=LANE, persona_id="akao", after_seq=0)
    assert window.items == []


@pytest.mark.integration
async def test_overheard_excludes_words_aimed_at_her(living_db):
    """定向给她的走定向那条路，旁听不重复给一遍。"""
    await _stand("ayana", "家/客厅")
    await _say("h1", audience=("ayana",))

    overheard = await read_overheard_by(lane=LANE, persona_id="ayana", after_seq=0)
    assert overheard.items == []

    both = await read_perceived_by(lane=LANE, persona_id="ayana", after_seq=0)
    assert [i.happening_id for i in both.items] == ["h1"]


@pytest.mark.integration
async def test_lane_isolation(living_db):
    await _stand("mio", "家/客厅")
    await record_happening(
        lane="prod",
        happening_id="h-prod",
        actor="akao",
        place="家/客厅",
        kind="speech",
        content="线上说的话",
        audience=("mio",),
        occurred_at=_TEN_AM,
    )

    window = await read_perceived_by(lane=LANE, persona_id="mio", after_seq=0)
    assert window.items == []


# --------------------------------------------------------------------------
# 二之二 · 旁听按「事情发生时她在不在场」判，不是按她读取时在哪
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_she_still_hears_what_was_said_while_she_was_in_the_room(living_db):
    """事情发生时她在客厅，之后上了楼——那句话仍然是她听见过的原话。

    按「读取时的最新位置」判会把这条反向裁掉：她在缝末换了房间，下一缝就用新
    位置去裁旧事件，在场的人漏听。事件可以在她整轮模型调用期间提交，所以这不是
    小概率——是每次她移动都可能发生。
    """
    await _stand("mio", "家/客厅", moment="m1")
    await _say("h1", content="哼着歌洗碗")

    # 缝末她上楼了（事情早就发生过了）
    await _stand("mio", "家/楼上/三妹房间", moment="m2")

    window = await read_perceived_by(lane=LANE, persona_id="mio", after_seq=0)
    assert [(i.reach, i.content) for i in window.items] == [
        (Reach.SAME_PLACE, "哼着歌洗碗")
    ]


@pytest.mark.integration
async def test_someone_who_walked_in_afterwards_did_not_overhear_it(living_db):
    """反过来：事情发生时她在学校，后来才回客厅——她没听见，也不该补听。"""
    await _stand("mio", "学校", moment="m1")
    await _say("h1", content="哼着歌洗碗")
    await _stand("mio", "家/客厅", moment="m2")

    window = await read_perceived_by(lane=LANE, persona_id="mio", after_seq=0)
    assert window.items == []


@pytest.mark.integration
async def test_the_same_happening_reads_the_same_no_matter_when_it_is_read(living_db):
    """同一条记录反复读，裁剪结果必须字字一样——中间她走遍全屋也不变。"""
    await _stand("mio", "家/客厅", moment="m1")
    await _say("h1", content="哼着歌洗碗")

    def _shape(window):
        return [(i.happening_id, i.reach, i.content) for i in window.items]

    first = _shape(await read_perceived_by(lane=LANE, persona_id="mio", after_seq=0))

    for i, place in enumerate(("家/楼上/三妹房间", "学校", "家/厨房"), start=2):
        await _stand("mio", place, moment=f"m{i}")
        again = await read_perceived_by(lane=LANE, persona_id="mio", after_seq=0)
        assert _shape(again) == first, f"她走到 {place} 之后同一条记录被裁成了别的样子"


# --------------------------------------------------------------------------
# 二之三 · 「说给谁」可以是好几个人
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_one_line_can_be_said_to_two_sisters_at_once(living_db):
    """同时对两个姐妹说话是一件事，不是复制两条事件。"""
    await _stand("ayana", "学校")
    await _stand("mio", "家/楼上/三妹房间")
    await _say("h1", audience=("ayana", "mio"), content="我买了三份抹茶")

    for who in ("ayana", "mio"):
        window = await read_perceived_by(lane=LANE, persona_id=who, after_seq=0)
        assert [(i.happening_id, i.directed, i.content) for i in window.items] == [
            ("h1", True, "我买了三份抹茶")
        ], who


@pytest.mark.integration
async def test_a_bystander_can_tell_who_the_words_were_said_to(living_db):
    """旁听的人要看得出这句是对谁说的，否则渲染不出「赤尾对绫奈说」。"""
    await _stand("mio", "家/客厅")
    await _say("h1", audience=("ayana",))

    window = await read_perceived_by(lane=LANE, persona_id="mio", after_seq=0)
    got = window.items[0]
    assert got.audience == ("ayana",)
    assert got.directed is False


@pytest.mark.integration
async def test_an_act_with_nobody_in_particular_has_an_empty_audience(living_db):
    await _stand("mio", "家/客厅")
    await _say("h1", content="哼着歌洗碗")

    window = await read_perceived_by(lane=LANE, persona_id="mio", after_seq=0)
    assert window.items[0].audience == ()


# --------------------------------------------------------------------------
# 二之四 · 渠道是客观事实：当面说 / 手机 / 群聊
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_phone_message_is_not_overheard_by_someone_in_the_same_room(living_db):
    """她坐在旁边也看不见别人手机上的字——这是物理事实，不是给行为分优先级。"""
    await _stand("mio", "家/客厅")
    await _stand("ayana", "学校")
    await _say("h1", medium=MEDIUM_PHONE, audience=("ayana",), content="到家了吗")

    assert (
        await read_perceived_by(lane=LANE, persona_id="mio", after_seq=0)
    ).items == []

    window = await read_perceived_by(lane=LANE, persona_id="ayana", after_seq=0)
    assert [(i.medium, i.directed, i.content) for i in window.items] == [
        (MEDIUM_PHONE, True, "到家了吗")
    ]


@pytest.mark.integration
async def test_a_group_chat_line_reaches_its_audience_and_nobody_else(living_db):
    """T4 要对飞书群说话——那是群聊这个渠道，不能借位置旁听混过去。"""
    await _stand("mio", "家/客厅")
    await _say(
        "h1", medium=MEDIUM_GROUP_CHAT, audience=("ayana",), content="今晚谁做饭"
    )

    assert (
        await read_perceived_by(lane=LANE, persona_id="mio", after_seq=0)
    ).items == []
    window = await read_perceived_by(lane=LANE, persona_id="ayana", after_seq=0)
    assert [i.medium for i in window.items] == [MEDIUM_GROUP_CHAT]


@pytest.mark.integration
async def test_speaking_out_loud_is_the_default_medium(living_db):
    await _stand("mio", "家/客厅")
    await _say("h1", content="哼着歌洗碗")

    window = await read_perceived_by(lane=LANE, persona_id="mio", after_seq=0)
    assert window.items[0].medium == MEDIUM_IN_PERSON


# --------------------------------------------------------------------------
# 三 · 按提交顺序读，不漏晚提交的行
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_cursor_does_not_skip_a_row_whose_occurred_at_is_earlier(living_db):
    """按发生时间开窗会永久越过它；按提交序读一定读得到。"""
    await _stand("mio", "家/客厅")

    await _say("late-in-time", occurred_at=_TEN_AM)
    first = await read_perceived_by(lane=LANE, persona_id="mio", after_seq=0)
    assert [i.happening_id for i in first.items] == ["late-in-time"]

    # 后落库、但「发生时间」比上一条早——时间窗读者会把它越过去
    await _say("early-in-time", occurred_at=_TEN_AM - dt.timedelta(hours=1))

    second = await read_perceived_by(
        lane=LANE, persona_id="mio", after_seq=first.next_cursor
    )
    assert [i.happening_id for i in second.items] == ["early-in-time"]


@pytest.mark.integration
async def test_concurrent_appends_produce_a_contiguous_seq_prefix(living_db):
    """并发写不会撞号、不会留洞——可见的 seq 永远是一段连续前缀。

    这是「游标读不漏」的根据：若能出现「seq 7 已可见、seq 6 还在飞」，
    游标推到 7 之后 6 就永久丢了。
    """
    rows = await asyncio.gather(
        *(_say(f"h{i}", occurred_at=_TEN_AM) for i in range(5))
    )
    assert sorted(r.seq for r in rows) == [1, 2, 3, 4, 5]


@pytest.mark.integration
async def test_a_pending_append_is_not_jumped_over_by_the_cursor(living_db):
    """一条尚未落定的记录被读者跳过 = 永久丢失。占用未放开时它不可能被越过。"""
    await _stand("mio", "家/客厅")
    await _say("settled")

    async with hold(happening_seq_lock_key(LANE)):
        # 这条记录正在排队等号，还没落库
        pending = asyncio.create_task(_say("pending"))
        await asyncio.sleep(0.1)

        window = await read_perceived_by(lane=LANE, persona_id="mio", after_seq=0)
        assert [i.happening_id for i in window.items] == ["settled"]
        cursor = window.next_cursor

    await pending

    later = await read_perceived_by(
        lane=LANE, persona_id="mio", after_seq=cursor
    )
    assert [i.happening_id for i in later.items] == ["pending"]


@pytest.mark.integration
async def test_the_row_is_committed_before_the_occupation_is_handed_over(living_db):
    """锁内提交：下一个拿到占用的人一定看得见上一个人写的行。

    换成进程内锁之后这条必须单独证——旧版靠 pg 的锁连接顺带说明，现在锁和
    数据库彻底无关，"占用放开前该行已可见"只剩 ``append_in_commit_order``
    自己在占用里 commit 这一条依据。它是「游标不会越过在飞的记录」的全部根据：
    若能出现「占用已放开、行还没提交」，下一个人读到的最大 seq 就会把它跳过去。
    """
    key = happening_seq_lock_key(LANE)

    async with hold(key):
        # 我们占着，writer 只能在门外排队：一步也不许往前走
        writer = asyncio.create_task(_say("queued"))
        await asyncio.sleep(0.1)
        assert not writer.done(), "占用没拦住它 —— 互斥根本没生效"
        rows, _ = await _scan(lane=LANE, after_seq=0, limit=100)
        assert rows == [], "排队中的写入已经可见 —— 它连号都还没取到"

    # FIFO：writer 排在我们前面，所以这次拿到占用时它的临界区已经走完
    async with hold(key):
        rows, _ = await _scan(lane=LANE, after_seq=0, limit=100)
        assert [h.happening_id for h in rows] == ["queued"], (
            "占用已经交接，但行还没提交 —— 游标读者会把它永久越过去"
        )

    await writer


@pytest.mark.integration
async def test_each_row_is_read_exactly_once_as_the_cursor_advances(living_db):
    await _stand("mio", "家/客厅")
    for i in range(4):
        await _say(f"h{i}")

    seen: list[str] = []
    cursor = 0
    for _ in range(4):
        window = await read_perceived_by(
            lane=LANE, persona_id="mio", after_seq=cursor, limit=2
        )
        seen.extend(i.happening_id for i in window.items)
        cursor = window.next_cursor

    assert seen == ["h0", "h1", "h2", "h3"]


@pytest.mark.integration
async def test_replaying_the_same_happening_id_does_not_duplicate(living_db):
    """durable 重投 / 工具重试用同一 happening_id 再写一次——只落一行。

    而且重放**不换听众**：在场快照以第一次写入的为准。第二次重投时她可能已经走了，
    让重投改写快照等于"同一件事因为被重投过一次，当时在场的人就没听见"。
    """
    await _stand("mio", "家/客厅", moment="m1")
    first = await _say("h1")

    await _stand("mio", "学校", moment="m2")  # 重投之前她已经出门了
    replayed = await _say("h1")

    assert replayed.seq == first.seq
    assert replayed.who_was_where == {"mio": "家/客厅"}

    window = await read_perceived_by(lane=LANE, persona_id="mio", after_seq=0)
    assert [i.happening_id for i in window.items] == ["h1"]


@pytest.mark.integration
async def test_empty_read_leaves_the_cursor_where_it_was(living_db):
    await _stand("mio", "家/客厅")
    window = await read_perceived_by(lane=LANE, persona_id="mio", after_seq=7)
    assert window.items == []
    assert window.next_cursor == 7


# --------------------------------------------------------------------------
# 四 · kind / medium 是机制层硬定的枚举，不是自由字符串
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_an_unknown_medium_is_rejected_at_write(living_db):
    """``"in-person"`` 这种手滑必须当场炸。

    它落库之后的表现是静默的：``medium != MEDIUM_IN_PERSON`` 这一支会把它当成
    "隔着设备"，同屋的人从此一句都听不见，而日志里什么都没有。
    """
    with pytest.raises(ValidationError):
        await _say("h1", medium="in-person")


@pytest.mark.integration
async def test_an_unknown_kind_is_rejected_at_write(living_db):
    with pytest.raises(ValidationError):
        await record_happening(
            lane=LANE,
            happening_id="h1",
            actor="akao",
            place="家/客厅",
            kind="thought",
            medium=MEDIUM_IN_PERSON,
            content="想了点事",
            occurred_at=_TEN_AM,
        )
