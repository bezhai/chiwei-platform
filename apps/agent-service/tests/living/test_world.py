"""world 的稀疏轮次：低频问一句「有什么新东西该出现了吗」，默认答「没有」。

三条硬边界，各有对应的用例：

  * **不产叙述。** 上一代 world 每轮被要求描述世界，烧掉总消耗一半去写静物记账。
    这一版一轮只有一种产出：往账上写一件**会到期**的事。跑完一轮没有任何
    ``Happening`` —— 她感知到什么由到期交付那条路管。
  * **不挑收件人。** 工具只收「什么事、在哪、多久之后」，没有 recipient 参数。
  * **低频且可调。** 间隔是业务参数（Dynamic Config），不是写死的常数；轮次之间
    离得太近就不跑，而且这个「跑没跑」有落库的账可查。
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.agent.neutral import Message, Role
from app.agent.runtime_context import agent_context
from app.living.upcoming import (
    list_due_upcoming,
    list_upcoming_between,
    schedule_upcoming,
)
from app.living.world import (
    DEFAULT_WORLD_ROUND_MINUTES,
    EXPECT_MAX_MINUTES,
    EXPECT_MIN_MINUTES,
    WorldRound,
    expect,
    latest_world_round,
    run_world_round,
    world_round_minutes,
)

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))


def _at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 7, 25, hour, minute, tzinfo=_CST)


@pytest.fixture
async def world_db(living_db):
    """living 三张表 + world 的轮次账本。"""
    from tests.runtime.conftest import migrate

    await migrate(WorldRound, living_db)
    return living_db


class FakeRunner:
    """替身 world：把「这一轮它调了哪些 expect」写死，不碰真模型。

    走的是真工具（``expect.invoke``）和真 context 绑定，所以派生 id、时间锚、
    lane 隔离这些都是被真的验到的，只有模型那一步是假的。
    """

    def __init__(self, *calls: dict, said: str = "没有") -> None:
        self.calls = list(calls)
        self.said = said
        self.runs: list[tuple[list[Message], dict]] = []

    async def run(self, messages, **kwargs):
        self.runs.append((messages, kwargs))
        with agent_context(kwargs["context"]):
            for args in self.calls:
                await expect.invoke(args)
        return Message(role=Role.ASSISTANT, content=self.said)


@pytest.fixture
def stub_round(monkeypatch):
    """装一个替身 world + 把轮次间隔钉在默认值上。"""
    from app.living import world as world_mod

    def install(*calls: dict, said: str = "没有") -> FakeRunner:
        runner = FakeRunner(*calls, said=said)
        monkeypatch.setattr(world_mod, "build_world_runner", lambda: runner)
        return runner

    async def fixed_minutes() -> int:
        return DEFAULT_WORLD_ROUND_MINUTES

    monkeypatch.setattr(world_mod, "world_round_minutes", fixed_minutes)
    return install


# --------------------------------------------------------------------------
# 一 · 默认答「没有」
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_quiet_round_writes_nothing_but_still_leaves_a_record(
    world_db, stub_round
):
    """默认输出是一个词。跑过这一轮要留痕 —— 不然「没有的比例」根本算不出来。"""
    stub_round()

    round_ = await run_world_round(lane=LANE, now=_at(10))

    assert round_ is not None
    assert round_.produced == 0
    assert round_.said == "没有"
    assert await list_upcoming_between(
        lane=LANE, since=_at(0), until=_at(23, 59)
    ) == []


@pytest.mark.integration
async def test_a_round_never_writes_a_happening(world_db, stub_round):
    """world 一轮的产出只有「会到期的东西」，不是一段世界叙述。"""
    from app.living.happening import read_perceived_by
    from app.living.whereabouts import note_whereabouts

    await note_whereabouts(
        lane=LANE,
        persona_id="akao",
        moment_id="m1",
        place="家/客厅",
        doing="待着",
        noted_at=_at(9),
    )
    stub_round({"what": "楼下有人在搬东西", "in_minutes": 20, "place": "家/客厅"})

    await run_world_round(lane=LANE, now=_at(10))

    window = await read_perceived_by(lane=LANE, persona_id="akao")
    assert window.items == [], "world 轮次直接产出了 Happening —— 它只该往账上写"


# --------------------------------------------------------------------------
# 二 · 产出是「会到期的新东西」
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_an_expected_thing_lands_on_the_ledger_with_its_own_moment(
    world_db, stub_round
):
    stub_round({"what": "快递送到门口", "in_minutes": 30, "place": "家/门口"})

    round_ = await run_world_round(lane=LANE, now=_at(10))

    assert round_.produced == 1
    pending = await list_upcoming_between(lane=LANE, since=_at(0), until=_at(23, 59))
    assert [(u.what, u.due_at, u.place) for u in pending] == [
        ("快递送到门口", _at(10, 30), "家/门口")
    ]


@pytest.mark.integration
async def test_a_thing_without_a_place_stays_placeless_on_the_ledger(
    world_db, stub_round
):
    """world 只说「什么事、在哪、什么时候」；说不出在哪就是说不出，不许它编一个。"""
    stub_round({"what": "外面开始下雨", "in_minutes": 15})

    await run_world_round(lane=LANE, now=_at(10))

    pending = await list_upcoming_between(lane=LANE, since=_at(0), until=_at(23, 59))
    assert [(u.what, u.place) for u in pending] == [("外面开始下雨", None)]


@pytest.mark.integration
async def test_the_same_expectation_twice_in_one_round_lands_once(world_db, stub_round):
    """整轮重放 / 模型自己重复调一次，同一件事只能在账上占一行。"""
    same = {"what": "快递送到门口", "in_minutes": 30, "place": "家/门口"}
    stub_round(same, dict(same))

    round_ = await run_world_round(lane=LANE, now=_at(10))

    pending = await list_upcoming_between(lane=LANE, since=_at(0), until=_at(23, 59))
    assert len(pending) == 1
    assert round_.produced == 1


@pytest.mark.integration
@pytest.mark.parametrize(
    "minutes", [0, EXPECT_MIN_MINUTES - 1, EXPECT_MAX_MINUTES + 1, -30]
)
async def test_a_moment_outside_the_window_is_refused(world_db, stub_round, minutes):
    """超范围报错喂回模型让它重填，绝不静默夹成边界值、也绝不落一条错时刻。"""
    stub_round()

    with agent_context_for(LANE, _at(10)):
        outcome = await expect.invoke({"what": "什么时候都行", "in_minutes": minutes})

    assert isinstance(outcome, dict), f"越界的 in_minutes={minutes} 被接受了"
    assert await list_upcoming_between(lane=LANE, since=_at(0), until=_at(23, 59)) == []


def agent_context_for(lane: str, now: dt.datetime):
    """手工搭一个跟 ``run_world_round`` 同款的工具 context（只给上面那条用例用）。"""
    from app.agent.context import AgentContext
    from app.living.world import FEATURE_LANE, FEATURE_NOW

    return agent_context(
        AgentContext(features={FEATURE_LANE: lane, FEATURE_NOW: now.isoformat()})
    )


# --------------------------------------------------------------------------
# 三 · 它看得见账上已经有什么（不然会一遍遍重排同一件事）
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_round_is_told_what_is_already_on_the_ledger(world_db, stub_round):
    runner = stub_round()
    await schedule_upcoming(
        lane=LANE, item_id="day:2026-07-25:dinner", what="晚饭做好了", due_at=_at(18)
    )
    await schedule_upcoming(
        lane=LANE, item_id="world:parcel", what="快递送到门口", due_at=_at(9)
    )

    await run_world_round(lane=LANE, now=_at(10))

    (messages, _kwargs) = runner.runs[0]
    said_to_it = "\n".join(m.content for m in messages if m.role is Role.USER)
    assert "晚饭做好了" in said_to_it, "它看不见还没到期的安排，会一遍遍重排同一件事"
    assert "快递送到门口" in said_to_it, "它看不见刚刚发生过的事，会立刻再排一次"


@pytest.mark.integration
async def test_the_ledger_window_only_covers_this_lane(world_db, stub_round):
    runner = stub_round()
    await schedule_upcoming(
        lane="prod", item_id="p", what="线上的晚饭", due_at=_at(18)
    )

    await run_world_round(lane=LANE, now=_at(10))

    (messages, _kwargs) = runner.runs[0]
    assert "线上的晚饭" not in "\n".join(m.content for m in messages)


@pytest.mark.integration
async def test_list_upcoming_between_is_bounded_at_both_ends(world_db):
    await schedule_upcoming(lane=LANE, item_id="a", what="太早", due_at=_at(5))
    await schedule_upcoming(lane=LANE, item_id="b", what="窗内", due_at=_at(12))
    await schedule_upcoming(lane=LANE, item_id="c", what="太晚", due_at=_at(22))

    got = await list_upcoming_between(lane=LANE, since=_at(8), until=_at(18))
    assert [u.what for u in got] == ["窗内"]


# --------------------------------------------------------------------------
# 四 · 低频，而且间隔可调
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_second_round_too_soon_does_not_run(world_db, stub_round):
    runner = stub_round()

    await run_world_round(lane=LANE, now=_at(10))
    skipped = await run_world_round(
        lane=LANE, now=_at(10) + dt.timedelta(minutes=DEFAULT_WORLD_ROUND_MINUTES - 1)
    )

    assert skipped is None
    assert len(runner.runs) == 1, "间隔没到就跑了模型 —— 这是上一代烧钱的形状"


@pytest.mark.integration
async def test_a_round_runs_again_once_the_interval_has_passed(world_db, stub_round):
    runner = stub_round()

    await run_world_round(lane=LANE, now=_at(10))
    later = await run_world_round(
        lane=LANE, now=_at(10) + dt.timedelta(minutes=DEFAULT_WORLD_ROUND_MINUTES)
    )

    assert later is not None
    assert len(runner.runs) == 2


@pytest.mark.integration
async def test_the_interval_comes_from_dynamic_config(world_db, monkeypatch):
    from app.living import world as world_mod

    seen: dict[str, str] = {}

    def fake_get(key: str, *, default: str = "") -> str:
        seen["key"] = key
        return "15"

    monkeypatch.setattr(world_mod.dynamic_config, "get", fake_get)
    assert await world_round_minutes() == 15
    assert seen["key"] == world_mod.LIVING_WORLD_ROUND_MINUTES_KEY


@pytest.mark.integration
async def test_a_junk_interval_falls_back_to_the_default(world_db, monkeypatch):
    from app.living import world as world_mod

    monkeypatch.setattr(
        world_mod.dynamic_config, "get", lambda key, *, default="": "一小时"
    )
    assert await world_round_minutes() == DEFAULT_WORLD_ROUND_MINUTES


@pytest.mark.integration
async def test_rounds_of_another_lane_do_not_gate_this_one(world_db, stub_round):
    runner = stub_round()

    await run_world_round(lane="prod", now=_at(10))
    mine = await run_world_round(lane=LANE, now=_at(10, 1))

    assert mine is not None
    assert len(runner.runs) == 2


@pytest.mark.integration
async def test_the_latest_round_is_the_one_that_ran_last(world_db, stub_round):
    stub_round()

    await run_world_round(lane=LANE, now=_at(10))
    await run_world_round(
        lane=LANE, now=_at(10) + dt.timedelta(minutes=DEFAULT_WORLD_ROUND_MINUTES)
    )

    latest = await latest_world_round(lane=LANE)
    assert latest is not None
    assert latest.ran_at == _at(10) + dt.timedelta(minutes=DEFAULT_WORLD_ROUND_MINUTES)


def test_the_world_round_stays_on_the_offline_model():
    """world 一天二十几轮，``offline-model`` 合适；别跟 life 那条高频线混用别名。"""
    from app.living.world import _WORLD_ROUND_CFG

    assert _WORLD_ROUND_CFG.model_id == "offline-model"


@pytest.mark.integration
async def test_what_a_round_produced_is_countable_afterwards(world_db, stub_round):
    """验收要能逐条列出「哪几轮说了没有、哪几轮产出了什么」。"""
    stub_round({"what": "快递送到门口", "in_minutes": 30}, said="排了一件")

    round_ = await run_world_round(lane=LANE, now=_at(10))

    assert (round_.produced, round_.said) == (1, "排了一件")
    assert round_.lane == LANE


# --------------------------------------------------------------------------
# 五 · 一轮的时间锚跨重试稳定（不然幂等被击穿，同一件事排两次）
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_crash_before_the_round_record_does_not_schedule_the_same_thing_twice(
    world_db, stub_round, monkeypatch
):
    """先写 Upcoming、后写完成记录 —— 中间崩掉，下一拍必须落回同一格。

    ``item_id`` 从 ``what|place|due_at`` 派生，而 ``due_at = now + in_minutes``。
    ``now`` 一动 ``due_at`` 就动、派生 id 跟着动，幂等直接被击穿：账上出现两件
    「快递送到门口」，只差三分钟，事后根本看不出是重复。
    """
    from app.living import world as world_mod

    stub_round({"what": "快递送到门口", "in_minutes": 30, "place": "家/门口"})

    real_insert = world_mod.insert_idempotent

    async def crash(row):
        raise RuntimeError("落轮次记录时崩了")

    monkeypatch.setattr(world_mod, "insert_idempotent", crash)
    with pytest.raises(RuntimeError):
        await run_world_round(lane=LANE, now=_at(10, 0))

    monkeypatch.setattr(world_mod, "insert_idempotent", real_insert)
    again = await run_world_round(lane=LANE, now=_at(10, 3))

    assert again is not None, "上一轮没留下记录，这一拍该重跑"
    pending = await list_upcoming_between(lane=LANE, since=_at(0), until=_at(23, 59))
    assert [(u.what, u.due_at) for u in pending] == [("快递送到门口", _at(10, 30))], (
        "重试把同一件事又排了一次 —— 时间锚没跨重试稳住"
    )


@pytest.mark.integration
async def test_the_round_is_stamped_on_its_grid_cell(world_db, stub_round):
    stub_round()

    round_ = await run_world_round(lane=LANE, now=_at(10, 37))

    assert round_.ran_at == _at(10, 0)
    assert round_.round_id == _at(10, 0).isoformat(timespec="minutes")


# --------------------------------------------------------------------------
# 六 · 它排得到多远，账本就要看得到多远
# --------------------------------------------------------------------------


def test_the_ledger_reaches_exactly_as_far_as_it_can_schedule():
    """两个窗口不一致 = 它排了一件自己下一轮看不见的事，然后再排一次。"""
    from app.living.world import EXPECT_MAX_MINUTES, LEDGER_LOOK_AHEAD

    assert LEDGER_LOOK_AHEAD == dt.timedelta(minutes=EXPECT_MAX_MINUTES)


@pytest.mark.integration
async def test_the_farthest_thing_it_can_schedule_is_still_on_the_ledger_next_round(
    world_db, stub_round
):
    stub_round({"what": "后天的祭典", "in_minutes": EXPECT_MAX_MINUTES - 60})
    await run_world_round(lane=LANE, now=_at(10))

    next_round = stub_round()
    await run_world_round(
        lane=LANE, now=_at(10) + dt.timedelta(minutes=DEFAULT_WORLD_ROUND_MINUTES)
    )

    (messages, _kwargs) = next_round.runs[0]
    said_to_it = "\n".join(m.content for m in messages if m.role is Role.USER)
    assert "后天的祭典" in said_to_it, (
        "它排得到、却看不见自己排过 —— 下一轮会把同一件事再排一遍"
    )


@pytest.mark.integration
async def test_what_the_round_expected_actually_comes_due(world_db, stub_round):
    """整条链闭合：world 排的东西到点了就该被交付路径拿到。"""
    stub_round({"what": "快递送到门口", "in_minutes": 30})

    await run_world_round(lane=LANE, now=_at(10))

    assert [u.what for u in await list_due_upcoming(lane=LANE, until=_at(10, 29))] == []
    assert [u.what for u in await list_due_upcoming(lane=LANE, until=_at(10, 31))] == [
        "快递送到门口"
    ]


@pytest.mark.integration
async def test_an_empty_ledger_still_says_what_time_it_is(world_db, stub_round):
    """账上空着的时候，它更得知道现在几点。

    账本非空时"现在"还能从各行的日子钟点加「已经发生 / 还没到」的记号反推个大概；
    空账本渲染成「（账上现在什么都没有）」，这一轮就**一个时间线索都没有**了 ——
    而恰恰是这种时候它最该判断"这个点该不该冒出点什么"，也最容易乱添。判断依据
    跟一缝同源：都用这一轮自己的锚，不现取钟。
    """
    runner = stub_round()

    await run_world_round(lane=LANE, now=_at(10))

    (messages, _kwargs) = runner.runs[0]
    said_to_it = "\n".join(m.content for m in messages if m.role is Role.USER)
    assert "2026-07-25" in said_to_it and "10:00" in said_to_it, (
        f"空账本这一轮没告诉它现在几点，它只能瞎猜。它看到的是：\n{said_to_it}"
    )
