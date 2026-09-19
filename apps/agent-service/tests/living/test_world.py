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

from app.agent.neutral import Message, Role, ToolCall
from app.agent.runtime_context import agent_context
from app.living.documents import read_document
from app.living.upcoming import (
    list_due_upcoming,
    list_upcoming_between,
    schedule_upcoming,
)
from app.living.world import (
    DEFAULT_WORLD_FLOOR_MINUTES,
    DEFAULT_WORLD_HEARTBEAT_MINUTES,
    EXPECT_MAX_MINUTES,
    EXPECT_MIN_MINUTES,
    WORLD_KEPT_TOOLS,
    WORLD_MATERIAL_TOOLS,
    WORLD_ROUND_TOOLS,
    WorldPace,
    WorldRound,
    expect,
    latest_world_round,
    run_world_round,
    world_pace,
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

    def __init__(
        self, *calls: dict, said: str = "没有", reads: str | None = None
    ) -> None:
        self.calls = list(calls)
        self.said = said
        self.reads = reads
        self.runs: list[tuple[list[Message], dict]] = []

    async def run(self, messages, **kwargs):
        self.runs.append((messages, kwargs))
        sink = kwargs.get("transcript_sink")
        if sink is None:
            raise AssertionError(
                "这一轮没给 transcript_sink —— 那样它产出的每一条都进不了连续上下文"
            )
        with agent_context(kwargs["context"]):
            for args in self.calls:
                await expect.invoke(args)
            if self.reads is not None:
                # 真调那只手，所以路径防护、根目录、返回正文都是被真的验到的。
                body = await read_document.invoke({"path": self.reads})
                call = ToolCall(
                    id="d1", name="read_document", arguments={"path": self.reads}
                )
                sink.append(
                    Message(role=Role.ASSISTANT, content="", tool_calls=[call])
                )
                sink.append(Message(role=Role.TOOL, content=body, tool_call_id="d1"))
        said = Message(role=Role.ASSISTANT, content=self.said)
        sink.append(said)
        return said


@pytest.fixture
def stub_round(monkeypatch):
    """装一个替身 world + 把轮次间隔钉在默认值上。"""
    from app.living import world as world_mod

    def install(
        *calls: dict, said: str = "没有", reads: str | None = None
    ) -> FakeRunner:
        runner = FakeRunner(*calls, said=said, reads=reads)
        monkeypatch.setattr(world_mod, "build_world_runner", lambda: runner)
        return runner

    async def fixed_pace() -> WorldPace:
        return WorldPace(
            heartbeat_minutes=DEFAULT_WORLD_HEARTBEAT_MINUTES,
            floor_minutes=DEFAULT_WORLD_FLOOR_MINUTES,
        )

    monkeypatch.setattr(world_mod, "world_pace", fixed_pace)
    return install


@pytest.fixture
def world_docs(tmp_path, monkeypatch):
    """world 那棵文档树，根目录指到 tmp。"""
    from app.living.documents import DOCS_DIR_ENV

    monkeypatch.setenv(DOCS_DIR_ENV, str(tmp_path / "mount"))
    monkeypatch.setenv("LANE", LANE)
    root = tmp_path / "mount" / LANE
    root.mkdir(parents=True)
    return root


async def _someone_did_something(
    *, at, content: str = "打开了电视", which: str = "h1"
):
    """三姐妹之一做了一件当面看得见的事。"""
    from app.living.happening import record_happening

    return await record_happening(
        lane=LANE,
        happening_id=which,
        actor="akao",
        place="家/客厅",
        kind="act",
        content=content,
        occurred_at=at,
    )


async def _the_world_itself_did_something(*, at, which: str = "w1"):
    """世界自己发生的事（日历到期交付、每日外部素材写的都是这个 actor）。"""
    from app.living.happening import record_happening
    from app.living.records import WORLD_ACTOR

    return await record_happening(
        lane=LANE,
        happening_id=which,
        actor=WORLD_ACTOR,
        place="家",
        kind="act",
        content="天黑了",
        occurred_at=at,
    )


async def _cost_rows() -> int:
    from sqlalchemy import text as _text

    from app.data.session import get_session

    async with get_session() as s:
        return (
            await s.execute(
                _text(
                    "SELECT count(*) FROM data_thinking_tokens_spent "
                    "WHERE actor = 'world'"
                )
            )
        ).scalar_one()


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
        lane=LANE, now=_at(10) + dt.timedelta(minutes=DEFAULT_WORLD_FLOOR_MINUTES - 1)
    )

    assert skipped is None
    assert len(runner.runs) == 1, "硬下限没到就跑了模型 —— 这是上一代烧钱的形状"


