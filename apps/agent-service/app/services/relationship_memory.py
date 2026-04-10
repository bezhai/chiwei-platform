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
    get_relationship_memories_for_users_v2,
    save_relationship_memory_v2,
)
from app.utils.content_parser import parse_content

logger = logging.getLogger(__name__)


async def format_timeline(
    messages: list,
    persona_name: str,
    *,
    tz: timezone = timezone.utc,
    max_messages: int = 2000,
    with_ids: bool = False,
) -> str:
    """格式化消息列表为时间线文本

    格式: [HH:MM] 名字: 消息内容
    with_ids=True 时: #id [HH:MM] 名字: 消息内容（供 LLM 引用消息）
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
            prefix = f"#{msg.id} " if with_ids and msg.id else ""
            lines.append(f"{prefix}[{time_str}] {speaker}: {rendered[:200]}")

    return "\n".join(lines)


def _parse_llm_json(content) -> list | None:
    """从 LLM 响应中解析 JSON 数组"""
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        ).strip()
    else:
        content = (content or "").strip()

    if not content or content == "[]":
        return None

    if content.startswith("```"):
        content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return None


async def _filter_relevant_messages(
    messages: list,
    persona_name: str,
    persona_lite: str,
) -> list:
    """Stage 1: 话题切分 + 赤尾相关性筛选

    把全部消息（带 #id）丢给 LLM，让它切分话题并标记赤尾参与的话题，
    返回赤尾参与话题中的消息 id 列表。
    """
    timeline = await format_timeline(messages, persona_name, with_ids=True)
    if not timeline:
        return []

    agent = ChatAgent(
        prompt_id="relationship_filter",
        tools=[],
        model_id=settings.relationship_model,
        trace_name="relationship-filter",
    )
    result = await agent.run(
        messages=[HumanMessage(content="分析对话，找出我参与的话题")],
        prompt_vars={
            "persona_name": persona_name,
            "persona_lite": persona_lite,
            "messages": timeline,
        },
    )

    topics = _parse_llm_json(result.content)
    if not topics:
        logger.info(f"[{persona_name}] Stage 1: no relevant topics found")
        return []

    # 收集所有相关消息 id
    relevant_ids: set[int] = set()
    for topic in topics:
        if isinstance(topic, dict):
            for mid in topic.get("message_ids", []):
                if isinstance(mid, int):
                    relevant_ids.add(mid)

    logger.info(
        f"[{persona_name}] Stage 1: {len(topics)} topics, "
        f"{len(relevant_ids)} relevant messages out of {len(messages)}"
    )
    return list(relevant_ids)


async def extract_relationship_updates(
    persona_id: str,
    chat_id: str,
    user_ids: list[str],
    messages: list,
) -> None:
    """两阶段关系记忆提取

    Stage 1: 话题切分 + 筛选赤尾参与的对话片段
    Stage 2: 基于筛选后的消息提取关系记忆更新

    afterthought 和 rebuild 共用此函数。
    """
    if not user_ids or not messages:
        return

    persona = await get_bot_persona(persona_id)
    persona_name = persona.display_name if persona else persona_id
    persona_lite = persona.persona_lite if persona else ""

    # 私聊全是赤尾和对方的对话，不需要筛选；群聊需要话题切分
    chat_type = messages[0].chat_type if messages else "group"

    if chat_type == "p2p":
        filtered_messages = messages
        filtered_user_ids = user_ids
    else:
        relevant_ids = await _filter_relevant_messages(
            messages, persona_name, persona_lite,
        )
        if not relevant_ids:
            logger.info(f"[{persona_id}] No relevant messages for chat {chat_id}, skip extract")
            return

        id_set = set(relevant_ids)
        filtered_messages = [m for m in messages if m.id in id_set]
        filtered_user_ids = list({
            m.user_id for m in filtered_messages
            if m.role == "user" and m.user_id and m.user_id != "__proactive__"
        })
        if not filtered_user_ids:
            return

    # --- 提取 ---
    filtered_timeline = await format_timeline(filtered_messages, persona_name)

    current_memories = await get_relationship_memories_for_users_v2(persona_id, filtered_user_ids)

    core_facts_lines = []
    impression_lines = []
    for uid in filtered_user_ids:
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
            "messages": filtered_timeline,
            "current_core_facts": "\n".join(core_facts_lines),
            "current_impression": "\n".join(impression_lines),
        },
    )

    updates = _parse_llm_json(result.content)
    if not updates:
        logger.info(f"[{persona_id}] No relationship updates for chat {chat_id}")
        return

    for item in updates:
        if not isinstance(item, dict):
            continue
        uid = item.get("user_id", "")
        name = item.get("user_name", "") or await get_username(uid) or uid[:6]
        core_facts = item.get("core_facts", "")
        impression = item.get("impression", "")
        if uid and (core_facts or impression):
            await save_relationship_memory_v2(
                persona_id=persona_id,
                user_id=uid,
                core_facts=core_facts,
                impression=impression,
                source="afterthought",
            )
            logger.info(
                f"[{persona_id}] Relationship updated for {name}: "
                f"facts={core_facts[:30]}... impression={impression[:30]}..."
            )
