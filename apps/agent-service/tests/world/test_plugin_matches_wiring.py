"""world 的插件（:mod:`app.plugins.world`）登记的，和旧的接线（:mod:`app.world.wiring`）登记的是
同一套：同样的收件箱和每一个选项、同样的四条路由和它们的检查、同样的知识来源。旧接线删掉时，
这个文件跟着删。

处理函数是同一个函数（:meth:`Rounds.receive`），绑在各自的 ``Rounds`` 上：插件每次起来新建一个，
所以除了比对象，还让一组投递分别经过两边，看它们跑出同样的轮。
"""
from __future__ import annotations

import asyncio
import dataclasses

import pytest

from app.host import Host
from app.messaging.message import Kind, new_message
from app.messaging.receiving import INBOX_REGISTRY
from app.runtime.wire import WIRING_REGISTRY
from app.world import pending
from app.world.rounds import Rounds
from app.world.sources import enabled_sources
from tests.messaging.helpers import eventually

from .conftest import load_world_wiring, sets_wake, start_without_io


def _from(sender: str, body: str):
    return new_message(sender=sender, recipient="world", body=body, kind=Kind.MESSAGE)


def _wiring_routes() -> dict[tuple[str, str], dict]:
    routes = {}
    for w in WIRING_REGISTRY:
        for src in w.sources:
            if src.kind != "http":
                continue
            # 宿主的路由都是"处理函数的回答就是 200 的正文"，旧接线里也只有这一种。
            assert src.params["response"] is True
            (consumer,) = w.consumers
            routes[(src.params["method"], src.params["path"])] = {
                "request": w.data_type,
                "handler": consumer,
                "inner_secret": src.params.get("requires_inner_secret", False),
                "lane_match": src.params.get("requires_lane_match", False),
                "answers_with_lane": src.params.get("answers_with_lane", False),
            }
    return routes


def _plugin_routes(host: Host) -> dict[tuple[str, str], dict]:
    return {
        (r.detail["method"], r.detail["path"]): {
            k: r.detail[k]
            for k in ("request", "handler", "inner_secret", "lane_match", "answers_with_lane")
        }
        for r in host.registered()
        if r.kind == "route"
    }


async def test_the_plugin_opens_the_world_inbox_with_every_option_of_the_wiring(world_plugin):
    load_world_wiring()
    old = INBOX_REGISTRY["world"]
    host = await world_plugin()
    new = INBOX_REGISTRY["world"]
    (registered,) = [r for r in host.registered() if r.kind == "inbox"]

    # 处理函数之外的每一个选项都是同一个对象（处理时限是同一个值）。
    assert dataclasses.replace(new, on_message=None) == dataclasses.replace(old, on_message=None)
    assert registered.name == "world"
    assert registered.detail["on_message"] == new.on_message
    # 处理函数是同一个函数，绑在一个新的 Rounds 上。
    assert new.on_message.__func__ is old.on_message.__func__ is Rounds.receive
    assert new.on_message.__self__ is not old.on_message.__self__


async def test_the_plugin_registers_the_four_record_routes_of_the_wiring_with_their_checks(
    world_plugin,
):
    load_world_wiring()
    old = _wiring_routes()
    new = _plugin_routes(await world_plugin())

    assert len(old) == 4
    assert new == old


async def test_the_plugin_registers_the_knowledge_sources_of_the_wiring_in_its_order(
    world_plugin, monkeypatch
):
    from inner_shared.dynamic_config import dynamic_config

    monkeypatch.setattr(dynamic_config, "get", lambda k, default="": default)
    load_world_wiring()
    old = await enabled_sources()
    await world_plugin()
    new = await enabled_sources()

    assert len(old) == 3
    assert [id(s) for s in new] == [id(s) for s in old]


async def test_the_plugin_does_not_start_in_a_process_that_loaded_the_old_wiring():
    """两份都加载就有两个 ``Rounds``；收件箱和来源都只能登记一次，所以起不来，而不是悄悄用上其中
    一个。"""
    load_world_wiring()
    from app.plugins.world import PLUGIN

    host = Host("world", [PLUGIN])
    with pytest.raises(ValueError, match="already registered"):
        await start_without_io(host)


@pytest.mark.parametrize("loaded_by", ["wiring", "plugin"])
async def test_deliveries_run_the_same_rounds_through_either(world, world_plugin, loaded_by):
    """一轮进行中到的两条，由下一轮一起处理：两边一样。"""
    if loaded_by == "plugin":
        await world_plugin()
        world.handler = INBOX_REGISTRY["world"].on_message
    gate = asyncio.Event()

    async def plan():
        if len(world.runner.runs) == 1:
            await gate.wait()
        return await sets_wake()()

    world.runner.plan = plan
    running = asyncio.create_task(world.deliver(_from("赤尾", "我出门了。")))
    await eventually(lambda: len(world.runner.runs) == 1, timeout=5)
    later = [_from("千凪", "我在做饭。"), _from("绫奈", "我在看书。")]
    waiting = []
    for message in later:
        waiting.append(asyncio.create_task(world.deliver(message)))
        await eventually(
            lambda m=message: m.message_id in {p.message_id for p in pending.read()},
            timeout=5,
        )
    gate.set()
    outcomes = await asyncio.wait_for(asyncio.gather(running, *waiting), timeout=5)

    assert outcomes == [None, None, None]
    assert len(world.runner.runs) == 2
    first, second = (run[-1].content for run in world.runner.runs)
    assert "我出门了。" in first
    assert "这一轮有 2 条消息" in second
    assert "我在做饭。" in second and "我在看书。" in second
    assert "我出门了。" not in second
    assert pending.read() == []
