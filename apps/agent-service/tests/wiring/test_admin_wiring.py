"""Admin wiring acceptance.

旧 life-tick / glimpse / schedule 触发 + schedule CRUD 路由已随 world/life
重写删除；voice 触发随 voice 子系统拆除一并删除。剩 search（DLQ admin 在
test_dlq_admin 覆盖）。
"""
from __future__ import annotations

import importlib


def _reload_admin_wiring():
    """Reset registry and re-import wiring so routes are clean per test.

    Reload the admin submodule directly: importlib.reload(parent_package)
    doesn't re-execute submodules because the names are already cached
    in sys.modules. Mirrors the pattern in test_safety_wiring.py /
    test_memory.py.
    """
    import app.wiring.admin as a
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring

    clear_wiring()
    clear_bindings()
    importlib.reload(a)


def test_admin_wiring_registers_all_paths():
    _reload_admin_wiring()
    from fastapi import FastAPI

    from app.runtime.http_source import register_http_sources

    app = FastAPI()
    register_http_sources(app)

    paths_methods = set()
    for r in app.routes:
        methods = (getattr(r, "methods", set()) or set()) - {"HEAD"}
        for m in methods:
            paths_methods.add((r.path, m))

    expected = {
        ("/admin/search", "POST"),
    }
    missing = expected - paths_methods
    assert not missing, f"missing wires: {missing}"

    # 旧 life / glimpse / schedule / voice 路由必须已删干净。
    deleted = {
        ("/admin/trigger-voice", "POST"),
        ("/admin/trigger-life-engine-tick", "POST"),
        ("/admin/trigger-glimpse", "POST"),
        ("/admin/debug-glimpse", "POST"),
        ("/admin/trigger-schedule", "POST"),
        ("/api/schedule", "GET"),
        ("/api/schedule", "POST"),
        ("/api/schedule/current", "GET"),
        ("/api/schedule/daily/{target_date}", "GET"),
        ("/api/schedule/{schedule_id}", "DELETE"),
    }
    leftover = deleted & paths_methods
    assert not leftover, f"deleted routes still wired: {leftover}"


def test_routes_py_only_health():
    """routes.py 不能再有 admin/api endpoint。"""
    import app.api.routes as r

    importlib.reload(r)
    paths = {route.path for route in r.router.routes}
    assert paths == {"/health"}, f"routes.py paths drift: {paths}"
