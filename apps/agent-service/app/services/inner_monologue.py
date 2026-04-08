"""内心独白生成 — 替代 reply_style 的示例锚点

定期生成赤尾此刻的内心感受，注入 system prompt 的 <voice> 段。
LLM 从感受自然涌现回复风格，不再模仿示例。
"""

import logging
from datetime import datetime, timedelta, timezone

from app.agents.infra.langfuse_client import get_prompt
from app.agents.infra.model_builder import ModelBuilder
from app.config.config import settings
from app.orm.crud import get_bot_persona, get_plan_for_period
from app.orm.memory_crud import get_today_fragments, save_inner_monologue

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))


async def generate_inner_monologue(persona_id: str) -> str | None:
    """生成赤尾此刻的内心独白"""
    persona = await get_bot_persona(persona_id)
    if not persona:
        return None

    # Life Engine 状态
    from app.services.life_engine import _load_state
    le_state = await _load_state(persona_id)
    current_state = le_state.current_state if le_state else "（状态未知）"
    response_mood = le_state.response_mood if le_state else ""

    # 当前时段的 schedule
    now = datetime.now(CST)
    today = now.strftime("%Y-%m-%d")
    schedule = await get_plan_for_period("daily", today, today, persona_id)
    schedule_text = schedule.content if schedule else "（今天没有安排）"

    # 最近碎片
    frags = await get_today_fragments(persona_id, grains=["conversation"])
    frag_text = "\n".join(f.content[:100] for f in frags[-3:]) if frags else "（今天还没跟人聊过）"

    # 调用 LLM
    prompt = get_prompt("inner_monologue")
    compiled = prompt.compile(
        persona_name=persona.display_name,
        persona_lite=persona.persona_lite,
        current_state=current_state,
        response_mood=response_mood,
        schedule_segment=schedule_text,
        recent_fragments=frag_text,
        current_time=now.strftime("%H:%M"),
    )

    model = await ModelBuilder.build_chat_model(settings.identity_drift_model)
    response = await model.ainvoke([{"role": "user", "content": compiled}])

    content = response.content
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        ).strip()

    if not content:
        logger.warning(f"[{persona_id}] Inner monologue generation returned empty")
        return None

    await save_inner_monologue(persona_id, content, source="cron")
    logger.info(f"[{persona_id}] Inner monologue generated: {content[:50]}...")
    return content
