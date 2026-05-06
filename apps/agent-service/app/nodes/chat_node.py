"""Phase 5a chat 主 pipeline 节点（占位骨架）。

  route_chat_node:  ChatTrigger -> N × emit(ChatRequest)
  chat_node:        ChatRequest -> N × emit(ChatResponseSegment)

骨架阶段两个 @node 的 body 都是 ``raise NotImplementedError``；
真正的逻辑（消息校验 / fan-out / prep / 主流 / pre-safety 短路 / final）
由 Phase 5a Task 4-11 逐步填入。占位的目的是让 wiring/chat.py + compile_graph
先建立可验证的拓扑骨架。
"""
from __future__ import annotations

import asyncio
import logging
import time
from uuid import uuid4

# MessageRouter / emit / 各 helper 都 imported at module level so 单元测试可
# 用 monkeypatch.setattr(chat_node_mod, ...) 替换，节点内直接复用同名引用。
from app.chat.content_parser import parse_content
from app.chat.post_actions import fetch_guard_message
from app.chat.pre_safety_gate import run_pre_safety_via_graph
from app.chat.router import MessageRouter
from app.data.queries import (
    find_gray_config,
    find_message_content,
    is_chat_request_completed,
    resolve_bot_name_for_persona,
    set_agent_response_bot,
)
from app.data.session import get_session
from app.domain.chat_dataflow import ChatRequest, ChatResponseSegment, ChatTrigger
from app.runtime import node
from app.runtime.emit import emit

logger = logging.getLogger(__name__)


@node
async def route_chat_node(t: ChatTrigger) -> None:
    """ChatTrigger -> ChatRequest fan-out。

    步骤：
      0. 入口校验 message_id 非空
      1. redelivered 短路（is_chat_request_completed helper）
      2. MessageRouter.route 决定 persona 列表
      3. fan-out emit ChatRequest（每个 persona 一条；后续 persona uuid 重生成 session_id）
    """
    if t.message_id is None:
        raise ValueError(
            "ChatTrigger.message_id is None; cannot fan out ChatRequest"
        )

    async with get_session() as s:
        already_done = await is_chat_request_completed(
            s, t.session_id, is_proactive=t.is_proactive
        )
    if already_done:
        logger.warning(
            "skip redelivered chat_request: session_id=%s, message_id=%s",
            t.session_id,
            t.message_id,
        )
        return

    router = MessageRouter()
    persona_ids = await router.route(
        chat_id=t.chat_id or "",
        mentions=list(t.mentions),
        bot_name=t.bot_name or "",
        is_p2p=t.is_p2p,
        is_proactive=t.is_proactive,
    )
    if not persona_ids:
        logger.info("no persona to reply: message_id=%s", t.message_id)
        return

    for i, pid in enumerate(persona_ids):
        session_for_persona = t.session_id if i == 0 else str(uuid4())
        await emit(ChatRequest(
            message_id=t.message_id,
            persona_id=pid,
            session_id=session_for_persona,
            chat_id=t.chat_id,
            is_p2p=t.is_p2p,
            root_id=t.root_id,
            user_id=t.user_id,
            is_proactive=t.is_proactive,
            bot_name=t.bot_name,
            lane=t.lane,
            enqueued_at=t.enqueued_at,
        ))


@node
async def chat_node(req: ChatRequest) -> None:
    """ChatRequest -> N × ChatResponseSegment (per persona).

    Phases (内部分块，不拆 node):
      1. prep: fetch + parse + gray + guard + pre_task 启动 (this task)
      2. fetch 为空 -> emit 1 段 "未找到" + return  (Task 8)
      3. resolve response_bot_name + agent_responses 行更新  (Task 9)
      4. base_payload 构造（含 lane）  (Task 9)
      5. 主循环 + 中段 emit  (Task 10)
      6. final 段 + pre-safety blocked 路径  (Task 11)
    """
    # 1. prep
    async with get_session() as s:
        raw_content = await find_message_content(s, req.message_id)
    parsed = parse_content(raw_content) if raw_content else None
    async with get_session() as s:
        gray_config = (await find_gray_config(s, req.message_id)) or {}
    effective_persona = req.persona_id or req.bot_name or ""
    guard_message = await fetch_guard_message(effective_persona)
    pre_task = asyncio.create_task(
        run_pre_safety_via_graph(
            message_id=req.message_id,
            content=parsed.render() if parsed else "",
            persona_id=effective_persona,
        )
    )

    # 2. fetch 为空 -> emit 1 段 "未找到" + return
    if not raw_content:
        await emit(ChatResponseSegment(
            message_id=req.message_id,
            persona_id=req.persona_id,
            part_index=0,
            session_id=req.session_id,
            chat_id=req.chat_id,
            is_p2p=req.is_p2p,
            root_id=req.root_id,
            user_id=req.user_id,
            is_proactive=req.is_proactive,
            bot_name=req.bot_name,
            lane=req.lane,
            content="抱歉，未找到相关消息记录",
            status="success",
            is_last=True,
            full_content=None,
            published_at=int(time.time() * 1000),
        ))
        pre_task.cancel()
        return

    # 3. resolve response_bot_name + 更新 agent_responses 行
    async with get_session() as s:
        response_bot_name = await resolve_bot_name_for_persona(
            s, req.persona_id, req.chat_id or "",
        )
    if not response_bot_name:
        response_bot_name = req.bot_name or ""
    if req.session_id:
        try:
            async with get_session() as s:
                await set_agent_response_bot(
                    s, req.session_id, response_bot_name, req.persona_id,
                )
        except Exception as e:
            logger.warning("Failed to update agent_response: %s", e)

    # 4. base_payload (segments 共用字段)
    base_payload = dict(
        message_id=req.message_id,
        persona_id=req.persona_id,
        session_id=req.session_id,
        chat_id=req.chat_id,
        is_p2p=req.is_p2p,
        root_id=req.root_id,
        user_id=req.user_id,
        is_proactive=req.is_proactive,
        bot_name=response_bot_name,
        lane=req.lane,  # CRITICAL: sink 不会自动注入 header lane
    )

    # 后续 task 加 主循环 / final
    _ = (parsed, gray_config, guard_message, base_payload)
