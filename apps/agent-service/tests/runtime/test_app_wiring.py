"""每个 App 只加载属于自己的那部分接线。

同一个镜像跑出几个 App（agent-service 之外，将来有 world）。一个 App 的进程启动时
只 import 它自己声明的接线模块：world 的进程里不出现 life 的任何代码，也就不会建
life 的表、跑 life 的钟、开 life 的收件箱。

"加载了什么"只能在一个干净进程里看：本测试进程早就把 ``app.wiring`` 全导进来了。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from app.runtime.engine import Runtime

_PROBE = (
    "import sys, json;"
    "from unittest.mock import patch, MagicMock;"
    "patch('inner_shared.logger.setup_logging', MagicMock()).start();"
    "from app.runtime.bootstrap import load_dataflow_graph;"
    "load_dataflow_graph({app!r});"
    "import app.workers.runtime_entry, app.main, app.messaging.lifecycle;"
    "from app.runtime.wire import WIRING_REGISTRY;"
    "from app.messaging.receiving import INBOX_REGISTRY;"
    "print(json.dumps({{"
    "'living': sorted(m for m in sys.modules if m == 'app.living' or m.startswith('app.living.')),"
    "'wired_types': sorted(w.data_type.__module__ + '.' + w.data_type.__name__ for w in WIRING_REGISTRY),"
    "'inboxes': sorted(INBOX_REGISTRY)}}))"
)


def _loaded_by(app_name: str) -> dict:
    env = dict(os.environ)
    env.pop("LANE", None)
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE.format(app=app_name)],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_world_app_loads_none_of_the_life_code():
    loaded = _loaded_by("world")

    assert loaded["living"] == [], (
        "world 的进程里出现了 life 的代码：" + ", ".join(loaded["living"])
    )
    assert loaded["wired_types"] == []
    assert loaded["inboxes"] == []


def test_the_agent_service_app_loads_its_own_wiring():
    loaded = _loaded_by("agent-service")

    assert "app.living.moment" in loaded["living"]
    assert "app.living.moment.LifeMomentTick" in loaded["wired_types"]
    assert loaded["inboxes"] == ["operator"]


def test_an_app_nobody_declared_cannot_load():
    from app.runtime.bootstrap import load_dataflow_graph

    with pytest.raises(RuntimeError, match="no-such-app"):
        load_dataflow_graph("no-such-app")


async def test_prepare_for_run_loads_the_wiring_of_the_app_it_boots():
    from app.runtime.bootstrap import prepare_for_run

    loaded: list[str] = []
    with patch(
        "app.runtime.bootstrap.load_dataflow_graph",
        lambda app_name: loaded.append(app_name) or MagicMock(),
    ):
        await prepare_for_run("world")

    assert loaded == ["world"]


def test_every_declared_app_is_a_known_app():
    from app.deployment import APP_WIRING
    from app.runtime.placement import known_apps

    assert set(APP_WIRING) == {"agent-service", "world"}
    assert set(APP_WIRING) <= known_apps()


async def test_an_app_without_any_node_still_boots_its_runtime():
    """world 第一期可能一个 dataflow 节点都没有（它靠收件箱醒），运行时照样起得来。"""
    rt = Runtime(app_name="world", migrate_schema_on_run=False)
    await rt.start_source_loops()
    await rt.stop_source_loops()
