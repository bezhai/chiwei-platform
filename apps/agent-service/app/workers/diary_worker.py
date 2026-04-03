"""
赤尾日记生成 Worker — ArQ cron job

每天凌晨 3:00 CST（UTC 19:00）为指定群生成赤尾第一人称日记。
核心原子函数 generate_diary_for_chat(chat_id, target_date) 可独立调用回溯。
日记生成后会自动提取/更新人物印象。
"""

import json
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from app.agents.infra.langfuse_client import get_prompt
from app.agents.infra.model_builder import ModelBuilder
from app.config.config import settings
from app.orm.crud import (
    get_active_diary_chat_ids,
    get_active_p2p_chat_ids,
    get_all_impressions_for_chat,
    get_chat_messages_in_range,
    get_diaries_in_range,
    get_recent_diaries,
    get_username,
    upsert_diary_entry,
    upsert_person_impression,
    upsert_weekly_review,
    upsert_group_culture_gestalt,
)
from app.utils.content_parser import parse_content

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


async def _get_persona_lite_for_bot(bot_name: str) -> str:
    """从 bot_persona 表加载 persona_lite"""
    from app.orm.crud import get_bot_persona
    try:
        persona = await get_bot_persona(bot_name)
        return persona.persona_lite if persona else ""
    except Exception as e:
        logger.warning(f"[{bot_name}] Failed to load persona_lite: {e}")
        return ""


# ==================== ArQ cron 入口 ====================


async def cron_generate_diaries(ctx) -> None:
    """cron 入口：为活跃群和私聊的每个 persona bot 生成昨天的日记"""
    from app.orm.crud import get_all_persona_bot_names
    yesterday = date.today() - timedelta(days=1)

    bot_names = await get_all_persona_bot_names()
    group_ids = await get_active_diary_chat_ids(min_replies=5, days=7)
    p2p_ids = await get_active_p2p_chat_ids(min_replies=2, days=1)
    all_ids = group_ids + p2p_ids

    if not all_ids or not bot_names:
        logger.info("No active chats or bots, skip diary generation")
        return

    for bot_name in bot_names:
        for chat_id in all_ids:
            try:
                await generate_diary_for_chat(chat_id, yesterday, bot_name=bot_name)
            except Exception as e:
                logger.error(f"[{bot_name}] Diary failed for {chat_id}: {e}")


# ==================== 核心原子函数 ====================


