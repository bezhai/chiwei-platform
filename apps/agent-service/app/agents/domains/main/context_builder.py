"""上下文构建模块

负责构建聊天上下文，包括历史消息获取、格式化和结构化。
"""

import asyncio
import logging
from dataclasses import dataclass

from langchain.messages import AIMessage, HumanMessage

from app.agents.infra.langfuse_client import get_prompt
from app.clients.image_client import image_client
from app.services.quick_search import QuickSearchResult, quick_search
from app.utils.content_parser import parse_content

logger = logging.getLogger(__name__)


async def build_chat_context(
    message_id: str, limit: int = 10
) -> tuple[list[HumanMessage | AIMessage], list[str], str, str, str, str]:
    """构建聊天上下文，支持私聊和群聊使用不同组装策略

    群聊: 使用系统Prompt进行上下文组装成一条HumanMessage
    私聊: 直接使用历史消息组装成 HumanMessage 和 AIMessage 列表

    注意: banned_word 检测已移至 guard graph，此处不再检测

    Args:
        message_id: 触发消息的ID
        limit: 获取的历史消息数量限制

    Returns:
        tuple: (消息列表, 图片URL列表, chat_id, 触发用户名, 聊天类型, 触发用户ID)
    """
    # L1: 使用 quick_search 拉取近期历史
    l1_results = await quick_search(message_id=message_id, limit=limit)

    if not l1_results:
        logger.warning(f"No results found for message_id: {message_id}")
        return [], [], "", "", "p2p", ""

    chat_type = l1_results[-1].chat_type or "p2p"  # 默认私聊

    # 1. 从content里批量提取图片keys, 获得图片key到URL的映射
    all_image_keys: list[tuple[str, str, str]] = []  # (key, message_id, role)

    # 提取所有图片keys
    for msg in l1_results:
        parsed = parse_content(msg.content)
        for key in parsed.image_keys:
            all_image_keys.append((key, msg.message_id, msg.role))

    # 批量处理所有图片，建立key到URL的映射
    image_key_to_url: dict[str, str] = {}
    if all_image_keys:
        image_tasks = [
            image_client.process_image(key, msg_id if role == "user" else None)
            for key, msg_id, role in all_image_keys
        ]
        image_results = await asyncio.gather(*image_tasks, return_exceptions=True)

        # 建立映射关系，失败的图片不加入映射
        for i, result in enumerate(image_results):
            key, msg_id, _ = all_image_keys[i]
            if isinstance(result, str) and result:
                image_key_to_url[key] = result
            else:
                logger.warning(f"图片处理失败: key={key}, message_id={msg_id}")

    # 2. 根据chat_type使用不同策略组装消息列表
    if chat_type == "group":
        # 群聊：使用prompt模板组装成一条HumanMessage
        messages = await _build_group_messages(l1_results, message_id, image_key_to_url)
    else:
        # 私聊：直接组装成HumanMessage和AIMessage列表
        messages = await _build_p2p_messages(l1_results, image_key_to_url)

    # 提取所有成功的图片URL列表（用于context）
    image_urls = list(image_key_to_url.values())

    # 提取触发消息的用户名和用户ID（最后一条消息即为触发消息）
    trigger_username = l1_results[-1].username or ""
    trigger_user_id = l1_results[-1].user_id or ""

    return (
        messages,
        image_urls,
        l1_results[0].chat_id or "",
        trigger_username,
        chat_type,
        trigger_user_id,
    )


async def _build_group_messages(
    messages: list[QuickSearchResult],
    trigger_id: str,
    image_key_to_url: dict[str, str],
) -> list[HumanMessage | AIMessage]:
    """构建群聊消息列表

    使用 prompt 模板将历史组装成一条 HumanMessage

    Args:
        messages: 消息列表
        trigger_id: 触发消息的ID
        image_key_to_url: 图片key到URL的映射

    Returns:
        包含一条 HumanMessage 的列表
    """
    # 复用现有的 context 构建逻辑
    context = await _build_context_from_messages(messages, trigger_id)

    # 使用 langfuse prompt 模板
    user_content = get_prompt("context_builder").compile(
        group_name=context.chat_name,
        chat_history=context.chat_history,
        trigger_content=context.trigger_content,
        trigger_username=context.trigger_username,
    )

    # 构建多模态消息
    content_blocks: list = [{"type": "text", "text": user_content}]

    # 追加所有成功获取的图片
    for url in image_key_to_url.values():
        content_blocks.append({"type": "image", "url": url})

    return [HumanMessage(content_blocks=content_blocks)]  # type: ignore


