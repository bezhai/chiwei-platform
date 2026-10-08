"""T1 acceptance on a real broker: after stop, a plugin's clocks, tasks and consumers have
stopped and nothing it registered is left; a start that fails half-way cleans up without hanging.

The consumer counts are read on a separate connection just before the host closes the shared
one: closing the connection would drop every consumer anyway, so counting afterwards would not
show that the messaging drain and the durable stop did their job.
"""
from __future__ import annotations

import asyncio
from typing import Annotated

import aio_pika
import pytest

from app.host import Host
from app.infra.rabbitmq import lane_queue, mq
from app.messaging import receiving
from app.messaging.broker import SCHEDULED, inbox_route, question_route
from app.runtime import Data, Key, durable, node
from app.runtime.wire import WIRING_REGISTRY, WireSpec
from tests.messaging.conftest import LANE
from tests.messaging.helpers import eventually

from .conftest import plugin


class _Probe(Data):
    pid: Annotated[str, Key]


@node
async def _probe_read(p: _Probe) -> None:  # pragma: no cover - nothing is published here
    raise AssertionError


async def _ignore(message) -> None:  # pragma: no cover - nothing is sent here
    raise AssertionError


QUEUES = [
    lane_queue(inbox_route("probe").queue, LANE),
    lane_queue(question_route("probe").queue, LANE),
    lane_queue(SCHEDULED.queue, LANE),
    lane_queue(durable._route_for(WireSpec(_Probe), _probe_read).queue, LANE),
]


async def _consumer_counts(amqp_url: str) -> dict[str, int]:
    connection = await aio_pika.connect(amqp_url)
    try:
        counts: dict[str, int] = {}
        for name in QUEUES:
            channel = await connection.channel()
            queue = await channel.declare_queue(name, passive=True)
            counts[name] = queue.declaration_result.consumer_count
            await channel.close()
        return counts
    finally:
        await connection.close()


def _nothing_left(host: Host) -> None:
    assert host.registered() == ()
    assert receiving.INBOX_REGISTRY == {}
    assert receiving.INBOXES_AT_START == []
    assert WIRING_REGISTRY == []
    assert durable._consumer_tags == []
    assert receiving._consumers == []


async def test_after_stop_clocks_tasks_and_consumers_have_stopped(
    broker, delayed_broker, monkeypatch
):
    monkeypatch.setenv("DATAFLOW_ENABLE_TIME_SOURCES", "1")
    amqp_url = delayed_broker[0]
    ticks: list[object] = []
    task_cancelled = asyncio.Event()

    async def note(ts) -> None:
        ticks.append(ts)

    async def resident() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            task_cancelled.set()
            raise

    def setup(ctx) -> None:
        ctx.clock("probe", 0.05, note)
        ctx.task("resident", resident)
        ctx.inbox("probe", on_message=_ignore)
        ctx.durable(_Probe, _probe_read)

    counted_at_close: dict[str, int] = {}
    real_close = mq.close

    async def count_then_close() -> None:
        if not counted_at_close:
            counted_at_close.update(await _consumer_counts(amqp_url))
        await real_close()

    monkeypatch.setattr(mq, "close", count_then_close)
    host = Host("agent-service", [plugin("probe", setup)])
    await host.start(http=None, schema=False, mq=True, clocks=True, tasks=True)
    assert await _consumer_counts(amqp_url) == dict.fromkeys(QUEUES, 1)
    await eventually(lambda: len(ticks) >= 2, timeout=5)
    clock_tasks = [t for t in asyncio.all_tasks() if t.get_name().startswith("clock[")]
    assert clock_tasks

    await host.stop()
    fired = len(ticks)
    await asyncio.sleep(0.2)

    assert all(t.done() for t in clock_tasks)
    assert len(ticks) == fired, "a tick fired after stop"
    assert task_cancelled.is_set()
    assert counted_at_close == dict.fromkeys(QUEUES, 0), "consumers outlived their stop phase"
    _nothing_left(host)


async def test_a_start_that_fails_opening_an_inbox_cleans_up_and_can_start_again(
    broker, delayed_broker
):
    """Risk 2: the messaging start fails after the scheduled queue, the question queue and the
    durable consumer are already consuming; the cleanup must stop them, not hang, and leave the
    process able to start again."""
    attempts: list[int] = []

    async def on_open() -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("启动检查失败")

    def setup(ctx) -> None:
        ctx.inbox("probe", on_message=_ignore, on_open=on_open)
        ctx.durable(_Probe, _probe_read)

    host = Host("agent-service", [plugin("probe", setup)])
    start = host.start(http=None, schema=False, mq=True, clocks=False, tasks=False)
    with pytest.raises(RuntimeError, match="启动检查失败"):
        await asyncio.wait_for(start, timeout=30)
    _nothing_left(host)

    await asyncio.wait_for(
        host.start(http=None, schema=False, mq=True, clocks=False, tasks=False), timeout=30
    )
    try:
        assert await _consumer_counts(delayed_broker[0]) == dict.fromkeys(QUEUES, 1)
    finally:
        await asyncio.wait_for(host.stop(), timeout=60)
    assert attempts == [1, 1]
    _nothing_left(host)
