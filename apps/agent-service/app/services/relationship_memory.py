"""关系记忆提取 — afterthought 碎片生成后，判断是否需要更新 per-user 关系记忆"""

import json
import logging

from app.agents.infra.langfuse_client import get_prompt
from app.agents.infra.model_builder import ModelBuilder
from app.config.config import settings
from app.orm.crud import get_username
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
    让 LLM 判断对话中涉及的人是否有关系变化，有则写入 relationship_memory。
    """
    if not user_ids:
        return

    # 获取当前关系记忆
    current_memories = await get_relationship_memories_for_users(persona_id, user_ids)

    # 构建当前记忆上下文
    memory_lines = []
    for uid in user_ids:
        name = await get_username(uid) or uid[:6]
        mem = current_memories.get(uid)
        if mem:
            memory_lines.append(f"- {name}({uid}): {mem}")
        else:
            memory_lines.append(f"- {name}({uid}): （第一次互动，没有记忆）")

    prompt = get_prompt("relationship_extract")
    compiled = prompt.compile(
        messages=messages_timeline,
        current_memories="\n".join(memory_lines),
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
        logger.warning(f"[{persona_id}] Failed to parse relationship extract: {content[:100]}")
        return

    for item in updates:
        if not isinstance(item, dict):
            continue
        if item.get("action") != "UPDATE":
            continue
        uid = item.get("user_id", "")
        name = item.get("user_name", "") or await get_username(uid) or uid[:6]
        memory = item.get("memory", "")
        if uid and memory:
            await save_relationship_memory(
                persona_id=persona_id,
                user_id=uid,
                user_name=name,
                memory_text=memory,
                source="afterthought",
            )
            logger.info(f"[{persona_id}] Relationship updated for {name}: {memory[:50]}...")
