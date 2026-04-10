"""Afterthought — 对话经历碎片生成器

两阶段锁模型（与 IdentityDriftManager 相同模式）：
  一阶段（可中断）：收集消息，debounce 300 秒，超过 15 条强制 flush
  二阶段（不可中断）：LLM 生成 conversation 粒度的 ExperienceFragment

每个 (chat_id, persona_id) 组合独立管理。
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from langchain_core.messages import HumanMessage

from app.agents.infra.llm_service import LLMService
from app.config.config import settings
from app.orm.crud import get_bot_persona, get_chat_messages_in_range
from app.orm.memory_crud import create_fragment
from app.orm.memory_models import ExperienceFragment
from app.services.relationship_memory import format_timeline

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

# 默认常量
DEBOUNCE_SECONDS = 300  # 5 分钟
LOOKBACK_HOURS = 2


def _extract_text(content) -> str:
    """从 LLM response content 提取纯文本"""
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        ).strip()
    return (content or "").strip()


class AfterthoughtManager:
    """两阶段锁对话碎片管理器

    每个 (chat_id, persona_id) 组合独立管理，不并行生成。
    一阶段：收集消息（debounce 300 秒 + 强制 flush 15 条）
    二阶段：LLM 生成 conversation ExperienceFragment（不可中断）
    """

    _instance: "AfterthoughtManager | None" = None
    MAX_BUFFER = 15

    def __init__(self):
        self._buffers: dict[str, int] = {}  # "{chat_id}:{persona_id}" -> event count
        self._timers: dict[str, asyncio.Task] = {}  # "{chat_id}:{persona_id}" -> phase1 timer
        self._phase2_running: set[str] = set()  # keys in phase2

    @classmethod
    def get_instance(cls) -> "AfterthoughtManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _key(self, chat_id: str, persona_id: str) -> str:
        return f"{chat_id}:{persona_id}"

    async def on_event(self, chat_id: str, persona_id: str) -> None:
        """消息/回复事件 -> 进入两阶段锁流程"""
        key = self._key(chat_id, persona_id)
        self._buffers[key] = self._buffers.get(key, 0) + 1
        logger.info(
            f"Afterthought on_event: chat_id={chat_id}, persona={persona_id}, "
            f"buffer={self._buffers[key]}, "
            f"phase2_running={key in self._phase2_running}"
        )

        # 二阶段运行中 -> 只缓冲，不触发
        if key in self._phase2_running:
            return

        # 取消已有计时器（重置 debounce）
        if key in self._timers:
            self._timers[key].cancel()
            del self._timers[key]

        # 超过阈值 -> 强制进入二阶段
        if self._buffers.get(key, 0) >= self.MAX_BUFFER:
            asyncio.create_task(self._enter_phase2(chat_id, persona_id))
            return

        # 启动/重置 debounce 计时器
        self._timers[key] = asyncio.create_task(
            self._phase1_timer(chat_id, persona_id)
        )

    async def _phase1_timer(self, chat_id: str, persona_id: str) -> None:
        """一阶段计时器：N 秒无新消息后进入二阶段"""
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
            await self._enter_phase2(chat_id, persona_id)
        except asyncio.CancelledError:
            pass  # timer reset by new event

    async def _enter_phase2(self, chat_id: str, persona_id: str) -> None:
        """进入二阶段：清空缓冲区，执行 LLM 碎片生成"""
        key = self._key(chat_id, persona_id)
        event_count = self._buffers.pop(key, 0)
        self._timers.pop(key, None)

        if event_count == 0:
            return

        self._phase2_running.add(key)
        try:
            logger.info(
                f"Afterthought phase2 for {chat_id} persona={persona_id}: "
                f"{event_count} events buffered"
            )
            await _generate_conversation_fragment(chat_id, persona_id)
        except Exception as e:
            logger.error(f"Afterthought failed for {chat_id} persona={persona_id}: {e}")
        finally:
            self._phase2_running.discard(key)
            # 二阶段期间有新事件 -> 启动下一轮
            if self._buffers.get(key, 0) > 0:
                asyncio.create_task(self.on_event(chat_id, persona_id))


async def _generate_conversation_fragment(chat_id: str, persona_id: str) -> None:
    """生成 conversation 粒度的经历碎片

    1. 获取最近 2 小时的消息
    2. 直接通过 get_username 获取用户名（不走 entity_resolver）
    3. 构建场景描述（群名/私聊对象）
    4. 格式化消息时间线
    5. 调用 LLM 生成碎片内容
    6. 写入 ExperienceFragment
    """
    now = datetime.now(CST)
    start_dt = now - timedelta(hours=LOOKBACK_HOURS)
    start_ts = int(start_dt.timestamp() * 1000)
    end_ts = int(now.timestamp() * 1000)

    messages = await get_chat_messages_in_range(chat_id, start_ts, end_ts)
    if not messages:
        logger.info(f"[{persona_id}] No messages in last {LOOKBACK_HOURS}h for {chat_id}, skip")
        return

    chat_type = messages[0].chat_type if messages else "group"

    # Get persona info
    persona = await get_bot_persona(persona_id)
    persona_name = persona.display_name if persona else persona_id
    persona_lite = persona.persona_lite if persona else ""

    # Build scene description
    scene = await _build_scene(chat_id, chat_type, messages)

    # Format timeline with plain names
    timeline = await format_timeline(messages, persona_name, tz=CST)
    if not timeline:
        logger.info(f"[{persona_id}] Empty timeline for {chat_id}, skip")
        return

    # Call LLM via LLMService
    result = await LLMService.run(
        prompt_id="afterthought_conversation",
        prompt_vars={
            "persona_name": persona_name,
            "persona_lite": persona_lite,
            "scene": scene,
            "messages": timeline,
        },
        messages=[HumanMessage(content="生成经历碎片")],
        model_id=settings.diary_model,
        trace_name="afterthought",
    )
    content = _extract_text(result.content)

    if not content:
        logger.warning(f"[{persona_id}] Afterthought LLM returned empty for {chat_id}")
        return

    fragment = ExperienceFragment(
        persona_id=persona_id,
        grain="conversation",
        source_chat_id=chat_id,
        source_type=chat_type,
        time_start=start_ts,
        time_end=end_ts,
        content=content,
        mentioned_entity_ids=[],  # not used for now
        model=settings.diary_model,
    )
    await create_fragment(fragment)
    logger.info(f"[{persona_id}] Conversation fragment created for {chat_id}: {content[:60]}...")

    # 关系记忆提取（fire-and-forget，不阻塞主流程）
    try:
        from app.services.relationship_memory import extract_relationship_updates

        # 从消息中提取涉及的用户 ID（排除 bot 自身）
        unique_user_ids = list({
            m.user_id for m in messages
            if m.role == "user" and m.user_id and m.user_id != "__proactive__"
        })

        if unique_user_ids:
            await extract_relationship_updates(
                persona_id=persona_id,
                chat_id=chat_id,
                user_ids=unique_user_ids,
                messages=messages,
            )
    except Exception as e:
        logger.warning(f"[{persona_id}] Relationship extract failed (non-fatal): {e}")


async def _build_scene(chat_id: str, chat_type: str, messages: list) -> str:
    """Build scene description for the prompt"""
    if chat_type == "p2p":
        # Find the non-assistant user's name
        for msg in messages:
            if msg.role == "user" and msg.user_id:
                name = await get_username(msg.user_id)
                if name:
                    return f"和{name}的私聊"
        return "一段私聊"
    else:
        # Query group name
        try:
            from app.orm.base import AsyncSessionLocal
            from app.orm.models import LarkGroupChatInfo
            from sqlalchemy import select

            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(LarkGroupChatInfo.name).where(
                        LarkGroupChatInfo.chat_id == chat_id
                    )
                )
                group_name = result.scalar_one_or_none()
                if group_name:
                    return f"在「{group_name}」群里"
        except Exception:
            pass
        return "在群里"


