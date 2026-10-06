"""world 的醒来规则跑在真 broker + 真 Postgres 上：启动补醒 → 自定时刻 → 别人的消息 → 旧自定消息作废。

模型、上下文存储、成本记账换成替身；通信机制（定时送达、收件箱、去重、记录者）全是真的。
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.agent.neutral import Message as Turn
from app.agent.neutral import Role
from app.agent.runtime_context import agent_context
from app.infra.cst_time import now_cst
from app.messaging.lifecycle import start_messaging
from app.messaging.record import read_record
from app.messaging.sending import send
from app.world import agents, main_agent, wake
from app.world.actions import wake_me_at
from tests.messaging.conftest import (  # noqa: F401
    LANE,
    broker,
    delayed_broker,
    messaging_db,
)
from tests.messaging.helpers import eventually
from tests.runtime.conftest import test_db, test_db_dsn  # noqa: F401

from .conftest import load_world_wiring

pytestmark = pytest.mark.usefixtures("messaging_db")


class ScriptedRunner:
    """每轮按顺序取一个"再过几秒醒"，在这一轮的 context 里真调 ``wake_me_at``。"""

    def __init__(self, wake_in_seconds: list[float]):
        self.plan = list(wake_in_seconds)
        self.round_inputs: list[str] = []

    async def run(self, messages, *, context, transcript_sink, **_):
        self.round_inputs.append(messages[-1].content)
        seconds = self.plan.pop(0)
        with agent_context(context):
            at = now_cst() + timedelta(seconds=seconds)
            await wake_me_at.invoke({"at": at.isoformat(), "reason": f"{seconds} 秒后"})
        reply = Turn(role=Role.ASSISTANT, content="好。")
        transcript_sink.append(reply)
        return reply


@pytest.fixture
def world_process(broker, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("WORLD_DATA_DIR", str(tmp_path / "world-volume"))
    from inner_shared.dynamic_config import dynamic_config

    monkeypatch.setattr(dynamic_config, "get", lambda k, default="": default)
    monkeypatch.setattr(dynamic_config, "get_int", lambda k, default=0: default)

    async def load_session(key):
        return [], 0

    async def nothing(*a, **kw):
        return None

    monkeypatch.setattr(main_agent, "load_session", load_session)
    monkeypatch.setattr(main_agent, "commit_transcript", nothing)
    monkeypatch.setattr(agents, "record_round_cost", nothing)

    # 最后一次定到一天以后：测试结束时它还躺在延时交换机里，这个 broker 活不到那时候，
    # 不会有一条迟到的自定消息落进后面用例的收件箱。
    runner = ScriptedRunner([2.0, 1.5, 86_400.0])
    monkeypatch.setattr(agents, "build_runner", lambda config, tools: runner)

    load_world_wiring()
    return runner


async def test_world_wakes_on_start_on_its_own_time_and_on_messages_and_skips_replaced_wakes(
    world_process,
):
    runner = world_process

    # 启动：私有状态里什么都没有 → 立刻醒一次。
    await start_messaging()
    await eventually(lambda: len(runner.round_inputs) >= 1, timeout=10)
    assert "进程刚启动" in runner.round_inputs[0]

    # 那一轮定了 2 秒后醒 → 到点醒了第二轮。
    await eventually(lambda: len(runner.round_inputs) >= 2, timeout=10)
    assert "你给自己排的一次醒来" in runner.round_inputs[1]

    # 第二轮收完尾（1.5 秒后那次记进了状态）再往下走：替身模型开始跑时这一轮还没定时刻。
    def second_round_done():
        latest = wake.read_next_wake()
        return latest is not None and "1.5 秒后" in latest.reason

    await eventually(second_round_done, timeout=10)
    replaced = wake.read_next_wake()  # 第二轮定的 1.5 秒后

    # 在那之前有人发来消息 → 第三轮，定到一天后，1.5 秒那条被取代。
    delivery = await send(sender="operator", recipient="world", body="有人敲门。")
    await eventually(lambda: len(runner.round_inputs) >= 3, timeout=10)
    assert "有人敲门。" in runner.round_inputs[2]
    assert "1.5 秒后" in runner.round_inputs[2]  # 它看得到自己原来定的那次

    # 被取代的那条照样送到了，但没有再跑一轮。
    async def replaced_was_delivered():
        rows = await read_record(message_id=replaced.message_id)
        return "delivered" in [r["outcome"] for r in rows]

    await eventually(replaced_was_delivered, timeout=10)
    await asyncio.sleep(1.0)
    assert len(runner.round_inputs) == 3
    current = wake.read_next_wake()
    assert current.message_id != replaced.message_id
    assert current.at > now_cst() + timedelta(seconds=30)

    # 每一轮都对得上一个醒来原因：记录者里一条送到 world 的消息。
    rows = await read_record(participant="world", limit=200)
    delivered_to_world = [
        r for r in rows if r["recipient"] == "world" and r["outcome"] == "delivered"
    ]
    assert delivery.message_id in [r["message_id"] for r in delivered_to_world]
    assert len({r["message_id"] for r in delivered_to_world}) == 4  # 启动、自定、消息、被取代的


# ---------------------------------------------------------------------------
# 一轮最终进了死信
# ---------------------------------------------------------------------------


class RunnerFailingOn:
    """``fails(round_input)`` 为真的那一轮抛错；其余按顺序取一个"再过几秒醒"。"""

    def __init__(self, fails, wake_in_seconds: list[float]):
        self.fails = fails
        self.plan = list(wake_in_seconds)
        self.round_inputs: list[str] = []

    async def run(self, messages, *, context, transcript_sink, **_):
        round_input = messages[-1].content
        self.round_inputs.append(round_input)
        if self.fails(round_input):
            raise RuntimeError("这一轮跑不完")
        seconds = self.plan.pop(0)
        with agent_context(context):
            at = now_cst() + timedelta(seconds=seconds)
            await wake_me_at.invoke({"at": at.isoformat(), "reason": f"{seconds} 秒后"})
        reply = Turn(role=Role.ASSISTANT, content="好。")
        transcript_sink.append(reply)
        return reply


def _fast_retry(monkeypatch):
    from app.messaging import receiving
    from app.runtime.wire import RetryPolicy

    monkeypatch.setattr(
        receiving,
        "PROCESSING_RETRY",
        RetryPolicy(n=3, backoff="linear", base_delay_ms=200, max_delay_ms=300, lease_ms=60_000),
    )


async def test_someone_elses_message_dead_lettered_leaves_world_waking_on_its_planned_time(
    world_process, broker, monkeypatch  # noqa: F811
):
    """别人的消息那一轮最终失败：不另排；状态里原定的下次醒来还在，world 到点照常醒。"""
    from app.infra.rabbitmq import ISOLATED_DEAD_LETTERS

    _fast_retry(monkeypatch)
    runner = RunnerFailingOn(lambda s: "发来一条消息" in s, [4.0, 86_400.0])
    monkeypatch.setattr(agents, "build_runner", lambda config, tools: runner)

    await start_messaging()
    await eventually(
        lambda: (w := wake.read_next_wake()) is not None and "4.0 秒后" in w.reason,
        timeout=10,
    )
    planned = wake.read_next_wake()

    await send(sender="operator", recipient="world", body="有人敲门。")
    await eventually(lambda: broker.depth(f"{ISOLATED_DEAD_LETTERS}_{LANE}"), timeout=15)
    assert wake.read_next_wake() == planned, "别人的消息最终失败不该另排醒来"

    await eventually(
        lambda: sum("你给自己排的一次醒来" in s for s in runner.round_inputs) >= 1, timeout=10
    )
    assert runner.round_inputs[-1].count("4.0 秒后") == 1
    assert sum("发来一条消息" in s for s in runner.round_inputs) == 3


async def test_the_latest_wake_failing_again_and_again_is_never_dead_lettered(
    world_process, broker, monkeypatch  # noqa: F811
):
    """重试上限是 3：启动补醒那一轮连着失败 5 次，照样一直重试、每次都记下来，第 6 次跑完。"""
    from app.infra.rabbitmq import ISOLATED_DEAD_LETTERS

    _fast_retry(monkeypatch)
    failures = {"left": 5}

    def fails(round_input):
        if failures["left"]:
            failures["left"] -= 1
            return True
        return False

    runner = RunnerFailingOn(fails, [86_400.0])
    monkeypatch.setattr(agents, "build_runner", lambda config, tools: runner)

    await start_messaging()
    started = None

    def started_wake():
        nonlocal started
        started = started or wake.read_next_wake()
        return started

    await eventually(started_wake, timeout=10)
    await eventually(
        lambda: (w := wake.read_next_wake()) is not None and "86400.0 秒后" in w.reason,
        timeout=30,
    )

    assert len(runner.round_inputs) == 6
    assert await broker.depth(f"{ISOLATED_DEAD_LETTERS}_{LANE}") == 0
    rows = await read_record(message_id=started.message_id)
    assert [r["outcome"] for r in rows].count("retrying") == 5


# ---------------------------------------------------------------------------
# 一轮进行中到的几条消息，下一轮一起处理
# ---------------------------------------------------------------------------


class HeldFirstRound:
    """第一轮卡在模型那一段，直到放行；每轮记下眼前那段话，定到一天以后。"""

    def __init__(self):
        self.release = asyncio.Event()
        self.round_inputs: list[str] = []

    async def run(self, messages, *, context, transcript_sink, **_):
        self.round_inputs.append(messages[-1].content)
        if len(self.round_inputs) == 1:
            await self.release.wait()
        with agent_context(context):
            at = now_cst() + timedelta(days=1)
            await wake_me_at.invoke({"at": at.isoformat(), "reason": "明天再看。"})
        reply = Turn(role=Role.ASSISTANT, content="好。")
        transcript_sink.append(reply)
        return reply


async def test_messages_arriving_while_a_round_runs_are_taken_together_by_one_round(
    world_process, broker, test_db, monkeypatch  # noqa: F811
):
    from sqlalchemy import text

    from app.world import pending

    runner = HeldFirstRound()
    monkeypatch.setattr(agents, "build_runner", lambda config, tools: runner)

    await start_messaging()
    await eventually(lambda: len(runner.round_inputs) == 1, timeout=10)  # 启动补醒那一轮在跑

    bodies = ["千凪在厨房煮乌冬。", "绫奈在客厅看书。", "赤尾出门了。"]
    sent = [await send(sender="operator", recipient="world", body=b) for b in bodies]
    # 三次投递都到了处理函数手里、收下了，都在等。
    await eventually(lambda: len(pending.read()) == 3, timeout=10)
    runner.release.set()

    await eventually(lambda: len(runner.round_inputs) == 2, timeout=10)
    second = runner.round_inputs[1]
    assert sorted(bodies, key=second.index) == bodies

    # 三次投递各自都处理成功了，没有哪一次再起一轮。
    async def states() -> list[str]:
        async with test_db.begin() as conn:
            rows = await conn.execute(
                text(
                    "SELECT state FROM runtime_inflight "
                    "WHERE edge_id = :edge AND idempotent_key = ANY(:keys)"
                ),
                {"edge": f"inbox:world@{LANE}", "keys": [d.message_id for d in sent]},
            )
            return [r[0] for r in rows]

    async def all_succeeded():
        return await states() == ["succeeded"] * 3

    await eventually(all_succeeded, timeout=10)
    await asyncio.sleep(1.0)
    assert len(runner.round_inputs) == 2
    assert pending.read() == []
