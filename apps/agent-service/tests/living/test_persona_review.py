"""每周回看 —— 她读上一周自己写的那几页，重写一版「我是谁」。

跨天沉淀让她记得住昨天；这一层是**她会因为过去这一周而变**。链上最新一版就是她每
一轮、写日记时读到的自己（``persona_prompt_vars``），所以这里写下的东西下一
分钟就在她眼前。

七件必须真的成立，各占一节：

  * **一周只写一版**。判据是链上本周有没有 ``source='review'`` 的版本，不另设标记。
  * **能不调模型就不调**：不在窗口、本周写过了、上一周一页日记都没有，三种都在模型
    前面返回。
  * **正文空不落版本**。拿一版空白把这周记成"写过了"是最坏的一种：这一周她永远丢
    了，而且看起来一切正常。
  * **取的是上一个完整自然周**，不是本周。周一早上跑的时候取的是上周一到上周日。
  * **链空先 seed 一版**，内容是她在有自己的版本之前实际读到的那份
    （``bot_persona.persona_core``）。不然链上的历史是断的：v1 记的是她从没读过的
    东西。
  * **材料里三样都在**：上周的日记原文、她当前那一版、外部锚。
  * **锚是 ``bot_persona.persona_core`` 那个扁平列，不是链上最新版**。她每周基于自
    己上一版改自己，没有一个不参与循环的外部参照，几个月就漂到没边——而且没有任何
    报错。这一节是防止以后有人来"统一"掉那个锚的门。
"""
from __future__ import annotations

import datetime as dt

import pytest

import app.data.session as session_mod
from app.agent.neutral import Message, Role
from app.living.day_page import LivingDayPage
from app.living.persona import (
    PersonaVersion,
    read_latest_persona_version,
    write_persona_version,
)
from app.living.persona_review import (
    PERSONA_REVIEW_FROM,
    PERSONA_REVIEW_UNTIL,
    last_full_week,
    persona_review_tick,
    review_persona,
    week_material,
)
from app.runtime.persist import insert_idempotent, select_all_versions

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


@pytest.fixture
async def review_db(living_db):
    """``living_db`` 建齐了 ``LivingDayPage`` / ``PersonaVersion``，再补 ``bot_persona``。

    ``bot_persona`` 是外部锚和 seed 的来源，这一轮**真的去查它**（不 monkeypatch
    ``find_persona``）：这个文件里最要紧的一条断言是"喂进 prompt 的锚等于那个扁平列
    的原文"，把查询替掉的话这条断言验的就只是替身自己。
    """
    from app.data.models import Base, BotPersona

    async with living_db.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(c, tables=[BotPersona.__table__])
        )
    async with session_mod.get_session() as s:
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


class FakeReview:
    """替身：这一轮她写下的那一版是钉死的，只有模型那一步是假的。"""

    def __init__(self, said: str) -> None:
        self.said = said
        self.runs: list[tuple[list[Message], dict]] = []

    async def run(self, messages, **kwargs):
        self.runs.append((messages, kwargs))
        return Message(role=Role.ASSISTANT, content=self.said)

    @property
    def material(self) -> str:
        """最后一轮真的摆到她眼前的那段文字。"""
        return "\n".join(m.content for m in self.runs[-1][0])

    @property
    def prompt_vars(self) -> dict[str, str]:
        """最后一轮真的传进去的那几个 prompt 变量。"""
        return self.runs[-1][1]["prompt_vars"]


@pytest.fixture
def stub_review(monkeypatch):
    def install(said: str = "她这周开始每天写一页，好像有点上瘾。") -> FakeReview:
        runner = FakeReview(said)
        from app.living import persona_review as review_mod

        monkeypatch.setattr(
            review_mod, "build_persona_review_runner", lambda: runner
        )
        return runner

    return install


