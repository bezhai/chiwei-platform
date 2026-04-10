"""关系记忆提取 — afterthought 碎片生成后，判断是否需要更新 per-user 关系记忆

所有 LLM 调用走 ChatAgent，自动 Langfuse trace + 重试。
rebuild 复用 extract_relationship_updates，不维护两套提取逻辑。
"""

import json
import logging
from datetime import datetime, timezone

from langchain.messages import HumanMessage

from app.agents.core import ChatAgent
from app.config.config import settings
from app.orm.crud import get_bot_persona, get_username
from app.orm.memory_crud import (
    get_relationship_memories_for_users,
    save_relationship_memory,
)
from app.utils.content_parser import parse_content

logger = logging.getLogger(__name__)


async def format_timeline(
    messages: list,
    persona_name: str,
    *,
    tz: timezone = timezone.utc,
    max_messages: int = 50,
) -> str:
    """格式化消息列表为时间线文本

    格式: [HH:MM] 名字: 消息内容
    afterthought 和 rebuild 共用此函数。
    """
    messages = messages[-max_messages:]

    lines: list[str] = []
    for msg in messages:
        msg_time = datetime.fromtimestamp(msg.create_time / 1000, tz=tz)
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


async def extract_relationship_updates(
    persona_id: str,
    chat_id: str,
    user_ids: list[str],
    messages_timeline: str,
) -> None:
    """从一段对话中提取关系记忆更新

    在 afterthought 生成 conversation 碎片后调用，rebuild 也复用此函数。
    通过 ChatAgent 调用 LLM，自动 Langfuse trace。
    """
    if not user_ids:
        return

    persona = await get_bot_persona(persona_id)
    persona_name = persona.display_name if persona else persona_id
    persona_lite = persona.persona_lite if persona else ""

    current_memories = await get_relationship_memories_for_users(persona_id, user_ids)

    core_facts_lines = []
    impression_lines = []
    for uid in user_ids:
        name = await get_username(uid) or uid[:6]
        mem = current_memories.get(uid)
        if mem:
            core_facts, impression = mem
            core_facts_lines.append(f"- {name}({uid}): {core_facts or '（无）'}")
            impression_lines.append(f"- {name}({uid}): {impression or '（无）'}")
        else:
            core_facts_lines.append(f"- {name}({uid}): （第一次互动）")
            impression_lines.append(f"- {name}({uid}): （第一次互动）")

    agent = ChatAgent(
        prompt_id="relationship_extract",
        tools=[],
        model_id=settings.relationship_model,
        trace_name="relationship-extract",
    )
    result = await agent.run(
        messages=[HumanMessage(content="根据对话更新关系记忆")],
        prompt_vars={
            "persona_name": persona_name,
            "persona_lite": persona_lite,
            "messages": messages_timeline,
            "current_core_facts": "\n".join(core_facts_lines),
            "current_impression": "\n".join(impression_lines),
        },
    )

    content = result.content or ""
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        ).strip()

    if not content or content.strip() == "[]":
        logger.info(f"[{persona_id}] No relationship updates for chat {chat_id}")
        return

    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        updates = json.loads(content)
    except json.JSONDecodeError:
        logger.warning(f"[{persona_id}] Failed to parse relationship extract: {content[:200]}")
        return

    for item in updates:
        if not isinstance(item, dict):
            continue
        uid = item.get("user_id", "")
        name = item.get("user_name", "") or await get_username(uid) or uid[:6]
        core_facts = item.get("core_facts", "")
        impression = item.get("impression", "")
        if uid and (core_facts or impression):
            await save_relationship_memory(
                persona_id=persona_id,
                user_id=uid,
                user_name=name,
                core_facts=core_facts,
                impression=impression,
                source="afterthought",
            )
            logger.info(
                f"[{persona_id}] Relationship updated for {name}: "
                f"facts={core_facts[:30]}... impression={impression[:30]}..."
            )
