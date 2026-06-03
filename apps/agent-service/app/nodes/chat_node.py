"""Phase 5a chat 主 pipeline 节点。

  route_chat_node:  ChatTrigger -> N × emit(ChatRequest)
  chat_node:        ChatRequest -> N × emit(ChatResponseSegment)

替代了原 ``app.workers.chat_consumer`` + ``app.chat.pipeline.stream_chat``
路径，由 dataflow runtime 直接驱动 chat_request 队列。
"""

from __future__ import annotations

import asyncio
import logging
import time
from uuid import uuid4

# MessageRouter / emit / 各 helper 都 imported at module level so 单元测试可
# 用 monkeypatch.setattr(chat_node_mod, ...) 替换，节点内直接复用同名引用。
from app.agent.trace import turn_trace
from app.api.middleware import CHAT_FIRST_TOKEN, CHAT_PIPELINE_DURATION
from app.chat.agent_stream import _build_and_stream
from app.chat.content_parser import parse_content
from app.chat.persona_filter import MessageRouter
from app.chat.post_actions import fetch_guard_message
from app.chat.pre_safety import run_pre_safety_check
from app.data.queries import (
    create_pending_agent_response,
    find_gray_config,
    find_message_content,
    is_chat_request_completed,
    resolve_bot_name_for_persona,
    set_agent_response_bot,
)
from app.domain.chat_dataflow import ChatRequest, ChatResponseSegment, ChatTrigger
from app.nodes._chat_pre_safety import _resolve_pre_safety_for_part
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
        raise ValueError("ChatTrigger.message_id is None; cannot fan out ChatRequest")

    logger.info(
        "route_chat_node received: session_id=%s, message_id=%s, lane=%s, bot_name=%s",
        t.session_id,
        t.message_id,
        t.lane,
        t.bot_name,
    )

    already_done = await is_chat_request_completed(
        t.session_id, is_proactive=t.is_proactive
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
        persona_ids=list(t.persona_ids),
        bot_name=t.bot_name or "",
        is_p2p=t.is_p2p,
        is_proactive=t.is_proactive,
    )
    if not persona_ids:
        logger.info("no persona to reply: message_id=%s", t.message_id)
        return

    logger.info(
        "route_chat_node fanout: session_id=%s, message_id=%s, lane=%s, personas=%s",
        t.session_id,
        t.message_id,
        t.lane,
        persona_ids,
    )

    for i, pid in enumerate(persona_ids):
        session_for_persona = t.session_id if i == 0 else str(uuid4())
        if i > 0 and not t.is_proactive:
            if not session_for_persona or not t.chat_id:
                raise ValueError(
                    "multi-persona ChatTrigger requires session_id and chat_id "
                    "to create per-persona response rows"
                )
            await create_pending_agent_response(
                session_id=session_for_persona,
                trigger_common_message_id=t.message_id,
                common_conversation_id=t.chat_id,
                bot_name=t.bot_name,
            )
        await emit(
            ChatRequest(
                channel=t.channel,
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
            )
        )


