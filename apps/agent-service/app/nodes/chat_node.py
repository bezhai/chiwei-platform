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
from app.chat.content_parser import parse_content
from app.chat.context import build_human_chat_context
from app.chat.persona_filter import MessageRouter
from app.chat.post_actions import fetch_guard_message
from app.chat.pre_safety import run_pre_safety_check
from app.chat.render import render_chat_turn
from app.data.queries import (
    create_pending_agent_response,
    find_gray_config,
    find_message_content,
    find_username,
    is_chat_request_completed,
    resolve_bot_name_for_persona,
    set_agent_response_bot,
)
from app.data.queries.mailbox import deliver_event
from app.domain.chat_dataflow import ChatRequest, ChatResponseSegment, ChatTrigger
from app.domain.world_events import (
    EVENT_KIND_EXTERNAL,
    EVENT_KIND_EXTERNAL_PASSIVE,
)
from app.infra.cst_time import now_cst_iso
from app.life.feed_whitelist import should_feed_chat_to_life
from app.nodes._chat_pre_safety import _resolve_pre_safety_for_part
from app.runtime import node
from app.runtime.emit import emit
from app.runtime.lane_policy import current_deployment_lane

logger = logging.getLogger(__name__)


async def _yield_not_found():
    """空 context(源消息反查无历史)退回的单段"未找到"流。

    与剥离前 ``_build_and_stream`` 的空 ctx 分支同一文案、同一形态(yield 一段文本
    后结束),让下游 segmentation 照常把它收成一条 final 段。
    """
    yield "抱歉，未找到相关消息记录"


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

    already_done = await is_chat_request_completed(t.session_id)
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
        if i > 0:
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
        # 真人聊天两层:先剥离出来的真人 context 构建(用 message_id 反查源消息、捞
        # 历史、解析人设、拼生活状态),再交给共享渲染层(人设 prompt + 主模型 stream)。
        # context 反查为空(源消息无历史)→ 退回"未找到"文本,与旧 _build_and_stream
        # 的空 ctx 分支行为一致。
        turn_ctx = await build_human_chat_context(
            req.message_id, persona_id=req.persona_id or ""
        )
        if turn_ctx is None:
            stream = _yield_not_found()
        else:
            stream = render_chat_turn(
                turn_ctx,
                outbound_message_id=req.message_id,
                session_id=req.session_id,
                channel=req.channel,
                features=gray_config,
            )
        async for text in stream:
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

    # 对话回灌：聊完一次，作为一条 event 进这个 persona 的信箱（她事后知道"刚跟谁
    # 聊了啥"），不让"聊天里的她"和"世界里的她"分叉。真人私聊（p2p）回灌打被动 kind
    # （感知不唤醒，task 3）、白名单群回灌打 external（waking），kind 选择见
    # _replay_conversation_to_mailbox。快路径回复已经 emit 完，回灌在这之后、不挡
    # 回复；回灌失败只 log、不拖垮 chat。summary 直接
    # 用用户原话——这是她自己经历过的对话回灌进自己脑子（聊的时候就经历了、chat
    # 入口也过了 pre-safety），不是隐私泄露，不需要 LLM 概括脱敏；原话本身就是最
    # 真实的"聊了啥"，概括成二手货反而失真、每轮跑一次 offline LLM 纯浪费。
    # event_id 用 session_id 让重投幂等。
    user_message = parsed.render() if parsed else ""
    await _replay_conversation_to_mailbox(req, user_message=user_message)


# 防御性上限：纯防极端长文落进 durable 信箱,正常聊天消息根本到不了 200 字。
_REPLAY_MAX_CHARS = 200


