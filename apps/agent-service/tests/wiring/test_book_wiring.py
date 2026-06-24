"""书侧通道 + 书注册物已物理删除 — 负向断言（读小说重设计 Task 1 + Task 2）.

旧的书专用入站侧通道（``POST /api/internal/book/ingest`` → ``book_ingest_node`` →
``ingest_book`` 注册成一本书）被拆掉：文件不再走专用端点，而是和图片同一条媒体轨
（先成为 ``common_message.content`` 的普通文件项 + best-effort 缓存进对象存储）。

Task 2 进一步把整套书注册物删干净：``app/domain/book.py``（BookMeta/BookPage/ingest_book/
derive_book_id/find_book_meta/find_books_by_title/read_page）整个删除——读的时候才从对象
存储取文件、现解码现分页（解码/分页逻辑搬去 ``app/domain/reading_source.py``），没有任何
「注册一本书」的东西。本文件断言侧通道模块 + 书注册模块都不存在、HTTP 端点不复注册，
防止复活。
"""

from __future__ import annotations

import importlib.util

import pytest

REMOVED_MODULES = [
    "app.domain.book_ingest",
    "app.wiring.book",
    # Task 2：整个书注册物模块删除（读时取文件、现解码现分页，无书注册表）。
    "app.domain.book",
]


def _spec_exists(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:
        return False


@pytest.mark.parametrize("module", REMOVED_MODULES)
def test_book_side_channel_module_removed(module):
    assert not _spec_exists(module), f"{module} 应随书侧通道删除"


def test_book_ingest_route_not_registered():
    """``/api/internal/book/ingest`` 不再注册成任何 HTTP endpoint。"""
    import app.wiring  # noqa: F401  — 触发全部 wiring 注册
    from fastapi import FastAPI

    from app.runtime.http_source import register_http_sources

    app = FastAPI()
    register_http_sources(app)

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/internal/book/ingest" not in paths


def test_wiring_package_drops_book_module():
    """``app/wiring/__init__.py`` 不再 import book 子模块。"""
    import app.wiring as wiring_pkg

    assert not hasattr(wiring_pkg, "book"), "app.wiring 不应再聚合 book 子模块"
