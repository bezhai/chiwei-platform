"""agent-service 的运维路由：起它的插件宿主、把路由挂上 app 之后有哪些。

DLQ admin 在 test_dlq_admin 覆盖。完整的路由集合和各自的检查由 ``tests/apps/test_surface.py`` 按进程钉住。
"""
from __future__ import annotations

import importlib

from fastapi import FastAPI


async def test_the_agent_service_host_puts_the_admin_routes_on_the_app(app_host):
    app = FastAPI()
    await app_host("agent-service", http=app)

    paths_methods = set()
    for r in app.routes:
        methods = (getattr(r, "methods", set()) or set()) - {"HEAD"}
        for m in methods:
            paths_methods.add((r.path, m))

    expected = {
        ("/admin/search", "POST"),
    }
    missing = expected - paths_methods
    assert not missing, f"missing routes: {missing}"


def test_routes_py_only_health():
    """routes.py 不能再有 admin/api endpoint。"""
    import app.api.routes as r

    importlib.reload(r)
    paths = {route.path for route in r.router.routes}
    assert paths == {"/health"}, f"routes.py paths drift: {paths}"
