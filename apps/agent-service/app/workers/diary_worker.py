"""
赤尾日记生成 Worker — ArQ cron job

每天凌晨 3:00 CST（UTC 19:00）为指定群生成赤尾第一人称日记。
核心原子函数 generate_diary_for_chat(chat_id, target_date) 可独立调用回溯。
日记生成后会自动提取/更新人物印象。
"""

import json
import logging
from datetime import date, datetime, timedelta, timezone

from app.agents.infra.langfuse_client import get_prompt
from app.agents.infra.model_builder import ModelBuilder
from app.config.config import settings
from app.orm.crud import (
    get_active_diary_chat_ids,
    get_all_impressions_for_chat,
    get_chat_messages_in_range,
    get_recent_diaries,
    get_username,
    upsert_diary_entry,
    upsert_person_impression,
)
from app.utils.content_parser import parse_content

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# ==================== ArQ cron 入口 ====================


async def cron_generate_diaries(ctx) -> None:
    """cron 入口：为近7天赤尾活跃的群生成昨天的日记"""
    chat_ids = await get_active_diary_chat_ids(min_replies=5, days=7)
    if not chat_ids:
        logger.info("No active diary chats found, skip")
        return
    logger.info(f"Active diary chats: {len(chat_ids)}")

    yesterday = date.today() - timedelta(days=1)

    for chat_id in chat_ids:
        try:
            await generate_diary_for_chat(chat_id, yesterday)
        except Exception as e:
            logger.error(f"Diary generation failed for {chat_id} on {yesterday}: {e}")


# ==================== 核心原子函数 ====================


async def generate_diary_for_chat(chat_id: str, target_date: date) -> str | None:
    """为指定群生成指定日期的日记

    Args:
        chat_id: 群 ID
        target_date: 目标日期

    Returns:
        生成的日记内容，无消息时返回 None
    """
    date_str = target_date.isoformat()  # "2026-03-10"
    weekday = _WEEKDAY_CN[target_date.weekday()]

    # 1. 收集当天消息（CST 00:00 ~ 次日 00:00）
    day_start_cst = datetime(
        target_date.year, target_date.month, target_date.day, tzinfo=CST
    )
    day_end_cst = day_start_cst + timedelta(days=1)
    # 转为毫秒时间戳（create_time 是毫秒级 BigInteger）
    start_ts = int(day_start_cst.timestamp() * 1000)
    end_ts = int(day_end_cst.timestamp() * 1000)

    messages = await get_chat_messages_in_range(chat_id, start_ts, end_ts)

    # 2. 构建 user_id → 用户名 映射（提前构建，供 timeline 和印象后处理复用）
    user_ids = {msg.user_id for msg in messages}
    user_names: dict[str, str] = {}
    for uid in user_ids:
        name = await get_username(uid)
        user_names[uid] = name or uid[:8]

    # 3. 格式化消息时间线
    timeline = _format_messages_timeline(messages, user_names)

    if not timeline:
        logger.info(f"No messages for {chat_id} on {date_str}, skip")
        return None

    # 4. 查最近 3 篇日记
    recent = await get_recent_diaries(chat_id, date_str, limit=3)
    recent_diaries_text = _format_recent_diaries(recent)

    # 5. 获取 Langfuse prompt 并编译
    prompt_template = get_prompt("diary_generation")
    compiled_prompt = prompt_template.compile(
        date=date_str,
        weekday=weekday,
        messages=timeline,
        recent_diaries=recent_diaries_text,
    )

    # 6. 调用 LLM
    model = await ModelBuilder.build_chat_model(settings.diary_model)
    response = await model.ainvoke(
        [{"role": "user", "content": compiled_prompt}],
    )

    diary_content = response.content
    # Gemini 返回 list[dict]，提取文本
    if isinstance(diary_content, list):
        diary_content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in diary_content
        )
    if not diary_content:
        logger.warning(f"LLM returned empty content for {chat_id} on {date_str}")
        return None

    # 7. 写入数据库（upsert）
    await upsert_diary_entry(
        chat_id=chat_id,
        diary_date=date_str,
        content=diary_content,
        message_count=len(messages),
        model=settings.diary_model,
    )

    logger.info(
        f"Diary generated for {chat_id} on {date_str}: "
        f"{len(messages)} messages, {len(diary_content)} chars"
    )

    # 8. 后处理：从日记中提取/更新人物印象
    try:
        await post_process_impressions(chat_id, diary_content, user_names)
    except Exception as e:
        logger.error(f"Impression extraction failed for {chat_id}: {e}")

    return diary_content


