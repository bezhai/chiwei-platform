"""关系记忆提取 — afterthought 碎片生成后，判断是否需要更新 per-user 关系记忆"""

import json
import logging

from app.agents.infra.langfuse_client import get_prompt
from app.agents.infra.model_builder import ModelBuilder
from app.config.config import settings
from app.orm.crud import get_bot_persona, get_username
from app.orm.memory_crud import (
    get_relationship_memories_for_users,
    save_relationship_memory,
)

logger = logging.getLogger(__name__)


async def extract_relationship_updates(
    persona_id: str,
    chat_id: str,
    user_ids: list[str],
    messages_timeline: str,
) -> None:
    """从一段对话中提取关系记忆更新

    在 afterthought 生成 conversation 碎片后调用。
    让 LLM 以角色视角判断对话中涉及的人是否有关系变化，有则写入 relationship_memory。
    """
    if not user_ids:
        return

    # 获取 persona 信息（注入角色视角）
    persona = await get_bot_persona(persona_id)
    persona_name = persona.display_name if persona else persona_id
    persona_lite = persona.persona_lite if persona else ""

    # 获取当前关系记忆
    current_memories = await get_relationship_memories_for_users(persona_id, user_ids)

    # 构建当前记忆上下文（分 core_facts / impression）
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

    prompt = get_prompt("relationship_extract")
    compiled = prompt.compile(
        persona_name=persona_name,
        persona_lite=persona_lite,
        messages=messages_timeline,
        current_core_facts="\n".join(core_facts_lines),
        current_impression="\n".join(impression_lines),
    )

    model = await ModelBuilder.build_chat_model(settings.diary_model)
    response = await model.ainvoke([{"role": "user", "content": compiled}])

    content = response.content
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        ).strip()

    if not content or content.strip() == "[]":
        logger.info(f"[{persona_id}] No relationship updates for chat {chat_id}")
        return

    # 解析 JSON 输出
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
