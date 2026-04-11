"""赤尾 Identity 漂移状态机

继承 DebouncedPipeline 两阶段锁模型：
  一阶段（可中断）：收集消息，debounce N 秒，超过 M 条强制 flush
  二阶段（不可中断）：LLM 漂移计算，更新 identity 状态

每个群/私聊维护独立的漂移锁。
"""

import logging
from datetime import datetime, timedelta, timezone

from app.config.config import settings
from app.orm.crud import get_chat_messages_in_range
from app.services.debounced_pipeline import DebouncedPipeline
from app.services.persona_loader import load_persona
from app.services.timeline_formatter import format_timeline
from app.services.content_parser import parse_content

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))


class IdentityDriftManager(DebouncedPipeline):
    """两阶段锁 identity 漂移管理器

    每个 (chat_id, persona_id) 组合独立管理，不并行漂移。
    一阶段：收集消息（debounce N 秒 + 强制 flush M 条）
    二阶段：LLM 漂移计算（不可中断）
    """

    _instance: "IdentityDriftManager | None" = None

    def __init__(self):
        super().__init__(
            debounce_seconds=settings.identity_drift_debounce_seconds,
            max_buffer=settings.identity_drift_max_buffer,
        )

    @classmethod
    def get_instance(cls) -> "IdentityDriftManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def process(self, chat_id: str, persona_id: str, event_count: int) -> None:
        """二阶段：执行 identity 漂移"""
        await _run_drift(chat_id, persona_id)


async def _run_drift(chat_id: str, persona_id: str) -> None:
    """事件驱动漂移 — 调用统一 voice 生成，传入近期消息上下文"""
    pc = await load_persona(persona_id)
    recent_messages = await _get_recent_messages(chat_id, persona_name=pc.display_name)
    recent_replies = await _get_recent_persona_replies(chat_id, persona_id)

    if not recent_messages:
        logger.info(f"[{persona_id}] No recent messages for {chat_id}, skip drift")
        return

    # 拼装 recent_context 供统一生成函数使用
    parts = []
    if recent_messages:
        parts.append(f"群里刚才发生的事：\n{recent_messages}")
    if recent_replies:
        parts.append(f"你最近的回复：\n{recent_replies}")
    recent_context = "\n\n".join(parts)

    from app.services.voice_generator import generate_voice
    await generate_voice(persona_id, recent_context=recent_context, source="drift")


async def _get_recent_messages(chat_id: str, persona_name: str = "bot", max_messages: int = 50) -> str:
    """获取最近 1 小时内的消息，格式化为时间线（不分 bot，用于群聊上下文感知）"""
    start_dt = datetime.now(CST) - timedelta(hours=1)

    start_ts = int(start_dt.timestamp() * 1000)
    end_ts = int(datetime.now(CST).timestamp() * 1000)

    messages = await get_chat_messages_in_range(chat_id, start_ts, end_ts)
    if not messages:
        return ""

    return await format_timeline(messages, persona_name, tz=CST, max_messages=max_messages)


async def _get_recent_persona_replies(chat_id: str, persona_id: str, max_replies: int = 10) -> str:
    """获取指定 bot 最近的回复原文，用于偏差诊断

    通过 bot_name 过滤（conversation_messages 没有 persona_id 列，
    persona_id 在 agent_responses 表上，这里用 bot_name 做近似匹配）。
    """
    from app.services.bot_context import _resolve_bot_name_for_persona

    now = datetime.now(CST)
    start_ts = int((now - timedelta(hours=2)).timestamp() * 1000)
    end_ts = int(now.timestamp() * 1000)

    messages = await get_chat_messages_in_range(chat_id, start_ts, end_ts)
    if not messages:
        return ""

    bot_name = await _resolve_bot_name_for_persona(persona_id, chat_id)
    persona_msgs = [m for m in messages if m.role == "assistant" and m.bot_name == bot_name]
    persona_msgs = persona_msgs[-max_replies:]

    lines = []
    for i, msg in enumerate(persona_msgs, 1):
        rendered = parse_content(msg.content).render()
        if rendered and rendered.strip():
            lines.append(f"{i}. {rendered[:200]}")

    return "\n".join(lines)


