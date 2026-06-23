"""书接入 HTTP wiring 验收 — 读小说 Task 1.

channel-server 把飞书私聊文件下载后投到 ``POST /api/internal/book/ingest``。这条路由
必须经 ``Source.http(response=True)`` 在 runtime 注册成 FastAPI endpoint，并接到
``book_ingest_node``。
"""

from __future__ import annotations

import importlib


def _reload_book_wiring():
    import app.wiring.book as b
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring

    clear_wiring()
    clear_bindings()
    importlib.reload(b)


def test_book_ingest_route_registered():
    _reload_book_wiring()
    from fastapi import FastAPI

    from app.runtime.http_source import register_http_sources

    app = FastAPI()
    register_http_sources(app)

    paths_methods = set()
    for r in app.routes:
        methods = (getattr(r, "methods", set()) or set()) - {"HEAD"}
        for m in methods:
            paths_methods.add((r.path, m))

    assert ("/api/internal/book/ingest", "POST") in paths_methods
