"""赤尾主动发言扫描器 — 群聊潜水观察 + 小模型判断 + 合成消息投递

核心流程 (run_proactive_scan):
1. 安静时段检查（23:00~09:00 CST 不扫）
2. Redis 冷却检查（15 分钟内不重复扫）
3. 获取上次发言后的未读消息
4. 收集上下文（reply_style、group_culture、今日主动记录）
5. 小模型判断是否应该回复
6. 合成触发消息 + 发布 chat_request
"""

import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func as sa_func

from app.agents.infra.langfuse_client import get_prompt
from app.agents.infra.model_builder import ModelBuilder
from app.clients.rabbitmq import CHAT_REQUEST, RabbitMQClient
from app.clients.redis import AsyncRedisClient
from app.orm.base import AsyncSessionLocal
from app.orm.crud import get_group_culture_gestalt
from app.orm.models import ConversationMessage
from app.services.memory_context import get_reply_style
from app.utils.content_parser import parse_content

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────────────────
TARGET_CHAT_ID = "oc_a44255e98af05f1359aeb29eeb503536"
COOLDOWN_KEY = "proactive:last_scan_time"
COOLDOWN_MS = 15 * 60 * 1000  # 15 min
PROACTIVE_USER_ID = "__proactive__"
QUIET_HOURS = (23, 9)  # >= 23 or < 9
JUDGE_MODEL_ID = "proactive-judge-model"

CST = timezone(timedelta(hours=8))


# ── 冷却与时段 ────────────────────────────────────────────────────────────


def _is_quiet_hours(now: datetime | None = None) -> bool:
    """当前 CST 时间是否在安静时段（23:00~09:00）"""
    now = now or datetime.now(CST)
    hour = now.hour
    start, end = QUIET_HOURS
    return hour >= start or hour < end


async def should_scan() -> bool:
    """检查 Redis 冷却，15 分钟内扫过则返回 False"""
    redis = AsyncRedisClient.get_instance()
    last = await redis.get(COOLDOWN_KEY)
    if last is None:
        return True
    try:
        elapsed = int(time.time() * 1000) - int(last)
        return elapsed >= COOLDOWN_MS
    except (ValueError, TypeError):
        return True