@pytest.mark.integration
async def test_a_round_runs_again_once_the_heartbeat_has_passed(world_db, stub_round):
    runner = stub_round()

    await run_world_round(lane=LANE, now=_at(10))
    later = await run_world_round(
        lane=LANE, now=_at(10) + dt.timedelta(minutes=DEFAULT_WORLD_HEARTBEAT_MINUTES)
    )

    assert later is not None
    assert len(runner.runs) == 2


@pytest.mark.integration
async def test_both_numbers_come_from_dynamic_config(world_db, monkeypatch):
    from app.living import world as world_mod

    seen: list[str] = []

    def fake_get(key: str, *, default: str = "") -> str:
        seen.append(key)
        return {"living_world_heartbeat_minutes": "45"}.get(key, "7")

    monkeypatch.setattr(world_mod.dynamic_config, "get", fake_get)

    assert await world_pace() == WorldPace(heartbeat_minutes=45, floor_minutes=7)
    assert set(seen) == {
        world_mod.LIVING_WORLD_HEARTBEAT_MINUTES_KEY,
        world_mod.LIVING_WORLD_FLOOR_MINUTES_KEY,
    }


@pytest.mark.integration
@pytest.mark.parametrize(
    "heartbeat,floor",
    [("一小时", "10"), ("30", "0"), ("5", "10")],
    ids=["心跳不是数", "下限是零", "下限比心跳还长"],
)
async def test_a_pace_that_cannot_hold_falls_back_as_a_whole(
    world_db, monkeypatch, heartbeat, floor
):
    """整套退回，不逐项修补。

    逐项补出来的是一套谁也没设计过的节奏，而它的表现是"world 要么不醒要么一直醒"——
    两种都不会报错，只会在账单或者 ``WorldRound`` 的行数上看出来，而且要过好几天。
    """
    from app.living import world as world_mod

    monkeypatch.setattr(
        world_mod.dynamic_config,
        "get",
        lambda key, *, default="": heartbeat
        if key == world_mod.LIVING_WORLD_HEARTBEAT_MINUTES_KEY
        else floor,
    )

    assert await world_pace() == WorldPace(
        heartbeat_minutes=DEFAULT_WORLD_HEARTBEAT_MINUTES,
        floor_minutes=DEFAULT_WORLD_FLOOR_MINUTES,
    )


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
        lane=LANE, now=_at(10) + dt.timedelta(minutes=DEFAULT_WORLD_HEARTBEAT_MINUTES)
    )

    latest = await latest_world_round(lane=LANE)
    assert latest is not None
    assert latest.ran_at == _at(10) + dt.timedelta(minutes=DEFAULT_WORLD_HEARTBEAT_MINUTES)


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

    async def crash(row, *, session=None):
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
    """网格的步长是**硬下限**，不是心跳。

    两件事同时压在这上面：派生 id 要跨重试稳住（步长多大都行），以及**两轮必然落在
    不同格上**（这条只有步长等于硬下限时才成立 —— 两轮至少隔一个硬下限，那就至少跨
    一格）。撞了的话成本记账 ``ON CONFLICT DO NOTHING`` 会静默吞掉第二笔。
    """
    stub_round()

    round_ = await run_world_round(lane=LANE, now=_at(10, 37))

    assert round_.ran_at == _at(10, 30)
    assert round_.round_id == _at(10, 30).isoformat(timespec="minutes")


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

    # 账本在**界桩**上重铺，所以要跨过一个清理点才看得到刚排的这件事。中间那段时间它
    # 靠自己上一轮那次 expect 的返回记着（``expect`` 在"留着"那一档，保留 4 小时），
    # 而清理点比那个短得多，两条接得上。
    next_round = stub_round()
    await run_world_round(lane=LANE, now=_at(11, 5))

    (messages, _kwargs) = next_round.runs[0]
    said_to_it = "\n".join(m.text() for m in messages if m.role is Role.USER)
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
    跟 moment 同源：都用这一轮自己的锚，不现取钟。
    """
    runner = stub_round()

    await run_world_round(lane=LANE, now=_at(10))

    (messages, _kwargs) = runner.runs[0]
    said_to_it = "\n".join(m.content for m in messages if m.role is Role.USER)
    assert "2026-07-25" in said_to_it and "10:00" in said_to_it, (
        f"空账本这一轮没告诉它现在几点，它只能瞎猜。它看到的是：\n{said_to_it}"
    )


# --------------------------------------------------------------------------
# 五 · 节奏：心跳保底 + 有事提前 + 硬下限
#
# 旧的那一版是单一间隔：离上一轮不够久就不跑。问题是世界只会按钟点动，姐妹做了什么
# 它最多要等一个间隔才知道。改成三条一起管：
#
#   * **心跳**   最长这么久必醒一次，世界自己也会有事发生
#   * **提前**   姐妹做了事就提前醒，但
#   * **硬下限** 两轮之间绝不短于这个数
#
# 硬下限那条是防旧架构那个正反馈：world 唤醒角色、角色行动又唤醒 world，一天跑两百
# 多轮。没有它，"有事就提前"会退化成"有多少事就跑多少轮"。
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_it_does_not_run_again_before_the_floor(world_db, stub_round):
    """刚跑过，哪怕这会儿有人做了事也不跑 —— 硬下限先于一切。"""
    stub_round()
    await run_world_round(lane=LANE, now=_at(10))
    await _someone_did_something(at=_at(10, 3))

    assert await run_world_round(lane=LANE, now=_at(10, 5)) is None


@pytest.mark.integration
async def test_someone_acting_brings_the_next_round_forward(world_db, stub_round):
    """过了硬下限，有人做了事就提前醒 —— 不用干等一个心跳。"""
    stub_round()
    await run_world_round(lane=LANE, now=_at(10))
    await _someone_did_something(at=_at(10, 12))

    early = await run_world_round(lane=LANE, now=_at(10, 15))

    assert early is not None


@pytest.mark.integration
async def test_a_quiet_stretch_waits_for_the_heartbeat(world_db, stub_round):
    """没人做事就不提前 —— 过了硬下限也接着睡，等心跳。"""
    stub_round()
    await run_world_round(lane=LANE, now=_at(10))

    assert await run_world_round(lane=LANE, now=_at(10, 15)) is None
    assert await run_world_round(lane=LANE, now=_at(10, 40)) is not None


@pytest.mark.integration
async def test_the_world_does_not_wake_itself(world_db, stub_round):
    """**探针必须排除 world 自己。**

    ``Happening.actor`` 可以是 ``"world"`` —— 日历到期交付和每日外部素材写的都是它。
    探针不排除的话就成了一个闭环：world 排一件事 → 到点写一条 happening → 唤醒
    world → 它再排一件。硬下限只能把这个闭环压到一天 144 轮，压不掉。
    """
    stub_round()
    await run_world_round(lane=LANE, now=_at(10))
    await _the_world_itself_did_something(at=_at(10, 12))

    assert await run_world_round(lane=LANE, now=_at(10, 15)) is None, (
        "world 被自己写下的事叫醒了"
    )


@pytest.mark.integration
async def test_an_early_round_has_an_identity_of_its_own(world_db, stub_round):
    """提前的那一轮不能跟心跳那一轮撞 id —— 撞了成本记账会静默吞掉一笔。

    ``record_round_cost`` 是 ``ON CONFLICT DO NOTHING``：两轮同一个 ``round_id``
    时第二轮那笔 token 一行日志都不留地消失，而"这一天花了多少"只能从那张表数。
    """
    from app.domain.thinking_cost import ThinkingTokensSpent
    from tests.runtime.conftest import migrate

    await migrate(ThinkingTokensSpent, world_db)
    stub_round()

    first = await run_world_round(lane=LANE, now=_at(10))
    await _someone_did_something(at=_at(10, 12))
    second = await run_world_round(lane=LANE, now=_at(10, 15))

    assert first is not None and second is not None
    assert first.round_id != second.round_id
    assert await _cost_rows() == 2, "两轮只记上了一笔 —— round_id 撞了"


# --------------------------------------------------------------------------
# 六 · 它每轮看得见三姐妹做了什么
#
# 实测 12.4 天：286 轮，``produced`` 全是 0，每轮输入平均 1130 token —— 只有时刻和
# 一张到期事项账本。让一个 agent 看着一张只有时刻表的纸，问它"世界上还该发生点什么"，
# 286 次答"没有"是唯一合理的回答。它不是被砍狠了，是被饿死了。
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_it_sees_what_the_sisters_did(world_db, stub_round):
    runner = stub_round()
    await _someone_did_something(at=_at(9, 50), content="把厨房的灯打开了")

    await run_world_round(lane=LANE, now=_at(10))

    fed = "\n".join(m.text() for m in runner.runs[0][0])
    assert "把厨房的灯打开了" in fed


@pytest.mark.integration
async def test_it_does_not_see_the_same_thing_twice(world_db, stub_round):
    """游标推着走：上一轮看过的不再重发，否则它每轮都在读同一段。"""
    runner = stub_round()
    await _someone_did_something(at=_at(9, 50), content="把厨房的灯打开了")
    await run_world_round(lane=LANE, now=_at(10))
    await _someone_did_something(at=_at(10, 40), content="把灯关了", which="h2")
    await run_world_round(lane=LANE, now=_at(10, 40))

    # 只看这一轮新摆到它眼前的那条。上一轮那条刺激当然还在上下文里 —— 那是连续，
    # 不是重发；游标管的是"同一件事不会被当成新的再喂一次"。
    stimulus = runner.runs[1][0][-1].text()
    assert "把灯关了" in stimulus
    assert "把厨房的灯打开了" not in stimulus


@pytest.mark.integration
async def test_with_no_cursor_to_resume_from_it_only_reads_the_recent_stretch(
    world_db, stub_round
):
    """没有游标可接的时候只读最近这一段，不是从世界的开头读起。

    两种情形都会走到这儿：这条泳道 world 一轮都没跑过，和 ``next_seq`` 这一列是后加的
    （已有轮次那一行上是 NULL）。两种的共同点是"不知道该从哪儿接"，而**不是**"该从头
    读一遍"。

    实测（coe-living，2026-09-14）：加列那天第一轮从 seq=0 起算，于是它开始逐轮补读
    两周前的事 —— 一次 200 条、5508 条积压要 28 轮，每轮约 0.15 美元，而它要判断的是
    "这个点该不该冒出点新东西"。两周前谁说了什么对这个判断没有任何用，只会让它照着过时
    的剧情排事。跳过的那一段它本来也从没拿到过（旧 world 根本不读 happening）。

    边界是 ``LEDGER_LOOK_BACK``，不是"此刻"：崭新的泳道第一轮该看得见刚发生的那几件事
    （上面 ``test_it_sees_what_the_sisters_did`` 钉的就是这个），瞎的只该是太旧的那一段。
    """
    runner = stub_round()
    await _someone_did_something(
        at=_at(10) - dt.timedelta(days=7), content="上周就发生完了的事"
    )

    await run_world_round(lane=LANE, now=_at(10))

    fed = "\n".join(m.text() for m in runner.runs[0][0])
    assert "上周就发生完了的事" not in fed, (
        f"第一轮把积压的全读了一遍。拿到：\n{fed}"
    )


@pytest.mark.integration
async def test_a_round_written_before_the_cursor_existed_does_not_replay_history(
    world_db, stub_round
):
    """``next_seq`` 是 NULL 的那一行是"这一列还不存在时写的"，不是"读到 0"。

    两者在 ``or 0`` 底下长得一模一样，而后果差一整部历史。
    """
    from app.living.world import WorldRound
    from app.runtime.persist import insert_idempotent

    await insert_idempotent(
        WorldRound(
            lane=LANE,
            round_id="老轮次",
            ran_at=_at(8),
            produced=0,
            said="没有",
            next_seq=None,
        )
    )
    await _someone_did_something(
        at=_at(10) - dt.timedelta(days=7), content="上周就发生完了的事"
    )

    runner = stub_round()
    await run_world_round(lane=LANE, now=_at(10))

    fed = "\n".join(m.text() for m in runner.runs[0][0])
    assert "上周就发生完了的事" not in fed, (
        f"把「这一列还不存在」读成了「游标在 0」。拿到：\n{fed}"
    )


@pytest.mark.integration
async def test_it_picks_up_where_it_left_off(world_db, stub_round):
    """它也有连续上下文 —— 上一轮说过的话在下一轮还在眼前。"""
    runner = stub_round(said="我想让文化祭这条线动起来")
    await run_world_round(lane=LANE, now=_at(10))
    await run_world_round(lane=LANE, now=_at(10, 40))

    second = "\n".join(m.text() for m in runner.runs[1][0])
    assert "我想让文化祭这条线动起来" in second


@pytest.mark.integration
async def test_the_ledger_is_laid_down_at_a_checkpoint_not_every_round(
    world_db, stub_round
):
    """账本是"现在账上有什么"，读一百遍字字一样 —— 每轮重发就是把同一段话抄一遍。

    它跟她那边的状态快照同一个位置：只在界桩上重铺一次。
    """
    runner = stub_round()
    await schedule_upcoming(
        lane=LANE, item_id="i1", what="快递送到门口", due_at=_at(11)
    )
    await run_world_round(lane=LANE, now=_at(10))
    await run_world_round(lane=LANE, now=_at(10, 40))

    whole = "\n".join(m.text() for m in runner.runs[1][0])
    assert whole.count("快递送到门口") == 1, (
        "账本出现了不止一次 —— 它每轮都被重发了一遍"
    )
    assert "快递送到门口" not in runner.runs[1][0][-1].text(), (
        "账本落在这一轮的刺激里了，它该只在界桩上"
    )


# --------------------------------------------------------------------------
# 七 · 它手里那棵文档树
# --------------------------------------------------------------------------


def test_every_hand_it_has_is_classified():
    """world 这几只手也要各自明确落进"素材"或"留着"其中一档。

    漏掉的走裁剪层的默认档，而它每轮 ``read`` 一份文档 —— 完整保留 240 分钟正好是
    最贵的那个默认值，并且一句报错都没有。
    """
    every = {t.definition.name for t in WORLD_ROUND_TOOLS}
    assert WORLD_MATERIAL_TOOLS | WORLD_KEPT_TOOLS == every, (
        f"没分类的：{every - (WORLD_MATERIAL_TOOLS | WORLD_KEPT_TOOLS)}；"
        f"分类表里多出来的：{(WORLD_MATERIAL_TOOLS | WORLD_KEPT_TOOLS) - every}"
    )
    assert not (WORLD_MATERIAL_TOOLS & WORLD_KEPT_TOOLS)


def test_the_documents_are_material_and_its_own_writes_are_kept():
    """读回来的设定是素材（过期了再读一次就有）；它自己改过什么要留着。"""
    assert {"list_documents", "read_document"} <= WORLD_MATERIAL_TOOLS
    assert not (WORLD_MATERIAL_TOOLS & {"write_document", "edit_document"})
    assert "expect" in WORLD_KEPT_TOOLS


@pytest.mark.integration
async def test_what_it_read_is_still_in_front_of_it_next_round(
    world_db, stub_round, world_docs
):
    """按需读回来的那一份进了它的上下文 —— 不然下一轮它只能再读一遍。"""
    (world_docs / "设定").mkdir()
    (world_docs / "设定" / "这座城市.md").write_text(
        "临海的小城，夏天有海风。", encoding="utf-8"
    )
    runner = stub_round(reads="设定/这座城市.md")
    await run_world_round(lane=LANE, now=_at(10))
    await run_world_round(lane=LANE, now=_at(10, 40))

    second = "\n".join(m.text() for m in runner.runs[1][0])
    assert "临海的小城" in second


# --------------------------------------------------------------------------
# 八 · 让一件事发生：范围和持续
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_an_event_with_no_place_becomes_a_global_one(world_db, stub_round):
    """说不出在哪就是"到处" —— 天黑、台风不属于任何一栋。"""
    stub_round({"what": "起风了", "in_minutes": 30})

    await run_world_round(lane=LANE, now=_at(10))

    items = await list_upcoming_between(lane=LANE, since=_at(0), until=_at(23, 59))
    assert [i.place for i in items] == [None]  # 账上留空，交付那一下变成全局


@pytest.mark.integration
async def test_something_that_lasts_carries_its_end(world_db, stub_round):
    """一件会持续一段时间的事要说得出什么时候结束，不然后到的人不知道正在下雨。"""
    stub_round(
        {"what": "外面开始下雨", "in_minutes": 30, "lasts_minutes": 180}
    )

    await run_world_round(lane=LANE, now=_at(10))

    items = await list_upcoming_between(lane=LANE, since=_at(0), until=_at(23, 59))
    assert [i.lasts_until for i in items] == [_at(13, 30)]


@pytest.mark.integration
async def test_by_default_nothing_lingers(world_db, stub_round):
    """不说持续多久就是一瞬间的事 —— 绝大多数事都是这一类。"""
    stub_round({"what": "快递送到门口", "in_minutes": 30})

    await run_world_round(lane=LANE, now=_at(10))

    items = await list_upcoming_between(lane=LANE, since=_at(0), until=_at(23, 59))
    assert [i.lasts_until for i in items] == [None]


# --------------------------------------------------------------------------
# 九 · 让一件事发生：地点那个参数不给它任何地名样本
#
# 跟她那侧 ``switch_to`` / ``move_to`` 是同一条（见 ``tests/living/test_moment.py``
# 里那一节的两次真事故）：**举例就是词表**。world 手里的地名该从设定集里来 —— 它自己
# 就是写设定集的那个 —— 而工具描述里写死的那一条路径既不会跟着设定集改名，又会被逐字
# 抄走，抄出来的地方从此跟设定集上那个分成两半。
#
# 这只手原来举的是 ``家/玄关`` 和 ``学校``，旁边的注释还自承"举例是会被原样抄走的"。
# --------------------------------------------------------------------------


def test_the_place_parameter_of_expect_hands_it_no_place_name():
    """``expect`` 交给模型的每一段字，都不含一条具体路径、也不含一个具体地名。"""
    from tests.living.conftest import (
        model_facing_text,
        names_of_places_in,
        path_samples,
    )

    for where, text in model_facing_text(expect).items():
        assert not path_samples(text), (
            f"expect 的{where}里摆着一条路径样本 {path_samples(text)!r} —— "
            f"它会逐字抄走它。原文：\n{text}"
        )
        assert not names_of_places_in(text), (
            f"expect 的{where}里写着具体地名 {names_of_places_in(text)!r} —— "
            f"那是世界的内容，世界改名之后这几个字还在教它写一个不存在的地方。"
            f"原文：\n{text}"
        )


def test_expect_and_her_hands_say_a_place_the_same_way():
    """``expect`` 和她那两只落位置的手，说形状那一段**一字不差**。

    world 写的地点和她写的地点落在同一套地名空间上：同一个地方两边写成同一条路径才算
    同一处。两边各写一份措辞就会漂，而漂开的表现是同一个地方裂成两个 —— 那正是她那侧
    两次事故的形状。

    钉的是**三只手真正交给模型的那段字**，不是"两份拷贝有没有漂"：那句话现在只有一处
    定义（:data:`app.living.place.PLACE_SHAPE`），拷贝不存在了。谁哪天在任何一侧改回
    手写一段措辞，这里红。
    """
    from app.living.moment import MOMENT_TOOLS
    from app.living.place import PLACE_SHAPE

    hands = {t.name: t for t in MOMENT_TOOLS}
    described = {
        "expect": expect.definition.parameters["properties"]["place"]["description"],
        "switch_to": hands["switch_to"]
        .definition.parameters["properties"]["place"]["description"],
        "move_to": hands["move_to"]
        .definition.parameters["properties"]["place"]["description"],
    }

    for hand, text in described.items():
        assert PLACE_SHAPE in text, (
            f"{hand} 的 place 描述里说形状那一段跟别人漂开了 —— 三只手写的是同一套地名"
            f"空间。\n该有的那一段：{PLACE_SHAPE!r}\n{hand} 这边：{text!r}"
        )


def test_the_place_parameter_of_expect_still_says_what_shape_a_path_is():
    """清掉样本不等于不说形状 —— 分隔符和层级仍然要说出来。

    只钉"没有样本"的话，把整段描述删空也能绿，而那时它连该写成几层都不知道。
    """
    described = expect.definition.parameters["properties"]["place"]["description"]

    assert "/" in described, f"没告诉它层与层之间用什么隔开：{described!r}"
    assert "层" in described, f"没告诉它这是一条层级路径：{described!r}"


# --------------------------------------------------------------------------
# 十 · 外面那六个真实数据源是它自己的手
#
# 从前是代码替它去取：每个生活日凌晨四点问一轮六个源，贴上标签广播成一件所有人都
# 感知得到的事（那一层已删）。那条路逼出了一串写死的东西 —— 查哪座
# 城市、几点去查、取回来怎么说、一天查几次，全是代码替世界拿的主意。
#
# 现在六个源就是它手上的六只手：这家人住在哪是它写在设定集里的，所以城市由它传；想
# 什么时候看就什么时候看，想再看一次就再看一次。**代价是它可能不看** —— 那是接受了的，
# 不补兜底、不提醒、不自动注入。
# --------------------------------------------------------------------------


OUTSIDE_SOURCE_NAMES = {
    "query_weather",
    "query_sun_times",
    "query_lunar_term",
    "query_holiday",
    "query_anime_calendar",
    "query_city_events",
}


def test_the_six_real_world_sources_are_in_its_hands():
    """六个源都接进了这一轮的工具集 —— 漏一个是静默的，它只是永远看不见那一样。"""
    every = {t.definition.name for t in WORLD_ROUND_TOOLS}
    assert OUTSIDE_SOURCE_NAMES <= every, (
        f"没接进来的：{OUTSIDE_SOURCE_NAMES - every}"
    )


def test_the_six_sources_are_material_not_kept():
    """外面的事是**会过期**的素材：想知道后来变了没有，再看一次就有。

    留着的话，凌晨那一份天气会跟着它走一整天 —— 那正是旧投递层"一天只取一次"的病，
    换个地方重新长出来。
    """
    assert OUTSIDE_SOURCE_NAMES <= WORLD_MATERIAL_TOOLS
    assert not (OUTSIDE_SOURCE_NAMES & WORLD_KEPT_TOOLS)


def test_she_does_not_get_these_hands():
    """**这六只不在她手上。**

    她在世界里：下雨是她走到窗边感受到的，不是她查一次 API 查到的。world 是世界事实的
    唯一来源，它看见了写进世界，她通过世界感知。

    这条还有第二层：上游的字节进到谁的上下文里，转义那道就得跟到哪。她那段文本里有
    ``<msg … rel="owner">``（:mod:`app.living.phone`），而这六只手交回来的是上游原样
    的字节、没有过 :func:`app.living.records.esc` —— 哪天把它们塞进她手里，那就是一个
    真的伪造口子。world 那边没有这种带属性的标记，所以同样的字节在它那儿伪造不出身份。

    **这条只证明得了直接调用那一头。** 字节从 world 的上下文再走到她眼前的那几条间接
    的路（``Happening.content``、地方文档的正文和名字）各有各的转义，门禁在
    ``tests/living/test_no_forged_markup.py`` —— 拿这一条去论证那几条也安全是错的。
    """
    from app.living.moment import MOMENT_TOOLS

    hers = {t.definition.name for t in MOMENT_TOOLS}
    assert not (OUTSIDE_SOURCE_NAMES & hers), (
        f"她手上多了外面的数据源：{OUTSIDE_SOURCE_NAMES & hers} —— "
        "天气该是她走到窗边感受到的，不是她查出来的"
    )


def test_the_city_is_something_it_passes_in():
    """跟地点有关的那三只收 ``city``，所以"这家人住在哪"是世界的事实，不是配置。

    从前那两个 Dynamic Config（世界坐标 / 世界所在城市）就是
    "代码替它取数"逼出来的：代码要去查，代码就得知道查哪儿。
    """
    hands = {t.definition.name: t for t in WORLD_ROUND_TOOLS}
    for name in ("query_weather", "query_sun_times", "query_city_events"):
        props = hands[name].definition.parameters.get("properties", {})
        assert "city" in props, f"{name} 没收城市参数"


def test_no_hand_of_its_shows_it_a_sample_city():
    """举例就是词表 —— 城市那个参数一个市名样本都不给。

    判据和落点在 ``tests/unit/agent/tools/test_external_sources.py``；这里只钉住
    "接到 world 手上的就是那几只被钉过的手"，免得哪天换成一层自己写描述的包装。
    """
    from tests.living.conftest import model_facing_text
    from tests.unit.agent.tools.test_external_sources import (
        CITY_NAMES_ONCE_IN_TOOL_TEXT,
    )

    hands = {t.definition.name: t for t in WORLD_ROUND_TOOLS}
    for name in sorted(OUTSIDE_SOURCE_NAMES):
        for where, text in model_facing_text(hands[name]).items():
            named = [c for c in CITY_NAMES_ONCE_IN_TOOL_TEXT if c in text]
            assert not named, f"{name} 的{where}里写着 {named!r}"