@node
async def chat_node(req: ChatRequest) -> None:
    """ChatRequest -> N × ChatResponseSegment (per persona).

    Phases (内部分块，不拆 node):
      1. prep: fetch + parse + gray + guard + pre_task 启动
      2. fetch-empty short-circuit: emit 1 段 "未找到" + return
      3. resolve response_bot_name + common_agent_response 行更新
      4. base_payload 构造（含 lane）
      5. 主循环 + 段边界 pre-safety + 中段 emit
      6. final 段 + pre-safety blocked 路径
    """
    # 1. prep
    raw_content = await find_message_content(req.message_id)
    parsed = parse_content(raw_content) if raw_content else None
    gray_config = (await find_gray_config(req.message_id)) or {}
    effective_persona = req.persona_id or req.bot_name or ""
    guard_message = await fetch_guard_message(effective_persona)
    pre_task = asyncio.create_task(
        run_pre_safety_check(
            message_id=req.message_id,
            content=parsed.render() if parsed else "",
            persona_id=effective_persona,
        )
    )

    # 2. fetch 为空 -> emit 1 段 "未找到" + return
    if not raw_content:
        await emit(
            ChatResponseSegment(
                channel=req.channel,
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
            )
        )
        pre_task.cancel()
        return

    # 3. resolve response_bot_name + 更新 common_agent_response 行
    response_bot_name = await resolve_bot_name_for_persona(
        req.persona_id,
        req.chat_id or "",
    )
    if not response_bot_name:
        response_bot_name = req.bot_name or ""
    if req.session_id:
        try:
            await set_agent_response_bot(
                req.session_id,
                response_bot_name,
                req.persona_id,
            )
        except Exception as e:
            logger.warning("Failed to update agent_response: %s", e)

    # 4. base_payload (segments 共用字段)
    base_payload = {
        "channel": req.channel,
        "message_id": req.message_id,
        "persona_id": req.persona_id,
        "session_id": req.session_id,
        "chat_id": req.chat_id,
        "is_p2p": req.is_p2p,
        "root_id": req.root_id,
        "user_id": req.user_id,
        "is_proactive": req.is_proactive,
        "bot_name": response_bot_name,
        "lane": req.lane,  # CRITICAL: sink 不会自动注入 header lane
    }

    # 5. 主循环 + 中段 emit (with pre-safety BLOCK termination)
    SPLIT_MARKER = "---split---"
    MAX_MESSAGES = 4

    sent_length = 0
    part_index = 0
    full_content = ""

    # Observability — port of legacy chat_consumer:200-307 happy-path
    # signals. Metrics fire only on the happy-path branch (final emit).
    # BLOCK / fetch-empty paths skip metrics — no tokens to measure.
    t_start = time.monotonic()
    t_first_token: float | None = None
    token_count = 0

    async def _emit_block_guard():
        await emit(
            ChatResponseSegment(
                **base_payload,
                part_index=part_index,
                content=guard_message,
                status="success",
                is_last=True,
                full_content=guard_message,
                published_at=int(time.time() * 1000),
            )
        )

    # One chat turn = one langfuse trace: seed from (message_id,
    # effective_persona) — the SAME seed run_pre_safety uses — so this main
    # stream and the turn's 3 pre-check guards fold into one trace. In
    # chat_node (a coroutine, not an async generator) the contextvar set/reset
    # never straddles a yield-to-caller, so the token always resets in-context.
    with turn_trace(f"{req.message_id}:{effective_persona}"):
        async for text in _build_and_stream(
            req.message_id,
            gray_config,
            session_id=req.session_id,
            persona_id=req.persona_id,
            channel=req.channel,
        ):
            if not text:
                continue
            if t_first_token is None:
                t_first_token = time.monotonic()
            token_count += 1
            full_content += text
            pending = full_content[sent_length:]
            while SPLIT_MARKER in pending and part_index < MAX_MESSAGES - 1:
                idx = pending.index(SPLIT_MARKER)
                part = pending[:idx].strip()
                if part:
                    result = await _resolve_pre_safety_for_part(
                        part, pre_task, guard_message,
                    )
                    if result.blocked:
                        await _emit_block_guard()
                        return
                    await emit(ChatResponseSegment(
                        **base_payload,
                        part_index=part_index,
                        content=result.content,
                        status="success",
                        is_last=False,
                        full_content=None,
                        published_at=int(time.time() * 1000),
                    ))
                    part_index += 1
                sent_length += idx + len(SPLIT_MARKER)
                pending = full_content[sent_length:]

    t_stream_end = time.monotonic()
    if t_first_token is not None:
        CHAT_FIRST_TOKEN.observe(t_first_token - t_start)

    # 6. final 段
    remaining = full_content[sent_length:].replace(SPLIT_MARKER, "").strip()
    clean_full = full_content.replace(SPLIT_MARKER, "\n\n").strip()
    final_content = (
        (remaining or full_content) if (remaining or part_index == 0) else ""
    )
    result = await _resolve_pre_safety_for_part(
        final_content,
        pre_task,
        guard_message,
    )
    if result.blocked:
        await _emit_block_guard()
        return
    await emit(
        ChatResponseSegment(
            **base_payload,
            part_index=part_index,
            content=result.content,
            status="success",
            is_last=True,
            full_content=clean_full,
            published_at=int(time.time() * 1000),
        )
    )

    t_end = time.monotonic()
    CHAT_PIPELINE_DURATION.labels(stage="total").observe(t_end - t_start)
    logger.info(
        "chat_request_done",
        extra={
            "event": "chat_request_done",
            "session_id": req.session_id,
            "persona_id": req.persona_id,
            "stream_ms": round((t_stream_end - t_start) * 1000),
            "ttft_ms": round((t_first_token - t_start) * 1000)
            if t_first_token is not None
            else 0,
            "total_ms": round((t_end - t_start) * 1000),
            "tokens": token_count,
            "parts": part_index + 1,
        },
    )
