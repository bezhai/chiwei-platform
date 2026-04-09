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


async def rebuild_relationship_memory_for_user(
    persona_id: str,
    user_id: str,
    messages: list,
    persona_name: str,
    persona_lite: str,
    batch_size: int = 50,
) -> dict:
    """为单个 (persona_id, user_id) 渐进式重建关系记忆

    Args:
        messages: 该用户参与的 ConversationMessage 列表（按时间正序）
        persona_name: 角色显示名
        persona_lite: 角色简介
        batch_size: 每批消息数量

    Returns:
        {"batches": int, "core_facts": str, "impression": str}
    """
    from datetime import datetime, timezone

    user_name = await get_username(user_id) or user_id[:6]
    current_core_facts = ""
    current_impression = ""
    batch_count = 0

    for i in range(0, len(messages), batch_size):
        batch = messages[i : i + batch_size]
        batch_count += 1

        # 格式化时间线
        lines = []
        for msg in batch:
            msg_time = datetime.fromtimestamp(msg.create_time / 1000, tz=timezone.utc)
            time_str = msg_time.strftime("%H:%M")
            if msg.role == "assistant":
                speaker = persona_name
            else:
                name = await get_username(msg.user_id) or msg.user_id[:6]
                speaker = name
            content = msg.content or ""
            if content.strip():
                lines.append(f"[{time_str}] {speaker}: {content[:200]}")

        if not lines:
            continue

        timeline = "\n".join(lines)

        # 构建 prompt 上下文
        cf_line = f"- {user_name}({user_id}): {current_core_facts or '（第一次互动）'}"
        im_line = f"- {user_name}({user_id}): {current_impression or '（第一次互动）'}"

        prompt = get_prompt("relationship_extract")
        compiled = prompt.compile(
            persona_name=persona_name,
            persona_lite=persona_lite,
            messages=timeline,
            current_core_facts=cf_line,
            current_impression=im_line,
        )

        model = await ModelBuilder.build_chat_model(settings.diary_model)
        response = await model.ainvoke([{"role": "user", "content": compiled}])

        content_text = response.content
        if isinstance(content_text, list):
            content_text = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content_text
            ).strip()

        if not content_text or content_text.strip() == "[]":
            continue

        try:
            updates = json.loads(content_text)
        except json.JSONDecodeError:
            logger.warning(f"[rebuild] Failed to parse batch {batch_count}: {content_text[:100]}")
            continue

        for item in updates:
            if not isinstance(item, dict):
                continue
            if item.get("user_id") == user_id:
                current_core_facts = item.get("core_facts", current_core_facts)
                current_impression = item.get("impression", current_impression)
                break

        if current_core_facts or current_impression:
            await save_relationship_memory(
                persona_id=persona_id,
                user_id=user_id,
                user_name=user_name,
                core_facts=current_core_facts,
                impression=current_impression,
                source="rebuild",
            )

    return {
        "batches": batch_count,
        "core_facts": current_core_facts,
        "impression": current_impression,
    }
