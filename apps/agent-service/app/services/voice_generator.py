"""统一 Voice 生成 — 一次 LLM 调用同时生成内心独白 + 风格示例

合并原 inner_monologue.py 和 identity_drift.py 的 base 路径。
输出完整 <voice> 段内容，注入 system prompt。
"""

import logging
from datetime import datetime, timedelta, timezone

from langchain.messages import HumanMessage

from app.agents.core import ChatAgent
from app.config.config import settings
from app.orm.crud import get_bot_persona, get_plan_for_period
from app.orm.memory_crud import get_today_fragments, save_reply_style

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))


async def generate_voice(
    persona_id: str,
    recent_context: str = "",
    source: str = "cron",
) -> str | None:
    """生成完整 voice 内容（内心独白 + 风格示例）"""
    persona = await get_bot_persona(persona_id)
    if not persona:
        return None

    from app.services.life_engine import _load_state
    le_state = await _load_state(persona_id)
    current_state = le_state.current_state if le_state else "（状态未知）"
    response_mood = le_state.response_mood if le_state else ""

    now = datetime.now(CST)
    today = now.strftime("%Y-%m-%d")
    schedule = await get_plan_for_period("daily", today, today, persona_id)
    schedule_text = schedule.content if schedule else "（今天没有安排）"

    frags = await get_today_fragments(persona_id, grains=["conversation"])
    frag_text = (
        "\n".join(f.content[:100] for f in frags[-3:])
        if frags
        else "（今天还没跟人聊过）"
    )

    recent_ctx_block = ""
    if recent_context:
        recent_ctx_block = f"最近的对话和你的回复：\n{recent_context}"

    agent = ChatAgent(
        prompt_id="voice_generator",
        tools=[],
        model_id=settings.identity_drift_model,
        trace_name="voice-generator",
    )
    result = await agent.run(
        messages=[HumanMessage(content="生成当前状态的内心独白和语气示例")],
        prompt_vars={
            "persona_name": persona.display_name,
            "persona_lite": persona.persona_lite,
            "current_state": current_state,
            "response_mood": response_mood,
            "schedule_segment": schedule_text,
            "recent_fragments": frag_text,
            "recent_context": recent_ctx_block,
            "current_time": now.strftime("%H:%M"),
        },
    )

    content = result.content or ""
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        ).strip()

    if not content:
        logger.warning(f"[{persona_id}] Voice generation returned empty")
        return None

    await save_reply_style(persona_id, content, source=source)
    logger.info(f"[{persona_id}] Voice generated ({source}): {content[:60]}...")
    return content
