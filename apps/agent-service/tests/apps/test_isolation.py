"""每个 App 只加载属于自己的那部分代码。

同一个镜像跑出几个 App（agent-service、world）。一个 App 的进程启动时按它在
``app.deployment.APPS`` 里的清单起插件宿主，只 import 清单里的插件模块：world 的进程里不出现
life 的任何代码，也就不会建 life 的表、跑 life 的钟、开 life 的收件箱；反过来也一样。

"加载了什么"只能在一个干净进程里看：本测试进程早就把两边的代码全导进来了。探针按线上的
样子起宿主（:func:`tests.hosting.in_a_fresh_process`），只是不碰数据库、broker、HTTP，不起钟和
后台任务。
"""
from __future__ import annotations

import json

import pytest

from tests.hosting import in_a_fresh_process

_LOADED = """
import json, sys
from app.messaging.receiving import INBOX_REGISTRY
rs = host.registered()
print(json.dumps({
    'living': sorted(m for m in sys.modules if m == 'app.living' or m.startswith('app.living.')),
    'world': sorted(m for m in sys.modules if m == 'app.world' or m.startswith('app.world.')),
    'plugins': [p.name for p in host.plugins],
    'clocks': [r.name for r in rs if r.kind == 'clock'],
    'wired_types': sorted(
        r.detail['data_type'].__module__ + '.' + r.detail['data_type'].__name__
        for r in rs if r.kind in ('durable', 'outbound')),
    'inboxes': sorted(INBOX_REGISTRY),
    'inboxes_at_start': sorted(
        r.detail['open'].__module__ + '.' + r.detail['open'].__qualname__
        for r in rs if r.kind == 'inboxes_at_start'),
}))
"""


def _loaded_by(app_name: str) -> dict:
    out = in_a_fresh_process(app_name, _LOADED)
    return json.loads(out.strip().splitlines()[-1])


def test_the_world_app_loads_none_of_the_life_code():
    loaded = _loaded_by("world")

    assert loaded["living"] == [], (
        "world 的进程里出现了 life 的代码：" + ", ".join(loaded["living"])
    )
    assert loaded["plugins"] == ["world"]
    assert loaded["inboxes"] == ["world"]
    assert loaded["inboxes_at_start"] == [], "world 的进程不开三姐妹的收件箱"
    assert loaded["clocks"] == []
    assert all(t.startswith("app.world.") for t in loaded["wired_types"])


def test_the_agent_service_app_loads_none_of_the_world_code():
    loaded = _loaded_by("agent-service")

    assert loaded["world"] == [], (
        "agent-service 的进程里出现了 world 的代码：" + ", ".join(loaded["world"])
    )


def test_the_agent_service_app_loads_its_own_plugins():
    loaded = _loaded_by("agent-service")

    assert loaded["plugins"] == ["ops", "operator", "skills", "living"]
    assert "app.living.moment" in loaded["living"]
    assert "LifeMomentTick" in loaded["clocks"]
    # 三姐妹的收件箱名字在人设表里，setup 时只声明"开始接收时再开"，所以起完 setup 只看得到
    # ``operator``；开出来的是哪几个名字由 ``tests/living/test_received.py`` 验。
    assert loaded["inboxes"] == ["operator"]
    assert loaded["inboxes_at_start"] == ["app.living.received.open_inboxes"]


def test_an_app_nobody_declared_cannot_start():
    from app.host import Host, UnknownApp

    with pytest.raises(UnknownApp, match="no-such-app"):
        Host.for_app("no-such-app")


def test_every_declared_app_is_a_known_app():
    from app.deployment import APPS, DEFAULT_APP

    assert set(APPS) == {"agent-service", "world"}
    assert DEFAULT_APP in APPS
