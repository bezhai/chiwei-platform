"""书接入节点契约 — 读小说 Task 1 的 channel-server → agent-service 接入面.

channel-server 把飞书私聊的 txt/epub 文件下载后 base64 投到这条 HTTP 接入路径
(``POST /api/internal/book/ingest``)。本节点只做一件事：

  * 由 ``bot_name`` 解析目标 persona → 解析 + 按页落库（委托 ``ingest_book``）；
  * 解析失败回结构化失败（``ok=False`` + reason），channel-server 据此回真人提示。

它**绝不往她信箱投任何东西**：她靠跟你的真实对话（life 醒来从 recent_chats 实时拉）知道
这本书，不靠系统 fabricate「有人推荐你读 X」的动静去敲她（赤尾宪法，见 book_ingest 模块
docstring）。下面 mock 掉 ``ingest_book`` / ``resolve_persona_id``：节点职责是编排（解析→
存书、失败转结构化），落库正确性在 test_book.py 用真 PG 验，这里只验编排契约 + 「绝不碰
信箱」这条红线。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from app.domain.book import BookParseError
from app.domain.book_ingest import BookIngestRequest, book_ingest_node


def _req(**overrides) -> BookIngestRequest:
    base = {
        "lane": "coe-t1",
        "bot_name": "chiwei",
        "filename": "斜阳.txt",
        "file_b64": "5LiA5q615q2j5paH",  # 任意 base64，ingest_book 被 mock
    }
    base.update(overrides)
    return BookIngestRequest(**base)


class _FakeMeta:
    def __init__(self, *, title: str, total_pages: int):
        self.title = title
        self.total_pages = total_pages


async def test_ingest_node_stores_book_for_resolved_persona():
    """成功路径：解析入库，把书存给 bot 解析出的 persona，返回 book_id + 书名。"""
    with (
        patch(
            "app.domain.book_ingest.resolve_persona_id",
            new=AsyncMock(return_value="akao"),
        ),
        patch(
            "app.domain.book_ingest.ingest_book",
            new=AsyncMock(return_value="book-xyz"),
        ) as ingest,
        patch(
            "app.domain.book_ingest.find_book_meta",
            new=AsyncMock(return_value=_FakeMeta(title="斜阳", total_pages=12)),
        ),
    ):
        resp = await book_ingest_node(_req())

    assert resp == {"ok": True, "book_id": "book-xyz", "title": "斜阳"}
    ingest.assert_awaited_once()
    assert ingest.await_args.kwargs["persona_id"] == "akao", "书存给 bot_name 解析出的 persona"
    assert ingest.await_args.kwargs["lane"] == "coe-t1"


async def test_ingest_node_never_touches_the_inbox():
    """红线：ingest 绝不往她信箱投任何 event——她靠真实对话知道书、不靠系统造动静（宪法）。"""
    with (
        patch(
            "app.domain.book_ingest.resolve_persona_id",
            new=AsyncMock(return_value="akao"),
        ),
        patch(
            "app.domain.book_ingest.ingest_book",
            new=AsyncMock(return_value="bid"),
        ),
        patch(
            "app.domain.book_ingest.find_book_meta",
            new=AsyncMock(return_value=_FakeMeta(title="夜航船", total_pages=3)),
        ),
        patch("app.data.queries.mailbox.deliver_event", new=AsyncMock()) as deliver,
    ):
        await book_ingest_node(_req())

    deliver.assert_not_awaited()


async def test_ingest_node_parse_failure_returns_structured_error():
    """解析失败：回 ok=False + reason，**不抛**（channel-server 据此回真人提示）。"""
    with (
        patch(
            "app.domain.book_ingest.resolve_persona_id",
            new=AsyncMock(return_value="akao"),
        ),
        patch(
            "app.domain.book_ingest.ingest_book",
            new=AsyncMock(side_effect=BookParseError("坏 epub")),
        ),
    ):
        resp = await book_ingest_node(_req(filename="broken.epub"))

    assert resp["ok"] is False
    assert resp.get("reason"), "失败带可回给真人的 reason"


def test_book_ingest_request_is_transient_and_keyed():
    """BookIngestRequest 是 transient（不落库）的 HTTP 入参 Data，且有 Key（Data 约束）。"""
    from app.runtime.data import key_fields

    assert key_fields(BookIngestRequest), "Data 必须声明至少一个 Key"
    assert getattr(BookIngestRequest.Meta, "transient", False) is True
