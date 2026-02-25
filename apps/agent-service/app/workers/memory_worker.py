"""
记忆知识沉淀 Worker — ArQ cron job

每 4 小时运行一次，找出有足够新消息的用户，执行知识沉淀流程：
  消息收集 → 单次 LLM（过滤+提取+合并） → 写入 user_knowledge
"""

import asyncio
import json
import logging
from datetime import UTC, datetime

from langfuse import get_client as get_langfuse

from app.agents.infra.langfuse_client import get_prompt
from app.agents.infra.model_builder import ModelBuilder
from app.clients.redis import AsyncRedisClient
from app.orm.crud import (
    advance_consolidation_cursor,
    get_active_users_for_consolidation,
    get_user_knowledge,
    upsert_user_knowledge,
)
from app.services.message_collector import (
    MessageWithContext,
    collect_user_messages_with_context,
)

logger = logging.getLogger(__name__)

# 并发控制
_CONCURRENCY = 3

# 分布式锁
_LOCK_KEY = "memory:consolidation:lock"
_LOCK_TTL_SECONDS = 1800  # 30 min

# facts 上限
_MAX_FACTS = 30


# ==================== ArQ cron 入口 ====================


async def cron_consolidate_profiles(ctx) -> None:
    """每 4 小时运行一次：找出需要沉淀的用户并处理"""
    redis = AsyncRedisClient.get_instance()

    # 分布式锁
    got = await redis.set(_LOCK_KEY, "1", ex=_LOCK_TTL_SECONDS, nx=True)
    if not got:
        logger.info("Consolidation already running (lock held), skip")
        return

    langfuse = get_langfuse()
    trace = langfuse.trace(name="memory-consolidation-cron")

    try:
        # 查询需要沉淀的用户
        eligible_users = await get_active_users_for_consolidation(
            min_messages=10, max_users=50
        )

        if not eligible_users:
            logger.info("No users eligible for consolidation")
            trace.update(
                output={"status": "no_eligible_users"},
            )
            return

        logger.info(f"Found {len(eligible_users)} users eligible for consolidation")

        # 并发处理
        semaphore = asyncio.Semaphore(_CONCURRENCY)
        results = {"success": 0, "skipped": 0, "failed": 0}

        async def _process_with_semaphore(user_info: dict):
            async with semaphore:
                try:
                    ok = await consolidate_single_user(
                        user_info["user_id"],
                        since_time=user_info["since_time"],
                    )
                    if ok:
                        results["success"] += 1
                    else:
                        results["skipped"] += 1
                except Exception as e:
                    results["failed"] += 1
                    logger.error(
                        f"Consolidation failed for {user_info['user_id']}: {e}"
                    )

        await asyncio.gather(*[_process_with_semaphore(u) for u in eligible_users])

        logger.info(
            f"Consolidation complete: {results['success']} success, "
            f"{results['skipped']} skipped, {results['failed']} failed"
        )
        trace.update(
            output={
                "eligible_users": len(eligible_users),
                **results,
            },
        )

    finally:
        await redis.delete(_LOCK_KEY)


# ==================== 单用户沉淀流程 ====================


async def consolidate_single_user(user_id: str, since_time: int = 0) -> bool:
    """单用户完整沉淀流程

    Returns:
        True = 有更新, False = 跳过（游标仍会前移，避免重复处理）
    """
    langfuse = get_langfuse()
    span = langfuse.span(name="consolidate-user", input={"user_id": user_id})

    try:
        # 1. 加载已有知识
        knowledge = await get_user_knowledge(user_id)
        existing_facts: list[dict] = knowledge.facts if knowledge else []

        # 2. 收集消息 + 上下文
        messages_with_ctx, user_names = await collect_user_messages_with_context(
            user_id, since_time, max_messages=50
        )

        if not messages_with_ctx:
            span.update(output={"status": "no_messages"})
            span.end()
            return False

        # 一旦收集到消息，无论后续是否产生更新，都要前移游标
        latest_message_time = max(m.user_message.create_time for m in messages_with_ctx)

        # 3. 构建消息文本（纯文本，无标注）
        messages_text = _build_messages_text(messages_with_ctx, user_names)

        # 4. 渲染已有事实
        existing_facts_text = _render_existing_facts(existing_facts)

        # 5. 单次 LLM 调用：过滤噪音 + 提取事实 + 更新/删除已有事实
        extraction_result = await _call_extraction_llm(
            existing_facts_text, messages_text
        )

        if not extraction_result:
            await advance_consolidation_cursor(user_id, latest_message_time)
            span.update(output={"status": "llm_no_result"})
            span.end()
            return False

        new_facts = extraction_result.get("new_facts", [])
        updated_facts = extraction_result.get("updated_facts", [])
        removed_facts = extraction_result.get("removed_facts", [])
        personality_note = extraction_result.get("personality_note")
        communication_style = extraction_result.get("communication_style")

        if not new_facts and not updated_facts and not removed_facts:
            await advance_consolidation_cursor(user_id, latest_message_time)
            logger.info(f"No changes for user {user_id}")
            span.update(output={"status": "no_changes"})
            span.end()
            return False

        # 6. 合并事实
        merged_facts = _merge_facts(
            existing_facts, new_facts, updated_facts, removed_facts
        )

        # 7. 更新数据库（含游标前移）
        await upsert_user_knowledge(
            user_id=user_id,
            facts=merged_facts,
            personality_note=personality_note,
            communication_style=communication_style,
            last_consolidation_message_time=latest_message_time,
        )

        changes_count = len(new_facts) + len(updated_facts) + len(removed_facts)
        logger.info(f"User {user_id} knowledge updated: {changes_count} changes")
        span.update(
            output={
                "status": "updated",
                "new": len(new_facts),
                "updated": len(updated_facts),
                "removed": len(removed_facts),
                "total_facts": len(merged_facts),
            },
        )
        span.end()
        return True

    except Exception as e:
        logger.error(f"consolidate_single_user failed for {user_id}: {e}")
        span.update(output={"status": "error", "error": str(e)})
        span.end()
        raise