async def _page(persona_id: str, day: dt.date, text: str, *, lane: str = LANE) -> None:
    """直接落一页日记（这个文件验的不是日记怎么写出来的）。"""
    await insert_idempotent(
        LivingDayPage(
            lane=lane,
            persona_id=persona_id,
            day=day,
            text=text,
            written_at=dt.datetime.combine(
                day + dt.timedelta(days=1), dt.time(4, 30), tzinfo=_CST
            ),
            happenings=3,
        )
    )


async def _a_week_of_pages(persona_id: str = "akao", *, lane: str = LANE) -> None:
    """上一个完整自然周那七天，每天一页。"""
    for i in range(7):
        day = _LAST_MONDAY + dt.timedelta(days=i)
        await _page(persona_id, day, f"{day.strftime('%m-%d')} 这天：拍了几张。", lane=lane)


# --------------------------------------------------------------------------
# 一 · 取的是上一个完整自然周
# --------------------------------------------------------------------------


def test_the_window_sits_after_the_diary_is_written():
    """回看排在日记之后：日记 04:00–06:00 写完，上一周最后一天那页才存在。"""
    assert PERSONA_REVIEW_FROM == dt.time(6, 0)
    assert PERSONA_REVIEW_FROM < PERSONA_REVIEW_UNTIL


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
async def test_only_last_weeks_pages_are_material(review_db):
    """本周的、上上周的都不进材料 —— 半开区间必须真的裁。"""
    await _page("akao", dt.date(2026, 8, 30), "上上周日那页。")
    await _a_week_of_pages()
    await _page("akao", dt.date(2026, 9, 7), "本周一那页。")

    pages = await week_material(
        lane=LANE, persona_id="akao", since=_LAST_MONDAY, until=_THIS_MONDAY
    )

    assert [p.day for p in pages] == [
        _LAST_MONDAY + dt.timedelta(days=i) for i in range(7)
    ], f"取回来的不是那七天、或者没按日子升序：{[p.day for p in pages]}"


# --------------------------------------------------------------------------
# 二 · 一周只写一版
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_she_rewrites_who_she_is_from_last_week(review_db, stub_review):
    await _a_week_of_pages()
    stub_review("她还是那个拍胶片的人，只是这周开始在意起自己写的东西了。")

    got = await review_persona(lane=LANE, persona_id="akao", now=_NOW)

    assert got is not None, "到点了、上周也有日记，却什么都没写下"
    assert got.source == "review", "来源必须仍然是 review —— prod 那 38 行认的就是它"
    assert got.narrative == "她还是那个拍胶片的人，只是这周开始在意起自己写的东西了。"

    latest = await read_latest_persona_version(lane=LANE, persona_id="akao")
    assert latest.narrative == got.narrative, "写下的那一版没有成为她读到的自己"


@pytest.mark.integration
async def test_a_week_already_reviewed_is_not_reviewed_again(review_db, stub_review):
    """本周已经有 review 版 → 不写第二版，而且一次模型都不叫。"""
    await _a_week_of_pages()
    runner = stub_review()

    first = await review_persona(lane=LANE, persona_id="akao", now=_NOW)
    again = await review_persona(lane=LANE, persona_id="akao", now=_at(7, 0))

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
    await _a_week_of_pages()
    stub_review("周二才补上的这一版。")

    tuesday = dt.datetime(2026, 9, 8, 6, 30, tzinfo=_CST)
    got = await review_persona(lane=LANE, persona_id="akao", now=tuesday)

    assert got is not None and got.source == "review"


@pytest.mark.integration
async def test_once_written_the_rest_of_the_week_does_nothing(review_db, stub_review):
    """周一写过之后，这一周剩下每一天的那一拍都不再叫模型。"""
    await _a_week_of_pages()
    runner = stub_review()
    await review_persona(lane=LANE, persona_id="akao", now=_NOW)

    for day in (8, 9, 10, 11, 12, 13):
        at = dt.datetime(2026, 9, day, 6, 30, tzinfo=_CST)
        assert await review_persona(lane=LANE, persona_id="akao", now=at) is None

    assert len(runner.runs) == 1, f"这一周叫了 {len(runner.runs)} 次模型"


