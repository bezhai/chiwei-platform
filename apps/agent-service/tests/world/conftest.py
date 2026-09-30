"""world 测试共用：一块临时的私有卷，进程在某条泳道上；以及重新执行一遍 world 的接线。"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

LANE = "coe-world"


@pytest.fixture
def volume(tmp_path, monkeypatch) -> Path:
    """``WORLD_DATA_DIR`` 指向一个空目录，进程的部署泳道是 :data:`LANE`。"""
    root = tmp_path / "world-volume"
    root.mkdir()
    monkeypatch.setenv("WORLD_DATA_DIR", str(root))
    monkeypatch.setenv("LANE", LANE)
    return root


def load_world_wiring() -> None:
    """清空三张登记表，再执行一遍 ``app.world.wiring``。

    先 import 再清：这个 worker 第一次 import 它时模块体已经跑过一遍（收件箱、节点绑定
    都登记了），不清就 reload 会撞上"已经登记过"。
    """
    import app.world.wiring as wiring
    from app.messaging.receiving import clear_inboxes
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring

    clear_wiring()
    clear_bindings()
    clear_inboxes()
    importlib.reload(wiring)
