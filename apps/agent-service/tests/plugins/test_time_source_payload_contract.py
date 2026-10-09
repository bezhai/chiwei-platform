"""钟的 tick 形态契约测试 —— 抓"起得来、到钟的循环里第一拍才炸"那类 bug.

钟每拍在循环里调一次登记时给的 ``tick(ts)``（:mod:`app.host.clock`）：living 的钟在那里
**只用 ``data_type(ts=<iso>)`` 构造** payload（:func:`app.plugins.living._ticker`）——
钟上的 Data 必须是带 ``ts: str`` 字段的单字段 tick（正例见 ``app/living/moment.py`` 的
``LifeMomentTick(ts: Annotated[str, Key])``）。

宿主起来时不跑钟，所以一条钟的 Data 形态不对（缺 ts / 有其他必填字段）启动时检测不到——
照样起得来、集成测试照样过，但第一拍 ``tick(ts)`` 就 raise → 钟的循环记下这个错 → watchdog
``os._exit(1)`` → Pod 被杀重启 → 这条钟驱动的整条链路在生产里永远起不来。

这个文件对 agent-service 宿主上**每一条钟**真调一次它的 ``tick(ts)``：构造 payload 在调用
里就发生，交回的节点协程不跑、直接关掉。它能抓住这一整类 bug，不针对某一条具体的钟。
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest

from app.plugins.living import CLOCKS

# parametrize ID 只需要钟的名字（稳定）；测试体自己起宿主、自取那条钟。
_CLOCK_NAMES = [data_type.__name__ for data_type, _seconds, _node in CLOCKS]


async def _clocks(app_host) -> dict:
    host = await app_host("agent-service")
    return {r.name: r for r in host.registered() if r.kind == "clock"}


async def test_the_agent_service_host_has_clocks(app_host):
    """前置健全：宿主上确实有钟，而且就是 living 那一张表，否则下面的契约断言会 vacuously pass。"""
    clocks = await _clocks(app_host)

    assert sorted(clocks) == sorted(_CLOCK_NAMES)
    assert {r.plugin for r in clocks.values()} == {"living"}


@pytest.mark.parametrize("name", _CLOCK_NAMES)
async def test_each_clock_builds_its_tick_from_ts_alone(app_host, name):
    """每条钟的 ``tick(ts)`` 都要能在循环里造出它的 payload.

    调的是宿主上登记的那个 ``tick`` —— 它就是生产里钟每一拍在循环里调的那一个。造不出来
    （缺 ts / 有其他必填字段）它会 raise，等同于生产第一拍杀 Pod。这里把那次"杀 Pod"提前到 CI。
    """
    clock = (await _clocks(app_host))[name]

    work = clock.detail["tick"](datetime.now(tz=UTC))
    try:
        # 交回的是节点的协程，还没开始跑：payload 就是它的参数。
        assert inspect.iscoroutine(work)
        (payload,) = inspect.getcoroutinelocals(work)["args"]
        assert type(payload).__name__ == name
    finally:
        work.close()