@pytest.mark.integration
async def test_an_owner_version_this_week_does_not_block_the_review(
    review_db, stub_review
):
    """bezhai 本周盖过版不挡自动班 —— 幂等只认 ``source='review'``。"""
    await _a_week_of_pages()
    await write_persona_version(
        lane=LANE,
        persona_id="akao",
        narrative="bezhai 本周手写的一版。",
        source="owner",
        written_at="2026-09-07T05:00:00+08:00",
    )
    stub_review("她自己写的这一版。")

    got = await review_persona(lane=LANE, persona_id="akao", now=_NOW)

    assert got is not None and got.source == "review"


# --------------------------------------------------------------------------
# 三 · 能不调模型就不调
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_outside_the_window_she_does_not_look_back(review_db, stub_review):
    """凌晨三点日记还没写完，中午了这件事早该做完 —— 都不是回看的时候。"""
    await _a_week_of_pages()
    runner = stub_review()

    assert await review_persona(lane=LANE, persona_id="akao", now=_at(3)) is None
    assert await review_persona(lane=LANE, persona_id="akao", now=_at(12)) is None
    assert runner.runs == [], "没到点却叫了模型"
    assert await read_latest_persona_version(lane=LANE, persona_id="akao") is None


@pytest.mark.integration
async def test_a_week_with_no_pages_gets_no_version(review_db, stub_review):
    """上一周一页日记都没有：那是服务根本没跑的一周，不该有那一版。"""
    runner = stub_review()

    assert await review_persona(lane=LANE, persona_id="akao", now=_NOW) is None
    assert runner.runs == [], "一周一页日记都没有，却还是叫了模型"
    assert await read_latest_persona_version(lane=LANE, persona_id="akao") is None, (
        "没材料的一周连 seed 都不该留下 —— 那一版记的是没人读过的东西"
    )


@pytest.mark.integration
async def test_a_round_that_writes_nothing_leaves_no_version(review_db, stub_review):
    """她这一轮一个字都没写：不落版本，下一拍还在窗口里就再来一次。"""
    await _a_week_of_pages()
    stub_review("   ")

    assert await review_persona(lane=LANE, persona_id="akao", now=_NOW) is None

    versions = await select_all_versions(
        PersonaVersion, {"lane": LANE, "persona_id": "akao"}
    )
    assert [v.source for v in versions] == ["seed"], (
        f"空正文落成了版本，这一周被记成写过了：{[v.source for v in versions]}"
    )

    stub_review("补上了：她这周开始每天写一页。")
    later = await review_persona(lane=LANE, persona_id="akao", now=_at(7, 0))
    assert later is not None, "上一拍空了就再也不写了 —— 这一周她永远丢了"
    assert later.narrative == "补上了：她这周开始每天写一页。"


# --------------------------------------------------------------------------
# 四 · 链空先 seed 一版
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_an_empty_chain_gets_her_starting_point_first(review_db, stub_review):
    """v1 记的必须是她在有自己的版本之前实际读到的那份，不然链上的历史是断的。"""
    await _a_week_of_pages()
    stub_review("她这周开始每天写一页。")

    await review_persona(lane=LANE, persona_id="akao", now=_NOW)

    versions = await select_all_versions(
        PersonaVersion, {"lane": LANE, "persona_id": "akao"}
    )
    assert [v.source for v in versions] == ["seed", "review"]
    assert versions[0].narrative == _CORE, (
        "seed 那一版不是她链空时实际读到的那份（``persona_core``），链上的历史断了"
    )