async def _replay_conversation_to_mailbox(
    req: ChatRequest, *, user_message: str,
) -> None:
    """把"刚跟某用户聊了啥"投进 req.persona_id 的信箱。

    summary = "跟谁 + 聊了啥"：谁取真实用户名（解析不到退回 user_id），聊了啥
    **直接用用户原话**（``user_message``）——不概括、不上 LLM。这是她自己经历过的
    对话回灌进自己脑子，给 agent 的应该是真实输入，不是工程化概括过的二手货。只留
    一个宽松上限（``_REPLAY_MAX_CHARS``）纯防极端长文，超了截断加省略号；正常消息
    根本到不了。``user_message`` 为空（纯图片 / 表情渲染为空）时退回 ``刚和{谁}聊
    过一次``。

    **kind 按 ``is_p2p`` 分（task 3：真人私聊感知不唤醒）**：真人**私聊**回灌打被动
    ``EVENT_KIND_EXTERNAL_PASSIVE``——她已经在 chat 回合里回应过这个真人了，再额外
    唤醒一轮 life 用 gpt 单独跑纯属重复反应、浪费；被动 kind 只落信箱当被动上下文，她
    下次自然醒时（``list_unread_events``）读到、知道「刚跟某真人聊过」。白名单内的**群**
    回灌打 ``EVENT_KIND_EXTERNAL``（waking）——群是显式选来要听的、照常唤醒。被动 vs
    唤醒由 mailbox 的 ``PASSIVE_EVENT_KINDS`` 凭 kind 在即时敲门和补敲对账两处统一决定。

    ``session_id`` 缺失时**跳过回灌**：event_id 按 ``chat:{session_id}`` 去重，
    None 会塌成 ``chat:None`` 把不同的无关回灌错误合并成一条。宁可不写，也不错合并。

    lane 取**进程级部署泳道**（``current_deployment_lane() or "prod"``），与
    world / life 写读、取用端读全链路统一（必改 3）。不能用 ``req.lane``：prod
    下 ``req.lane`` 常为空 → external event 进 ``lane=""`` 信箱，而 life 在
    ``"prod"`` 唤醒读不到 → 对话回灌闭环分叉。失败吞掉只 log —— 这是对话之后的
    事后回灌，绝不能影响已经回完的即时回复。
    """
    if not req.persona_id:
        return
    if not req.session_id:
        logger.info(
            "skip conversation replay: session_id missing (persona=%s, user=%s)",
            req.persona_id, req.user_id,
        )
        return
    # life 感知白名单（spec Task 5 成本止血）：只有白名单内的群的对话回灌进
    # life；白名单外/空配置（fail-closed）的群聊跳过。p2p 不过滤。这里只挡
    # deliver_event 这一处回灌——chat 回复和安全链早已走完，不受影响。
    if not await should_feed_chat_to_life(chat_id=req.chat_id, is_p2p=req.is_p2p):
        logger.info(
            "skip life feed: chat %s not in life_feed_chat_whitelist "
            "(persona=%s)",
            req.chat_id, req.persona_id,
        )
        return
    lane = current_deployment_lane() or "prod"
    # 跟谁：优先真实用户名，解析不到退回 user_id（始终带 user_id 兜底，让信息可定位）。
    who = f"用户 {req.user_id}"
    try:
        name = await find_username(req.user_id) if req.user_id else None
        if name:
            who = f"{name}（用户 {req.user_id}）"
    except Exception as e:  # noqa: BLE001 — 名字解析失败退回 user_id，不挡回灌
        logger.warning("resolve username for replay failed: %s: %s", req.user_id, e)
    # 聊了啥：直接用用户原话（防御性截断极端长文），空时退回兜底文案。
    spoke = user_message.strip()
    if len(spoke) > _REPLAY_MAX_CHARS:
        spoke = spoke[:_REPLAY_MAX_CHARS] + "…"
    summary = f"刚和{who}聊了：{spoke}" if spoke else f"刚和{who}聊过一次"
    # 真人**私聊**（p2p）回灌打被动 kind（感知不唤醒，task 3）：她已经在 chat 回合里
    # 回应过这个真人了，再额外唤醒一轮 life 用 gpt 单独跑纯属重复反应、浪费——只落信箱
    # 当被动上下文，她下次自然醒时读到「刚跟某真人聊过」。白名单内的**群**回灌保持
    # external（waking）：群是显式选来要听的、照常唤醒。被动 vs 唤醒由 mailbox 的
    # PASSIVE_EVENT_KINDS 凭 kind 决定，chat 这里只负责按 is_p2p 选对 kind。
    kind = EVENT_KIND_EXTERNAL_PASSIVE if req.is_p2p else EVENT_KIND_EXTERNAL
    try:
        await deliver_event(
            lane=lane,
            persona_id=req.persona_id,
            event_id=f"chat:{req.session_id}",
            kind=kind,
            source=f"user:{req.user_id}",
            summary=summary,
            # CST aware ISO（含 +08:00），与 world/life 写入端同一个"现在"——
            # 旧的 Unix 毫秒会跟 ISO 同框混着喂给 agent、时间窗口比较差 8 小时。
            occurred_at=now_cst_iso(),
        )
    except Exception as e:  # noqa: BLE001 — 事后回灌失败不拖垮 chat 快路径
        logger.warning(
            "conversation replay to mailbox failed: persona=%s session=%s: %s",
            req.persona_id, req.session_id, e,
        )
