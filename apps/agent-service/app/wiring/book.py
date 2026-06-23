"""书接入 HTTP wiring — 读小说 Task 1.

channel-server 把飞书私聊文件下载后投到 ``POST /api/internal/book/ingest``。RPC 模式
（response=True）：channel-server 拿结构化结果回真人（成功 / 失败提示）。
"""
from app.domain.book_ingest import BookIngestRequest, book_ingest_node
from app.runtime import Source, wire

wire(BookIngestRequest).from_(
    Source.http("/api/internal/book/ingest", method="POST", response=True)
).to(book_ingest_node)
