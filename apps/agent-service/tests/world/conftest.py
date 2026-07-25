"""Re-export real-pg fixtures so world integration tests can use ``test_db``.

The session-scoped ``test_db_dsn`` and function-scoped ``test_db`` fixtures
live in ``tests/runtime/conftest.py``. pytest only auto-discovers conftest
fixtures along the path from rootdir to the test file, so tests under
``tests/world/`` cannot see them directly. Re-export here keeps the fixture
definition single-sourced under ``tests/runtime/`` while making it visible
to world tests too.
"""
from __future__ import annotations

import pytest

from tests.runtime.conftest import test_db, test_db_dsn  # noqa: F401


@pytest.fixture(autouse=True)
def _no_daylight_coords(monkeypatch):
    """默认「日照坐标没配」——让 world 用例保持 hermetic（不真去拉 Dynamic Config）。

    每轮 world 推演都会读 ``world_daylight_coords``（见
    :mod:`app.world.daylight`）。真实的 ``dynamic_config.get`` 是同步 httpx，测试里
    会真发一次请求（失败被吞、返回默认值，但白等一次 DNS / 连接超时）。这里统一
    桩成「没配」，等于生产上还没配坐标时的状态——也正好是降级路径的默认口径。

    需要日照锚点的用例自己覆写这个桩（monkeypatch 同一个属性即可，后设的生效）。
    """
    from app.world import daylight as daylight_mod

    monkeypatch.setattr(
        daylight_mod.dynamic_config,
        "get",
        lambda key, *, default="": default,
    )