# ==================== 辅助函数 ====================


def _build_messages_text(
    messages: list[MessageWithContext],
    user_names: dict[str, str],
) -> str:
    """构建纯文本消息列表，无标注"""
    parts: list[str] = []
    for i, mwc in enumerate(messages):
        rendered = mwc.render(user_names)
        parts.append(f"--- 消息 #{i} ---\n{rendered}")
    return "\n\n".join(parts)


def _render_existing_facts(facts: list[dict]) -> str:
    """渲染已有事实为编号列表"""
    if not facts:
        return "（暂无已有事实，这是首次沉淀）"

    lines: list[str] = []
    for i, fact in enumerate(facts):
        category = fact.get("category", "其他")
        content = fact.get("content", "")
        confidence = fact.get("confidence", 0.8)
        lines.append(f"{i + 1}. [{category}] {content} (置信度: {confidence})")

    return "\n".join(lines)


async def _call_extraction_llm(
    existing_facts_text: str, messages_text: str
) -> dict | None:
    """单次 LLM 调用完成过滤+提取+合并"""
    try:
        prompt_template = get_prompt("memory_extract_knowledge")
        system_prompt = prompt_template.compile(
            existing_facts=existing_facts_text,
            messages=messages_text,
        )

        model = await ModelBuilder.build_chat_model("memory-extract-model")

        response = await model.ainvoke(
            [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": "请根据以上消息提取用户知识。只输出 JSON。",
                },
            ],
        )

        content = response.content
        if not content:
            return None

        # 解析 JSON
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        elif "```" in content:
            content = content.split("```")[1].split("```")[0]

        return json.loads(content.strip())

    except Exception as e:
        logger.error(f"Extraction LLM failed: {e}")
        return None


def _merge_facts(
    existing: list[dict],
    new_facts: list[dict],
    updated_facts: list[dict],
    removed_facts: list[dict],
) -> list[dict]:
    """合并事实：删除 → 更新 → 追加，上限 _MAX_FACTS 条"""
    now_str = datetime.now(UTC).isoformat()

    # 复制一份避免修改原数据
    merged = list(existing)

    # 1. 删除
    remove_keys = {(r.get("category"), r.get("content")) for r in removed_facts}
    merged = [
        f for f in merged if (f.get("category"), f.get("content")) not in remove_keys
    ]

    # 2. 更新：按 (category, original_content) 匹配
    for update in updated_facts:
        category = update.get("category")
        original = update.get("original_content")
        for fact in merged:
            if fact.get("category") == category and fact.get("content") == original:
                if "content" in update and "original_content" in update:
                    fact["content"] = update.get("new_content", update.get("content"))
                if "confidence" in update:
                    fact["confidence"] = update["confidence"]
                fact["last_confirmed_at"] = now_str
                break

    # 3. 追加新事实
    for nf in new_facts:
        nf.setdefault("extracted_at", now_str)
        nf.setdefault("last_confirmed_at", now_str)
        nf.setdefault("confidence", 0.8)
        merged.append(nf)

    # 4. 上限淘汰：超出时按 confidence 最低移除
    if len(merged) > _MAX_FACTS:
        merged.sort(key=lambda f: f.get("confidence", 0))
        merged = merged[-_MAX_FACTS:]

    return merged
