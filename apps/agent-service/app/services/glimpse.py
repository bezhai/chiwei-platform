"""Glimpse 管线 — 赤尾"刷手机"时的窥屏观察（v2: 独立调度 + 增量去重 + 递进观察）

流程：读状态 → 拉增量消息 → LLM 观察（传入上次感想 + 今日搭话记录）→ 写碎片 + 状态
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from app.config.config import settings
from app.orm.memory_crud import (
    create_fragment,
    get_last_bot_reply_time,
    get_latest_glimpse_state,
    insert_glimpse_state,
)
from app.orm.memory_models import ExperienceFragment
from app.workers.proactive_scanner import (
    TARGET_CHAT_ID,
    get_unseen_messages,
    _get_recent_proactive_records,
)

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

# 安静时段：23:00~09:00 CST 不窥屏
QUIET_HOURS = (23, 9)

# 每小时主动搭话上限（工程兜底，LLM 自律为主）
HOURLY_PROACTIVE_LIMIT = 2

_WHITELIST_GROUPS = [TARGET_CHAT_ID]


def _now_cst() -> datetime:
    return datetime.now(CST)


def _is_quiet(now: datetime) -> bool:
    h = now.hour
    start, end = QUIET_HOURS
    return h >= start or h < end


async def _pick_group(persona_id: str) -> str | None:
    """选一个群去翻。v1: 固定白名单。"""
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
    last_observation: str = "",
    recent_proactive: list[dict] | None = None,
) -> str:
    """调用 LLM 进行窥屏观察"""
    from langfuse.langchain import CallbackHandler

    from app.agents.infra.langfuse_client import get_prompt
    from app.agents.infra.model_builder import ModelBuilder

    # 格式化今日搭话记录
    proactive_hint = ""
    if recent_proactive:
        n = len(recent_proactive)
        times = "、".join(r["time"] for r in recent_proactive[:5])
        proactive_hint = (
            f"\n- 你今天已经在这个群主动说了 {n} 次话了（{times}），"
            "再多就烦人了，除非有真的让你忍不住的话题"
        )

    prompt = get_prompt("glimpse_observe")
    compile_args = {
        "persona_name": persona_name,
        "persona_lite": persona_lite,
        "group_name": group_name,
        "messages": messages_text,
        "last_observation": (
            f"你上次翻这个群的时候，心里想的是：「{last_observation}」\n"
            if last_observation
            else ""
        ),
        "recent_proactive": proactive_hint,
    }
    try:
        compiled = prompt.compile(**compile_args)
    except KeyError:
        # prompt 模板尚未添加新变量，fallback
        compiled = prompt.compile(
            persona_name=persona_name,
            persona_lite=persona_lite,
            group_name=group_name,
            messages=messages_text,
        )

    model = await ModelBuilder.build_chat_model(settings.life_engine_model)
    response = await model.ainvoke(
        [{"role": "user", "content": compiled}],
        config={"callbacks": [CallbackHandler()], "run_name": "glimpse-observe"},
    )

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
    """执行一次窥屏观察（v2: 增量 + 递进）

    Returns: 状态字符串（用于日志/测试/admin 接口）
    """
    now = _now_cst()

    # 1. 安静时段不窥屏
    if _is_quiet(now):
        logger.debug(f"[{persona_id}] Glimpse skipped: quiet hours")
        return "skipped:quiet_hours"

    # 2. 选群
    chat_id = await _pick_group(persona_id)
    if not chat_id:
        return "skipped:no_group"

    # 3. 读状态
    state = await get_latest_glimpse_state(persona_id, chat_id)
    last_seen = state.last_seen_msg_time if state else 0
    last_observation = state.observation if state else ""

    # 4. 跳过已参与的对话
    bot_reply_time = await get_last_bot_reply_time(chat_id)
    effective_after = max(last_seen, bot_reply_time)

    # 5. 拉增量消息
    messages = await get_unseen_messages(chat_id, after=effective_after)
    if not messages:
        logger.debug(f"[{persona_id}] Glimpse: no new messages in {chat_id}")
        return "skipped:no_messages"

    # 6. 准备上下文
    persona_name, persona_lite = await _get_persona_info(persona_id)
    group_name = await _get_group_name(chat_id)
    messages_text = await _format_messages(messages, persona_name)

    if not messages_text.strip():
        return "skipped:empty_timeline"

    # 6b. 获取今日搭话记录（供 LLM 自律 + 工程兜底）
    recent_proactive = await _get_recent_proactive_records(chat_id)

    # 7. LLM 观察（传入上次感想 + 今日搭话记录）
    raw = await _call_glimpse_llm(
        persona_name=persona_name,
        persona_lite=persona_lite,
        group_name=group_name,
        messages_text=messages_text,
        last_observation=last_observation,
        recent_proactive=recent_proactive,
    )
    decision = _parse_glimpse_response(raw)

    # 记录本次看到的最新消息时间戳
    new_last_seen = messages[-1].create_time

    if not decision.get("interesting"):
        logger.info(f"[{persona_id}] Glimpse: nothing interesting in {group_name}")
        # 不有趣也要记录看到了哪里，避免重复拉
        await insert_glimpse_state(
            persona_id=persona_id,
            chat_id=chat_id,
            last_seen_msg_time=new_last_seen,
            observation="",
        )
        return "skipped:not_interesting"

    # 8. 创建碎片
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

    # 9. 搭话
    state_observation = observation
    if decision.get("want_to_speak"):
        stimulus = decision.get("stimulus", "")
        target = decision.get("target_message_id") or None
        # 工程兜底：统计最近 1 小时内的搭话次数
        one_hour_ago = now - timedelta(hours=1)
        hour_cutoff = one_hour_ago.strftime("%H:%M")
        recent_hour_count = sum(1 for r in recent_proactive if r["time"] >= hour_cutoff)
        if recent_hour_count >= HOURLY_PROACTIVE_LIMIT:
            state_observation = f"{observation}\n[want_to_speak:throttled] stimulus={stimulus}, count={recent_hour_count}/{HOURLY_PROACTIVE_LIMIT}"
            logger.info(f"[{persona_id}] Glimpse want_to_speak throttled: {recent_hour_count}>={HOURLY_PROACTIVE_LIMIT}")
        else:
            state_observation = f"{observation}\n[want_to_speak] stimulus={stimulus}, target={target}"
            logger.info(f"[{persona_id}] Glimpse want_to_speak: {stimulus}")
            try:
                from app.workers.proactive_scanner import submit_proactive_request

                await submit_proactive_request(
                    chat_id=chat_id,
                    persona_id=persona_id,
                    target_message_id=target,
                    stimulus=stimulus,
                )
            except Exception as e:
                logger.error(f"[{persona_id}] Glimpse proactive submit failed: {e}")

    # 10. 写状态
    await insert_glimpse_state(
        persona_id=persona_id,
        chat_id=chat_id,
        last_seen_msg_time=new_last_seen,
        observation=state_observation,
    )

    return "fragment_created"