@pytest.mark.integration
async def test_a_chain_that_already_has_versions_is_not_seeded(review_db, stub_review):
    """链非空（prod 就是这样）→ 不补 seed，直接在上一版基础上改。"""
    await _a_week_of_pages()
    await write_persona_version(
        lane=LANE,
        persona_id="akao",
        narrative="上一版：她在准备去日本读书。",
        source="review",
        written_at="2026-08-31T06:30:00+08:00",
    )
    stub_review("这一版：她把语言学校的事定下来了。")

    await review_persona(lane=LANE, persona_id="akao", now=_NOW)

    versions = await select_all_versions(
        PersonaVersion, {"lane": LANE, "persona_id": "akao"}
    )
    assert [v.source for v in versions] == ["review", "review"]


# --------------------------------------------------------------------------
# 五 · 材料里三样都在
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_last_weeks_pages_reach_her_verbatim(review_db, stub_review):
    """上周那几页原文照搬 —— 中间没有第二次概括。"""
    await _page("akao", _LAST_MONDAY, "周一：把胶片摊了一茶几。")
    await _page("akao", dt.date(2026, 9, 6), "周日：去了趟唱片店，买了张旧碟。")
    runner = stub_review()

    await review_persona(lane=LANE, persona_id="akao", now=_NOW)

    assert "把胶片摊了一茶几" in runner.material
    assert "买了张旧碟" in runner.material


@pytest.mark.integration
async def test_her_current_version_is_the_draft_she_rewrites(review_db, stub_review):
    """她当前那一版是底稿。不给的话她是凭空写一份，不是"改"。"""
    await _a_week_of_pages()
    await write_persona_version(
        lane=LANE,
        persona_id="akao",
        narrative="当前这一版：她在准备去日本读书。",
        source="review",
        written_at="2026-08-31T06:30:00+08:00",
    )
    runner = stub_review()

    await review_persona(lane=LANE, persona_id="akao", now=_NOW)

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
    await _a_week_of_pages()
    await write_persona_version(
        lane=LANE,
        persona_id="akao",
        narrative="链上最新那一版：她今年不拍胶片了。",
        source="review",
        written_at="2026-08-31T06:30:00+08:00",
    )
    runner = stub_review()

    await review_persona(lane=LANE, persona_id="akao", now=_NOW)

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
    await _a_week_of_pages()
    runner = stub_review()

    await review_persona(lane=LANE, persona_id="akao", now=_NOW)

    assert set(runner.prompt_vars) == {"persona_name", "persona_core"}


# --------------------------------------------------------------------------
# 六 · 泳道隔离
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_another_lanes_pages_are_not_her_week(review_db, stub_review):
    """prod 那条链上的日记不进 coe 这一轮 —— 漏了 lane 的后果是双向的。"""
    await _a_week_of_pages("akao", lane="prod")
    runner = stub_review()

    assert await review_persona(lane=LANE, persona_id="akao", now=_NOW) is None
    assert runner.runs == [], "读到了别的泳道的日记"
    assert await read_latest_persona_version(lane="prod", persona_id="akao") is None


@pytest.mark.integration
async def test_the_new_version_lands_only_on_this_lane(review_db, stub_review):
    await _a_week_of_pages()
    stub_review("coe 里改出来的一版。")

    await review_persona(lane=LANE, persona_id="akao", now=_NOW)

    assert await read_latest_persona_version(lane="prod", persona_id="akao") is None, (
        "coe 里跑实验改出来的人设当场生效在 prod 的她身上了"
    )


@pytest.mark.integration
async def test_sisters_do_not_share_a_week(review_db, stub_review):
    await _a_week_of_pages("akao")
    stub_review("赤尾这一版。")

    await review_persona(lane=LANE, persona_id="akao", now=_NOW)

    assert await read_latest_persona_version(lane=LANE, persona_id="ayana") is None


