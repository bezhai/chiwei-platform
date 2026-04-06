"""Glimpse 管线 — Life Engine "刷手机" 状态时的窥屏观察

流程：选群 → 读未读消息 → LLM 观察 → 有趣写碎片 → 想说话触发搭话
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from app.config.config import settings
from app.orm.memory_crud import create_fragment
from app.orm.memory_models import ExperienceFragment
from app.workers.proactive_scanner import get_unseen_messages, submit_proactive_request

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

# 安静时段：23:00~09:00 CST 不窥屏
QUIET_HOURS = (23, 9)

# 初期白名单群（与 ProactiveManager 同群）
from app.workers.proactive_scanner import TARGET_CHAT_ID

_WHITELIST_GROUPS = [TARGET_CHAT_ID]


def _now_cst() -> datetime:
    return datetime.now(CST)


def _is_quiet(now: datetime) -> bool:
    h = now.hour
    start, end = QUIET_HOURS
    return h >= start or h < end


async def _pick_group(persona_id: str) -> str | None:
    """选一个群去翻。v1: 固定白名单轮询。"""
    if _WHITELIST_GROUPS:
        return _WHITELIST_GROUPS[0]
    return None


async def _get_persona_info(persona_id: str) -> tuple[str, str]:
    from app.orm.crud import get_bot_persona

    persona = await get_bot_persona(persona_id)
    if persona:
        return persona.display_name, persona.persona_lite or ""
    return persona_id, ""


async def _get_group_name(chat_id: str) -> str:
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
            name = result.scalar_one_or_none()
            return name or chat_id[:10]
    except Exception:
        return chat_id[:10]


async def _format_messages(messages: list, persona_name: str = "") -> str:
    """格式化消息为时间线文本"""
    from app.orm.crud import get_username
    from app.utils.content_parser import parse_content

    lines = []
    for msg in messages[-30:]:
        ts = datetime.fromtimestamp(msg.create_time / 1000, tz=CST)
        time_str = ts.strftime("%H:%M")
        if msg.role == "assistant":
            speaker = persona_name or "bot"
        else:
            name = await get_username(msg.user_id)
            speaker = name or msg.user_id[:6]
        text = parse_content(msg.content).render()
        if text and text.strip():
            lines.append(f"[{time_str}] {speaker}: {text[:200]}")
    return "\n".join(lines)


async def _call_glimpse_llm(
    persona_name: str,
    persona_lite: str,
    group_name: str,
    messages_text: str,
) -> str:
    """调用 LLM 进行窥屏观察"""
    from app.agents.infra.langfuse_client import get_prompt
    from app.agents.infra.model_builder import ModelBuilder

    prompt = get_prompt("glimpse_observe")
    compiled = prompt.compile(
        persona_name=persona_name,
        persona_lite=persona_lite,
        group_name=group_name,
        messages=messages_text,
    )
    model = await ModelBuilder.build_chat_model(settings.life_engine_model)
    response = await model.ainvoke([{"role": "user", "content": compiled}])

    if isinstance(response.content, list):
        return "".join(
            p.get("text", "") if isinstance(p, dict) else str(p)
            for p in response.content
        ).strip()
    return (response.content or "").strip()


def _parse_glimpse_response(raw: str) -> dict:
    """解析 glimpse LLM 响应"""
    try:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(raw[start:end])
            return {
                "interesting": bool(data.get("interesting", False)),
                "observation": data.get("observation", ""),
                "want_to_speak": bool(data.get("want_to_speak", False)),
                "stimulus": data.get("stimulus"),
                "target_message_id": data.get("target_message_id"),
            }
    except (json.JSONDecodeError, ValueError):
        pass
    return {"interesting": False}


async def run_glimpse(persona_id: str) -> str:
    """执行一次窥屏观察

    Returns: 状态字符串（用于日志/测试）
    """
    now = _now_cst()

    # 安静时段不窥屏
    if _is_quiet(now):
        logger.debug(f"[{persona_id}] Glimpse skipped: quiet hours")
        return "skipped:quiet_hours"

    # 选群
    chat_id = await _pick_group(persona_id)
    if not chat_id:
        return "skipped:no_group"

    # 读未读消息
    messages = await get_unseen_messages(chat_id, persona_id)
    if not messages:
        logger.debug(f"[{persona_id}] Glimpse: no unseen messages in {chat_id}")
        return "skipped:no_messages"

    # 准备上下文
    persona_name, persona_lite = await _get_persona_info(persona_id)
    group_name = await _get_group_name(chat_id)
    messages_text = await _format_messages(messages, persona_name)

    if not messages_text.strip():
        return "skipped:empty_timeline"

    # LLM 观察
    raw = await _call_glimpse_llm(persona_name, persona_lite, group_name, messages_text)
    decision = _parse_glimpse_response(raw)

    if not decision.get("interesting"):
        logger.info(f"[{persona_id}] Glimpse: nothing interesting in {group_name}")
        return "skipped:not_interesting"

    # 创建 glimpse 碎片
    observation = decision.get("observation", "")
    if observation:
        first_ts = messages[0].create_time
        last_ts = messages[-1].create_time
        fragment = ExperienceFragment(
            persona_id=persona_id,
            grain="glimpse",
            source_chat_id=chat_id,
            source_type="group",
            time_start=first_ts,
            time_end=last_ts,
            content=observation,
            mentioned_entity_ids=[],
            model=settings.life_engine_model,
        )
        await create_fragment(fragment)
        logger.info(f"[{persona_id}] Glimpse fragment: {observation[:60]}...")

    # 想说话 → 触发主动搭话
    if decision.get("want_to_speak"):
        try:
            await submit_proactive_request(
                chat_id=chat_id,
                persona_id=persona_id,
                target_message_id=decision.get("target_message_id"),
                stimulus=decision.get("stimulus"),
            )
            logger.info(f"[{persona_id}] Glimpse → proactive in {group_name}")
            return "fragment_created+proactive"
        except Exception as e:
            logger.error(f"[{persona_id}] Glimpse proactive failed: {e}")

    return "fragment_created"
