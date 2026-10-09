"""world 作为一个插件（:mod:`app.plugins.world`）：world App 的清单只有它；它开设的收件箱怎么声明、
登记哪几条路由和知识来源；每次起来都有自己的一轮接一轮，停下时登记的东西全部撤掉；加载它不带进
life 的代码。

"每次起来"是同一个宿主停了再起：宿主每次起来都重新跑一遍 setup，就是一个新的 world 进程。
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from contextlib import suppress
from datetime import timedelta
from pathlib import Path

from app.messaging.message import Kind, new_message
from app.messaging.receiving import INBOX_REGISTRY
from app.world.rounds import Rounds
from app.world.sources import enabled_sources
from tests.hosting import start_without_io
from tests.messaging.helpers import eventually

from .conftest import sets_wake

AGENT_SERVICE = Path(__file__).resolve().parents[2]


def _from(sender: str, body: str):
    return new_message(sender=sender, recipient="world", body=body, kind=Kind.MESSAGE)


def _rounds_now() -> Rounds:
    """现在 world 收件箱的处理函数背后的那个 :class:`Rounds`。"""
    handler = INBOX_REGISTRY["world"].on_message
    assert handler.__func__ is Rounds.receive
    return handler.__self__


# ---------------------------------------------------------------------------
# 登记了什么
# ---------------------------------------------------------------------------


def test_the_world_app_runs_only_the_world_plugin():
    from app.deployment import APPS

    assert APPS["world"] == ("app.plugins.world",)


async def test_world_opens_one_inbox_whose_deliveries_wait_for_the_rounds_that_take_them(
    app_host,
):
    from app.messaging.receiving import PROCESSING_RETRY, _lease_ms
    from app.world.answer import answer_question
    from app.world.main_agent import ROUND_TIMEOUT
    from app.world.volume import writer_lock
    from app.world.wake import retry_latest_wake_without_limit, wake_on_start

    host = await app_host("world")

    assert [r.name for r in host.registered() if r.kind == "inbox"] == ["world"]
    spec = INBOX_REGISTRY["world"]
    rounds = _rounds_now()
    assert spec.on_question is answer_question
    assert spec.on_open is wake_on_start
    # 状态里的最新唤醒失败时不限次数重试，永不进死信；别的消息照常有限次重试。
    assert spec.retry_without_limit is retry_latest_wake_without_limit
    # 只在拿着卷的写锁时消费；启动补醒（on_open）也在拿到锁之后才跑。
    assert spec.consume_while is writer_lock
    # 一次投递可能要等前面正在跑的一轮、再等带着它的这一轮：处理时限和租约都要放长到这之上，
    # 超过通信机制默认的 15 分钟租约。
    assert spec.processing_timeout == rounds.delivery_timeout
    assert spec.processing_timeout >= 2 * ROUND_TIMEOUT
    assert spec.processing_timeout > timedelta(milliseconds=PROCESSING_RETRY.lease_ms)
    assert _lease_ms(spec) > spec.processing_timeout.total_seconds() * 1000


async def test_a_world_delivery_fits_inside_the_delivery_deadline(app_host):
    """world 一次投递最多等两轮（它的处理时限），再加上领取和记结果的余量，要放进通信机制给一次投递
    的期限（:data:`app.messaging.receiving.DELIVERY_DEADLINE`）；放不进去，开设收件箱时就拒绝。

    期限比 broker 等确认的时限短，到了就只把这一条交还重投，期限之后要做的也都落在 broker 的时限
    之内（``tests/messaging/test_delivery_deadline.py``）。所以一次投递没确认的全部时间，包括领取和
    记结果那几次查库，都在 broker 的时限之内：broker 不会在 world 正常等轮的时候关掉整个通道，连带
    取消同一通道上正在跑的一轮（2026-10-06 在 coe-world 上，一轮的时限是 30 分钟时）。"""
    from app.infra.rabbitmq import BROKER_ACK_TIMEOUT_MS
    from app.messaging.receiving import CLAIM_AND_SETTLE_ROOM, DELIVERY_DEADLINE

    await app_host("world")

    spec = INBOX_REGISTRY["world"]
    assert spec.processing_timeout + CLAIM_AND_SETTLE_ROOM <= DELIVERY_DEADLINE
    assert DELIVERY_DEADLINE < timedelta(milliseconds=BROKER_ACK_TIMEOUT_MS)


async def test_the_four_record_routes_carry_all_three_checks(app_host):
    """记录的人工读写接口：四条都要内网凭据、核对泳道、回答带泳道（行为在 ``test_admin.py``）。"""
    from app.world import admin

    host = await app_host("world")

    routes = {
        r.name: (r.detail["request"], r.detail["handler"])
        for r in host.registered()
        if r.kind == "route"
    }
    assert routes == {
        "GET /admin/world/records": (admin.RecordListRequest, admin.record_listing_node),
        "GET /admin/world/records/document": (admin.RecordReadRequest, admin.record_read_node),
        "PUT /admin/world/records/document": (admin.RecordWriteRequest, admin.record_write_node),
        "DELETE /admin/world/records/document": (
            admin.RecordDeleteRequest,
            admin.record_delete_node,
        ),
    }
    assert all(
        r.detail["inner_secret"] and r.detail["lane_match"] and r.detail["answers_with_lane"]
        for r in host.registered()
        if r.kind == "route"
    )


# ---------------------------------------------------------------------------
# 每次起来一个新的，停下时全部撤掉
# ---------------------------------------------------------------------------


async def test_each_start_builds_its_own_rounds(app_host):
    host = await app_host("world")
    first = _rounds_now()
    await host.stop()
    await start_without_io(host)
    second = _rounds_now()

    assert first is not second


async def test_a_round_left_running_by_the_previous_start_does_not_hold_up_the_next(world):
    """上一次起来时没跑完的那一轮还拿着它那把锁；再起来的 world 不等它。"""
    gate = asyncio.Event()

    async def plan():
        if len(world.runner.runs) == 1:
            await gate.wait()
        return await sets_wake()()

    world.runner.plan = plan
    host = world.host
    left_running = asyncio.create_task(INBOX_REGISTRY["world"].on_message(_from("赤尾", "我出门了。")))
    try:
        await eventually(lambda: len(world.runner.runs) == 1, timeout=5)
        await host.stop()

        await start_without_io(host)
        await asyncio.wait_for(
            INBOX_REGISTRY["world"].on_message(_from("千凪", "我在做饭。")), timeout=5
        )
    finally:
        left_running.cancel()
        with suppress(asyncio.CancelledError):
            await left_running

    assert len(world.runner.runs) == 2
    assert "我在做饭。" in world.runner.runs[1][-1].content


async def test_stop_takes_back_the_sources_the_inbox_and_the_routes(app_host, monkeypatch):
    from inner_shared.dynamic_config import dynamic_config

    monkeypatch.setattr(dynamic_config, "get", lambda k, default="": default)
    host = await app_host("world")
    assert [s.name for s in await enabled_sources()] == ["records", "reality", "told"]

    await host.stop()

    assert host.registered() == ()
    assert "world" not in INBOX_REGISTRY
    assert await enabled_sources() == []


def test_importing_the_plugin_loads_no_life_code():
    """world 的进程只加载这个插件。它带进 life 的代码，world 的进程就又背上了 life。"""
    probe = (
        "import json, sys; import app.plugins.world; "
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('app.'))))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=AGENT_SERVICE,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    loaded = json.loads(out.stdout.strip().splitlines()[-1])

    assert [m for m in loaded if m == "app.living" or m.startswith("app.living.")] == []
    assert "app.plugins.world" in loaded
