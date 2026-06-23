"""书接入节点 — channel-server → agent-service 的 HTTP 接入面（读小说 Task 1）.

真人在飞书私聊把 txt/epub 文件发给赤尾，channel-server 把文件下载、base64 后投到
``POST /api/internal/book/ingest``（wire 见 ``app/wiring/book.py``）。本节点负责编排：

  1. 由 ``bot_name`` 解析目标 persona（p2p 私聊里 bot 本身就是那个 persona）——决定这本书
     存给谁读；
  2. 解析 + 按页落库（委托 :func:`app.domain.book.ingest_book`），让这本书可被
     ``read(page_num)`` 一页页读到。

**只存书，绝不往她信箱投任何东西。** 她怎么知道你推荐了书？靠你跟她的**真实对话**——你
发书时跟她说的话走 chat，她醒来时从「最近聊过的对话」（recent_chats）自然读到（#279 模型：
对话内容不推信箱、life 醒来实时从 common_message 拉）。绝不 fabricate「有人推荐你读 X」这种
系统动静投进信箱去敲她——那是工程脑替真实对话造信号、还平白唤醒她，违反赤尾宪法（优化她的
真实输入，不在逻辑层造确定性信号）。

**失败口径**（spec）：解析失败不静默吞、也不抛穿透 HTTP——回结构化 ``{ok: False,
reason}``，channel-server 据此回真人一条提示。

**入口独立于文本 chat 链路**：书走这条专门接入路径，不进 chat。这跟 spec「飞书文件
消息今天的 chat 链路不处理」一致。

lane 是 durable Data 的显式 Key（不带会污染 prod）：节点从 ``req.lane`` 取——
channel-server 在 body 里显式带上当前泳道（它有 ``getLane()``），HTTP source 把 body
字段灌进 ``BookIngestRequest``。
"""

from __future__ import annotations

import base64
from typing import Annotated

from app.data.queries.persona import resolve_persona_id
from app.domain.book import (
    BookParseError,
    find_book_meta,
    ingest_book,
)
from app.runtime.data import Data, Key
from app.runtime.node import node


class BookIngestRequest(Data):
    """书接入 HTTP 入参（transient，不落库）。

    ``lane`` 进 Key 满足 Data 约束 + 显式带泳道（durable 落库按它隔离）。``file_b64``
    是飞书文件下载后的 base64 字节。``bot_name`` 用于解析目标 persona（这本书存给谁读）。
    """

    lane: Annotated[str, Key]
    bot_name: str           # 收到这条私聊的 bot（p2p 里即目标 persona 的 bot）
    filename: str           # 原始文件名（.epub 走 epub 解析、否则按 txt 读 + 取书名）
    file_b64: str           # 文件字节的 base64

    class Meta:
        transient = True


@node
async def book_ingest_node(req: BookIngestRequest):
    """把飞书发来的文件解析成一本可读的书、存好。返回结构化结果（失败不抛穿透 HTTP）。

    **只存书，不投信箱、不发任何信号。** 她靠跟你的真实对话知道这本书（见模块 docstring）。

    返回值故意不加类型注解：``@node`` 对带 Data 返回注解的节点做 Data-only 校验，而这是
    HTTP RPC 节点、返回 dict 给 channel-server（照 ``admin_search_node`` 的先例）。
    """
    persona_id = await resolve_persona_id(req.bot_name)

    try:
        raw = base64.b64decode(req.file_b64)
    except Exception as exc:  # noqa: BLE001 — 坏 base64 当解析失败回真人
        return {"ok": False, "reason": f"文件内容无法解码：{exc}"}

    try:
        book_id = await ingest_book(
            lane=req.lane,
            persona_id=persona_id,
            filename=req.filename,
            raw=raw,
        )
    except BookParseError as exc:
        # 解析失败：回结构化失败，channel-server 据此回真人一条提示（不静默吞、不抛）。
        return {"ok": False, "reason": f"这个文件没能解析成一本书：{exc}"}

    meta = await find_book_meta(lane=req.lane, book_id=book_id)
    title = meta.title if meta is not None else req.filename
    return {"ok": True, "book_id": book_id, "title": title}