# --------------------------------------------------------------------------
# 七 · 接进那条钟
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_tick_reviews_each_of_them(review_db, stub_review, monkeypatch):
    """忘了挂钟的症状是静默的：模块写好了、测试全绿，而线上一版都不会有。"""
    from app.living import persona_review as review_mod

    for who in ("akao", "ayana", "chinagi"):
        await _a_week_of_pages(who)
    stub_review("这一周。")

    monkeypatch.setattr(review_mod, "living_lane", lambda: LANE)
    monkeypatch.setattr(review_mod, "now_cst", lambda: _NOW)

    await persona_review_tick.__wrapped__(
        review_mod.PersonaReviewTick(ts=_NOW.isoformat())
    )

    for who in ("akao", "ayana", "chinagi"):
        latest = await read_latest_persona_version(lane=LANE, persona_id=who)
        assert latest is not None and latest.source == "review", (
            f"{who} 没有这一周的版本 —— 那条钟没把三个人都推到"
        )


@pytest.mark.integration
async def test_one_sister_blowing_up_does_not_take_the_others_down(
    review_db, stub_review, monkeypatch
):
    """一个人炸不拖累另两个，异常也不往上抛（往上抛就是整拍失败）。"""
    from app.living import persona_review as review_mod

    for who in ("akao", "ayana", "chinagi"):
        await _a_week_of_pages(who)
    stub_review("这一周。")

    real = review_mod.review_persona

    async def blow_up_on_ayana(*, lane, persona_id, now):
        if persona_id == "ayana":
            raise RuntimeError("模型这一轮炸了")
        return await real(lane=lane, persona_id=persona_id, now=now)

    monkeypatch.setattr(review_mod, "review_persona", blow_up_on_ayana)
    monkeypatch.setattr(review_mod, "living_lane", lambda: LANE)
    monkeypatch.setattr(review_mod, "now_cst", lambda: _NOW)

    await persona_review_tick.__wrapped__(
        review_mod.PersonaReviewTick(ts=_NOW.isoformat())
    )

    for who in ("akao", "chinagi"):
        assert await read_latest_persona_version(lane=LANE, persona_id=who) is not None
    assert await read_latest_persona_version(lane=LANE, persona_id="ayana") is None


@pytest.mark.integration
async def test_the_tick_payload_is_a_single_ts_field():
    """挂时间源的 Data 多一个必填字段 = 每一拍 ValidationError 直接杀 Pod。"""
    from app.living.persona_review import PersonaReviewTick
    from app.runtime.data import key_fields

    assert set(PersonaReviewTick.model_fields) == {"ts"}
    assert set(key_fields(PersonaReviewTick)) == {"ts"}


def test_the_clock_is_declared_in_the_living_wiring():
    """挂没挂上按生产 wiring 的注册表核对，不是"我记得挂了"。

    reload 手法同 ``tests/wiring/test_outbound_wiring.py``：根 conftest 的 autouse
    fixture 每个用例前后都清 ``WIRING_REGISTRY``，所以这里重跑一次模块体，看
    ``wire(...)`` 到底注册出了什么。
    """
    import importlib

    from app.living.persona_review import (
        PERSONA_REVIEW_TICK_SECONDS,
        PersonaReviewTick,
        persona_review_tick,
    )
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import WIRING_REGISTRY, clear_wiring

    # 先 import 再清再 reload —— 顺序同 ``tests/wiring/test_outbound_wiring.py``。
    # 反过来的话，这个进程里还没 import 过 ``app.wiring.living`` 时 import 本身会跑
    # 一遍模块体、reload 再跑一遍，注册表里就是两条同样的边。
    module = importlib.import_module("app.wiring.living")
    clear_wiring()
    clear_bindings()
    importlib.reload(module)

    wires = [w for w in WIRING_REGISTRY if w.data_type is PersonaReviewTick]
    assert len(wires) == 1, (
        "app.wiring.living 里没有 PersonaReviewTick 那条钟 —— 模块写好了、"
        "测试全绿，而线上一版都不会有"
    )
    (wire,) = wires
    assert [(s.kind, s.params) for s in wire.sources] == [
        ("interval", {"seconds": float(PERSONA_REVIEW_TICK_SECONDS)})
    ]
    assert wire.consumers == [persona_review_tick]
