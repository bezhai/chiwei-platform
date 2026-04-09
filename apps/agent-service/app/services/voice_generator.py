"""统一 Voice 生成 — 一次 LLM 调用同时生成内心独白 + 风格示例

合并原 inner_monologue.py 和 identity_drift.py 的 base 路径。
输出完整 <voice> 段内容，注入 system prompt。
"""

import logging
from datetime import datetime, timedelta, timezone

from app.agents.infra.langfuse_client import get_prompt
from app.agents.infra.model_builder import ModelBuilder
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
    """生成完整 voice 内容（内心独白 + 风格示例）

    Args:
        persona_id: bot persona ID
        recent_context: 可选，近期消息+回复（event-driven 路径传入）
        source: "cron" 或 "drift"，写入 reply_style_log.source
    """
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
    frag_text = (
        "\n".join(f.content[:100] for f in frags[-3:])
        if frags
        else "（今天还没跟人聊过）"
    )

    # 组装 recent_context（event-driven 路径会传入具体内容）
    recent_ctx_block = ""
    if recent_context:
        recent_ctx_block = (
            f"最近的对话和你的回复：\n{recent_context}"
        )

    # 调用 LLM
    prompt = get_prompt("voice_generator")
    compiled = prompt.compile(
        persona_name=persona.display_name,
        persona_lite=persona.persona_lite,
        current_state=current_state,
        response_mood=response_mood,
        schedule_segment=schedule_text,
        recent_fragments=frag_text,
        recent_context=recent_ctx_block,
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
        logger.warning(f"[{persona_id}] Voice generation returned empty")
        return None

    # 保存到 reply_style_log（复用现有表，voice 内容包含 monologue + examples）
    await save_reply_style(persona_id, content, source=source)
    logger.info(f"[{persona_id}] Voice generated ({source}): {content[:60]}...")
    return content
