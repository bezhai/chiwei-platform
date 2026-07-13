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
import uuid
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
    is_chat_request_completed,
    resolve_bot_name_for_persona,
    set_agent_response_bot,
)
from app.data.queries.mailbox import (
    deliver_event,  # module-level so tests can monkeypatch
)
from app.domain.chat_dataflow import ChatRequest, ChatResponseSegment, ChatTrigger
from app.domain.world_events import EVENT_KIND_OWN_CHAT_REPLY
from app.infra import cst_time
from app.life.feed_whitelist import should_feed_chat_to_life
from app.nodes._chat_pre_safety import _resolve_pre_safety_for_part
from app.runtime import node
from app.runtime.emit import emit
from app.runtime.lane_policy import current_deployment_lane

# chat 已回复投信箱的 event_id 派生命名空间：固定 UUID，让同一次 (lane, message_id,
# persona_id) 重投(mq redelivery / ChatRequest 重放)派生同一 event_id，
# deliver_event 按 (lane, persona, event_id) 幂等去重，不重复叫醒。与
# world/tools.py、life_wake.py 里各自的 event_id 派生命名空间分开，避免同文字撞 id
# （同一惯例见 app.world.tools._EVENT_ID_NS 系列注释）。
_CHAT_REPLY_EVENT_NS = uuid.UUID("d3a8f1c2-6b4e-4f9a-8c1d-2e3f4a5b6c7d")

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
            req.message_id,
            persona_id=req.persona_id or "",
            bot_name=req.bot_name or "",
            channel=req.channel,
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
    # 显式判空:LLM 单轮偶发返回空文本+空 tool_calls(trace
    # 82323210372fe067ec2a60abd8e9fdb3)时,render_chat_turn 整段吐不出任何
    # token,final_content 会落到 `remaining or full_content` 的兜底分支——
    # remaining 已 strip 过,但兜底用的 full_content 未 strip,纯空白 token
    # (只吐空格/换行)会在这里绕过朴素的真值判断被当"非空"内容。channel-server
    # 侧(chat-response-handler.ts)本来就有"content 为空 + is_last=True → 不
    # 发送给用户但标记 completed"的分支,只是它用 JS 的 `!content` 判真值——非空
    # 白字符串(比如一个空格)是 truthy,会绕过那个分支被当"非空"内容真的发出
    # 去,这正是用户偶发看到"空气泡"的根因。这里把即将 emit 的 content strip
    # 干净,真正为空的会变成空字符串,命中 channel-server 已有的正确分支(不
    # 发送、但仍标记完成)。不能直接跳过这次 emit——那样会让 channel-server 永
    # 远收不到这一轮的收尾消息,common_agent_response.status 卡在 pending 拿不
    # 到完成标记,is_chat_request_completed 的重投短路判断也会跟着失效。
    final_text = result.content.strip()
    if not final_text:
        logger.warning(
            "chat_node final segment empty after strip: "
            "session_id=%s persona_id=%s message_id=%s part_index=%s",
            req.session_id,
            req.persona_id,
            req.message_id,
            part_index,
        )
    await emit(
        ChatResponseSegment(
            **base_payload,
            part_index=part_index,
            content=final_text,
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

    # Task 1 与 Task 3 的执行顺序命门:clean_full 是「这一轮实际发给用户的完整
    # 内容」(SPLIT_MARKER 已还原、已 strip)——不管 final 段是否被上面判空跳过,
    # clean_full 忠实反映的都只是"真正发出去的那些字"(final 段被跳过时,
    # clean_full 的尾部本来就只是空白、strip 后自然不带出未发送的内容)。只有
    # clean_full 非空(这一轮确实有内容发给了用户)才投递信箱告诉 life「我刚回复
    # 了什么」;完全空的一轮(判空检查让什么都没发出去)不告诉她"我刚回复了",
    # 避免对着一段没发出去的空回复,还让 life 以为已经处理过这次交互。
    if clean_full:
        await _wake_life_after_chat(req, reply_text=clean_full)


def _derive_chat_reply_event_id(
    *, lane: str, message_id: str, persona_id: str
) -> str:
    """确定性派生一条「chat 已回复」投信箱 event 的 id（重投幂等）。

    ChatRequest 按 (message_id, persona_id) 是自然键，同一次交互的重投（MQ
    redelivery / 整轮重放）派生同一 id，``deliver_event`` 按 (lane, persona,
    event_id) 幂等去重，不会重复投递、也不会重复敲门唤醒 life。
    """
    return uuid.uuid5(
        _CHAT_REPLY_EVENT_NS, f"{lane}\x1f{message_id}\x1f{persona_id}"
    ).hex


async def _wake_life_after_chat(req: ChatRequest, reply_text: str) -> None:
    """chat 确认这段回复内容已经发出去后，把回复原文投进 life 的 durable 信箱。

    旧实现只 ``emit(EventArrived(...))`` 纯敲门、不带内容，life 醒来时靠实时查
    ``common_message`` 猜"我是否已经回复过这句话"——但 chat 的回复是由
    channel-server 的 chat-response-worker 在飞书发送成功后才异步落库的，存在
    时序竞态：落库慢于 life 的 5 秒 debounce 触发时，life 会误判"没人回过"，自己
    再生成一次内容相近的回复。

    改用 ``deliver_event``（world notify / npc_visit 等既有场景同款的 durable
    信箱投递机制）：回复原文落 durable 表，不受 ``EventArrived`` 的 debounce
    折叠影响——同一 5 秒窗口内即使还有别的唤醒事件到达，最先投递的这条内容依旧
    完整躺在信箱里，被唤醒的那一轮 ``life_wake_node`` 靠 ``list_unread_events``
    扫描整个信箱、不靠"是哪一条 EventArrived 触发了这一轮"来找内容。

    ``deliver_event`` 内部对新投递的非被动 event 会自己 ``emit(EventArrived(...))``
    敲门唤醒（``own_chat_reply`` 不在 ``PASSIVE_EVENT_KINDS`` 里），所以这里**不再
    自己额外 emit** —— 否则会敲两次门、造成重复唤醒。

    真人私聊直接投递（不查白名单）；群聊必须在白名单内才投递。投递/唤醒失败只记
    warning，不影响已经 emit 出去的即时回复（回复已经发给用户了，这里失败只是
    life 少了一次"及时"的机会，不是回复本身失败）。
    """
    if not req.persona_id:
        return
    if not req.is_p2p and not await should_feed_chat_to_life(
        chat_id=req.chat_id, is_p2p=False
    ):
        logger.info(
            "skip group chat life wake: chat %s not in life_feed_chat_whitelist "
            "(persona=%s)",
            req.chat_id,
            req.persona_id,
        )
        return

    lane = current_deployment_lane() or "prod"
    event_id = _derive_chat_reply_event_id(
        lane=lane, message_id=req.message_id, persona_id=req.persona_id
    )
    try:
        await deliver_event(
            lane=lane,
            persona_id=req.persona_id,
            event_id=event_id,
            summary=reply_text,
            occurred_at=cst_time.now_cst_iso(),
            kind=EVENT_KIND_OWN_CHAT_REPLY,
            source="chat",
            chat_id=req.chat_id,
            chat_scope="direct" if req.is_p2p else "group",
        )
    except Exception as e:  # noqa: BLE001 — chat 回复已发出，唤醒副作用失败不拖垮回复
        logger.warning(
            "chat life wake failed: lane=%s persona=%s chat=%s is_p2p=%s: %s",
            lane,
            req.persona_id,
            req.chat_id,
            req.is_p2p,
            e,
        )
