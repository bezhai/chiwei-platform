"""赤尾 Identity 漂移状态机

两阶段锁模型：
  一阶段（可中断）：收集消息，debounce N 秒，超过 M 条强制 flush
  二阶段（不可中断）：LLM 漂移计算，更新 identity 状态

每个群/私聊维护独立的漂移锁。
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.agents.infra.langfuse_client import get_prompt
from app.agents.infra.model_builder import ModelBuilder
from app.clients.redis import AsyncRedisClient
from app.config.config import settings
from app.orm.crud import get_chat_messages_in_range, get_plan_for_period, get_username
from app.utils.content_parser import parse_content

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

# Redis key 前缀
_KEY_PREFIX = "reply_style"


def _state_key(chat_id: str, persona_id: str) -> str:
    return f"{_KEY_PREFIX}:{chat_id}:{persona_id}"


def _base_key(persona_id: str) -> str:
    return f"reply_style:__base__:{persona_id}"


async def _get_persona_context(persona_id: str) -> tuple[str, str]:
    """Returns (display_name, persona_lite) for prompt compilation"""
    from app.orm.crud import get_bot_persona
    persona = await get_bot_persona(persona_id)
    if persona:
        return persona.display_name, persona.persona_lite
    return persona_id, ""


_BASE_TTL_SECONDS = 43200  # 12 小时，覆盖到下一次定时生成


async def get_base_reply_style(persona_id: str = "akao") -> str | None:
    """读取指定 bot 的全局基线 reply_style"""
    redis = AsyncRedisClient.get_instance()
    return await redis.get(_base_key(persona_id))


async def set_base_reply_style(style: str, persona_id: str = "akao") -> None:
    """写入指定 bot 的全局基线 reply_style"""
    redis = AsyncRedisClient.get_instance()
    await redis.set(_base_key(persona_id), style, ex=_BASE_TTL_SECONDS)
    logger.info(f"[{persona_id}] Base reply_style updated: {style[:50]}...")


async def generate_base_reply_style(persona_id: str = "akao") -> str | None:
    """基于当前 Schedule 生成指定 bot 的全局基线 reply_style

    不依赖任何群/私聊的消息，只用 schedule + 当前时段。
    在 8:00/14:00/18:00 由 cron 调用，为没有独立漂移的会话提供基线。
    """
    schedule_context = await _get_schedule_context(persona_id)
    if not schedule_context or schedule_context.startswith("（"):
        logger.info(f"[{persona_id}] No schedule available, skip base reply_style generation")
        return None

    now = datetime.now(CST)
    persona_name, persona_lite = await _get_persona_context(persona_id)
    prompt = get_prompt("drift_base_generator")
    compiled = prompt.compile(
        persona_name=persona_name,
        persona_lite=persona_lite,
        schedule_daily=schedule_context,
        current_time=now.strftime("%H:%M"),
    )

    model = await ModelBuilder.build_chat_model(settings.identity_drift_model)
    response = await model.ainvoke([{"role": "user", "content": compiled}])
    style = _extract_text(response.content)

    if not style:
        logger.warning(f"[{persona_id}] Base reply_style generation returned empty")
        return None

    await set_base_reply_style(style, persona_id)
    return style


async def get_identity_state(chat_id: str, persona_id: str) -> str | None:
    """读取指定 bot 在指定群的漂移状态"""
    redis = AsyncRedisClient.get_instance()
    return await redis.hget(_state_key(chat_id, persona_id), "state")


async def get_identity_updated_at(chat_id: str, persona_id: str) -> str | None:
    """读取上次漂移更新时间（ISO 格式）"""
    redis = AsyncRedisClient.get_instance()
    return await redis.hget(_state_key(chat_id, persona_id), "updated_at")


async def set_identity_state(chat_id: str, persona_id: str, state: str, ttl: int = 86400) -> None:
    """写入指定 bot 在指定群的漂移状态"""
    redis = AsyncRedisClient.get_instance()
    now = datetime.now(CST).isoformat()
    pipe = redis.pipeline()
    pipe.hset(_state_key(chat_id, persona_id), mapping={"state": state, "updated_at": now})
    pipe.expire(_state_key(chat_id, persona_id), ttl)
    await pipe.execute()
    logger.info(f"[{persona_id}] Identity state updated for {chat_id}: {state[:50]}...")


class IdentityDriftManager:
    """两阶段锁 identity 漂移管理器

    每个 (chat_id, persona_id) 组合独立管理，不并行漂移。
    一阶段：收集消息（debounce N 秒 + 强制 flush M 条）
    二阶段：LLM 漂移计算（不可中断）
    """

    _instance: "IdentityDriftManager | None" = None

    def __init__(self):
        self._buffers: dict[str, int] = {}  # "{chat_id}:{persona_id}" -> event count
        self._timers: dict[str, asyncio.Task] = {}  # "{chat_id}:{persona_id}" -> phase1 timer
        self._phase2_running: set[str] = set()  # keys in phase2

    @classmethod
    def get_instance(cls) -> "IdentityDriftManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _key(self, chat_id: str, persona_id: str) -> str:
        return f"{chat_id}:{persona_id}"

    async def on_event(self, chat_id: str, persona_id: str) -> None:
        """消息/回复事件 -> 进入两阶段锁流程

        buffer 使用从上次漂移以来的真实消息数量，
        而不是简单 +1，这样活跃群的非@消息也计入密度。
        """
        key = self._key(chat_id, persona_id)
        msg_count = await _count_messages_since_last_drift(chat_id, persona_id)
        self._buffers[key] = max(self._buffers.get(key, 0) + 1, msg_count)
        logger.info(
            f"Identity drift on_event: chat_id={chat_id}, persona={persona_id}, "
            f"buffer={self._buffers[key]}, "
            f"msg_since_drift={msg_count}, "
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
        if self._buffers.get(key, 0) >= settings.identity_drift_max_buffer:
            asyncio.create_task(self._enter_phase2(chat_id, persona_id))
            return

        # 启动/重置 debounce 计时器
        self._timers[key] = asyncio.create_task(
            self._phase1_timer(chat_id, persona_id)
        )
        logger.info(
            f"Identity drift timer started: chat_id={chat_id}, persona={persona_id}, "
            f"debounce={settings.identity_drift_debounce_seconds}s"
        )

    async def _phase1_timer(self, chat_id: str, persona_id: str):
        """一阶段计时器：N 秒无新消息后进入二阶段"""
        try:
            await asyncio.sleep(settings.identity_drift_debounce_seconds)
            await self._enter_phase2(chat_id, persona_id)
        except asyncio.CancelledError:
            pass  # timer reset by new event

    async def _enter_phase2(self, chat_id: str, persona_id: str):
        """进入二阶段：清空缓冲区，执行 LLM 漂移"""
        key = self._key(chat_id, persona_id)
        event_count = self._buffers.pop(key, 0)
        self._timers.pop(key, None)

        if event_count == 0:
            return

        self._phase2_running.add(key)
        try:
            logger.info(
                f"Identity drift phase2 for {chat_id} persona={persona_id}: "
                f"{event_count} events buffered"
            )
            await _run_drift(chat_id, persona_id)
        except Exception as e:
            logger.error(f"Identity drift failed for {chat_id} persona={persona_id}: {e}")
        finally:
            self._phase2_running.discard(key)
            # 二阶段期间有新事件 -> 启动下一轮
            if self._buffers.get(key, 0) > 0:
                asyncio.create_task(self.on_event(chat_id, persona_id))


async def _run_drift(chat_id: str, persona_id: str) -> None:
    """两阶段漂移管线：观察 → 生成

    Agent 1（观察）：群聊事件 + persona 近期回复 + 基准人设 → 观察报告
    Agent 2（生成）：观察报告 → reply_style
    """
    # 1. 收集上下文
    persona_name, persona_lite = await _get_persona_context(persona_id)
    current_state = await get_identity_state(chat_id, persona_id)
    recent_messages = await _get_recent_messages(chat_id, persona_name=persona_name)
    schedule_context = await _get_schedule_context(persona_id)
    recent_replies = await _get_recent_persona_replies(chat_id, persona_id)

    if not recent_messages:
        logger.info(f"[{persona_id}] No recent messages for {chat_id}, skip drift")
        return

    now = datetime.now(CST)
    model = await ModelBuilder.build_chat_model(settings.identity_drift_model)

    # 2. Agent 1: 观察
    observer_prompt = get_prompt("drift_observer")
    observer_compiled = observer_prompt.compile(
        persona_name=persona_name,
        persona_lite=persona_lite,
        schedule_daily=schedule_context,
        current_reply_style=current_state or "（刚醒来，还没有形成今天的说话方式）",
        message_buffer=recent_messages,
        recent_akao_replies=recent_replies or "（还没有最近的回复）",
        current_time=now.strftime("%H:%M"),
    )

    observer_response = await model.ainvoke(
        [{"role": "user", "content": observer_compiled}],
    )
    observation_report = _extract_text(observer_response.content)

    if not observation_report:
        logger.warning(f"[{persona_id}] Observer returned empty for {chat_id}")
        return

    logger.info(f"[{persona_id}] Drift observer for {chat_id}: {observation_report[:80]}...")

    # 3. Agent 2: 生成
    generator_prompt = get_prompt("drift_generator")
    generator_compiled = generator_prompt.compile(
        persona_name=persona_name,
        observation_report=observation_report,
    )

    generator_response = await model.ainvoke(
        [{"role": "user", "content": generator_compiled}],
    )
    new_style = _extract_text(generator_response.content)

    if not new_style:
        logger.warning(f"[{persona_id}] Generator returned empty for {chat_id}")
        return

    # 4. 保存
    await set_identity_state(chat_id, persona_id, new_style)


def _extract_text(content) -> str:
    """从 LLM response content 提取纯文本"""
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        ).strip()
    return (content or "").strip()


async def _count_messages_since_last_drift(chat_id: str, persona_id: str) -> int:
    """统计上次漂移以来的消息数量（含非@赤尾的消息）"""
    updated_at_str = await get_identity_updated_at(chat_id, persona_id)
    if updated_at_str:
        try:
            start_dt = datetime.fromisoformat(updated_at_str)
        except ValueError:
            start_dt = datetime.now(CST) - timedelta(hours=1)
    else:
        start_dt = datetime.now(CST) - timedelta(hours=1)

    start_ts = int(start_dt.timestamp() * 1000)
    end_ts = int(datetime.now(CST).timestamp() * 1000)

    messages = await get_chat_messages_in_range(chat_id, start_ts, end_ts)
    return len(messages) if messages else 0


async def _get_recent_messages(chat_id: str, persona_name: str = "bot", max_messages: int = 50) -> str:
    """获取最近 1 小时内的消息，格式化为时间线（不分 bot，用于群聊上下文感知）"""
    start_dt = datetime.now(CST) - timedelta(hours=1)

    start_ts = int(start_dt.timestamp() * 1000)
    end_ts = int(datetime.now(CST).timestamp() * 1000)

    messages = await get_chat_messages_in_range(chat_id, start_ts, end_ts)
    if not messages:
        return ""

    # 取最近 max_messages 条
    messages = messages[-max_messages:]

    # 格式化
    lines = []
    for msg in messages:
        msg_time = datetime.fromtimestamp(msg.create_time / 1000, tz=CST)
        time_str = msg_time.strftime("%H:%M")
        if msg.role == "assistant":
            speaker = persona_name
        else:
            name = await get_username(msg.user_id)
            speaker = name or msg.user_id[:6]

        rendered = parse_content(msg.content).render()
        if rendered and rendered.strip():
            lines.append(f"[{time_str}] {speaker}: {rendered[:200]}")

    return "\n".join(lines)


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

    bot_name = await _resolve_bot_name_for_persona(persona_id)
    persona_msgs = [m for m in messages if m.role == "assistant" and m.bot_name == bot_name]
    persona_msgs = persona_msgs[-max_replies:]

    lines = []
    for i, msg in enumerate(persona_msgs, 1):
        rendered = parse_content(msg.content).render()
        if rendered and rendered.strip():
            lines.append(f"{i}. {rendered[:200]}")

    return "\n".join(lines)


async def _get_schedule_context(persona_id: str = "akao") -> str:
    """获取当前时段的 Schedule daily"""
    now = datetime.now(CST)
    today = now.strftime("%Y-%m-%d")
    schedule = await get_plan_for_period("daily", today, today, persona_id)
    if schedule and schedule.content:
        return schedule.content
    return "（今天还没有写日程）"
