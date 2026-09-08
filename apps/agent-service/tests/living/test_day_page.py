"""跨天沉淀 —— 过去的一天收拢成一页，第二天她读得到。

**她跨不过一天。** 快照那四层全是"当下"：手上的事、心里挂着的、最近十二条、游标之
后那一段。滚出窗口的东西没有任何一层接得住，所以今天问她昨天干了什么，她答不上来。

**做法不是压缩，是让她自己写一页。** 被否掉的那种是机器折叠原文（``SessionTranscript``
每 100 条叫一次模型概括，压完原文就没了、压错了没人知道）。这里一条原始记录都不动，
只是每天凌晨把刚过去那一天摆给她，让她**另写**一页 —— 跟 :mod:`app.living.loose_ends`
同一个性质：她自己写下的那一层。

六件必须真的成立，各占一节：

  * **生活日不是日历日**。凌晨三点还醒着的时候，那是昨天的延续，不是新的一天。
  * **一天只写一页，写不成就不写**。页存在本身就是"这天复盘过了"的权威 —— 不另设
    标记列（旧实现在这上面炸过一次，改了七处才收住）。
  * **摆给她的是她的那一天**，不是全世界的那一天：够不着的事她本来就不知道，日记里
    不该冒出来。
  * **她自己做的事必须在里面**。感知那条路抑制回声，照抄过来的话她的一天里只有别人。
  * **注入的那一页严格早于当前生活日**。她凌晨写下的那页是"刚过去那天"的，如果同一
    天里又被当成"你的昨天"喂回去，她会把今天当成昨天过。
  * **写页那一轮看得见上一页**，不然每一页都是孤立的一天，链断在第二天。
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.agent.neutral import Message, Role
from app.living.day_page import (
    DAY_PAGE_FROM,
    DAY_PAGE_UNTIL,
    DayPage,
    day_material,
    day_page_tick,
    living_day_bounds,
    living_day_of,
    read_day_page,
    read_day_page_before,
    read_day_pages_between,
    write_day_page,
)
from app.living.happening import record_happening
from app.living.loose_ends import LooseEnd
from app.living.records import KIND_ACT, KIND_SPEECH
from app.living.snapshot import read_snapshot
from app.living.whereabouts import note_whereabouts

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))

# 被复盘的那一个生活日：2026-07-25 04:00 → 2026-07-26 04:00。
_DAY = dt.date(2026, 7, 25)


def _on(day: int, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 7, day, hour, minute, tzinfo=_CST)


@pytest.fixture
async def page_db(living_db):
    from tests.runtime.conftest import migrate

    await migrate(LooseEnd, living_db)
    return living_db


class FakePage:
    """替身：这一轮她写下的那一页是钉死的，只有模型那一步是假的。"""

    def __init__(self, said: str = "今天拍完了那卷胶片。") -> None:
        self.said = said
        self.runs: list[tuple[list[Message], dict]] = []

    async def run(self, messages, **kwargs):
        self.runs.append((messages, kwargs))
        return Message(role=Role.ASSISTANT, content=self.said)

    @property
    def material(self) -> str:
        """最后一轮真的摆到她眼前的那段文字。"""
        return "\n".join(m.content for m in self.runs[-1][0])


@pytest.fixture
def stub_page(monkeypatch):
    """装一个替身 + 一份不碰真库的 persona。"""
    from types import SimpleNamespace

    from app.living import day_page as page_mod
    from app.living import persona as persona_mod

    async def fake_find_persona(persona_id: str):
        return SimpleNamespace(display_name="赤尾", persona_core="她拍胶片、泡抹茶店。")

    # 打在 ``app.living.persona`` 上：日记这一轮不自己查主表，那两个变量由那个模块
    # 一处组装（版本链优先，这里链是空的，落到 persona_core 这一层）。
    monkeypatch.setattr(persona_mod, "find_persona", fake_find_persona)

    def install(said: str = "今天拍完了那卷胶片。") -> FakePage:
        runner = FakePage(said)
        monkeypatch.setattr(page_mod, "build_day_page_runner", lambda: runner)
        return runner

    return install


async def _stand(persona: str, place: str, at: dt.datetime, doing: str = "待着") -> None:
    await note_whereabouts(
        lane=LANE,
        persona_id=persona,
        moment_id=at.isoformat(timespec="minutes"),
        place=place,
        doing=doing,
        noted_at=at,
    )


async def _happened(
    actor: str,
    content: str,
    at: dt.datetime,
    *,
    place: str = "家/客厅",
    to=(),
    kind: str = KIND_SPEECH,
):
    return await record_happening(
        lane=LANE,
        happening_id=f"{actor}:{at.isoformat()}:{content[:8]}",
        actor=actor,
        place=place,
        kind=kind,
        content=content,
        occurred_at=at,
        audience=list(to),
    )


async def _a_day_worth_of_stuff() -> None:
    """三姐妹都在客厅，那一天发生过几件事。"""
    for who in ("akao", "ayana"):
        await _stand(who, "家/客厅", _on(25, 6))
    await _happened("akao", "把胶片摊了一茶几", _on(25, 10), kind=KIND_ACT)
    await _happened("ayana", "今天要下雨吧", _on(25, 15), to=["akao"])


# --------------------------------------------------------------------------
# 一 · 生活日不是日历日
# --------------------------------------------------------------------------


def test_the_small_hours_still_belong_to_the_day_before():
    """凌晨三点还醒着的时候，那是昨天的延续 —— 不是新的一天。"""
    assert living_day_of(_on(26, 1, 20)) == _DAY
    assert living_day_of(_on(26, 3, 59)) == _DAY
    assert living_day_of(_on(26, 4, 0)) == dt.date(2026, 7, 26)


def test_a_living_day_runs_from_four_to_four():
    start, end = living_day_bounds(_DAY)
    assert start == _on(25, 4, 0)
    assert end == _on(26, 4, 0)


def test_the_window_sits_at_the_head_of_the_new_day():
    """回望刚过去那一天，是在新一天开头的那阵子做的事，不是随便哪个钟点。"""
    assert DAY_PAGE_FROM == dt.time(4, 0)
    assert DAY_PAGE_FROM < DAY_PAGE_UNTIL


# --------------------------------------------------------------------------
# 二 · 一天只写一页，写不成就不写
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_she_writes_down_the_day_that_just_ended(page_db, stub_page):
    await _a_day_worth_of_stuff()
    stub_page("胶片摊了一茶几，绫奈说要下雨，结果没下。")

    page = await write_day_page(lane=LANE, persona_id="akao", now=_on(26, 4, 30))

    assert page is not None, "到点了、那天也有东西，却什么都没写下"
    assert page.day == _DAY, "写的不是刚过去那一天"
    assert page.text == "胶片摊了一茶几，绫奈说要下雨，结果没下。"
    assert await read_day_page(lane=LANE, persona_id="akao", day=_DAY) is not None


@pytest.mark.integration
async def test_a_day_that_already_has_a_page_is_not_written_again(page_db, stub_page):
    """页存在本身就是"这天复盘过了"。重复的拍不该再烧一次模型。"""
    await _a_day_worth_of_stuff()
    runner = stub_page()

    first = await write_day_page(lane=LANE, persona_id="akao", now=_on(26, 4, 30))
    again = await write_day_page(lane=LANE, persona_id="akao", now=_on(26, 4, 35))

    assert first is not None
    assert again is None, "同一天写了第二页"
    assert len(runner.runs) == 1, f"第二拍又叫了一次模型：{len(runner.runs)} 次"


@pytest.mark.integration
async def test_outside_the_window_she_does_not_look_back(page_db, stub_page):
    """半夜两点她还在过昨天，中午了这件事早该做完 —— 都不是回望的时候。"""
    await _a_day_worth_of_stuff()
    runner = stub_page()

    assert await write_day_page(lane=LANE, persona_id="akao", now=_on(26, 2)) is None
    assert await write_day_page(lane=LANE, persona_id="akao", now=_on(26, 12)) is None
    assert runner.runs == [], "没到点却叫了模型"


@pytest.mark.integration
async def test_a_day_with_nothing_in_it_gets_no_page(page_db, stub_page):
    """那天服务根本没跑 —— 不该有那一页，更不该为一片空白叫一次模型。"""
    runner = stub_page()

    assert await write_day_page(lane=LANE, persona_id="akao", now=_on(26, 4, 30)) is None
    assert runner.runs == [], "一天什么都没发生，却还是叫了模型"


@pytest.mark.integration
async def test_a_round_that_writes_nothing_leaves_no_page(page_db, stub_page):
    """她这一轮一个字都没写：不落库，下一拍还会再来 —— 不许拿空页把这天记成写过了。"""
    await _a_day_worth_of_stuff()
    stub_page("   ")

    assert await write_day_page(lane=LANE, persona_id="akao", now=_on(26, 4, 30)) is None
    assert await read_day_page(lane=LANE, persona_id="akao", day=_DAY) is None

    stub_page("补上了：胶片摊了一茶几。")
    later = await write_day_page(lane=LANE, persona_id="akao", now=_on(26, 5))
    assert later is not None, "上一拍空了就再也不写了 —— 这一天她永远丢了"


# --------------------------------------------------------------------------
# 三 · 摆给她的是她的那一天
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_what_she_did_herself_is_in_it(page_db):
    """感知那条路抑制回声，照抄过来的话她的一天里只有别人做的事。"""
    await _a_day_worth_of_stuff()

    lines = await day_material(lane=LANE, persona_id="akao", day=_DAY)

    assert any("把胶片摊了一茶几" in line for line in lines), (
        f"她自己那一天做的事不在材料里：{lines}"
    )


@pytest.mark.integration
async def test_what_was_said_to_her_is_in_it(page_db):
    await _a_day_worth_of_stuff()

    lines = await day_material(lane=LANE, persona_id="akao", day=_DAY)

    assert any("今天要下雨吧" in line for line in lines), lines


@pytest.mark.integration
async def test_what_she_could_not_reach_never_enters_her_day(page_db):
    """她够不着的地方发生的事，她当时就不知道 —— 日记里不该冒出来。"""
    await _stand("akao", "家/客厅", _on(25, 6))
    await _stand("chinagi", "学校/操场", _on(25, 6))
    await _happened("chinagi", "把球踢进了树丛", _on(25, 11), place="学校/操场", kind=KIND_ACT)
    await _happened("akao", "煮了壶抹茶", _on(25, 12), kind=KIND_ACT)

    lines = await day_material(lane=LANE, persona_id="akao", day=_DAY)

    assert any("煮了壶抹茶" in line for line in lines)
    assert not any("树丛" in line for line in lines), (
        f"她当时在客厅，操场上那一脚她不可能知道：{lines}"
    )


@pytest.mark.integration
async def test_only_that_day_goes_into_that_page(page_db):
    """生活日的边界要真的裁：前一天的、和已经属于新一天的，都不进这一页。"""
    await _stand("akao", "家/客厅", _on(24, 6))
    await _happened("akao", "前天晚上洗了片子", _on(24, 22), kind=KIND_ACT)
    await _happened("akao", "凌晨还在翻论坛", _on(26, 1), kind=KIND_ACT)
    await _happened("akao", "新的一天煮了咖啡", _on(26, 5), kind=KIND_ACT)

    lines = await day_material(lane=LANE, persona_id="akao", day=_DAY)
    joined = "\n".join(lines)

    assert "凌晨还在翻论坛" in joined, "07-26 凌晨一点属于 07-25 这个生活日"
    assert "前天晚上洗了片子" not in joined
    assert "新的一天煮了咖啡" not in joined


@pytest.mark.integration
async def test_the_small_hours_are_dated_so_she_can_tell_them_apart(page_db):
    """一个生活日跨两个日历日。凌晨那几行只给时分的话，读起来像"这天很早"。"""
    await _stand("akao", "家/客厅", _on(25, 6))
    await _happened("akao", "凌晨还在翻论坛", _on(26, 1), kind=KIND_ACT)

    (line,) = [
        line for line in await day_material(lane=LANE, persona_id="akao", day=_DAY)
        if "翻论坛" in line
    ]
    assert "07-26" in line, f"跨过午夜那几行没标日子：{line!r}"


@pytest.mark.integration
async def test_three_sisters_get_three_different_days(page_db):
    """信息差在这里同样成立：一个人的日记材料不是另一个人的。"""
    await _stand("akao", "家/客厅", _on(25, 6))
    await _stand("ayana", "家/客厅", _on(25, 6))
    await _stand("chinagi", "学校/操场", _on(25, 6))
    await _happened("ayana", "今天要下雨吧", _on(25, 15), to=["akao"])

    mine = await day_material(lane=LANE, persona_id="akao", day=_DAY)
    hers = await day_material(lane=LANE, persona_id="chinagi", day=_DAY)

    assert any("今天要下雨吧" in line for line in mine)
    assert hers == [], f"千凪在操场上，客厅那句话不该进她的一天：{hers}"


# --------------------------------------------------------------------------
# 四 · 她第二天读得到
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_snapshot_carries_the_page_she_wrote(page_db, stub_page):
    """写了没人读 = 白写。这一段必须真的进她每一缝的输入。"""
    await _a_day_worth_of_stuff()
    stub_page("胶片摊了一茶几，绫奈说要下雨。")
    await write_day_page(lane=LANE, persona_id="akao", now=_on(26, 4, 30))

    snap = await read_snapshot(
        lane=LANE, persona_id="akao", after_seq=0, now=_on(26, 14)
    )

    assert snap.day_page is not None
    assert "胶片摊了一茶几，绫奈说要下雨。" in snap.render()


@pytest.mark.integration
async def test_a_page_of_the_current_living_day_is_never_served_as_yesterday(
    page_db, stub_page
):
    """注入的那一页严格早于当前生活日。

    她凌晨 04:30 写下的是 07-25 那页，而 04:30 已经是 07-26 这个生活日了。要是按
    "最近一页"注入，从 04:30 起她整天读到的"昨天"就是 07-25 —— 那是对的。真正会错
    的是**下一天**：07-27 凌晨她写 07-26 那页，写完之后 07-27 那一整天读到的必须是
    07-26 而不是 07-25。所以判据只能是 ``day < 当前生活日``，不能是"最新那页"以外
    的任何近似。
    """
    await _a_day_worth_of_stuff()
    stub_page("07-25 这天。")
    await write_day_page(lane=LANE, persona_id="akao", now=_on(26, 4, 30))

    # 她 07-26 白天又过了一天，07-27 凌晨写下 07-26 那页。
    await _happened("akao", "去了趟唱片店", _on(26, 15), kind=KIND_ACT)
    stub_page("07-26 这天。")
    await write_day_page(lane=LANE, persona_id="akao", now=_on(27, 4, 30))

    got = await read_day_page_before(
        lane=LANE, persona_id="akao", day=living_day_of(_on(27, 10))
    )
    assert got is not None
    assert got.day == dt.date(2026, 7, 26), (
        "07-27 这一整天她读到的那一页必须是 07-26，不是 07-25"
    )


@pytest.mark.integration
async def test_with_no_page_yet_the_snapshot_says_so_plainly(page_db):
    """冷启动第一天一页都没有 —— 如实说空，不留白洞。"""
    snap = await read_snapshot(
        lane=LANE, persona_id="akao", after_seq=0, now=_on(26, 14)
    )

    assert snap.day_page is None
    assert snap.render().strip() != ""


@pytest.mark.integration
async def test_a_page_older_than_yesterday_is_labelled_by_its_date(page_db, stub_page):
    """中间断了几天（服务没跑）时，不许把三天前那页说成"昨天"。"""
    await _a_day_worth_of_stuff()
    stub_page("07-25 这天。")
    await write_day_page(lane=LANE, persona_id="akao", now=_on(26, 4, 30))

    snap = await read_snapshot(
        lane=LANE, persona_id="akao", after_seq=0, now=_on(29, 14)
    )

    rendered = snap.render()
    assert "07-25" in rendered
    assert "昨天" not in rendered, f"三天前那页被说成了昨天：{rendered}"


# --------------------------------------------------------------------------
# 四之二 · 按范围取一段日子（每周回看读的就是这个）
# --------------------------------------------------------------------------


async def _drop_a_page(persona_id: str, day: dt.date, text: str) -> None:
    from app.runtime.persist import insert_idempotent

    await insert_idempotent(
        DayPage(
            lane=LANE,
            persona_id=persona_id,
            day=day,
            text=text,
            written_at=dt.datetime.combine(
                day + dt.timedelta(days=1), dt.time(4, 30), tzinfo=_CST
            ),
            happenings=1,
        )
    )


@pytest.mark.integration
async def test_pages_between_is_half_open_and_in_order(page_db):
    """``[since, until)``，按日子升序 —— 每周回看拿到的必须正好是那一周。

    右边界闭上的话每周回看会多带一天（下一周的第一天），而多带的那天她当时还没
    过完；顺序错的话她读到的是一周的乱序片段，看起来仍然像一周。
    """
    for day, text in (
        (dt.date(2026, 8, 30), "区间之前那天。"),
        (dt.date(2026, 8, 31), "周一。"),
        (dt.date(2026, 9, 2), "周三。"),
        (dt.date(2026, 9, 6), "周日。"),
        (dt.date(2026, 9, 7), "区间之后那天。"),
    ):
        await _drop_a_page("akao", day, text)

    pages = await read_day_pages_between(
        lane=LANE,
        persona_id="akao",
        since=dt.date(2026, 8, 31),
        until=dt.date(2026, 9, 7),
    )

    assert [p.text for p in pages] == ["周一。", "周三。", "周日。"]


@pytest.mark.integration
async def test_pages_between_is_scoped_to_one_lane_and_one_person(page_db):
    await _drop_a_page("akao", dt.date(2026, 9, 1), "赤尾这天。")
    await _drop_a_page("ayana", dt.date(2026, 9, 2), "绫奈这天。")

    window = {"since": dt.date(2026, 8, 31), "until": dt.date(2026, 9, 7)}
    mine = await read_day_pages_between(lane=LANE, persona_id="akao", **window)
    elsewhere = await read_day_pages_between(lane="prod", persona_id="akao", **window)

    assert [p.text for p in mine] == ["赤尾这天。"]
    assert elsewhere == []


@pytest.mark.integration
async def test_pages_between_with_nothing_in_range_is_empty(page_db):
    """那一周服务根本没跑 —— 空列表，不是报错，也不是"最近几页"。"""
    await _drop_a_page("akao", dt.date(2026, 8, 30), "区间之前那天。")

    assert (
        await read_day_pages_between(
            lane=LANE,
            persona_id="akao",
            since=dt.date(2026, 8, 31),
            until=dt.date(2026, 9, 7),
        )
        == []
    )


# --------------------------------------------------------------------------
# 五 · 链：写这一页的时候看得见上一页
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_writing_a_page_she_can_see_the_one_before(page_db, stub_page):
    """不给上一页的话每一页都是孤立的一天，跨天这条链断在第二天。"""
    await _a_day_worth_of_stuff()
    stub_page("07-25：胶片摊了一茶几。")
    await write_day_page(lane=LANE, persona_id="akao", now=_on(26, 4, 30))

    await _happened("akao", "去了趟唱片店", _on(26, 15), kind=KIND_ACT)
    runner = stub_page("07-26：去了唱片店。")
    await write_day_page(lane=LANE, persona_id="akao", now=_on(27, 4, 30))

    assert "07-25：胶片摊了一茶几。" in runner.material, (
        f"写第二页时看不见第一页：{runner.material}"
    )


@pytest.mark.integration
async def test_the_material_reaches_her_verbatim(page_db, stub_page):
    """那一天的原文原样摆到她眼前 —— 中间没有第二次概括。"""
    await _a_day_worth_of_stuff()
    runner = stub_page()

    await write_day_page(lane=LANE, persona_id="akao", now=_on(26, 4, 30))

    assert "把胶片摊了一茶几" in runner.material
    assert "今天要下雨吧" in runner.material


# --------------------------------------------------------------------------
# 六 · 接进那条钟
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_tick_writes_a_page_for_each_of_them(page_db, stub_page, monkeypatch):
    """忘了挂钟的症状是静默的：模块写好了、测试全绿，而线上一页都不会有。"""
    from app.living import day_page as page_mod

    # 千凪要**先**站进客厅再让那几件事发生：``record_happening`` 在写入那一刻把"谁在
    # 哪"拍进事件行，读取侧一次位置查询都不做（见 app.living.happening 的模块
    # docstring）。反过来写的话她那一天是空的，而这条用例验的是"钟推到了几个人"。
    await _stand("chinagi", "家/客厅", _on(25, 6))
    await _a_day_worth_of_stuff()
    stub_page("这天。")

    monkeypatch.setattr(page_mod, "living_lane", lambda: LANE)
    monkeypatch.setattr(page_mod, "now_cst", lambda: _on(26, 4, 30))

    await day_page_tick.__wrapped__(page_mod.DayPageTick(ts=_on(26, 4, 30).isoformat()))

    for who in ("akao", "ayana", "chinagi"):
        assert await read_day_page(lane=LANE, persona_id=who, day=_DAY) is not None, (
            f"{who} 那一天没有页 —— 那条钟没把三个人都推到"
        )