async def _mark_scanned() -> None:
    """写入当前时间戳到 Redis 冷却 key"""
    redis = AsyncRedisClient.get_instance()
    now_ms = int(time.time() * 1000)
    await redis.set(COOLDOWN_KEY, str(now_ms), ex=COOLDOWN_MS // 1000 + 60)


# ── 未读消息获取 ──────────────────────────────────────────────────────────


async def get_unseen_messages(limit: int = 30) -> list[ConversationMessage]:
    """获取赤尾上次发言之后的用户消息

    1. 找 target chat 中 role='assistant' 的最大 create_time（最后一次出现）
    2. 取 create_time 更晚的 role='user' 且 user_id != PROACTIVE_USER_ID 的消息
    """
    async with AsyncSessionLocal() as session:
        # 子查询：最后一次 assistant 发言时间
        last_presence_q = (
            select(sa_func.max(ConversationMessage.create_time))
            .where(
                ConversationMessage.chat_id == TARGET_CHAT_ID,
                ConversationMessage.role == "assistant",
            )
            .scalar_subquery()
        )

        # 主查询：之后的用户消息
        stmt = (
            select(ConversationMessage)
            .where(
                ConversationMessage.chat_id == TARGET_CHAT_ID,
                ConversationMessage.role == "user",
                ConversationMessage.user_id != PROACTIVE_USER_ID,
                ConversationMessage.create_time > sa_func.coalesce(last_presence_q, 0),
            )
            .order_by(ConversationMessage.create_time.asc())
            .limit(limit)
        )

        result = await session.execute(stmt)
        return list(result.scalars().all())


# ── 小模型判断 ────────────────────────────────────────────────────────────


def _format_messages_for_judge(messages: list[ConversationMessage]) -> str:
    """将消息格式化为 [HH:MM:SS] user_id[:8]: text"""
    lines = []
    for msg in messages:
        ts = datetime.fromtimestamp(msg.create_time / 1000, tz=CST)
        time_str = ts.strftime("%H:%M:%S")
        user_short = msg.user_id[:8] if msg.user_id else "unknown"
        text = parse_content(msg.content).render()
        lines.append(f"[{time_str}] {user_short}: {text}")
    return "\n".join(lines)


async def _get_recent_proactive_records() -> list[dict]:
    """查询今日的主动触发记录（user_id=PROACTIVE_USER_ID）"""
    today_start = datetime.now(CST).replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_ms = int(today_start.timestamp() * 1000)

    async with AsyncSessionLocal() as session:
        stmt = (
            select(ConversationMessage)
            .where(
                ConversationMessage.chat_id == TARGET_CHAT_ID,
                ConversationMessage.user_id == PROACTIVE_USER_ID,
                ConversationMessage.create_time >= today_start_ms,
            )
            .order_by(ConversationMessage.create_time.desc())
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()

    records = []
    for msg in rows:
        ts = datetime.fromtimestamp(msg.create_time / 1000, tz=CST)
        records.append({
            "time": ts.strftime("%H:%M"),
            "summary": parse_content(msg.content).render()[:80],
        })
    return records


async def judge_response(
    messages_text: str,
    reply_style: str,
    group_culture: str,
    recent_proactive: list[dict],
) -> dict:
    """调用小模型判断是否主动回复

    Returns:
        {"respond": bool, "target_message_id": str | None, "stimulus": str | None}
    """
    try:
        prompt_template = get_prompt("proactive_judge")
        compiled = prompt_template.compile(
            messages=messages_text,
            reply_style=reply_style,
            group_culture=group_culture,
            recent_proactive=json.dumps(recent_proactive, ensure_ascii=False),
        )

        from langfuse.langchain import CallbackHandler

        model = await ModelBuilder.build_chat_model(JUDGE_MODEL_ID)
        response = await model.ainvoke(
            [{"role": "user", "content": compiled}],
            config={"callbacks": [CallbackHandler()]},
        )
        raw = _extract_text(response.content)

        return _parse_judge_response(raw)
    except Exception as e:
        logger.error("judge_response failed: %s", e, exc_info=True)
        return {"respond": False}


def _parse_judge_response(raw: str) -> dict:
    """解析 JSON 响应，失败返回 respond=False"""
    try:
        # 尝试提取 JSON（LLM 可能在 JSON 前后加文字）
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(raw[start:end])
            return {
                "respond": bool(data.get("respond", False)),
                "target_message_id": data.get("target_message_id"),
                "stimulus": data.get("stimulus"),
            }
    except (json.JSONDecodeError, ValueError):
        pass
    return {"respond": False}


# ── 合成消息与投递 ─────────────────────────────────────────────────────────


async def submit_proactive_request(
    target_message_id: str | None,
    stimulus: str | None,
) -> str:
    """创建合成触发消息并发布 chat_request

    Returns:
        生成的 session_id
    """
    session_id = str(uuid.uuid4())
    message_id = f"proactive_{int(time.time() * 1000)}"
    now_ms = int(time.time() * 1000)

    # 构建合成消息内容
    content = json.dumps(
        {"v": 2, "text": stimulus or "", "items": [{"type": "text", "value": stimulus or ""}]},
        ensure_ascii=False,
    )

    # 写入数据库
    async with AsyncSessionLocal() as session:
        msg = ConversationMessage(
            message_id=message_id,
            user_id=PROACTIVE_USER_ID,
            content=content,
            role="user",
            root_message_id=message_id,
            reply_message_id=target_message_id,
            chat_id=TARGET_CHAT_ID,
            chat_type="group",
            create_time=now_ms,
            message_type="proactive_trigger",
            vector_status="skipped",
            bot_name="chiwei",
        )
        session.add(msg)
        await session.commit()

    # 发布到 chat_request 队列（不指定 lane，跟随当前泳道）
    from app.clients.rabbitmq import _current_lane
    current_lane = _current_lane()
    client = RabbitMQClient.get_instance()
    await client.publish(
        CHAT_REQUEST,
        {
            "session_id": session_id,
            "message_id": message_id,
            "chat_id": TARGET_CHAT_ID,
            "is_p2p": False,
            "root_id": message_id,
            "user_id": PROACTIVE_USER_ID,
            "bot_name": "chiwei",
            "is_proactive": True,
            "lane": current_lane,
            "enqueued_at": now_ms,
        },
    )

    logger.info(
        "Proactive request submitted: session_id=%s, target=%s",
        session_id,
        target_message_id,
    )
    return session_id


# ── 主编排 ────────────────────────────────────────────────────────────────


async def run_proactive_scan(source: str = "cron") -> dict:
    """主动扫描编排

    Returns:
        {"skipped": str} 或 {"submitted": session_id} 或 {"decided": "no_response"}
    """
    # 1. 安静时段
    if _is_quiet_hours():
        logger.debug("proactive_scan skipped: quiet hours")
        return {"skipped": "quiet_hours"}

    # 频率控制已由 ProactiveManager 接管，scanner 不再做冷却检查

    # 2. 获取未读消息
    messages = await get_unseen_messages()
    if not messages:
        logger.debug("proactive_scan: no unseen messages")
        return {"skipped": "no_messages"}

    # Langfuse trace 包裹判断 + 投递
    from langfuse import get_client as get_langfuse, propagate_attributes

    langfuse = get_langfuse()
    scan_session_id = str(uuid.uuid4())

    with langfuse.start_as_current_observation(as_type="trace", name="proactive-scan"):
        with propagate_attributes(session_id=scan_session_id):
            # 5. 收集上下文
            messages_text = _format_messages_for_judge(messages)
            reply_style = await get_reply_style(TARGET_CHAT_ID)
            group_culture = await get_group_culture_gestalt(TARGET_CHAT_ID)
            recent_proactive = await _get_recent_proactive_records()

            # 6. 小模型判断
            decision = await judge_response(
                messages_text=messages_text,
                reply_style=reply_style,
                group_culture=group_culture,
                recent_proactive=recent_proactive,
            )

            if not decision.get("respond"):
                logger.info("proactive_scan decided not to respond (source=%s)", source)
                return {"decided": "no_response"}

            # 7. 投递
            session_id = await submit_proactive_request(
                target_message_id=decision.get("target_message_id"),
                stimulus=decision.get("stimulus"),
            )

    logger.info("proactive_scan submitted (source=%s, session=%s)", source, session_id)
    return {"submitted": session_id}


# ── 辅助 ──────────────────────────────────────────────────────────────────


def _extract_text(content) -> str:
    """从 LLM 响应中提取文本"""
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return content or ""