# ==================== 辅助函数 ====================


def _format_messages_timeline(messages: list, user_names: dict[str, str]) -> str:
    """将消息列表格式化为时间线文本

    格式: [14:32] 群友A: 今天吃什么
    跳过纯图片/表情包/空内容
    """
    lines: list[str] = []
    for msg in messages:
        # 渲染内容（跳过图片）
        rendered = parse_content(msg.content).render()
        if not rendered or not rendered.strip():
            continue

        # 时间戳 → CST 时间
        msg_time = datetime.fromtimestamp(msg.create_time / 1000, tz=CST)
        time_str = msg_time.strftime("%H:%M")

        # 发言者名称
        if msg.role == "assistant":
            speaker = "赤尾"
        else:
            speaker = user_names.get(msg.user_id, msg.user_id[:8])

        lines.append(f"[{time_str}] {speaker}: {rendered}")

    return "\n".join(lines)


def _format_recent_diaries(diaries: list) -> str:
    """格式化最近日记供 prompt 注入"""
    if not diaries:
        return "（这是第一篇日记，没有历史参考）"

    # diaries 按 date desc 返回，反转为时间正序
    parts: list[str] = []
    for diary in reversed(diaries):
        parts.append(f"--- {diary.diary_date} ---\n{diary.content}")

    return "\n\n".join(parts)


# ==================== 印象后处理 ====================


async def post_process_impressions(
    chat_id: str,
    diary_content: str,
    user_names: dict[str, str],
) -> None:
    """从日记中提取人物印象并 upsert 到数据库

    Args:
        chat_id: 群 ID
        diary_content: 刚生成的日记内容
        user_names: user_id → 用户名 映射
    """
    # 1. 过滤出日记中提到的用户（减少噪音，提升 LLM 匹配准确率）
    relevant_users = {
        uid: name for uid, name in user_names.items() if name in diary_content
    }
    if not relevant_users:
        logger.info(f"No users mentioned in diary for {chat_id}, skip impression")
        return

    # 2. 查已有印象
    existing = await get_all_impressions_for_chat(chat_id)
    if existing:
        existing_text = "\n".join(
            f"- {user_names.get(imp.user_id, imp.user_id[:8])}(user_id={imp.user_id}): "
            f"{imp.impression_text}"
            for imp in existing
        )
    else:
        existing_text = "（暂无）"

    # 3. 格式化 user_mapping（只包含日记中提到的人）
    user_mapping_text = "\n".join(
        f"- {uid} → {name}" for uid, name in relevant_users.items()
    )

    # 4. 获取 Langfuse prompt 并编译
    prompt_template = get_prompt("diary_extract_impressions")
    compiled_prompt = prompt_template.compile(
        diary=diary_content,
        existing_impressions=existing_text,
        user_mapping=user_mapping_text,
    )

    # 5. 调用 LLM
    model = await ModelBuilder.build_chat_model(settings.diary_model)
    response = await model.ainvoke(
        [{"role": "user", "content": compiled_prompt}],
    )

    raw = response.content
    if isinstance(raw, list):
        raw = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in raw
        )

    # 6. 解析 JSON
    # 去掉可能的 markdown 代码块包裹
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

    impressions = json.loads(raw)
    if not isinstance(impressions, list):
        logger.warning(f"Impression extraction returned non-list: {type(impressions)}")
        return

    # 7. Upsert 每条印象
    count = 0
    for item in impressions:
        uid = item.get("user_id")
        text = item.get("impression_text")
        if uid and text and uid in user_names:
            await upsert_person_impression(chat_id, uid, text)
            count += 1

    logger.info(f"Impressions updated for {chat_id}: {count} people")
