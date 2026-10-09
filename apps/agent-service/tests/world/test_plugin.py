"""world 作为一个插件（:mod:`app.plugins.world`）：每次起来都有自己的一轮接一轮，停下时登记的
东西全部撤掉，加载它既不带进 life 的代码，也不带进旧的接线。

"每次起来"是同一个宿主停了再起：宿主每次起来都重新跑一遍 setup，就是一个新的 world 进程。
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

from app.messaging.message import Kind, new_message
from app.messaging.receiving import INBOX_REGISTRY
from app.world.rounds import Rounds
from app.world.sources import enabled_sources
from tests.messaging.helpers import eventually

from .conftest import sets_wake, start_without_io

AGENT_SERVICE = Path(__file__).resolve().parents[2]


def _from(sender: str, body: str):
    return new_message(sender=sender, recipient="world", body=body, kind=Kind.MESSAGE)


def _rounds_now() -> Rounds:
    """现在 world 收件箱的处理函数背后的那个 :class:`Rounds`。"""
    handler = INBOX_REGISTRY["world"].on_message
    assert handler.__func__ is Rounds.receive
    return handler.__self__


async def test_each_start_builds_its_own_rounds(world_plugin):
    host = await world_plugin()
    first = _rounds_now()
    await host.stop()
    await start_without_io(host)
    second = _rounds_now()

    assert first is not second


async def test_a_round_left_running_by_the_previous_start_does_not_hold_up_the_next(
    world, world_plugin
):
    """上一次起来时没跑完的那一轮还拿着它那把锁；再起来的 world 不等它。"""
    gate = asyncio.Event()

    async def plan():
        if len(world.runner.runs) == 1:
            await gate.wait()
        return await sets_wake()()

    world.runner.plan = plan
    host = await world_plugin()
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


async def test_stop_takes_back_the_sources_the_inbox_and_the_routes(world_plugin, monkeypatch):
    from inner_shared.dynamic_config import dynamic_config

    monkeypatch.setattr(dynamic_config, "get", lambda k, default="": default)
    host = await world_plugin()
    assert [s.name for s in await enabled_sources()] == ["records", "reality", "told"]

    await host.stop()

    assert host.registered() == ()
    assert "world" not in INBOX_REGISTRY
    assert await enabled_sources() == []


def test_importing_the_plugin_loads_no_life_code_and_not_the_old_wiring():
    """world 的进程只加载这个插件。它带进 life 的代码，world 的进程就又背上了 life；它带进旧的
    接线（``app.world.wiring``），模块体就会再开一个 ``world`` 收件箱、建另一个 ``Rounds``。"""
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

    forbidden = ("app.living", "app.wiring", "app.world.wiring")
    assert [m for m in loaded if any(m == f or m.startswith(f + ".") for f in forbidden)] == []
    assert "app.plugins.world" in loaded
