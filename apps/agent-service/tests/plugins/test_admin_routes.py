"""``app/api/routes.py`` 只挂 ``/health``：运维路由都由插件宿主挂（``app.plugins.ops``、通信机制的人工
入口），完整的路由集合和各自的检查由 ``tests/apps/test_surface.py`` 按进程钉住。
"""
from __future__ import annotations

import importlib


def test_routes_py_only_health():
    """routes.py 不能再有 admin/api endpoint。"""
    import app.api.routes as r

    importlib.reload(r)
    paths = {route.path for route in r.router.routes}
    assert paths == {"/health"}, f"routes.py paths drift: {paths}"
