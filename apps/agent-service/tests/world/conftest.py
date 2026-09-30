"""world 测试共用：一块临时的私有卷，进程在某条泳道上。"""
from __future__ import annotations

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