async def generate_diary_for_chat(
    chat_id: str, target_date: date, bot_name: str = "chiwei"
) -> str | None:
    """为指定群或私聊生成指定日期的日记

    Args:
        chat_id: 群/私聊 ID
        target_date: 目标日期
        bot_name: bot 名称，用于加载对应人设

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

    # 判断聊天类型，构建 chat_hint 供 prompt 区分
    is_p2p = messages[0].chat_type == "p2p" if messages else False
    if is_p2p:
        # 私聊对象 = 非 assistant 的用户
        peer_names = [
            user_names[uid] for uid in user_ids
            if any(m.user_id == uid and m.role != "assistant" for m in messages)
        ]
        peer = peer_names[0] if peer_names else "对方"
        chat_hint = f"这是你和 {peer} 的私聊记录。记录你们之间的对话、话题和感受。"
    else:
        chat_hint = "这是群聊记录。记录群里发生的事、话题和你观察到的群友动态。"

    # 4. 查最近 3 篇日记
    recent = await get_recent_diaries(chat_id, date_str, limit=3)
    recent_diaries_text = _format_recent_diaries(recent)

    # 5. 获取人设和 Langfuse prompt 并编译
    persona_lite = await _get_persona_lite_for_bot(bot_name)
    prompt_template = get_prompt("diary_generation")
    compiled_prompt = prompt_template.compile(
        persona_lite=persona_lite,
        chat_hint=chat_hint,
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
        await post_process_impressions(chat_id, diary_content, user_names, bot_name)
    except Exception as e:
        logger.error(f"Impression extraction failed for {chat_id}: {e}")

    # 9. 后处理：蒸馏群文化 gestalt
    try:
        await post_process_group_culture(chat_id, diary_content, bot_name)
    except Exception as e:
        logger.error(f"Group culture distill failed for {chat_id}: {e}")

    return diary_content


# ==================== 辅助函数 ====================


def _format_messages_timeline(messages: list, user_names: dict[str, str]) -> str:
    """将消息列表格式化为树状时间线

    利用 reply_message_id 构建回复链，用 tree 风格连接符展示层级关系：

    [14:30] 群友A: 今天吃什么
    ├─ [14:33] 群友B: 火锅吧
    │  └─ [14:35] 群友C: 好主意
    └─ [14:36] 群友D: 我也想吃
    [14:32] 群友E: 那个停车事件看了吗
    └─ [14:34] 群友F: 看了 太离谱了
    """
    MAX_DEPTH = 3

    # 1. 过滤并格式化每条消息
    formatted: dict[str, str] = {}  # msg_id → formatted line
    msg_order: list[str] = []  # 按时间顺序的 msg_id 列表
    for msg in messages:
        rendered = parse_content(msg.content).render()
        if not rendered or not rendered.strip():
            continue
        msg_time = datetime.fromtimestamp(msg.create_time / 1000, tz=CST)
        time_str = msg_time.strftime("%H:%M")
        speaker = (
            "赤尾"
            if msg.role == "assistant"
            else user_names.get(msg.user_id, msg.user_id[:8])
        )
        formatted[msg.message_id] = f"[{time_str}] {speaker}: {rendered}"
        msg_order.append(msg.message_id)

    # 2. 构建树：记录每个节点的深度，超过 MAX_DEPTH 的回退为根节点
    children: dict[str, list[str]] = defaultdict(list)
    roots: list[str] = []
    depth_of: dict[str, int] = {}

    for msg in messages:
        if msg.message_id not in formatted:
            continue
        parent = msg.reply_message_id
        if parent and parent in formatted:
            parent_depth = depth_of.get(parent, 0)
            child_depth = parent_depth + 1
            if child_depth > MAX_DEPTH:
                roots.append(msg.message_id)
                depth_of[msg.message_id] = 0
            else:
                children[parent].append(msg.message_id)
                depth_of[msg.message_id] = child_depth
        else:
            roots.append(msg.message_id)
            depth_of[msg.message_id] = 0

    # 3. 树状渲染
    lines: list[str] = []
    rendered_set: set[str] = set()

    def render_node(msg_id: str, prefix: str, is_last: bool, is_root: bool):
        if msg_id in rendered_set:
            return
        rendered_set.add(msg_id)
        if is_root:
            lines.append(formatted[msg_id])
            child_prefix = ""
        else:
            connector = "└─ " if is_last else "├─ "
            lines.append(f"{prefix}{connector}{formatted[msg_id]}")
            child_prefix = prefix + ("   " if is_last else "│  ")

        child_ids = children.get(msg_id, [])
        for i, child_id in enumerate(child_ids):
            render_node(child_id, child_prefix, i == len(child_ids) - 1, False)

    for root_id in roots:
        render_node(root_id, "", False, True)

    # 4. 兜底：处理未被渲染的消息（循环引用等边界情况）
    for msg_id in msg_order:
        if msg_id not in rendered_set and msg_id in formatted:
            lines.append(formatted[msg_id])

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
    bot_name: str,
) -> None:
    """从日记中提取人物印象并 upsert 到数据库

    Args:
        chat_id: 群 ID
        diary_content: 刚生成的日记内容
        user_names: user_id → 用户名 映射
        bot_name: bot 名称，用于 per-bot 印象隔离
    """
    # 1. 过滤出日记中提到的用户（减少噪音，提升 LLM 匹配准确率）
    relevant_users = {
        uid: name for uid, name in user_names.items() if name in diary_content
    }
    if not relevant_users:
        logger.info(f"No users mentioned in diary for {chat_id}, skip impression")
        return

    # 2. 查已有印象
    existing = await get_all_impressions_for_chat(chat_id, bot_name)
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
            await upsert_person_impression(chat_id, uid, text, bot_name)
            count += 1

    logger.info(f"Impressions updated for {chat_id}: {count} people")


# ==================== 群文化 gestalt 蒸馏 ====================


async def post_process_group_culture(
    chat_id: str,
    diary_content: str,
    bot_name: str,
) -> None:
    """从日记中蒸馏群文化 gestalt

    一句话描述 bot 对这个群的整体感觉。

    Args:
        chat_id: 群 ID
        diary_content: 刚生成的日记内容
        bot_name: bot 名称，用于 per-bot gestalt 隔离
    """
    from app.orm.crud import get_group_culture_gestalt

    existing_gestalt = await get_group_culture_gestalt(chat_id, bot_name)

    prompt_template = get_prompt("group_culture_distill")
    compiled_prompt = prompt_template.compile(
        diary=diary_content,
        previous_gestalt=existing_gestalt or "（这是第一次写，没有参考）",
    )

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
    raw = raw.strip()

    if raw:
        await upsert_group_culture_gestalt(chat_id, raw, bot_name)
        logger.info(f"Group culture gestalt updated for {chat_id}: {raw[:50]}")


# ==================== 周记生成 ====================


async def cron_generate_weekly_reviews(ctx) -> None:
    """cron 入口：为活跃群的每个 persona bot 生成上周的周记"""
    from app.orm.crud import get_all_persona_bot_names

    chat_ids = await get_active_diary_chat_ids(min_replies=5, days=7)
    bot_names = await get_all_persona_bot_names()

    if not chat_ids or not bot_names:
        logger.info("No active chats or bots, skip weekly review generation")
        return
    logger.info(f"Weekly review chats: {len(chat_ids)}, bots: {len(bot_names)}")

    for bot_name in bot_names:
        for chat_id in chat_ids:
            try:
                await generate_weekly_review_for_chat(chat_id, bot_name=bot_name)
            except Exception as e:
                logger.error(f"[{bot_name}] Weekly review failed for {chat_id}: {e}")


async def generate_weekly_review_for_chat(
    chat_id: str, target_monday: date | None = None, bot_name: str = "chiwei"
) -> str | None:
    """为指定群生成周记

    Args:
        chat_id: 群 ID
        target_monday: 目标周的周一日期，默认为上周一
        bot_name: bot 名称，用于加载对应人设和印象

    Returns:
        生成的周记内容，无日记时返回 None
    """
    # 1. 计算上周日期范围
    if target_monday is None:
        today = date.today()
        # 上周一 = 本周一 - 7天
        target_monday = today - timedelta(days=today.weekday() + 7)
    week_start = target_monday.isoformat()
    week_end = (target_monday + timedelta(days=6)).isoformat()  # 周日

    # 2. 查上周的日记
    diaries = await get_diaries_in_range(chat_id, week_start, week_end)
    if not diaries:
        logger.info(f"No diaries for {chat_id} in {week_start}~{week_end}, skip")
        return None

    diaries_text = "\n\n".join(
        f"--- {d.diary_date} ({_WEEKDAY_CN[date.fromisoformat(d.diary_date).weekday()]}) ---\n{d.content}"
        for d in diaries
    )

    # 3. 查当前人物印象（作为参考上下文）
    impressions = await get_all_impressions_for_chat(chat_id, bot_name)
    if impressions:
        # 需要 user_id → name 映射
        impressions_text = []
        for imp in impressions:
            name = await get_username(imp.user_id) or imp.user_id[:8]
            impressions_text.append(f"- {name}: {imp.impression_text}")
        impressions_context = "\n".join(impressions_text)
    else:
        impressions_context = "（暂无）"

    # 4. 获取人设和 Langfuse prompt 并编译
    prompt_template = get_prompt("weekly_review_generation")
    compiled_prompt = prompt_template.compile(
        persona_lite=await _get_persona_lite_for_bot(bot_name),
        week_start=week_start,
        week_end=week_end,
        diaries=diaries_text,
        impressions=impressions_context,
    )

    # 5. 调用 LLM
    model = await ModelBuilder.build_chat_model(settings.diary_model)
    response = await model.ainvoke(
        [{"role": "user", "content": compiled_prompt}],
    )

    content = response.content
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    if not content:
        logger.warning(f"LLM returned empty weekly review for {chat_id}")
        return None

    # 6. 写入数据库
    await upsert_weekly_review(
        chat_id=chat_id,
        week_start=week_start,
        week_end=week_end,
        content=content,
        model=settings.diary_model,
    )

    logger.info(
        f"Weekly review generated for {chat_id} ({week_start}~{week_end}): "
        f"{len(diaries)} diaries, {len(content)} chars"
    )
    return content