async def _build_p2p_messages(
    messages: list[QuickSearchResult], image_key_to_url: dict[str, str]
) -> list[HumanMessage | AIMessage]:
    """构建私聊消息列表

    直接将历史消息组装成 HumanMessage 和 AIMessage 列表

    Args:
        messages: 消息列表
        image_key_to_url: 图片key到URL的映射

    Returns:
        HumanMessage 和 AIMessage 的列表
    """
    result: list[HumanMessage | AIMessage] = []

    for msg in messages:
        # 提取消息中的图片keys和纯文本（render() 跳过图片，图片作为独立 content block 发送）
        parsed = parse_content(msg.content)
        image_keys = parsed.image_keys
        text_content = parsed.render()

        # 构建消息内容块
        content_blocks: list = []

        # 添加纯文本内容（不包含元信息前缀，避免 LLM 模仿格式）
        if text_content:
            content_blocks.append({"type": "text", "text": text_content})

        # 添加该消息对应的图片（只添加成功获取URL的图片）
        for key in image_keys:
            if key in image_key_to_url:
                content_blocks.append({"type": "image", "url": image_key_to_url[key]})
            else:
                logger.warning(
                    f"消息中的图片未找到URL: key={key}, message_id={msg.message_id}"
                )

        # 如果没有任何内容，跳过该消息
        if not content_blocks:
            continue

        # 根据 role 创建对应的消息类型
        if msg.role == "assistant":
            result.append(AIMessage(content_blocks=content_blocks))  # type: ignore
        else:  # user 或其他角色都作为 HumanMessage
            result.append(HumanMessage(content_blocks=content_blocks))  # type: ignore

    return result


def _extract_and_replace_images(
    content: str, start_index: int
) -> tuple[str, list[str]]:
    """从消息内容中提取图片keys，替换为【图片N】标记

    Args:
        content: 原始消息内容（v2 JSON 格式）
        start_index: 起始图片编号

    Returns:
        tuple[str, list[str]]: (处理后的文本, 图片keys列表)
    """
    parsed = parse_content(content)
    rendered = parsed.render(image_fn=lambda i, _key: f"【图片{start_index + i + 1}】")
    return rendered, parsed.image_keys


@dataclass
class ChatContext:
    """聊天上下文数据类

    Attributes:
        chat_history: 格式化的聊天历史
        trigger_content: 触发消息的格式化内容
        trigger_username: 触发用户的用户名
        chat_type: 聊天类型（p2p/group）
        chat_name: 群聊名称（私聊时为None）
    """

    chat_history: str
    trigger_content: str
    trigger_username: str
    chat_name: str | None


def _build_message_id_map(messages: list[QuickSearchResult]) -> dict[str, int]:
    """构建消息ID到编号的映射

    Args:
        messages: 消息列表

    Returns:
        dict[str, int]: 消息ID到编号的映射（从1开始）
    """
    return {msg.message_id: idx + 1 for idx, msg in enumerate(messages)}


def _format_chat_message(
    msg: QuickSearchResult,
    image_counter: dict[str, int],
    message_index: int,
    message_id_map: dict[str, int],
) -> tuple[str, list[str]]:
    """格式化单条聊天消息，提取图片keys

    Args:
        msg: 消息对象
        image_counter: 图片计数器字典，包含 "count" 键
        message_index: 当前消息的编号
        message_id_map: 消息ID到编号的映射

    Returns:
        tuple[str, list[str]]: (格式化后的消息字符串, 图片keys列表)
    """
    time_str = msg.create_time.strftime("%Y-%m-%d %H:%M:%S")
    username = msg.username or "未知用户"

    # 提取并替换图片标记
    processed_content, image_keys = _extract_and_replace_images(
        msg.content, image_counter["count"]
    )

    # 更新计数器
    image_counter["count"] += len(image_keys)

    # 构建回复标注
    reply_tag = ""
    if msg.reply_message_id:
        if msg.reply_message_id in message_id_map:
            # 回复的消息在上下文中
            parent_index = message_id_map[msg.reply_message_id]
            reply_tag = f" [↪️回复消息{parent_index}]"
        else:
            # 回复的消息不在上下文中
            reply_tag = " [↪️回复(消息已不在上下文)]"

    formatted_text = f"【消息{message_index}】[{time_str}] [User: {username}]{reply_tag}: {processed_content}"
    return formatted_text, image_keys


async def _build_context_from_messages(
    messages: list[QuickSearchResult], trigger_id: str
) -> ChatContext:
    """从消息列表构建聊天上下文

    Args:
        messages: 消息列表
        trigger_id: 触发消息的ID

    Returns:
        ChatContext对象
    """
    history_messages = []
    trigger_username = "未知用户"
    trigger_formatted = "（未找到触发消息）"
    chat_name = None

    # 初始化图片计数器（用于【图片N】标记）
    image_counter = {"count": 0}

    # 构建消息ID到编号的映射
    message_id_map = _build_message_id_map(messages)

    for idx, msg in enumerate(messages):
        message_index = idx + 1
        formatted_text, _ = _format_chat_message(
            msg, image_counter, message_index, message_id_map
        )

        if msg.message_id == trigger_id:
            trigger_username = msg.username or "未知用户"
            chat_name = msg.chat_name
            trigger_formatted = formatted_text
        else:
            history_messages.append(formatted_text)

    chat_history = (
        "\n".join(history_messages) if history_messages else "（暂无历史记录）"
    )

    return ChatContext(
        chat_history=chat_history,
        trigger_content=trigger_formatted,
        trigger_username=trigger_username,
        chat_name=chat_name,
    )
